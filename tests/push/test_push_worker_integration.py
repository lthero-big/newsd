"""The worker's tick(): shared pipeline builds the issue, then the due push goes out in the same tick."""
from sqlalchemy import select

from poolseed import Pool
from newsd import worker
from newsd.models import PushDelivery
from pushkit import WEBHOOK, configure, make_user


def test_tick_builds_the_issue_then_pushes_it(db, net, monkeypatch):
    monkeypatch.setenv("NEWSD_PUSH_GRACE_HOURS", "24")  # "now" can be any time of day
    monkeypatch.setenv("NEWSD_LLM_RPM", "0")
    from newsd.settings import reset_settings
    reset_settings()

    async def no_collect(db_, cycle, **kw):  # the pool is seeded directly; the internet is not touched
        return {}

    monkeypatch.setattr(worker, "run_cycle", no_collect)
    p = Pool(db)
    p.item("Qwen 3 released today", summary="a new open model")
    p.item("Unrelated gardening tips", summary="tomatoes")
    alice = p.user("alice", interest=["Qwen"])
    bob = p.user("bob", interest=["Gardening"])
    configure(db, alice, {"webhook": {"enabled": True, "target": WEBHOOK, "type": "slack"}}, time="00:00")
    # bob has push switched off: he gets a report but no message
    net.on(WEBHOOK, (200, "ok"))

    rep = worker.tick("manual")
    assert rep["status"] == "ok"
    assert len(net.requests) == 1
    text = net.body(net.requests[0])["text"]
    assert "Qwen 3 released today" in text and "gardening" not in text.lower()
    db.expire_all()
    [d] = db.scalars(select(PushDelivery)).all()
    assert d.user_id == alice.id and d.status == "ok" and d.kind == "scheduled"
    # a second tick (same day) neither rebuilds nor re-sends
    worker.tick("manual")
    assert len(net.requests) == 1
