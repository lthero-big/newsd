"""`newsd updater`: the opt-in Docker update sidecar (DEPTAI-216 4b).

Only this container mounts /var/run/docker.sock; the web container just calls it over the private compose network
with a token derived from NEWSD_SECRET_KEY. It can do exactly one thing: pull ``NEWSD_IMAGE:<version>`` and
recreate the containers of *its own* compose project whose service is in NEWSD_UPDATER_SERVICES (default
web,worker), one at a time, web first. A container is recreated with its existing configuration (env, volumes,
ports, networks, labels, restart policy, healthcheck) and only the image swapped. If the new web container does
not become healthy, it is removed and the old one is renamed back and started again.

The new web container runs the database migrations on start (as every newsd web start does), so "pull + recreate"
is the whole upgrade. No ports are published: the API is reachable from the compose network only.
"""
from __future__ import annotations

import hmac
import logging
import os
import re
import socket
import threading
import time
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel

log = logging.getLogger("newsd.updater")
SOCK = os.environ.get("NEWSD_DOCKER_SOCK", "/var/run/docker.sock")
PROJECT_LABEL, SERVICE_LABEL = "com.docker.compose.project", "com.docker.compose.service"


def _ts() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


class DockerError(Exception):
    pass


class Docker:
    """The few Engine API calls we need, over the unix socket (no docker CLI in the image)."""

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.c = httpx.Client(transport=transport or httpx.HTTPTransport(uds=SOCK), base_url="http://docker", timeout=60)
        # unversioned paths = the daemon's own API version (Engine 29 refuses anything below 1.44)

    def _ok(self, r: httpx.Response, *ok: int):
        if r.status_code not in (ok or (200, 201, 204)):
            raise DockerError(f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text[:300]}")
        return r

    def inspect(self, cid: str) -> dict:
        return self._ok(self.c.get(f"/containers/{cid}/json")).json()

    def image(self, ref: str) -> dict | None:
        r = self.c.get(f"/images/{ref}/json")
        return None if r.status_code == 404 else self._ok(r).json()

    def containers(self, project: str) -> list[dict]:
        import json
        f = json.dumps({"label": [f"{PROJECT_LABEL}={project}"]})
        return self._ok(self.c.get("/containers/json", params={"all": "1", "filters": f})).json()

    def pull(self, repo: str, tag: str, timeout: float = 900) -> None:
        with self.c.stream("POST", "/images/create", params={"fromImage": repo, "tag": tag}, timeout=timeout) as r:
            if r.status_code != 200:
                r.read()
                raise DockerError(f"pull {repo}:{tag}: {r.status_code} {r.text[:300]}")
            for line in r.iter_lines():
                if '"error"' in line:
                    raise DockerError(f"pull {repo}:{tag}: {line[:300]}")

    def create(self, name: str, body: dict) -> str:
        return self._ok(self.c.post("/containers/create", params={"name": name}, json=body)).json()["Id"]

    def start(self, cid):
        self._ok(self.c.post(f"/containers/{cid}/start"), 204, 304)

    def stop(self, cid, t: int = 30):
        self._ok(self.c.post(f"/containers/{cid}/stop", params={"t": t}, timeout=t + 30), 204, 304)

    def rename(self, cid, name):
        self._ok(self.c.post(f"/containers/{cid}/rename", params={"name": name}), 204)

    def connect(self, net: str, cid: str, endpoint: dict):
        self._ok(self.c.post(f"/networks/{net}/connect", json={"Container": cid, "EndpointConfig": endpoint}), 200)

    def remove(self, cid):
        self._ok(self.c.delete(f"/containers/{cid}", params={"force": "1"}), 204, 404)


def _minus(values: list | None, image_values: list | None) -> list:
    """Container env/… minus what the *old image* contributed, so the new image's own defaults win."""
    drop = set(image_values or [])
    return [v for v in (values or []) if v not in drop]


def clone_spec(old: dict, old_image: dict | None, new_ref: str) -> dict:
    """Body for POST /containers/create that reproduces ``old`` on ``new_ref``."""
    cfg, icfg = dict(old["Config"]), dict((old_image or {}).get("Config") or {})
    body = {k: cfg.get(k) for k in ("Hostname", "Domainname", "User", "Tty", "OpenStdin", "StdinOnce", "AttachStdin",
                                    "AttachStdout", "AttachStderr", "ExposedPorts", "StopSignal", "StopTimeout") if cfg.get(k) is not None}
    body["Image"] = new_ref
    body["Env"] = _minus(cfg.get("Env"), icfg.get("Env"))
    labels = {k: v for k, v in (cfg.get("Labels") or {}).items() if (icfg.get("Labels") or {}).get(k) != v}
    body["Labels"] = labels
    for k in ("Cmd", "Entrypoint", "WorkingDir", "Healthcheck", "Volumes"):
        if cfg.get(k) is not None and cfg.get(k) != icfg.get(k):  # only what compose / the user set explicitly
            body[k] = cfg[k]
    if body.get("Hostname", "").startswith(old["Id"][:12]):
        body.pop("Hostname")  # docker's default hostname is the container id
    body["HostConfig"] = old["HostConfig"]
    nets = old.get("NetworkSettings", {}).get("Networks") or {}
    body["NetworkingConfig"] = {"EndpointsConfig": {
        n: {"Aliases": [a for a in (v.get("Aliases") or []) if a != old["Id"][:12]] or None,
            **({"IPAMConfig": v["IPAMConfig"]} if v.get("IPAMConfig") else {})}
        for n, v in nets.items()}}
    return body


class Updater:
    def __init__(self, docker: Docker | None = None, *, image: str | None = None, services: list[str] | None = None,
                 self_id: str | None = None, health_timeout: float = 180, grace: float = 8):
        self.d = docker or Docker()
        self.image = (image or os.environ.get("NEWSD_IMAGE", "ghcr.io/lthero-big/newsd")).strip()
        self.services = services or [s.strip() for s in os.environ.get("NEWSD_UPDATER_SERVICES", "web,worker").split(",") if s.strip()]
        self.self_id = self_id or socket.gethostname()
        self.health_timeout = health_timeout
        self.grace = grace  # a container without a healthcheck counts as up after running this long
        self.state: dict = {"state": "idle", "version": None, "steps": [], "error": None}
        self.lock = threading.Lock()

    # -- status
    def step(self, msg: str, level: str = "info"):
        log.info("%s", msg)
        self.state["steps"].append({"at": _ts(), "level": level, "msg": msg})

    def project(self) -> str:
        return (self.d.inspect(self.self_id)["Config"].get("Labels") or {}).get(PROJECT_LABEL) or ""

    def targets(self) -> list[dict]:
        proj = self.project()
        if not proj:
            raise DockerError("updater is not running inside a docker compose project")
        by = {}
        for c in self.d.containers(proj):
            svc = (c.get("Labels") or {}).get(SERVICE_LABEL)
            if svc in self.services and c["Id"][:12] != self.self_id[:12]:
                by.setdefault(svc, []).append(c)
        return [c for s in self.services for c in by.get(s, [])]

    # -- the job
    def begin(self, version: str) -> None:
        if not re.fullmatch(r"v?\d+(\.\d+){0,2}([-.+]?[0-9A-Za-z.]+)?", version or ""):
            raise ValueError("bad version")
        with self.lock:
            if self.state["state"] == "running":
                raise RuntimeError("an update is already running")
            self.state = {"state": "running", "version": version, "steps": [], "error": None, "started_at": _ts()}
        threading.Thread(target=self._run, args=(version,), daemon=True, name="updater").start()

    def _run(self, version: str):
        try:
            self.run(version)
            self.state.update(state="done", finished_at=_ts())
        except Exception as exc:
            log.exception("update failed")
            self.state.update(state="failed", error=f"{exc}", finished_at=_ts())
            self.step(f"更新失败：{exc}", "error")

    def run(self, version: str):
        tag = version[1:] if version.startswith("v") else version
        ref = f"{self.image}:{tag}"
        targets = self.targets()
        if not targets:
            raise DockerError(f"没有找到要更新的容器（服务 {', '.join(self.services)}）")
        self.step(f"拉取镜像 {ref}")
        try:
            self.d.pull(self.image, tag)
        except DockerError as exc:
            if self.d.image(ref) is None:
                raise
            self.step(f"拉取失败，使用本地已有的 {ref}（{str(exc)[:120]}）", "warn")
        for c in targets:
            self.recreate(c["Id"], ref)
        self.step(f"全部容器已切换到 {ref}")

    def recreate(self, cid: str, ref: str):
        old = self.d.inspect(cid)
        name = old["Name"].lstrip("/")
        svc = (old["Config"].get("Labels") or {}).get(SERVICE_LABEL, name)
        old_img = self.d.image(old["Image"])
        spec = clone_spec(old, old_img, ref)
        eps = spec["NetworkingConfig"]["EndpointsConfig"]
        extra = dict(list(eps.items())[1:])  # API < 1.44 takes one network at create; connect the rest after
        spec["NetworkingConfig"]["EndpointsConfig"] = dict(list(eps.items())[:1])
        self.step(f"重建 {svc}：停止旧容器")
        self.d.stop(cid)
        backup = f"{name}-old-{int(time.time())}"
        self.d.rename(cid, backup)
        new = None
        try:
            new = self.d.create(name, spec)
            for net, ep in extra.items():
                self.d.connect(net, new, ep)
            self.d.start(new)
            self.wait_healthy(new, svc)
        except Exception as exc:
            self.step(f"{svc} 新容器没有正常启动，回滚到旧容器：{exc}", "error")
            if new:
                self.d.remove(new)
            self.d.rename(cid, name)
            self.d.start(cid)
            raise DockerError(f"{svc} 启动失败，已回滚") from exc
        self.d.remove(cid)
        self.step(f"{svc} 已更新")

    def wait_healthy(self, cid: str, svc: str):
        t0 = time.monotonic()
        end = t0 + self.health_timeout
        has_hc = None
        while time.monotonic() < end:
            st = self.d.inspect(cid)["State"]
            if st.get("Status") in ("exited", "dead"):
                raise DockerError(f"容器已退出（exit {st.get('ExitCode')}）")
            h = (st.get("Health") or {}).get("Status")
            has_hc = h is not None if has_hc is None else has_hc
            if h == "healthy":
                return
            if h == "unhealthy":
                raise DockerError("健康检查失败")
            if not has_hc and st.get("Running") and time.monotonic() - t0 >= self.grace:
                return
            time.sleep(min(1, self.grace / 4 or 1))
        raise DockerError("等待健康检查超时")


class UpdateReq(BaseModel):
    version: str


def make_app(updater: Updater | None = None, token: str | None = None):
    from fastapi import FastAPI, Header, HTTPException

    from .updates import updater_token
    up = updater or Updater()
    tok = token or updater_token()
    app = FastAPI(title="newsd-updater", docs_url=None, redoc_url=None, openapi_url=None)

    def auth(authorization: str | None):
        if not authorization or not hmac.compare_digest(authorization.removeprefix("Bearer ").strip(), tok):
            raise HTTPException(401, "bad token")

    @app.get("/status")
    def status(authorization: str | None = Header(default=None)):
        auth(authorization)
        return {**up.state, "image": up.image, "services": up.services}

    @app.post("/update", status_code=202)
    def update(body: UpdateReq, authorization: str | None = Header(default=None)):
        auth(authorization)
        try:
            up.begin(body.version)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"started": True}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}
    return app


def serve(host: str = "0.0.0.0", port: int = 8090) -> int:  # pragma: no cover
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .settings import get_settings
    if not get_settings().secret_key:
        raise SystemExit("NEWSD_SECRET_KEY is required (the updater token is derived from it)")
    if not os.path.exists(SOCK):
        raise SystemExit(f"{SOCK} not found: mount the Docker socket into the updater container")
    uvicorn.run(make_app(), host=host, port=port, log_level="info")
    return 0
