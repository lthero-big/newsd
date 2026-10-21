"""DEPTAI-216 3c: a worker run leaves structured log events (start / fetch failures / per-user issues / end)."""
import logging

from sqlalchemy import select

from newsd.models import LogEvent
from test_worker import _setup, run


def test_run_writes_log_events(db, net):
    _setup(db, net, users=("alice",), bad_source=True)
    rep = run(db, net, trigger="manual", key="2026-10-21", cycle="c-ev")
    ev = list(db.scalars(select(LogEvent).where(LogEvent.run_id == rep["id"]).order_by(LogEvent.id)))
    kinds = [(e.level, e.module) for e in ev]
    assert kinds[0] == ("INFO", "scheduler") and "运行开始" in ev[0].message
    bad = [e for e in ev if e.module == "fetch" and e.level == "ERROR"]
    assert len(bad) == 1 and bad[0].source_key == "rss:bad" and "500" in bad[0].message
    assert any(e.module == "fetch" and e.level == "INFO" and "采集完成" in e.message for e in ev)
    mine = [e for e in ev if e.module == "compose"]
    assert len(mine) == 1 and mine[0].username == "alice" and mine[0].user_id is not None and "入选 3" in mine[0].message
    assert kinds[-1] == ("WARN", "scheduler") and "partial" in ev[-1].message  # one source failed


def test_db_log_handler_and_redaction(db):
    from newsd import eventlog
    h = eventlog.DBLogHandler()
    lg = logging.getLogger("newsd.pool.test")
    lg.addHandler(h)
    try:
        lg.info("not stored")  # below WARNING
        lg.warning("fetch https://hooks.example/secret/path?token=abc with sk-abcdefghijklmnop1234 failed")
        try:
            raise RuntimeError("kaboom")
        except RuntimeError:
            lg.exception("crashed")
        eventlog.flush()
    finally:
        lg.removeHandler(h)
    rows = list(db.scalars(select(LogEvent).order_by(LogEvent.id)))
    assert [r.level for r in rows] == ["WARN", "ERROR"] and all(r.module == "fetch" for r in rows)
    assert "secret" not in rows[0].message and "abcdefghijklmnop" not in rows[0].message and "https://hooks.example/***" in rows[0].message
    assert "kaboom" in rows[1].detail["stack"] and rows[1].detail["logger"] == "newsd.pool.test"
