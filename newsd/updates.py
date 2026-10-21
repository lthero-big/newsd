"""Version discovery and web-triggered updates (DEPTAI-216 4b).

* Discovery: the latest GitHub release of NEWSD_UPDATE_REPO (default lthero-big/newsd) is checked at most every
  6 hours (lazily, from the admin overview) and cached in ``global_settings`` so every process sees the same answer.
* Applying an update depends on how newsd runs (``mode()``):
    - ``docker``: the web container never touches the Docker socket. It asks the opt-in ``updater`` sidecar
      (``newsd updater``, see newsd/updater.py) to pull the release image and recreate web and worker; the new web
      container runs the migrations on start.
    - ``local``: a git checkout. Fast-forward to the release tag, reinstall only if the dependencies changed,
      back up a SQLite database, run the migrations, then restart (NEWSD_RESTART_CMD, or the process re-executes
      itself; the worker notices the new version on disk and re-executes too).
    - ``vercel``: nothing to do in-process; a new deployment is triggered by pushing to the connected repository.
* The job state lives in ``global_settings`` as well, so the page can show the outcome after the restart.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from . import __version__

log = logging.getLogger("newsd.updates")

CHECK_KEY, JOB_KEY = "update.check", "update.job"
CHECK_EVERY = timedelta(hours=6)
ROOT = Path(__file__).resolve().parent.parent  # the checkout (local mode) or site-packages (installed)
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat(timespec="seconds") if d else None


# ---------------------------------------------------------------- versions

def parse(v: str | None) -> tuple[int, ...] | None:
    """'v0.2.0' / '0.2' / '1.0.0rc1' -> (0, 2, 0) etc. Pre-release suffixes are ignored; None if not a version."""
    m = re.match(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", v or "")
    return tuple(int(x or 0) for x in m.groups()) if m else None


def newer(latest: str | None, current: str = __version__) -> bool:
    a, b = parse(latest), parse(current)
    return bool(a and b and a > b)


def disk_version(root: Path | None = None) -> str | None:
    """The version of the code on disk right now (differs from __version__ once a local update has run)."""
    try:
        txt = ((root or Path(__file__).resolve().parent) / "__init__.py").read_text()
    except OSError:
        return None
    m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", txt)
    return m.group(1) if m else None


# ---------------------------------------------------------------- settings

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def repo() -> str:
    return _env("NEWSD_UPDATE_REPO", "lthero-big/newsd")


def mode() -> str:
    m = _env("NEWSD_DEPLOY_MODE").lower()
    if m in ("docker", "local", "vercel", "none"):
        return m
    if os.environ.get("VERCEL"):
        return "vercel"
    if Path("/.dockerenv").exists() or os.environ.get("NEWSD_IN_DOCKER") == "1":
        return "docker"
    return "local"


def updater_url() -> str:
    return _env("NEWSD_UPDATER_URL", "http://updater:8090").rstrip("/")


def updater_token(secret: str | None = None) -> str:
    """Shared secret between web and the updater sidecar, derived from NEWSD_SECRET_KEY (no extra config)."""
    from .settings import get_settings
    key = (secret if secret is not None else get_settings().secret_key).encode()
    return hmac.new(key, b"newsd-updater-v1", hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- state in global_settings

def _get(db, key):
    from .models import GlobalSetting
    row = db.get(GlobalSetting, key)
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


def _put(db, key, value: dict):
    from .models import GlobalSetting
    row = db.get(GlobalSetting, key)
    if row is None:
        db.add(GlobalSetting(key=key, value=value))
    else:
        row.value = value
    db.commit()


def _session():
    from .db import SessionLocal
    return SessionLocal()


# ---------------------------------------------------------------- discovery

def fetch_latest(timeout: float = 10) -> dict:
    """Ask GitHub for the latest (non-draft, non-prerelease) release. NEWSD_UPDATE_URL overrides the endpoint
    (mirrors, tests); it must return the same JSON shape."""
    url = _env("NEWSD_UPDATE_URL") or f"https://api.github.com/repos/{repo()}/releases/latest"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"newsd/{__version__}"}
    tok = _env("NEWSD_GITHUB_TOKEN")
    if tok and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {tok}"
    r = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    if r.status_code == 404:
        return {"latest": None, "notes": "", "url": f"https://github.com/{repo()}/releases", "published_at": None}
    r.raise_for_status()
    j = r.json()
    return {"latest": j.get("tag_name"), "name": j.get("name") or j.get("tag_name"), "notes": (j.get("body") or "")[:20000],
            "url": j.get("html_url"), "published_at": j.get("published_at")}


def check(db, force: bool = False) -> dict:
    """Return the cached release info, refreshing it when forced or older than CHECK_EVERY."""
    cur = _get(db, CHECK_KEY)
    at = cur.get("checked_at")
    stale = not at or _now() - datetime.fromisoformat(at) > CHECK_EVERY
    if (force or stale) and _env("NEWSD_UPDATE_CHECK", "1") != "0":
        try:
            info = fetch_latest()
            cur = {**info, "checked_at": _iso(_now()), "error": None}
        except Exception as exc:  # network, rate limit, bad JSON: keep the last good answer
            cur = {**cur, "checked_at": _iso(_now()), "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        _put(db, CHECK_KEY, cur)
    return cur


def check_in_background() -> None:
    def run():
        db = _session()
        try:
            check(db)
        except Exception:
            log.exception("update check failed")
        finally:
            db.close()
    threading.Thread(target=run, name="update-check", daemon=True).start()


def capability() -> tuple[bool, str]:
    """Can this installation update itself from the web page? (ok, reason shown otherwise)."""
    m = mode()
    if m == "vercel":
        return False, "Vercel 部署：推送到仓库或在 Vercel 控制台点 Redeploy 即可更新，迁移在构建时执行。"
    if m == "none":
        return False, "已通过 NEWSD_DEPLOY_MODE=none 关闭网页端更新。"
    if m == "docker":
        try:
            r = httpx.get(updater_url() + "/status", headers={"Authorization": f"Bearer {updater_token()}"}, timeout=3)
            if r.status_code == 200:
                return True, ""
            return False, f"更新服务返回 {r.status_code}，请检查 updater 容器。"
        except httpx.HTTPError:
            return False, "没有找到更新服务（updater 容器）。用 docker compose --profile updater up -d 启用，或手动 docker compose pull && docker compose up -d。"
    if not (ROOT / ".git").exists():
        return False, "当前不是 git 检出的安装，无法自动更新；请按 README 的「升级」一节手动升级。"
    if not shutil.which("git"):
        return False, "没有找到 git 命令。"
    dirty = _git("status", "--porcelain", "--untracked-files=no", check=False)
    if dirty.returncode == 0 and dirty.stdout.strip():
        return False, "代码目录有未提交的修改，为避免覆盖，请先处理后再更新。"
    return True, ""


def overview(db, refresh: bool = True) -> dict:
    info = _get(db, CHECK_KEY)
    if refresh:
        at = info.get("checked_at")
        if not at or _now() - datetime.fromisoformat(at) > CHECK_EVERY:
            check_in_background()
    job = job_status(db)
    ok, reason = capability()
    return {"current": __version__, "latest": info.get("latest"), "name": info.get("name"), "notes": info.get("notes") or "",
            "url": info.get("url"), "published_at": info.get("published_at"), "checked_at": info.get("checked_at"),
            "error": info.get("error"), "has_update": newer(info.get("latest")), "mode": mode(), "can_apply": ok,
            "reason": reason, "job": job, "check_enabled": _env("NEWSD_UPDATE_CHECK", "1") != "0"}


# ---------------------------------------------------------------- jobs

def job_status(db) -> dict:
    job = _get(db, JOB_KEY)
    if job.get("state") == "running" and job.get("mode") == "docker":
        job = _poll_updater(db, job)
    elif job.get("state") == "restarting":
        # the restart happened if the process now runs the target version
        if parse(job.get("target")) == parse(__version__):
            job = _finish(db, job, "done", f"已重启，当前版本 {__version__}")
        elif _now() - datetime.fromisoformat(job["updated_at"]) > timedelta(minutes=5):
            job = _finish(db, job, "failed", "5 分钟内没有以新版本重启，请检查进程管理（NEWSD_RESTART_CMD / systemd）。")
    return job


def _step(db, job: dict, msg: str, level: str = "info") -> dict:
    job.setdefault("steps", []).append({"at": _iso(_now()), "level": level, "msg": msg[:2000]})
    job["updated_at"] = _iso(_now())
    _put(db, JOB_KEY, job)
    log.info("update: %s", msg)
    return job


def _finish(db, job: dict, state: str, msg: str) -> dict:
    job["state"] = state
    job["finished_at"] = _iso(_now())
    job = _step(db, job, msg, "error" if state == "failed" else "info")
    from .eventlog import record
    record("ERROR" if state == "failed" else "INFO", "admin", f"更新{'失败' if state == 'failed' else '完成'} · {job.get('target')}",
           user=job.get("by"), detail={"steps": job.get("steps", [])[-8:]})
    return job


class UpdateError(Exception):
    pass


def start(db, user, target: str | None = None) -> dict:
    """Begin updating to ``target`` (default: the latest known release). Runs in a background thread."""
    with _lock:
        job = job_status(db)
        if job.get("state") in ("running", "restarting") and _now() - datetime.fromisoformat(job["updated_at"]) < timedelta(minutes=15):
            raise UpdateError("已经有一个更新在进行中")
        ok, reason = capability()
        if not ok:
            raise UpdateError(reason)
        info = _get(db, CHECK_KEY)
        target = (target or info.get("latest") or "").strip()
        if not parse(target):
            raise UpdateError("没有可更新的版本")
        if not newer(target):
            raise UpdateError(f"{target} 不比当前版本 {__version__} 新")
        if not re.fullmatch(r"v?\d+(\.\d+){0,2}([-.+]?[0-9A-Za-z.]+)?", target):
            raise UpdateError("版本号格式不对")
        job = {"state": "running", "mode": mode(), "target": target, "from": __version__, "by": user.username,
               "started_at": _iso(_now()), "updated_at": _iso(_now()), "steps": []}
        _step(db, job, f"开始更新 {__version__} → {target}（{'Docker' if job['mode'] == 'docker' else '本机'}）")
    runner = _run_docker if job["mode"] == "docker" else _run_local
    threading.Thread(target=_guard, args=(runner, target), name="update", daemon=True).start()
    return job


def _guard(runner, target):
    db = _session()
    try:
        runner(db, target)
    except Exception as exc:  # never leave a job "running" forever
        log.exception("update failed")
        _finish(db, _get(db, JOB_KEY), "failed", f"{type(exc).__name__}: {exc}")
    finally:
        db.close()


# ---- docker: hand over to the updater sidecar

def _run_docker(db, target):
    job = _get(db, JOB_KEY)
    r = httpx.post(updater_url() + "/update", json={"version": target}, headers={"Authorization": f"Bearer {updater_token()}"}, timeout=10)
    if r.status_code >= 400:
        _finish(db, job, "failed", f"更新服务拒绝了请求：{r.status_code} {r.text[:300]}")
        return
    _step(db, job, "已交给更新服务：拉取镜像并依次重建 web、worker；web 启动时自动迁移数据库")


def _poll_updater(db, job):
    try:
        r = httpx.get(updater_url() + "/status", headers={"Authorization": f"Bearer {updater_token()}"}, timeout=3)
        st = r.json()
    except Exception:
        return job  # the web container is probably being replaced right now
    seen = {s["msg"] for s in job.get("steps", [])}
    changed = False
    for s in st.get("steps", []):
        if s.get("msg") and s["msg"] not in seen:
            job.setdefault("steps", []).append(s)
            changed = True
    if st.get("state") in ("done", "failed") and st.get("version") == job.get("target"):
        ok = st["state"] == "done" and parse(job.get("target")) == parse(__version__)
        if st["state"] == "failed":
            err = st.get("error") or "更新服务报告失败"
            done = any(err in s.get("msg", "") for s in job.get("steps", []))
            return _finish(db, job, "failed", "更新失败，原来的容器已恢复运行" if done else err)
        if ok:
            return _finish(db, job, "done", f"已更新到 {__version__}")
        return _finish(db, job, "failed", f"镜像已切换，但现在运行的是 {__version__}，不是 {job.get('target')}："
                                          "镜像标签和程序版本号不一致，请检查镜像。")
    if changed:
        job["updated_at"] = _iso(_now())
        _put(db, JOB_KEY, job)
    return job


# ---- local: git fast-forward, deps, backup, migrate, restart

def _git(*args, check: bool = True):
    p = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=300)
    if check and p.returncode != 0:
        raise UpdateError(f"git {' '.join(args)} 失败：{(p.stderr or p.stdout).strip()[:400]}")
    return p


def _deps(text: str) -> list[str]:
    import tomllib
    proj = tomllib.loads(text).get("project", {})
    return sorted(proj.get("dependencies", [])) + sorted(f"{k}:{v}" for k, vs in proj.get("optional-dependencies", {}).items() for v in vs)


def _backup_sqlite(db, tag: str) -> str | None:
    url = str(db.get_bind().url)
    if not url.startswith("sqlite") or url.endswith(":memory:"):
        return None
    import sqlite3
    path = Path(url.split("///", 1)[-1])
    dest = path.with_name(f"{path.name}.bak-update-{__version__}-{_now():%Y%m%d-%H%M%S}")
    src, dst = sqlite3.connect(str(path)), sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close(); src.close()
    os.chmod(dest, 0o600)
    return str(dest)


def _pip_cmd() -> list[str] | None:
    if subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True).returncode == 0:
        return [sys.executable, "-m", "pip", "install", "--quiet"]
    if shutil.which("uv"):
        return ["uv", "pip", "install", "--quiet", "--python", sys.executable]
    return None


def _run_local(db, target):
    job = _get(db, JOB_KEY)
    tag = target if target.startswith("v") else f"v{target}"
    remote = _env("NEWSD_UPDATE_REMOTE", "origin")
    _git("fetch", "--quiet", "--tags", remote)
    if _git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}", check=False).returncode != 0:
        return _finish(db, job, "failed", f"远端 {remote} 上没有标签 {tag}")
    old = _git("rev-parse", "HEAD").stdout.strip()
    if _git("merge-base", "--is-ancestor", old, tag, check=False).returncode != 0:
        return _finish(db, job, "failed", f"当前代码包含 {tag} 里没有的提交，不能快进；请手动合并后再更新。")
    deps_changed = _deps(_git("show", f"{old}:pyproject.toml").stdout) != _deps(_git("show", f"{tag}:pyproject.toml").stdout)
    pip = _pip_cmd() if deps_changed else None
    if deps_changed and pip is None:
        return _finish(db, job, "failed", "新版本的依赖有变化，但没有找到 pip 或 uv，无法安装；请手动升级。")
    _step(db, job, f"已获取 {tag}（{old[:7]} → {_git('rev-parse', '--short', tag).stdout.strip()}）")
    branch = _git("symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip()
    _git("merge", "--ff-only", "--quiet", tag) if branch else _git("checkout", "--quiet", "--detach", tag)
    _step(db, job, f"代码已快进到 {tag}" + (f"（分支 {branch}）" if branch else ""))
    try:
        if deps_changed:
            p = subprocess.run([*pip, "-e", str(ROOT)], capture_output=True, text=True, timeout=1800)
            if p.returncode != 0:
                raise UpdateError("安装依赖失败：" + (p.stderr or p.stdout).strip()[-600:])
            _step(db, job, "依赖有变化，已重新安装")
        bak = _backup_sqlite(db, tag)
        if bak:
            _step(db, job, f"已备份数据库：{bak}")
        else:
            _step(db, job, "数据库不是 SQLite，未自动备份（Postgres 请自行 pg_dump）", "warn")
        p = migrate()
        if p.returncode != 0:
            raise UpdateError("数据库迁移失败：" + (p.stderr or p.stdout).strip()[-600:] + (f"。可用备份 {bak} 恢复" if bak else ""))
        _step(db, job, "数据库迁移完成")
    except Exception as exc:
        _git("reset", "--quiet", "--keep", old, check=False) if branch else _git("checkout", "--quiet", "--detach", old, check=False)
        return _finish(db, job, "failed", f"{exc}（代码已退回 {old[:7]}）")
    job["state"] = "restarting"
    _step(db, job, "正在重启 web 和 worker…")
    restart()


def migrate() -> subprocess.CompletedProcess:
    """`newsd db upgrade` with the *new* code (a fresh interpreter; this process still has the old modules loaded)."""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)}
    return subprocess.run([sys.executable, "-c", "import sys; from newsd.cli import main; sys.exit(main(['db', 'upgrade']))"],
                          capture_output=True, text=True, timeout=1800, cwd=os.getcwd(), env=env)


def restart(delay: float = 1.5) -> None:
    """NEWSD_RESTART_CMD (run detached, e.g. `systemctl restart newsd-web newsd-worker`), else re-exec this
    process; a running worker sees the new version on disk within a minute and re-executes itself."""
    cmd = _env("NEWSD_RESTART_CMD")

    def go():
        time.sleep(delay)
        if cmd:
            subprocess.Popen(shlex.split(cmd), start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=os.getcwd())
            return
        reexec()
    threading.Thread(target=go, name="update-restart", daemon=True).start()


def reexec() -> None:  # pragma: no cover - replaces the process
    logging.shutdown()
    os.execv(sys.executable, [sys.executable, *sys.argv])


def watch_for_new_code() -> None:
    """Worker job: re-exec when a local update replaced the code on disk (the web process did the migration)."""
    v = disk_version()
    if v and v != __version__ and parse(v):
        log.warning("code on disk is %s (running %s): restarting the worker", v, __version__)
        reexec()

