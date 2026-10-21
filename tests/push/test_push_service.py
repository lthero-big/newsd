"""Scheduling, retry/backoff and delivery records."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from newsd.models import PushDelivery
from newsd.push import channels as ch
from newsd.push import service
from pushkit import TG_TOKEN, WEBHOOK, add_issue, configure, make_user

TG_URL = "https://api.telegram.org/bot"
WH = {"webhook": {"enabled": True, "target": WEBHOOK, "type": "generic"}}


def utc(h, m=0, day=21):
    return datetime(2026, 10, day, h, m)


def rows(db):
    db.expire_all()
    return list(db.scalars(select(PushDelivery).order_by(PushDelivery.id)))


def setup_user(db, name="alice", channels=None, key="2026-10-21", titles=("Qwen 3", "Sora 2"), **kw):
    u = make_user(db, name)
    configure(db, u, channels or WH, **kw)
    if key:
        add_issue(db, u, key, list(titles))
    return u


# ------------------------------------------------------------------------------------------------ when is something due

def test_nothing_is_sent_before_the_users_time_and_everything_once_after(db, net):
    setup_user(db, time="08:00", tz="UTC")
    net.on(WEBHOOK, (200, {"ok": 1}))
    assert service.dispatch_due(db, utc(7, 59))["planned"] == 0 and net.requests == []
    rep = service.dispatch_due(db, utc(8, 0))
    assert rep["planned"] == 1 and rep["ok"] == 1 and len(net.requests) == 1
    [d] = rows(db)
    assert d.status == "ok" and d.attempts == 1 and d.kind == "scheduled" and d.issue_key == "2026-10-21" and d.progress == d.total == 1
    assert service.dispatch_due(db, utc(8, 1))["planned"] == 0 and service.dispatch_due(db, utc(20, 0))["planned"] == 0
    assert len(net.requests) == 1 and len(rows(db)) == 1  # exactly once per (user, issue, channel)


def test_time_is_in_the_users_own_timezone(db, net):
    setup_user(db, "shanghai", time="08:00", tz="Asia/Shanghai")
    setup_user(db, "ny", time="08:00", tz="America/New_York")
    net.on(WEBHOOK, (200, {"ok": 1}))
    service.dispatch_due(db, utc(0, 5))  # 08:05 in Shanghai, 20:05 the evening before in New York
    assert [(r.user_id, r.status) for r in rows(db)] == [(1, "ok")]
    service.dispatch_due(db, utc(12, 5))  # 08:05 in New York
    assert [(r.user_id, r.status) for r in rows(db)] == [(1, "ok"), (2, "ok")]


def test_waits_for_the_report_then_sends_and_gives_up_after_the_grace_period(db, net, monkeypatch):
    u = setup_user(db, key=None, time="08:00")
    net.on(WEBHOOK, (200, {"ok": 1}))
    assert service.dispatch_due(db, utc(8, 30))["planned"] == 0  # no issue yet: nothing planned, nothing lost
    add_issue(db, u, "2026-10-21", ["Late report"])
    assert service.dispatch_due(db, utc(9, 30))["ok"] == 1
    # a user who switches push on in the evening does not get a stale morning report
    _v = setup_user(db, "carol", time="06:00")
    assert service.dispatch_due(db, utc(20, 0))["planned"] == 0
    assert [r.user_id for r in rows(db)] == [u.id]


def test_disabled_push_disabled_channel_and_inactive_user_send_nothing(db, net):
    _off = setup_user(db, "off", enabled=False)
    _nochan = setup_user(db, "nochan", channels={"webhook": {"enabled": False, "target": WEBHOOK}})
    gone = setup_user(db, "gone")
    gone.active = False
    db.commit()
    assert service.dispatch_due(db, utc(9))["planned"] == 0 and net.requests == []


def test_issue_key_follows_the_worker_timezone(db, net, monkeypatch):
    monkeypatch.setenv("NEWSD_WORKER_TZ", "Asia/Shanghai")
    from newsd.settings import reset_settings
    reset_settings()
    setup_user(db, key="2026-10-22", time="08:00", tz="Asia/Shanghai")  # 2026-10-21T23:30Z is already the 22nd in Shanghai
    net.on(WEBHOOK, (200, {"ok": 1}))
    assert service.dispatch_due(db, utc(23, 30))["planned"] == 0  # 07:30 on the 22nd: not yet
    assert service.dispatch_due(db, utc(0, 5, day=22))["ok"] == 1


# ------------------------------------------------------------------------------------------------ retries

def test_transient_failure_is_retried_with_backoff_then_succeeds(db, net, monkeypatch):
    monkeypatch.setenv("NEWSD_PUSH_BACKOFF", "60,300")
    from newsd.settings import reset_settings
    reset_settings()
    setup_user(db)
    net.on(WEBHOOK, (503, "unavailable"), (503, "unavailable"), (200, {"ok": 1}))
    service.dispatch_due(db, utc(8, 0))
    [d] = rows(db)
    assert d.status == "retrying" and d.attempts == 1 and "503" in d.error and d.next_attempt_at == utc(8, 1)
    assert service.dispatch_due(db, utc(8, 0, ))["attempted"] == 0  # backoff not over
    assert service.dispatch_due(db, utc(8, 1))["retrying"] == 1
    [d] = rows(db)
    assert d.attempts == 2 and d.next_attempt_at == utc(8, 6)  # 300 s for the second retry
    assert service.dispatch_due(db, utc(8, 5))["attempted"] == 0
    assert service.dispatch_due(db, utc(8, 6))["ok"] == 1
    [d] = rows(db)
    assert d.status == "ok" and d.attempts == 3 and d.error is None and d.finished_at is not None
    assert [e["ok"] for e in d.log] == [False, False, True] and "503" in d.log[0]["error"]
    assert len(net.requests) == 3


def test_attempts_are_capped_and_the_row_ends_failed(db, net, monkeypatch):
    monkeypatch.setenv("NEWSD_PUSH_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("NEWSD_PUSH_BACKOFF", "10")
    from newsd.settings import reset_settings
    reset_settings()
    setup_user(db)
    net.on(WEBHOOK, (500, "down"))
    t = utc(8, 0)
    for _ in range(6):
        service.dispatch_due(db, t)
        t += timedelta(minutes=1)
    [d] = rows(db)
    assert d.status == "failed" and d.attempts == 3 and d.max_attempts == 3 and d.next_attempt_at is None and "500" in d.error
    assert len(net.requests) == 3  # no fourth try, and a failed row is not re-planned for the same issue


def test_permanent_errors_fail_immediately(db, net):
    setup_user(db, channels={"telegram": {"enabled": True, "target": "424242", "token": TG_TOKEN}})
    net.on(TG_URL, (401, {"ok": False, "description": "Unauthorized"}))
    rep = service.dispatch_due(db, utc(8, 0))
    [d] = rows(db)
    assert rep["failed"] == 1 and d.status == "failed" and d.attempts == 1 and "401" in d.error and TG_TOKEN not in d.error
    assert d.log[0]["error"] == d.error


def test_rate_limit_waits_at_least_retry_after(db, net, monkeypatch):
    monkeypatch.setenv("NEWSD_PUSH_BACKOFF", "5")
    from newsd.settings import reset_settings
    reset_settings()
    setup_user(db, channels={"telegram": {"enabled": True, "target": "424242", "token": TG_TOKEN}})
    net.on(TG_URL, (429, {"ok": False, "parameters": {"retry_after": 120}}, {"retry-after": "120"}), (200, {"ok": True}))
    service.dispatch_due(db, utc(8, 0))
    [d] = rows(db)
    assert d.status == "retrying" and d.next_attempt_at == utc(8, 2)


def test_summary_and_items_resumes_where_it_stopped(db, net):
    """Per-item mode: overview + 3 items. The third message fails once; the retry sends only what is missing."""
    setup_user(db, channels={"webhook": {"enabled": True, "target": WEBHOOK, "type": "slack", "mode": "summary_and_items"}},
               titles=("One", "Two", "Three"))
    net.on(WEBHOOK, (200, "ok"), (200, "ok"), (500, "oops"), (200, "ok"))
    service.dispatch_due(db, utc(8, 0))
    [d] = rows(db)
    assert d.status == "retrying" and d.progress == 2 and d.total == 4
    service.dispatch_due(db, utc(8, 1))
    [d] = rows(db)
    assert d.status == "ok" and d.progress == 4 and d.attempts == 2
    texts = [net.body(r)["text"] for r in net.requests if net.body(r)]
    sent = [t for i, t in enumerate(texts) if i != 2]  # the 500 is index 2
    assert len(texts) == 5 and "共 3 条" in sent[0] and sent[1].startswith("1/3") and "One" in sent[1] and "Two" in sent[2] and "Three" in sent[3]
    assert [t for t in texts if t.startswith("1/3")].__len__() == 1  # item 1 was not re-sent


def test_per_item_cap_and_delay(db, net, monkeypatch):
    monkeypatch.setenv("NEWSD_PUSH_MAX_ITEMS", "2")
    monkeypatch.setenv("NEWSD_PUSH_ITEM_DELAY", "0.25")
    from newsd.settings import reset_settings
    reset_settings()
    setup_user(db, channels={"webhook": {"enabled": True, "target": WEBHOOK, "mode": "summary_and_items"}}, titles=[f"T{i}" for i in range(6)])
    net.on(WEBHOOK, (200, {"ok": 1}))
    naps = []
    service.dispatch_due(db, utc(8, 0), sleep=naps.append)
    [d] = rows(db)
    assert d.status == "ok" and d.total == 3 and len(net.requests) == 3  # overview + 2 items
    assert naps == [0.25, 0.25]  # spaced out between messages, never before the first
    assert "共 6 条" in net.body(net.requests[0])["text"]


def test_two_dispatchers_never_send_the_same_row_twice(db, net):
    setup_user(db)
    net.on(WEBHOOK, (200, {"ok": 1}))
    [d] = service.plan_due(db, utc(8, 0))
    assert service._claim(db, d, utc(8, 0)) is True
    assert service._claim(db, d, utc(8, 0)) is False  # the second one sees the lease
    assert service._claim(db, d, utc(8, 11)) is True  # a crashed claimer's lease expires after 10 minutes


def test_channels_are_independent(db, net, smtp):
    """One failing channel does not stop or duplicate the others."""
    setup_user(db, channels={"webhook": {"enabled": True, "target": WEBHOOK}, "email": {"enabled": True, "target": "alice@example.com"},
                             "telegram": {"enabled": True, "target": "424242", "token": TG_TOKEN}})
    net.on(WEBHOOK, (200, {"ok": 1}))
    net.on(TG_URL, (500, "down"))
    service.dispatch_due(db, utc(8, 0))
    by = {r.channel: r for r in rows(db)}
    assert by["webhook"].status == "ok" and by["email"].status == "ok" and by["telegram"].status == "retrying"
    assert len(smtp.messages) == 1 and smtp.messages[0]["msg"]["Subject"] == "newsd 日报 2026-10-21"
    body = smtp.messages[0]["msg"].get_body(("html",)).get_content()
    assert "Qwen 3" in body and "<a href=" in body


# ------------------------------------------------------------------------------------------------ isolation + content

def test_each_user_only_gets_their_own_report_on_their_own_channels(db, net):
    _a = setup_user(db, "alice", channels={"webhook": {"enabled": True, "target": "https://hooks.example.com/alice-hook-1111"}}, titles=["Alice only story"])
    _b = setup_user(db, "bob", channels={"webhook": {"enabled": True, "target": "https://hooks.example.com/bob-hook-2222"}}, titles=["Bob only story"])
    net.on("https://hooks.example.com/", (200, {"ok": 1}))
    service.dispatch_due(db, utc(8, 0))
    by_url = {str(r.url): net.body(r)["text"] for r in net.requests}
    assert "Alice only story" in by_url["https://hooks.example.com/alice-hook-1111"] and "Bob" not in by_url["https://hooks.example.com/alice-hook-1111"]
    assert "Bob only story" in by_url["https://hooks.example.com/bob-hook-2222"] and "Alice" not in by_url["https://hooks.example.com/bob-hook-2222"]


def test_trashed_items_are_not_pushed(db, net):
    from newsd.models import IssueItem, ItemMark
    u = setup_user(db, titles=["Keep me", "Trash me"])
    ii = db.scalars(select(IssueItem).order_by(IssueItem.rank)).all()[1]
    db.add(ItemMark(user_id=u.id, item_id=ii.item_id, state="trash")); db.commit()
    net.on(WEBHOOK, (200, {"ok": 1}))
    service.dispatch_due(db, utc(8, 0))
    text = net.body(net.requests[0])["text"]
    assert "Keep me" in text and "Trash me" not in text and "共 1 条" in text


def test_issue_link_uses_the_public_url(db, net, monkeypatch):
    monkeypatch.setenv("NEWSD_PUBLIC_URL", "https://news.example.org/")
    from newsd.settings import reset_settings
    reset_settings()
    setup_user(db)
    net.on(WEBHOOK, (200, {"ok": 1}))
    service.dispatch_due(db, utc(8, 0))
    b = net.body(net.requests[0])
    assert b["url"] == "https://news.example.org/issue/2026-10-21" and "https://news.example.org/issue/2026-10-21" in b["text"]


def test_feed_content_cannot_inject_markup_into_telegram(db, net):
    u = setup_user(db, channels={"telegram": {"enabled": True, "target": "424242", "token": TG_TOKEN}}, key=None)
    add_issue(db, u, "2026-10-21", [("<script>x</script> & co", "javascript:alert(1)", "<img src=x onerror=alert(1)>")])
    net.on(TG_URL, (200, {"ok": True}))
    service.dispatch_due(db, utc(8, 0))
    text = net.body(net.requests[0])["text"]
    assert "<script>" not in text and "<img" not in text and "javascript:" not in text and "&lt;script&gt;" in text


# ------------------------------------------------------------------------------------------------ test pushes + records

def test_test_push_records_one_attempt_and_reports_failures(db, net):
    u = setup_user(db, key=None)
    net.on(WEBHOOK, (200, {"ok": 1}), (500, "down " + WEBHOOK))
    d = service.send_test(db, u, "webhook")
    assert d.status == "ok" and d.kind == "test" and d.issue_key == "" and d.attempts == 1
    assert "测试" in net.body(net.requests[0])["text"] and "example.com/newsd-test" in net.body(net.requests[0])["text"]
    d = service.send_test(db, u, "webhook")
    assert d.status == "failed" and d.attempts == 1 and "500" in d.error and "SECRETPATH" not in d.error  # no retry for tests
    assert service.dispatch_due(db, utc(8, 0))["planned"] == 0  # tests never count as the scheduled delivery


def test_test_push_needs_a_configured_channel_and_is_rate_limited(db, net):
    u = make_user(db, "zed")
    with pytest.raises(service.PushRequestError, match="not configured"):
        service.send_test(db, u, "telegram")
    with pytest.raises(service.PushRequestError) as e:
        service.send_test(db, u, "sms")
    assert e.value.status == 404
    configure(db, u, WH)
    net.on(WEBHOOK, (200, {"ok": 1}))
    for _ in range(service.TEST_PER_MINUTE):
        assert service.send_test(db, u, "webhook").status == "ok"
    with pytest.raises(service.PushRequestError) as e:
        service.send_test(db, u, "webhook")
    assert e.value.status == 429


def test_worker_push_tick_and_scheduler_job(db, net, monkeypatch):
    """The worker's per-minute job calls the dispatcher; tick() hands over right after the pipeline."""
    from newsd import worker
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    setup_user(db, time=now.strftime("%H:%M"))  # due right now, whatever time of day the suite runs (grace window)
    net.on(WEBHOOK, (200, {"ok": 1}))
    monkeypatch.setattr(service, "now", lambda: now)
    key = service.issue_key_today(now)
    from newsd.models import Issue
    db.query(Issue).update({Issue.key: key}); db.commit()
    rep = worker.push_tick()
    assert rep["ok"] == 1 and len(net.requests) == 1
    sched = worker.build_scheduler(blocking=False, job=lambda: None, push_job=worker.push_tick)
    job = {j.id: j for j in sched.get_jobs()}["push-dispatch"]
    assert job.trigger.interval == timedelta(minutes=1) and job.max_instances == 1


def test_a_crash_in_one_delivery_does_not_stop_the_rest(db, net, monkeypatch):
    setup_user(db, "a")
    setup_user(db, "b")
    net.on(WEBHOOK, (200, {"ok": 1}))
    real = ch.send_webhook
    calls = []

    def flaky(cfg, msg):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("bug with " + cfg.url)
        return real(cfg, msg)

    monkeypatch.setattr(ch, "send_webhook", flaky)
    service.dispatch_due(db, utc(8, 0))
    st = sorted((r.user_id, r.status) for r in rows(db))
    assert st == [(1, "retrying"), (2, "ok")]
    assert "SECRETPATH" not in rows(db)[0].error
