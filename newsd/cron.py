"""DEPTAI-216 4d: ``/api/cron/tick`` does the worker's job for deployments without a long-running process (Vercel), or for
any external scheduler (Vercel Cron, GitHub Actions, cron-job.org, a plain crontab with curl).

One tick:
1. closes ``cron`` runs that were left "running" by a function the platform killed (time limit);
2. runs the daily pipeline when the latest scheduled time (NEWSD_WORKER_TIMES in NEWSD_WORKER_TZ) has passed and no
   scheduled run started since then. Ticks are idempotent: a second tick for the same slot does nothing, so a missed or
   duplicated cron delivery is harmless. ``?run=force`` runs anyway, ``?run=skip`` only does 3;
3. sends the push deliveries that are due (each user's own push time, retries);
4. refreshes the cached "new version" check (at most every 6 hours).

The endpoint is off until a key is configured: ``CRON_SECRET`` (the variable Vercel Cron sends as
``Authorization: Bearer <CRON_SECRET>``) or ``NEWSD_CRON_SECRET``. Callers send the key in that header or in ``X-Newsd-Cron-Key``.
Keys in the query string are not accepted (they end up in access logs).
"""
from __future__ import annotations

import hmac
import logging
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from .auth import get_db
from .models import WorkerRun
from .settings import get_settings

log = logging.getLogger("newsd.cron")
router = APIRouter()

SCHEDULED = ("schedule", "cron")  # runs that count as "the scheduled run for this slot" (worker or tick)
MODES = ("auto", "force", "skip")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def cron_secret() -> str:
    return (os.environ.get("NEWSD_CRON_SECRET") or os.environ.get("CRON_SECRET") or "").strip()


def stale_after() -> timedelta:
    """A cron run still "running" after this long was killed by the platform (Vercel: 300 s Hobby, up to 800 s Pro)."""
    return timedelta(minutes=float(os.environ.get("NEWSD_CRON_STALE_MINUTES", "35")))


def authorized(request: Request, secret: str) -> bool:
    auth = request.headers.get("authorization", "")
    given = auth[7:].strip() if auth[:7].lower() == "bearer " else request.headers.get("x-newsd-cron-key", "").strip()
    return bool(secret) and bool(given) and hmac.compare_digest(given.encode(), secret.encode())


def last_slot(now: datetime, times: list[tuple[int, int]], tz: ZoneInfo) -> datetime:
    """The latest scheduled time at or before ``now`` (both naive UTC)."""
    local = now.replace(tzinfo=timezone.utc).astimezone(tz)
    best = None
    for d in (local.date(), local.date() - timedelta(days=1)):
        for hh, mm in times:
            t = datetime.combine(d, time(hh, mm), tzinfo=tz)
            if t <= local and (best is None or t > best):
                best = t
    return best.astimezone(timezone.utc).replace(tzinfo=None)


def close_stale(db, now: datetime) -> list[int]:
    cut = now - stale_after()
    ids = []
    for run in db.scalars(select(WorkerRun).where(WorkerRun.status == "running", WorkerRun.trigger == "cron",
                                                   WorkerRun.started_at < cut)):
        run.status, run.finished_at = "interrupted", now
        run.errors = (run.errors or []) + ["the function running this tick was stopped before the run finished (platform time limit?)"]
        ids.append(run.id)
    if ids:
        from .eventlog import add as ev
        ev(db, "WARN", "scheduler", f"定时接口：{len(ids)} 次运行未正常结束，已标记为中断（可能超过平台的函数时长限制）",
           detail={"runs": ids})
    db.commit()
    return ids


def _blocking(db, slot: datetime, mode: str, exclude: int | None = None, before: int | None = None):
    """Why the pipeline must not start now, or None. A fresh running run always blocks; in auto mode so does any scheduled
    run that started at or after the slot."""
    q = select(WorkerRun).where(WorkerRun.trigger.in_(SCHEDULED))
    if exclude is not None:
        q = q.where(WorkerRun.id != exclude)
    if before is not None:
        q = q.where(WorkerRun.id < before)
    for run in db.scalars(q.order_by(WorkerRun.id.desc()).limit(50)):
        if run.status == "running" and run.started_at >= _now() - stale_after():
            return "busy", run
        if mode == "auto" and run.started_at >= slot and run.status != "running":
            return "done", run
    return None


def claim(db, slot: datetime, mode: str, now: datetime) -> tuple[WorkerRun | None, str, WorkerRun | None]:
    """Insert our run row, then make sure no earlier competitor exists. Every tick commits its row before it looks, so of two
    overlapping ticks at least one sees the other and only the lower id proceeds."""
    hit = _blocking(db, slot, mode)
    if hit:
        return None, hit[0], hit[1]
    run = WorkerRun(trigger="cron", status="running", started_at=now, cycle="", issue_key="")
    db.add(run)
    db.commit()
    hit = _blocking(db, slot, mode, exclude=run.id, before=run.id)
    if hit:
        db.delete(run)
        db.commit()
        return None, hit[0], hit[1]
    return run, "", None


def _pipeline(db, run: WorkerRun) -> dict:
    from .worker import parse_tz, run_pipeline_sync
    s = get_settings()
    return run_pipeline_sync(db, trigger="cron", tz=parse_tz(s.worker_tz), extract=s.worker_extract,
                             since_hours=s.worker_since_hours, run=run)


def tick(db, mode: str = "auto", now: datetime | None = None) -> dict:
    from .worker import parse_times, parse_tz, push_tick, run_view
    s = get_settings()
    now = now or _now()
    slot = last_slot(now, parse_times(s.worker_times), parse_tz(s.worker_tz))
    out: dict = {"ok": True, "at": now.isoformat() + "Z", "slot": slot.isoformat() + "Z", "mode": mode,
                 "interrupted": close_stale(db, now)}
    pipe: dict = {"ran": False}
    if mode == "skip":
        pipe["reason"] = "skip"
    else:
        run, why, other = claim(db, slot, mode, now)
        if run is None:
            pipe.update(reason=why, run_id=other.id if other else None)
        else:
            try:
                rep = _pipeline(db, run)
                pipe.update(ran=True, run={k: rep.get(k) for k in ("id", "status", "issue_key", "seconds", "error_count",
                                                                   "users_ok", "users_failed")})
            except Exception as exc:  # noqa: BLE001  (run_pipeline records stage/user failures itself; this is the rest)
                db.rollback()
                log.exception("cron pipeline crashed")
                run = db.get(WorkerRun, run.id)
                if run is not None and run.status == "running":
                    run.status, run.finished_at = "failed", _now()
                    run.errors = (run.errors or []) + [f"{type(exc).__name__}: {exc}"[:300]]
                    db.commit()
                pipe.update(ran=True, run=run_view(run, detail=False) if run else None)
                out["ok"] = False
    out["pipeline"] = pipe
    out["push"] = push_tick(db)
    try:
        from . import updates
        info = updates.check(db)
        out["latest_release"] = info.get("latest")
    except Exception:  # noqa: BLE001  (never fail a tick over the version check)
        db.rollback()
        log.exception("update check failed")
    return out


@router.api_route("/api/cron/tick", methods=["GET", "POST"])
def cron_tick(request: Request, run: str = "auto", db=Depends(get_db)):
    secret = cron_secret()
    if not secret:
        raise HTTPException(503, "cron endpoint disabled: set CRON_SECRET (or NEWSD_CRON_SECRET)")
    if not authorized(request, secret):
        raise HTTPException(401, "unauthorized")
    if run not in MODES:
        raise HTTPException(400, "run must be one of: " + ", ".join(MODES))
    return tick(db, run)
