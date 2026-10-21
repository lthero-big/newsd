"""Delivery engine: decides what is due, sends, retries with backoff, records every attempt in ``push_deliveries``.

* ``dispatch_due`` (called by the worker once a minute and right after each pipeline run) creates a delivery per
  enabled channel for every user whose push time has passed and whose issue for today exists, then attempts every
  delivery that is pending or whose retry time has come.
* A failed attempt that is worth repeating (network error, timeout, HTTP 5xx / 429, SMTP 4xx) is rescheduled with
  the configured backoff; permanent errors (bad token, 4xx, refused recipient, blocked host) fail immediately.
* Rows are claimed with a lease (next_attempt_at pushed forward) before sending, so two dispatchers never send twice."""
from __future__ import annotations

import logging
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, update

from ..models import Issue, PushDelivery, User, now
from ..settings import get_settings
from . import channels as ch
from .digest import build_digest, build_messages, sample_digest
from .prefs import CHANNELS, ChannelConfig, enabled_channels, load_channel, user_schedule

log = logging.getLogger("newsd.push")
LEASE = timedelta(minutes=10)
TEST_PER_MINUTE = 5
_lock = threading.Lock()


def backoff_seconds(attempt_no: int) -> float:
    """Delay before the retry that follows failed attempt number ``attempt_no`` (1-based); the last value repeats."""
    try:
        steps = [float(x) for x in get_settings().push_backoff.split(",") if x.strip()] or [60.0]
    except ValueError:
        steps = [60.0]
    return steps[min(attempt_no, len(steps)) - 1]


def messages_for(cfg: ChannelConfig, digest) -> list:
    """The messages one delivery consists of. Pure given (config, digest), so a retry rebuilds the identical list."""
    if cfg.channel == "telegram":
        return build_messages(digest, "html", "summary", ch.LIMITS["telegram"])
    if cfg.channel == "email":
        return build_messages(digest, "html", "summary", ch.LIMITS["email"])
    return build_messages(digest, ch.FLAVOR_OF[cfg.wtype], cfg.mode, ch.LIMITS[cfg.wtype])


def _send(cfg: ChannelConfig, digest, msg) -> None:
    if cfg.channel == "telegram":
        ch.send_telegram(cfg, msg)
    elif cfg.channel == "email":
        plain = build_messages(digest, "plain", "summary", ch.LIMITS["email"])[0]
        ch.send_email(cfg, msg.title + (" (测试)" if digest.sample else ""), plain.text, _email_html(msg.text))
    else:
        ch.send_webhook(cfg, msg)


def _email_html(body: str) -> str:
    return '<div style="font:14px/1.6 sans-serif;white-space:pre-wrap">' + body + "</div>"


def _attempt(db, d: PushDelivery, cfg: ChannelConfig, digest, sleep=_time.sleep, at: datetime | None = None) -> None:
    """One attempt: send the messages not yet sent. Updates the row; never raises for a send failure."""
    n = d.attempts + 1
    t = at or now()
    entry = {"n": n, "at": t.isoformat(), "ok": False, "sent": 0, "error": None}
    d.attempts, d.last_attempt_at, d.next_attempt_at = n, t, None
    start = d.progress
    try:
        msgs = messages_for(cfg, digest)
        d.total = len(msgs)
        delay = get_settings().push_item_delay
        for i in range(d.progress, len(msgs)):
            if i > start and delay and cfg.channel != "email":
                sleep(delay)
            _send(cfg, digest, msgs[i])
            d.progress = i + 1
            db.commit()
        d.status, d.error, d.finished_at = "ok", None, t
        entry.update(ok=True)
    except ch.PushError as exc:
        _fail(d, entry, str(exc), exc.retryable, exc.retry_after, t)
    except Exception as exc:  # noqa: BLE001  (a bug must not stop the other deliveries; assume transient)
        log.exception("push delivery %s crashed", d.id)
        _fail(d, entry, ch.redact(f"{type(exc).__name__}: {exc}", cfg.secrets()), True, None, t)
    entry["sent"] = d.progress - start
    d.log = (d.log or []) + [entry]
    db.commit()


def _fail(d: PushDelivery, entry: dict, error: str, retryable: bool, retry_after: float | None, t: datetime) -> None:
    entry["error"] = error
    d.error = error
    if retryable and d.attempts < d.max_attempts:
        d.status = "retrying"
        d.next_attempt_at = t + timedelta(seconds=max(backoff_seconds(d.attempts), retry_after or 0))
    else:
        d.status, d.finished_at = "failed", t


def _claim(db, d: PushDelivery, at: datetime) -> bool:
    """Lease the row so a concurrent dispatcher skips it. True when this caller won."""
    res = db.execute(update(PushDelivery).where(PushDelivery.id == d.id, PushDelivery.status.in_(("pending", "retrying")),
                                                or_(PushDelivery.next_attempt_at.is_(None), PushDelivery.next_attempt_at <= at))
                     .values(next_attempt_at=at + LEASE))
    db.commit()
    return res.rowcount == 1


def run_delivery(db, d: PushDelivery, sleep=_time.sleep, at: datetime | None = None) -> PushDelivery:
    """Attempt one scheduled delivery (claimed by the caller)."""
    user = db.get(User, d.user_id)
    cfg = load_channel(db, d.user_id, d.channel)
    issue = db.scalar(select(Issue).where(Issue.user_id == d.user_id, Issue.key == d.issue_key))
    if user is None or issue is None or not cfg.enabled:
        d.status, d.finished_at, d.next_attempt_at = "skipped", now(), None
        d.error = "issue no longer exists" if issue is None else "channel was disabled"
        db.commit()
        return d
    _attempt(db, d, cfg, build_digest(db, issue, user.username), sleep, at)
    return d


def issue_key_today(at: datetime) -> str:
    from ..worker import local_key, parse_tz
    return local_key(parse_tz(get_settings().worker_tz), at)


def plan_due(db, at: datetime | None = None) -> list[PushDelivery]:
    """Create pending deliveries for everything that is due at ``at`` (naive UTC). Idempotent per (user, issue, channel)."""
    at = at or now()
    cfg = get_settings()
    key = issue_key_today(at)
    created = []
    users = db.scalars(select(User).where(User.active.is_(True)).order_by(User.id))
    for u in users:
        sch = user_schedule(db, u.id)
        chans = enabled_channels(db, u.id) if sch["enabled"] else []
        if not chans:
            continue
        try:
            local = at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(sch["timezone"]))
            hh, mm = map(int, sch["time"].split(":"))
        except Exception:  # noqa: BLE001
            continue
        due_at = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if local < due_at or local - due_at > timedelta(hours=cfg.push_grace_hours):
            continue
        if db.scalar(select(Issue.id).where(Issue.user_id == u.id, Issue.key == key)) is None:
            continue  # report not ready yet; the next minute (or the pipeline's own dispatch) tries again
        for c in chans:
            if db.scalar(select(PushDelivery.id).where(PushDelivery.user_id == u.id, PushDelivery.issue_key == key,
                                                       PushDelivery.channel == c, PushDelivery.kind == "scheduled")) is not None:
                continue
            d = PushDelivery(user_id=u.id, kind="scheduled", channel=c, issue_key=key, status="pending",
                             max_attempts=max(1, cfg.push_max_attempts), created_at=at)
            db.add(d)
            created.append(d)
    db.commit()
    return created


def dispatch_due(db, at: datetime | None = None, sleep=_time.sleep) -> dict:
    """Plan what became due, then attempt everything pending / due for retry. Safe to call concurrently and repeatedly."""
    with _lock:
        at = at or now()
        planned = plan_due(db, at)
        rows = list(db.scalars(select(PushDelivery).where(
            PushDelivery.kind == "scheduled", PushDelivery.status.in_(("pending", "retrying")),
            or_(PushDelivery.next_attempt_at.is_(None), PushDelivery.next_attempt_at <= at)).order_by(PushDelivery.id)))
        out = {"planned": len(planned), "attempted": 0, "ok": 0, "retrying": 0, "failed": 0, "skipped": 0}
        for d in rows:
            if not _claim(db, d, at):
                continue
            db.refresh(d)
            run_delivery(db, d, sleep, at)
            out["attempted"] += 1
            out[d.status] = out.get(d.status, 0) + 1
        return out


class PushRequestError(Exception):
    """Misconfiguration or rate limit for a test push; message is user-facing. ``status`` is the HTTP status to use."""
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def send_test(db, user: User, channel: str, sleep=_time.sleep) -> PushDelivery:
    """Send a sample digest through the user's saved config of ``channel`` (one attempt, no retry) and record it."""
    if channel not in CHANNELS:
        raise PushRequestError("unknown channel", 404)
    since = now() - timedelta(minutes=1)
    recent = len(list(db.scalars(select(PushDelivery.id).where(PushDelivery.user_id == user.id, PushDelivery.kind == "test",
                                                               PushDelivery.created_at >= since))))
    if recent >= TEST_PER_MINUTE:
        raise PushRequestError("too many test pushes; wait a minute", 429)
    cfg = load_channel(db, user.id, channel)
    target = {"email": cfg.target, "telegram": cfg.target, "webhook": cfg.url}[channel]
    if not target:
        raise PushRequestError(f"{channel} is not configured yet; fill it in and save first")
    d = PushDelivery(user_id=user.id, kind="test", channel=channel, issue_key="", status="pending", max_attempts=1)
    db.add(d)
    db.commit()
    _attempt(db, d, cfg, sample_digest(user.username), sleep)
    return d


def delivery_view(d: PushDelivery, username: str | None = None) -> dict:
    out = {"id": d.id, "kind": d.kind, "channel": d.channel, "issue_key": d.issue_key, "status": d.status, "attempts": d.attempts,
           "max_attempts": d.max_attempts, "progress": d.progress, "total": d.total, "error": d.error,
           "created_at": d.created_at.isoformat(), "last_attempt_at": d.last_attempt_at.isoformat() if d.last_attempt_at else None,
           "next_attempt_at": d.next_attempt_at.isoformat() if d.next_attempt_at and d.status == "retrying" else None,
           "finished_at": d.finished_at.isoformat() if d.finished_at else None, "log": d.log or []}
    if username is not None:
        out["user"] = username
    return out
