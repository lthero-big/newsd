"""DEPTAI-216 3c: structured log events for the admin 日志与任务 page.

``add(db, ...)`` queues an event on the caller's session (committed with the caller's work); ``record(...)``
writes one in its own session (safe from anywhere, never raises). ``DBLogHandler`` mirrors WARNING+ records
of the ``newsd`` loggers. Everything is redacted (URL paths, bot tokens, key-looking strings) and capped.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import traceback
from datetime import timedelta

from sqlalchemy import delete

from .models import LogEvent, now

LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")
MODULES = {"scheduler": "调度", "fetch": "抓取", "compose": "日报", "llm": "模型", "push": "推送", "auth": "登录",
           "admin": "管理", "api": "接口"}
KEEP_DAYS = 30
MSG_LEN, STACK_LEN = 400, 4000
_KEYISH = re.compile(r"\b(sk|xai|gsk|key|tok|nd|nm|ns|inv)[-_][A-Za-z0-9_\-]{12,}\b")
_URL_PATH = re.compile(r"(https?://[^/\s'\"<>]+)/[^\s'\"<>]*")
_BOT = re.compile(r"bot\d+:[\w-]+")
log = logging.getLogger("newsd.eventlog")


def clean(text) -> str:
    text = str(text or "")
    text = _KEYISH.sub(lambda m: m.group(1) + "-***", text)
    text = _BOT.sub("bot***", text)
    return _URL_PATH.sub(r"\1/***", text)


def _detail(detail: dict | None) -> dict | None:
    if not detail:
        return None
    out = {}
    for k, v in detail.items():
        if isinstance(v, str):
            out[k] = clean(v)[:STACK_LEN]
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        else:
            out[k] = clean(repr(v))[:STACK_LEN] if not isinstance(v, (dict, list)) else v
    return out


def make(level: str, module: str, message: str, *, source: str | None = None, run_id: int | None = None,
         user=None, detail: dict | None = None, at=None) -> LogEvent:
    level = level.upper()
    level = "WARN" if level == "WARNING" else level if level in LEVELS else "ERROR" if level == "CRITICAL" else "INFO"
    uid = getattr(user, "id", None) if user is not None and not isinstance(user, str) else None
    uname = user if isinstance(user, str) else getattr(user, "username", None)
    return LogEvent(at=at or now(), level=level, module=(module or "api")[:32], message=clean(message)[:MSG_LEN],
                    source_key=(source or None) and source[:191], run_id=run_id, user_id=uid, username=uname,
                    detail=_detail(detail))


def add(db, level: str, module: str, message: str, **kw) -> None:
    db.add(make(level, module, message, **kw))


def record(level: str, module: str, message: str, **kw) -> None:
    from .db import SessionLocal
    try:
        s = SessionLocal()
        try:
            s.add(make(level, module, message, **kw))
            s.commit()
        finally:
            s.close()
    except Exception:  # noqa: BLE001  (logging must never break the caller)
        pass


def prune(db, days: int = KEEP_DAYS) -> int:
    n = db.execute(delete(LogEvent).where(LogEvent.at < now() - timedelta(days=days))).rowcount or 0
    db.commit()
    return n


_q: queue.Queue = queue.Queue(maxsize=1000)
_writer: threading.Thread | None = None


def _drain() -> None:
    while True:
        kw = _q.get()
        try:
            record(**kw)
        finally:
            _q.task_done()


def enqueue(**kw) -> None:
    """Hand an event to the background writer: the logging call site never waits on the database
    (a WARNING inside a transaction would otherwise block on SQLite's write lock). Drops when full."""
    global _writer
    if _writer is None or not _writer.is_alive():
        _writer = threading.Thread(target=_drain, name="newsd-eventlog", daemon=True)
        _writer.start()
    try:
        _q.put_nowait(kw)
    except queue.Full:
        pass


def flush() -> None:
    """Wait until queued handler events are written (tests, shutdown)."""
    _q.join()


_busy = threading.local()
_MOD_OF = {"newsd.worker": "scheduler", "newsd.pool": "fetch", "newsd.push": "push", "newsd.llm": "llm",
           "newsd.issues": "compose", "newsd.auth": "auth"}


class DBLogHandler(logging.Handler):
    """WARNING+ records of the newsd loggers become log events (with the stack trace for exceptions)."""

    def __init__(self):
        super().__init__(level=logging.WARNING)

    def emit(self, rec: logging.LogRecord) -> None:
        if getattr(_busy, "on", False) or rec.name == log.name:
            return
        _busy.on = True
        try:
            mod = next((m for p, m in _MOD_OF.items() if rec.name.startswith(p)), "api")
            detail = {"logger": rec.name}
            if rec.exc_info:
                detail["stack"] = "".join(traceback.format_exception(*rec.exc_info))[-STACK_LEN:]
            enqueue(level=rec.levelname, module=mod, message=rec.getMessage(), detail=detail)
        except Exception:  # noqa: BLE001
            pass
        finally:
            _busy.on = False


_installed = False


def install() -> None:
    """Attach the handler once per process (app start-up and the worker)."""
    global _installed
    if _installed:
        return
    logging.getLogger("newsd").addHandler(DBLogHandler())
    _installed = True
