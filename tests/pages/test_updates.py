"""DEPTAI-216 item 4b: version discovery, the admin update API, the local (git) update path and the docker updater."""
import subprocess

import pytest
from pagekit import login, register

from newsd import __version__
from newsd import updates as U


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("NEWSD_UPDATE_URL", "NEWSD_UPDATE_CHECK", "NEWSD_UPDATE_REPO", "NEWSD_DEPLOY_MODE", "NEWSD_RESTART_CMD", "NEWSD_GITHUB_TOKEN",
              "NEWSD_UPDATER_URL"):
        monkeypatch.delenv(k, raising=False)


def _admin(client):
    return login(client, "root", "rootpass123")


def _release(tag="v9.9.0", body="- 新功能\n- 修复"):
    return {"latest": tag, "name": f"newsd {tag}", "notes": body, "url": f"https://github.com/x/newsd/releases/tag/{tag}",
            "published_at": "2026-10-21T10:00:00Z"}


def test_version_compare():
    assert U.parse("v0.2.0") == (0, 2, 0) and U.parse("1.2") == (1, 2, 0) and U.parse("2.0.0rc1") == (2, 0, 0)
    assert U.parse("latest") is None
    assert U.newer("v0.2.0", "0.1.0") and not U.newer("v0.1.0", "0.1.0") and not U.newer("v0.0.9", "0.1.0")
    assert not U.newer(None, "0.1.0") and not U.newer("nightly", "0.1.0")


def test_check_caches_and_api_is_admin_only(client, monkeypatch):
    calls = []
    monkeypatch.setattr(U, "fetch_latest", lambda timeout=10: calls.append(1) or _release())
    monkeypatch.setenv("NEWSD_DEPLOY_MODE", "vercel")
    register(client, "erin")
    assert client.get("/api/admin/update").status_code == 403
    assert client.post("/api/admin/update/apply", json={}).status_code == 403
    _admin(client)
    j = client.post("/api/admin/update/check").json()
    assert j["current"] == __version__ and j["latest"] == "v9.9.0" and j["has_update"] and j["notes"].startswith("- 新功能")
    assert j["mode"] == "vercel" and not j["can_apply"] and "Redeploy" in j["reason"]
    client.get("/api/admin/update")
    import time
    time.sleep(0.2)
    assert len(calls) == 1  # fresh cache: no second request
    # a failing check keeps the last good answer and reports the error
    monkeypatch.setattr(U, "fetch_latest", lambda timeout=10: (_ for _ in ()).throw(RuntimeError("rate limited")))
    j = client.post("/api/admin/update/check").json()
    assert j["latest"] == "v9.9.0" and "rate limited" in j["error"]
    # vercel cannot apply from the web
    r = client.post("/api/admin/update/apply", json={})
    assert r.status_code == 409 and "Redeploy" in r.text
    # the overview page carries the card
    assert 'id="upd"' in client.get("/admin").text and "admin-update.js" in client.get("/admin").text


def test_check_disabled_and_no_release(client, monkeypatch):
    monkeypatch.setenv("NEWSD_UPDATE_CHECK", "0")
    monkeypatch.setattr(U, "fetch_latest", lambda timeout=10: pytest.fail("must not fetch"))
    _admin(client)
    j = client.post("/api/admin/update/check").json()
    assert j["latest"] is None and not j["has_update"] and j["check_enabled"] is False


def test_fetch_latest_parses_github(monkeypatch):
    import httpx

    def fake(url, **kw):
        assert url == "https://api.github.com/repos/lthero-big/newsd/releases/latest"
        return httpx.Response(200, json={"tag_name": "v0.3.0", "name": "0.3", "body": "notes", "html_url": "u", "published_at": "p"},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", fake)
    assert U.fetch_latest() == {"latest": "v0.3.0", "name": "0.3", "notes": "notes", "url": "u", "published_at": "p"}
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(404, request=httpx.Request("GET", url)))
    assert U.fetch_latest()["latest"] is None  # no release yet


# ---------------------------------------------------------------- local (git) path

def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)


@pytest.fixture()
def checkout(tmp_path, monkeypatch):
    """origin (bare) + a clone standing in for the installation, with tags v0.1.0 (= current) and v9.9.0."""
    origin, work = tmp_path / "origin.git", tmp_path / "app"
    seed = tmp_path / "seed"
    (seed / "newsd").mkdir(parents=True)
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    for k, v in (("user.name", "t"), ("user.email", "t@t")):
        _git(seed, "config", k, v)
    py = '[project]\nname = "newsd"\ndependencies = ["fastapi"]\n'
    (seed / "pyproject.toml").write_text(py)
    (seed / "newsd" / "__init__.py").write_text(f'__version__ = "{__version__}"\n')
    _git(seed, "add", "-A"); _git(seed, "commit", "-qm", "base"); _git(seed, "tag", f"v{__version__}")
    (seed / "newsd" / "__init__.py").write_text('__version__ = "9.9.0"\n')
    _git(seed, "commit", "-qam", "release"); _git(seed, "tag", "v9.9.0")
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "reset", "-q", "--hard", f"v{__version__}")
    monkeypatch.setattr(U, "ROOT", work)
    monkeypatch.setenv("NEWSD_DEPLOY_MODE", "local")
    return work


def _run_now(monkeypatch):
    """Run the update thread inline so the test can look at the result."""
    import threading

    class Inline:
        def __init__(self, target, args=(), **kw):
            self.t, self.a = target, args

        def start(self):
            self.t(*self.a)
    monkeypatch.setattr(threading, "Thread", Inline)


def test_local_update_fast_forwards_migrates_and_restarts(client, checkout, monkeypatch):
    monkeypatch.setattr(U, "fetch_latest", lambda timeout=10: _release())
    migrated, restarted = [], []
    monkeypatch.setattr(U, "migrate", lambda: migrated.append(1) or subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(U, "restart", lambda delay=1.5: restarted.append(1))
    _admin(client)
    j = client.post("/api/admin/update/check").json()
    assert j["mode"] == "local" and j["can_apply"], j["reason"]
    _run_now(monkeypatch)
    assert client.post("/api/admin/update/apply", json={"version": "v9.9.0"}).status_code == 202
    assert (checkout / "newsd" / "__init__.py").read_text() == '__version__ = "9.9.0"\n' and U.disk_version(checkout / "newsd") == "9.9.0"
    assert migrated == [1] and restarted == [1]
    job = client.get("/api/admin/update").json()["job"]
    msgs = " | ".join(s["msg"] for s in job["steps"])
    assert job["state"] == "restarting" and "已备份数据库" in msgs and "数据库迁移完成" in msgs and "依赖有变化" not in msgs
    bak = [s["msg"] for s in job["steps"] if s["msg"].startswith("已备份数据库")][0].split("：", 1)[1]
    import os
    assert os.path.exists(bak) and oct(os.stat(bak).st_mode)[-3:] == "600"
    # a second click while restarting is refused
    assert client.post("/api/admin/update/apply", json={}).status_code == 409
    logs = client.get("/api/admin/logs", params={"module": "admin", "level": "INFO,WARN,ERROR"}).json()["items"]
    assert any(e["message"].startswith("开始更新") for e in logs)


def test_local_update_rolls_back_when_migration_fails(client, checkout, monkeypatch):
    monkeypatch.setattr(U, "fetch_latest", lambda timeout=10: _release())
    monkeypatch.setattr(U, "migrate", lambda: subprocess.CompletedProcess([], 1, "", "alembic exploded"))
    monkeypatch.setattr(U, "restart", lambda delay=1.5: pytest.fail("must not restart"))
    _admin(client)
    client.post("/api/admin/update/check")
    _run_now(monkeypatch)
    client.post("/api/admin/update/apply", json={})
    job = client.get("/api/admin/update").json()["job"]
    assert job["state"] == "failed" and "alembic exploded" in job["steps"][-1]["msg"] and "代码已退回" in job["steps"][-1]["msg"]
    assert U.disk_version(checkout / "newsd") == __version__


def test_local_update_refuses_dirty_tree_and_unknown_tag(client, checkout, monkeypatch):
    monkeypatch.setattr(U, "fetch_latest", lambda timeout=10: _release("v9.9.1"))
    _admin(client)
    client.post("/api/admin/update/check")
    _run_now(monkeypatch)
    client.post("/api/admin/update/apply", json={})
    assert "没有标签 v9.9.1" in client.get("/api/admin/update").json()["job"]["steps"][-1]["msg"]
    (checkout / "pyproject.toml").write_text("changed\n")
    j = client.get("/api/admin/update").json()
    assert not j["can_apply"] and "未提交的修改" in j["reason"]
    assert client.post("/api/admin/update/apply", json={"version": "v9.9.0"}).status_code == 409
    assert client.post("/api/admin/update/apply", json={"version": "v0.0.1"}).status_code == 409  # not newer


def test_restart_detection(client, monkeypatch):
    db = __import__("newsd.db", fromlist=["SessionLocal"]).SessionLocal()
    try:
        now = U._iso(U._now())
        U._put(db, U.JOB_KEY, {"state": "restarting", "target": f"v{__version__}", "updated_at": now, "steps": []})
        assert U.job_status(db)["state"] == "done"  # the running process is the target version
        U._put(db, U.JOB_KEY, {"state": "restarting", "target": "v9.9.0", "updated_at": "2020-01-01T00:00:00", "steps": []})
        assert U.job_status(db)["state"] == "failed"
    finally:
        db.close()


def test_worker_reexecs_on_new_code(monkeypatch, tmp_path):
    (tmp_path / "__init__.py").write_text('__version__ = "9.9.0"\n')
    monkeypatch.setattr(U, "disk_version", lambda root=None: "9.9.0")
    hit = []
    monkeypatch.setattr(U, "reexec", lambda: hit.append(1))
    U.watch_for_new_code()
    assert hit == [1]
    monkeypatch.setattr(U, "disk_version", lambda root=None: __version__)
    U.watch_for_new_code()
    assert hit == [1]


# ---------------------------------------------------------------- docker updater

class FakeDocker:
    """In-memory stand-in for the Engine API calls the updater uses."""

    def __init__(self, fail_service=None):
        self.cs, self.images, self.log, self.fail = {}, {}, [], fail_service
        self.images["ghcr.io/x/newsd:0.1.0"] = {"Config": {"Env": ["PATH=/usr/bin", "PYTHON_VERSION=3.12.1"], "Cmd": ["newsd", "serve"],
                                                          "Labels": {"org.opencontainers.image.version": "0.1.0"}}}
        self.images["ghcr.io/x/newsd:9.9.0"] = {"Config": {}}
        n = 0
        for svc, cmd in (("updater", ["newsd", "updater"]), ("web", None), ("worker", ["newsd", "worker", "start"])):
            n += 1
            cid = f"{n:012d}" + "f" * 52
            self.cs[cid] = {"Id": cid, "Name": f"/newsd-{svc}-1", "Image": "ghcr.io/x/newsd:0.1.0", "State": {"Status": "running", "Running": True},
                            "Config": {"Hostname": cid[:12], "Image": "ghcr.io/x/newsd:0.1.0",
                                       "Env": ["PATH=/usr/bin", "PYTHON_VERSION=3.12.1", "NEWSD_SECRET_KEY=s"],
                                       "Cmd": cmd or ["newsd", "serve"], "Healthcheck": {"Test": ["CMD", "true"]} if svc == "web" else None,
                                       "Labels": {"com.docker.compose.project": "newsd", "com.docker.compose.service": svc,
                                                  "org.opencontainers.image.version": "0.1.0"}},
                            "HostConfig": {"Binds": ["newsd-data:/data"], "RestartPolicy": {"Name": "unless-stopped"}},
                            "NetworkSettings": {"Networks": {"newsd_default": {"Aliases": ["web" if svc == "web" else svc, cid[:12]]}}}}
        self.updater_id = next(iter(self.cs))[:12]

    def _find(self, ref):
        for cid, c in self.cs.items():
            if cid.startswith(ref) or c["Name"].lstrip("/") == ref:
                return cid
        raise KeyError(ref)

    def inspect(self, cid):
        return self.cs[self._find(cid)]

    def image(self, ref):
        return self.images.get(ref)

    def containers(self, project):
        return [{"Id": c["Id"], "Labels": c["Config"]["Labels"]} for c in self.cs.values() if c["Config"]["Labels"].get("com.docker.compose.project") == project]

    def pull(self, repo, tag):
        self.log.append(("pull", f"{repo}:{tag}"))

    def create(self, name, body):
        self.seq = getattr(self, "seq", 100) + 1
        cid = f"{self.seq:012d}" + "e" * 52
        svc = body["Labels"]["com.docker.compose.service"]
        health = "unhealthy" if svc == self.fail else "healthy"
        self.cs[cid] = {"Id": cid, "Name": "/" + name, "Image": body["Image"], "body": body,
                        "State": {"Status": "running", "Running": True, **({"Health": {"Status": health}} if body.get("Healthcheck") else {})},
                        "Config": {"Labels": body["Labels"]}}
        self.log.append(("create", name, body["Image"]))
        return cid

    def start(self, cid):
        self.cs[self._find(cid)]["State"]["Running"] = True
        self.log.append(("start", self.cs[self._find(cid)]["Name"]))

    def stop(self, cid, t=30):
        self.log.append(("stop", self.cs[self._find(cid)]["Name"]))

    def rename(self, cid, name):
        self.cs[self._find(cid)]["Name"] = "/" + name
        self.log.append(("rename", name.split("-old-")[0] + ("-old" if "-old-" in name else "")))

    def connect(self, net, cid, ep):
        self.log.append(("connect", net))

    def remove(self, cid):
        self.log.append(("remove", self.cs.pop(self._find(cid))["Name"].split("-old-")[0]))


def test_updater_recreates_web_then_worker_with_same_config():
    from newsd.updater import Updater
    d = FakeDocker()
    up = Updater(d, image="ghcr.io/x/newsd", self_id=d.updater_id, health_timeout=5, grace=0.05)
    up.run("v9.9.0")
    assert ("pull", "ghcr.io/x/newsd:9.9.0") in d.log
    creates = [x for x in d.log if x[0] == "create"]
    assert [c[1] for c in creates] == ["newsd-web-1", "newsd-worker-1"] and all(c[2] == "ghcr.io/x/newsd:9.9.0" for c in creates)
    web = next(c for c in d.cs.values() if c["Name"] == "/newsd-web-1")["body"]
    assert web["Env"] == ["NEWSD_SECRET_KEY=s"]  # the old image's PATH / PYTHON_VERSION are not pinned onto the new one
    assert "Cmd" not in web and web["Healthcheck"] == {"Test": ["CMD", "true"]}  # image default cmd vs compose healthcheck
    assert web["Labels"] == {"com.docker.compose.project": "newsd", "com.docker.compose.service": "web"}
    assert web["HostConfig"]["Binds"] == ["newsd-data:/data"] and "Hostname" not in web
    assert web["NetworkingConfig"]["EndpointsConfig"] == {"newsd_default": {"Aliases": ["web"]}}
    worker = next(c for c in d.cs.values() if c["Name"] == "/newsd-worker-1")["body"]
    assert worker["Cmd"] == ["newsd", "worker", "start"]
    assert sorted(c["Name"] for c in d.cs.values()) == ["/newsd-updater-1", "/newsd-web-1", "/newsd-worker-1"]
    assert "全部容器已切换到 ghcr.io/x/newsd:9.9.0" in up.state["steps"][-1]["msg"]


def test_updater_rolls_back_an_unhealthy_web():
    from newsd.updater import DockerError, Updater
    d = FakeDocker(fail_service="web")
    old_web = next(cid for cid, c in d.cs.items() if c["Name"] == "/newsd-web-1")
    up = Updater(d, image="ghcr.io/x/newsd", self_id=d.updater_id, health_timeout=5, grace=0.05)
    with pytest.raises(DockerError):
        up.run("9.9.0")
    assert d.cs[old_web]["Name"] == "/newsd-web-1" and ("start", "/newsd-web-1") in d.log
    assert not any(x[0] == "create" and x[1] == "newsd-worker-1" for x in d.log)  # stops before touching the worker
    assert any("回滚" in s["msg"] for s in up.state["steps"])


def test_updater_api_needs_the_token(monkeypatch):
    from fastapi.testclient import TestClient

    from newsd.updater import Updater, make_app
    d = FakeDocker()
    up = Updater(d, image="ghcr.io/x/newsd", self_id=d.updater_id, health_timeout=5, grace=0.05)
    started = []
    monkeypatch.setattr(up, "begin", lambda v: started.append(v))
    tok = U.updater_token("k")
    c = TestClient(make_app(up, tok))
    assert c.get("/status").status_code == 401 and c.get("/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/status", headers={"Authorization": f"Bearer {tok}"}).json()["state"] == "idle"
    assert c.post("/update", json={"version": "v9.9.0"}, headers={"Authorization": f"Bearer {tok}"}).status_code == 202 and started == ["v9.9.0"]
    assert tok != U.updater_token("other")
    with pytest.raises(ValueError):
        Updater(d, image="x", self_id=d.updater_id).begin("1.0; rm -rf /")


def test_docker_mode_without_updater_explains(client, monkeypatch):
    monkeypatch.setenv("NEWSD_DEPLOY_MODE", "docker")
    monkeypatch.setenv("NEWSD_UPDATER_URL", "http://127.0.0.1:9")  # nothing listens
    monkeypatch.setattr(U, "fetch_latest", lambda timeout=10: _release())
    _admin(client)
    j = client.post("/api/admin/update/check").json()
    assert j["mode"] == "docker" and not j["can_apply"] and "updater" in j["reason"]


def test_docker_job_follows_the_updater(client, monkeypatch):
    import httpx
    st = {"state": "running", "version": "v9.9.0", "steps": [{"at": "t", "level": "info", "msg": "拉取镜像 x:9.9.0"}]}
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(200, json=st, request=httpx.Request("GET", url)))
    db = __import__("newsd.db", fromlist=["SessionLocal"]).SessionLocal()
    try:
        now = U._iso(U._now())
        U._put(db, U.JOB_KEY, {"state": "running", "mode": "docker", "target": "v9.9.0", "updated_at": now, "steps": []})
        j = U.job_status(db)
        assert j["state"] == "running" and j["steps"][-1]["msg"] == "拉取镜像 x:9.9.0"
        st["state"] = "done"  # updater finished, but this process is still the old version: image tag / __version__ mismatch
        j = U.job_status(db)
        assert j["state"] == "failed" and "版本号不一致" in j["steps"][-1]["msg"]
        U._put(db, U.JOB_KEY, {"state": "running", "mode": "docker", "target": f"v{__version__}", "updated_at": now, "steps": []})
        st.update(state="done", version=f"v{__version__}")
        assert U.job_status(db)["state"] == "done"
        U._put(db, U.JOB_KEY, {"state": "running", "mode": "docker", "target": "v9.9.0", "updated_at": now, "steps": []})
        st.update(state="failed", version="v9.9.0", error="web 启动失败，已回滚")
        j = U.job_status(db)
        assert j["state"] == "failed" and "已回滚" in j["steps"][-1]["msg"]
        st["steps"].append({"at": "t", "level": "error", "msg": "更新失败：web 启动失败，已回滚"})
        U._put(db, U.JOB_KEY, {"state": "running", "mode": "docker", "target": "v9.9.0", "updated_at": now, "steps": []})
        j = U.job_status(db)
        assert j["state"] == "failed" and j["steps"][-1]["msg"] == "更新失败，原来的容器已恢复运行"
    finally:
        db.close()
