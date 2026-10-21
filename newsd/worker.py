"""M2-5 standalone worker: APScheduler fires the daily pipeline, which runs the shared collection once and then
builds every active user's issue for the current period. Every run is persisted in ``worker_runs``
(start/end, per-stage counts, per-user results, errors) for the admin page.

The pipeline is idempotent: the pool is keyed by (source, cycle) and an issue by (user, key), so a re-run in the same
hour/day reuses the pool and replaces the issue instead of duplicating anything.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import re
import traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .eventlog import add as ev, prune as prune_events
from .issues import build_issue, default_key
from .models import User, WorkerRun
from .pool import cycle_key, run_cycle
from .settings import get_settings

log = logging.getLogger("newsd.worker")

MAX_ERRORS = 50
ERR_LEN = 300


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _stack(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:ERR_LEN]


def parse_times(spec: str) -> list[tuple[int, int]]:
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", part)
        if not m or int(m[1]) > 23 or int(m[2]) > 59:
            raise ValueError(f"bad NEWSD_WORKER_TIMES entry {part!r}; expected HH:MM")
        out.append((int(m[1]), int(m[2])))
    if not out:
        raise ValueError("NEWSD_WORKER_TIMES is empty; expected e.g. 07:00 or 07:00,19:00")
    return sorted(set(out))


def parse_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"bad NEWSD_WORKER_TZ {name!r}: {exc}") from exc


def local_key(tz: ZoneInfo, now: datetime) -> str:
    """Issue key for "today" in the worker's time zone (the run at 07:00 Asia/Shanghai is that day's issue). now = naive UTC."""
    return default_key(now.replace(tzinfo=timezone.utc).astimezone(tz))


def mark_interrupted(db) -> int:
    """Runs still "running" when a worker starts belong to a process that died; close them out."""
    n = 0
    for run in db.scalars(select(WorkerRun).where(WorkerRun.status == "running")):
        run.status, run.finished_at = "interrupted", _now()
        run.errors = (run.errors or []) + ["worker stopped before this run finished"]
        n += 1
    db.commit()
    return n


class _Recorder:
    """Keeps the WorkerRun row up to date as the pipeline progresses (commit after every stage / user)."""

    def __init__(self, db, run: WorkerRun):
        self.db, self.run = db, run
        self.stages: list[dict] = []
        self.users: list[dict] = []
        self.errors: list[str] = []

    def save(self) -> None:
        # deep copies: stage dicts are mutated in place, and JSON change detection compares against the previous value
        self.run.stages, self.run.users = copy.deepcopy(self.stages), copy.deepcopy(self.users)
        self.run.errors = self.errors[:MAX_ERRORS]
        self.db.commit()

    def error(self, msg: str) -> None:
        if len(self.errors) < MAX_ERRORS:
            self.errors.append(msg[:ERR_LEN])

    def stage(self, name: str) -> dict:
        st = {"stage": name, "status": "running", "started_at": _now().isoformat(), "finished_at": None, "counts": {}, "error": None}
        self.stages.append(st)
        self.save()
        return st

    def end(self, st: dict, status: str, counts: dict | None = None, error: str | None = None) -> None:
        st.update(status=status, finished_at=_now().isoformat(), counts=counts or st["counts"], error=error)
        self.save()


def _pool_counts(rep: dict) -> tuple[dict, list[str]]:
    srcs = rep.get("sources", {})
    failed = {k: v for k, v in srcs.items() if str(v).startswith(("failed", "skipped:"))}
    counts = {k: rep.get(k, 0) for k in ("fetched_sources", "shared_sources", "new_items", "seen_items", "too_old", "extracted",
                                         "extract_failed", "extract_cache_hits", "requests")}
    counts["sources"] = len(srcs)
    counts["sources_failed"] = sum(1 for v in failed.values() if str(v).startswith("failed"))
    return counts, [f"source {k}: {v}" for k, v in failed.items() if str(v).startswith("failed")]


async def run_pipeline(db, *, trigger: str = "manual", cycle: str | None = None, key: str | None = None, fetcher=None,
                       client="auto", pool: bool = True, extract: bool = True, lookback_hours: float = 26,
                       since_hours: float = 26, usernames: list[str] | None = None, tz: ZoneInfo | None = None,
                       now: datetime | None = None) -> dict:
    """Collect into the shared pool, then build each active user's issue. Never raises for a stage/user failure:
    those are recorded on the run (status ok | partial | failed). Returns the stored run as a dict."""
    now = now or _now()
    cycle = cycle or cycle_key(now.replace(tzinfo=timezone.utc))
    key = key or local_key(tz or ZoneInfo("UTC"), now)
    run = WorkerRun(trigger=trigger, cycle=cycle, issue_key=key, status="running", started_at=now)
    db.add(run); db.commit()
    rec = _Recorder(db, run)
    log.info("run %s start trigger=%s cycle=%s key=%s", run.id, trigger, cycle, key)
    ev(db, "INFO", "scheduler", f"运行开始 · {trigger} · 期 {key}", run_id=run.id, detail={"cycle": cycle})
    db.commit()
    pool_failed = False
    try:
        if pool:
            st = rec.stage("collect")
            try:
                rep = await run_cycle(db, cycle, fetcher=fetcher, extract=extract, lookback_hours=lookback_hours)
                counts, errs = _pool_counts(rep)
                for e in errs:
                    rec.error(e)
                for k, v in (rep.get("sources") or {}).items():
                    if str(v).startswith("failed"):
                        ev(db, "ERROR", "fetch", f"抓取失败：{str(v).removeprefix('failed:') or 'failed'}", source=k, run_id=run.id,
                           detail={"status": str(v), "cycle": cycle})
                ev(db, "INFO", "fetch", f"采集完成 · {counts['sources']} 个源 · 新增 {counts.get('new_items', 0)} 条"
                   + (f" · 失败 {counts['sources_failed']}" if counts["sources_failed"] else ""), run_id=run.id, detail=counts)
                rec.end(st, "ok" if not errs else "partial", counts)
            except Exception as exc:  # noqa: BLE001  (stale pool is still better than no issue)
                db.rollback()
                pool_failed = True
                rec.error(f"collect: {_err(exc)}")
                rec.end(st, "failed", error=_err(exc))
                ev(db, "ERROR", "fetch", f"采集阶段失败：{_err(exc)}", run_id=run.id, detail={"stack": _stack(exc)})
                db.commit()
                log.exception("collection failed; building issues from the existing pool")

        st = rec.stage("issues")
        q = select(User).where(User.active.is_(True)).order_by(User.id)
        if usernames:
            q = q.where(User.username.in_(usernames))
        users = list(db.scalars(q))
        ok = failed = items = 0
        for u in users:
            t0 = _now()
            try:
                rep = await build_issue(db, u.id, key, client=client, since_hours=since_hours)
                row = {"user": u.username, "status": "ok", "ai": rep["ai"], "items_in": rep["items_in"], "items_out": rep["items_out"],
                       "dropped": rep.get("dropped", {}), "model_requests": (rep.get("usage") or {}).get("requests", 0)}
                ok += 1
                items += rep["items_out"]
                ev(db, "INFO", "compose", f"日报 {key} · 候选 {rep['items_in']} → 入选 {rep['items_out']}", run_id=run.id, user=u,
                   detail={"ai": rep["ai"], "dropped": rep.get("dropped", {})})
                fo = int((rep.get("usage") or {}).get("failovers", 0) or 0)
                if fo:
                    ev(db, "WARN", "llm", f"模型切换 {fo} 次（主用失败后改用备份）", run_id=run.id, user=u,
                       detail={"chain": ", ".join((rep.get("usage") or {}).get("chain") or [])})
            except Exception as exc:  # noqa: BLE001  (one user never stops the rest)
                db.rollback()
                row = {"user": u.username, "status": "failed", "error": _err(exc)}
                rec.error(f"user {u.username}: {_err(exc)}")
                ev(db, "ERROR", "compose", f"日报生成失败：{_err(exc)}", run_id=run.id, user=u, detail={"stack": _stack(exc)})
                failed += 1
                log.exception("issue for %s failed", u.username)
            row["seconds"] = round((_now() - t0).total_seconds(), 2)
            rec.users.append(row)
            rec.save()
        rec.end(st, "ok" if not failed else ("failed" if not ok else "partial"),
                {"users": len(users), "users_ok": ok, "users_failed": failed, "items_out": items})
        if (users and not ok) or (pool_failed and not ok):
            status = "failed"
        else:
            status = "ok" if not rec.errors else "partial"
    except BaseException as exc:  # noqa: BLE001  (shutdown mid-run: leave an honest record)
        db.rollback()
        rec.error(f"aborted: {_err(exc)}")
        run.status, run.finished_at = "interrupted", _now()
        rec.save()
        raise
    run.status, run.finished_at = status, _now()
    ev(db, {"ok": "INFO", "partial": "WARN"}.get(status, "ERROR"), "scheduler",
       f"运行结束 · {status} · 用时 {round((run.finished_at - run.started_at).total_seconds(), 1)}s"
       + (f" · {len(rec.errors)} 个错误" if rec.errors else ""), run_id=run.id, detail={"errors": "\n".join(rec.errors[:20])})
    rec.save()
    try:
        prune_events(db)
    except Exception:  # noqa: BLE001
        db.rollback()
    log.info("run %s %s: %s", run.id, status, rec.errors[:3])
    return run_view(run)


def run_pipeline_sync(db, **kw) -> dict:
    return asyncio.run(run_pipeline(db, **kw))


def run_view(run: WorkerRun, detail: bool = True) -> dict:
    out = {"id": run.id, "trigger": run.trigger, "cycle": run.cycle, "issue_key": run.issue_key, "status": run.status,
           "started_at": run.started_at.isoformat(), "finished_at": run.finished_at.isoformat() if run.finished_at else None,
           "seconds": round((run.finished_at - run.started_at).total_seconds(), 1) if run.finished_at else None,
           "stages": run.stages or [], "error_count": len(run.errors or []),
           "users_ok": sum(1 for u in run.users or [] if u.get("status") == "ok"),
           "users_failed": sum(1 for u in run.users or [] if u.get("status") == "failed")}
    if detail:
        out.update(users=run.users or [], errors=run.errors or [])
    return out


# --------------------------------------------------------------------------------------------- scheduler

def tick(trigger: str = "schedule") -> dict | None:
    """One scheduler firing: own DB session, whole pipeline, never raises (the scheduler must keep running)."""
    from .db import SessionLocal
    s = get_settings()
    db = SessionLocal()
    try:
        rep = run_pipeline_sync(db, trigger=trigger, tz=parse_tz(s.worker_tz), extract=s.worker_extract,
                                since_hours=s.worker_since_hours)
        push_tick(db)  # reports are ready now: deliver whatever is already due instead of waiting for the next minute
        return rep
    except Exception:  # noqa: BLE001
        log.exception("worker tick crashed")
        return None
    finally:
        db.close()


def push_tick(db=None) -> dict | None:
    """M4: send the deliveries that are due (and retries whose backoff has passed). Never raises. Runs every minute
    and right after each pipeline run; the dispatcher is idempotent and lease-protected, so overlap is harmless."""
    from .db import SessionLocal
    from .push.service import dispatch_due
    own = db is None
    db = db or SessionLocal()
    try:
        rep = dispatch_due(db)
        if rep["attempted"] or rep["planned"]:
            log.info("push: %s", rep)
        return rep
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("push dispatch crashed")
        return None
    finally:
        if own:
            db.close()


def build_scheduler(blocking: bool = True, job=tick, push_job=push_tick):
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    s = get_settings()
    tz = parse_tz(s.worker_tz)
    sched = (BlockingScheduler if blocking else BackgroundScheduler)(timezone=tz)
    for hh, mm in parse_times(s.worker_times):
        sched.add_job(job, CronTrigger(hour=hh, minute=mm, timezone=tz), id=f"pipeline-{hh:02d}:{mm:02d}",
                      name=f"daily pipeline {hh:02d}:{mm:02d} {s.worker_tz}", max_instances=1, coalesce=True,
                      misfire_grace_time=3600)
    if push_job is not None:  # each user's own push time / retry backoff: checked every minute
        sched.add_job(push_job, IntervalTrigger(minutes=1, timezone=tz), id="push-dispatch", name="push dispatch", max_instances=1,
                      coalesce=True, misfire_grace_time=300)
    return sched


def serve() -> int:
    """`newsd worker start`: block forever firing the pipeline at the configured times."""
    import signal

    from .db import SessionLocal
    s = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .eventlog import install
    install()  # 3c: WARNING+ records also go to the admin log (日志与任务)
    sched = build_scheduler(blocking=True)  # validates times / tz before anything else
    db = SessionLocal()
    try:
        n = mark_interrupted(db)
        if n:
            log.warning("closed %d run(s) left running by a previous worker", n)
    finally:
        db.close()
    for j in sched.get_jobs():
        log.info("scheduled %s", j.name)
    from apscheduler.triggers.interval import IntervalTrigger

    from .updates import watch_for_new_code
    sched.add_job(watch_for_new_code, IntervalTrigger(minutes=1), id="update-watch", name="restart after a web update",
                  max_instances=1, coalesce=True)  # 4b: the web process updated the checkout and migrated the DB
    if s.worker_run_on_start:
        sched.add_job(lambda: tick("startup"), id="startup", name="startup run", max_instances=1)  # fires immediately

    def _term(*_):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _term)  # docker stop -> same path as Ctrl-C
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("worker stopping")
        sched.shutdown(wait=False)
    return 0
