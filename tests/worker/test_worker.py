import asyncio
import json
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from llmkit import FakeLLM
from newsd import worker
from newsd.llm import keys
from newsd.models import Issue, IssueItem, Source, Subscription, User, WorkerRun
from poolkit import make_fetcher, rss

FEED = "https://feeds.example/ai.xml"
BAD_FEED = "https://broken.example/rss"
KEY = "sk-proc-0123456789abcdef"


def _setup(db, net, users=("alice", "bob"), bad_source=False):
    net.routes[FEED] = (200, "application/rss+xml", rss([
        ("OpenAI releases GPT-6", "https://news.example/a", "A new model."),
        ("Qwen ships open weights", "https://news.example/b", "Weights on HF."),
        ("Sora gets sound", "https://news.example/c", "Video model update."),
    ]))
    srcs = [Source(key="rss:ai", kind="rss", name="ai", url=FEED, config={})]
    if bad_source:
        net.routes[BAD_FEED] = (500, "text/plain", "boom")
        srcs.append(Source(key="rss:bad", kind="rss", name="bad", url=BAD_FEED, config={}))
    db.add_all(srcs); db.commit()
    out = []
    for name in users:
        u = User(username=name, password_hash="x")
        db.add(u); db.commit()
        for s in srcs:
            db.add(Subscription(user_id=u.id, source_id=s.id))
        out.append(u)
    db.commit()
    return out


def llm_responder(system, user, body, request):
    if "输出格式" in system:  # profile scoring
        return json.dumps({"items": [{"id": i["id"], "belongs": True, "score": 8, "section": "模型发布", "reason": "r"}
                                     for i in json.loads(user)]}, ensure_ascii=False)
    if "分组" in system:
        return '{"groups": []}'
    if "主编" in system:
        return json.dumps({"order": [i["id"] for i in json.loads(user)]})
    if "摘要卡片" in system:  # per-item digest (DEPTAI-216)
        return json.dumps({"items": [{"i": r["i"], "title": "译:" + r["title"], "summary": "摘要：" + r["title"]}
                                     for r in json.loads(user)]}, ensure_ascii=False)
    if "翻译成" in system:
        return json.dumps({"translations": [{"i": r["i"], "text": "译:" + r["text"]} for r in json.loads(user)]}, ensure_ascii=False)
    raise AssertionError("unexpected prompt: " + system[:80])


def run(db, net, **kw):
    async def go():
        async with make_fetcher(net) as f:
            return await worker.run_pipeline(db, fetcher=f, **kw)

    return asyncio.run(go())


def test_one_tick_end_to_end_with_mocked_network_and_llm(db, net, monkeypatch):
    alice, bob = _setup(db, net)
    keys.save_settings(db, alice.id, "openai", "m-1", api_key=KEY)  # only alice has a model key
    fake = FakeLLM("openai", llm_responder)
    monkeypatch.setattr(keys, "TRANSPORT", fake.transport)

    rep = run(db, net, trigger="schedule", key="2026-10-21", cycle="2026-10-21T07:00")

    assert rep["status"] == "ok" and rep["finished_at"] and rep["trigger"] == "schedule"
    assert rep["issue_key"] == "2026-10-21" and rep["cycle"] == "2026-10-21T07:00"
    collect, issues = rep["stages"]
    assert (collect["stage"], collect["status"]) == ("collect", "ok")
    assert collect["counts"]["new_items"] == 3 and collect["counts"]["fetched_sources"] == 1 and collect["counts"]["requests"] == 4  # feed + one og:image look-up per new article
    assert net.count(FEED) == 1  # two subscribers, one request
    assert (issues["stage"], issues["status"], issues["counts"]["users_ok"], issues["counts"]["items_out"]) == ("issues", "ok", 2, 6)
    by_user = {u["user"]: u for u in rep["users"]}
    assert by_user["alice"]["ai"] == "on" and by_user["alice"]["items_out"] == 3 and by_user["alice"]["model_requests"] > 0
    assert by_user["bob"]["ai"].startswith("skipped") and by_user["bob"]["items_out"] == 3 and by_user["bob"]["model_requests"] == 0
    # stored issues, one per user, for the period key
    for u in (alice, bob):
        issue = db.scalar(select(Issue).where(Issue.user_id == u.id, Issue.key == "2026-10-21"))
        assert issue is not None
        assert db.scalar(select(func.count()).select_from(IssueItem).where(IssueItem.issue_id == issue.id)) == 3
    # alice's titles were translated through the model, bob's were not
    ai = db.scalar(select(Issue).where(Issue.user_id == alice.id))
    assert all(i.llm_score == 8 for i in db.scalars(select(IssueItem).where(IssueItem.issue_id == ai.id)))
    assert {c["url"].split("/")[2] for c in fake.calls} == {"api.openai.com"}
    # what is on disk (fresh session) matches, including the final state of every stage
    from newsd.db import SessionLocal
    fresh = SessionLocal()
    try:
        row = fresh.get(WorkerRun, rep["id"])
        assert row.status == "ok" and row.started_at < row.finished_at and row.errors == []
        assert [(s["stage"], s["status"]) for s in row.stages] == [("collect", "ok"), ("issues", "ok")]
        assert all(s["finished_at"] for s in row.stages) and len(row.users) == 2
    finally:
        fresh.close()


def test_rerun_same_period_is_idempotent(db, net):
    _setup(db, net)
    first = run(db, net, key="2026-10-21", cycle="c1")
    second = run(db, net, key="2026-10-21", cycle="c1")
    assert first["status"] == second["status"] == "ok"
    assert second["stages"][0]["counts"]["fetched_sources"] == 0 and second["stages"][0]["counts"]["shared_sources"] == 1
    assert net.count(FEED) == 1
    assert db.scalar(select(func.count()).select_from(Issue)) == 2  # replaced, not duplicated
    assert db.scalar(select(func.count()).select_from(WorkerRun)) == 2


def test_failing_source_is_recorded_but_run_continues(db, net):
    _setup(db, net, bad_source=True)
    rep = run(db, net, key="k", cycle="c1")
    assert rep["status"] == "partial"
    assert rep["stages"][0]["status"] == "partial" and rep["stages"][0]["counts"]["sources_failed"] >= 1
    assert any("rss:bad" in e for e in rep["errors"])
    assert rep["users_ok"] == 2 and rep["users"][0]["items_out"] == 3


def test_one_user_failing_does_not_stop_the_others(db, net, monkeypatch):
    _setup(db, net, users=("alice", "bad", "carol"))
    real = worker.build_issue

    async def flaky(db, uid, *a, **kw):
        if db.get(User, uid).username == "bad":
            raise RuntimeError("scoring exploded")
        return await real(db, uid, *a, **kw)

    monkeypatch.setattr(worker, "build_issue", flaky)
    rep = run(db, net, key="k", cycle="c1")
    assert rep["status"] == "partial"
    assert [(u["user"], u["status"]) for u in rep["users"]] == [("alice", "ok"), ("bad", "failed"), ("carol", "ok")]
    assert "scoring exploded" in rep["users"][1]["error"] and "user bad" in rep["errors"][0]
    assert rep["stages"][1]["counts"] == {"users": 3, "users_ok": 2, "users_failed": 1, "items_out": 6}
    assert db.scalar(select(func.count()).select_from(Issue)) == 2


def test_collection_crash_still_builds_issues_from_existing_pool(db, net, monkeypatch):
    _setup(db, net)
    run(db, net, key="k1", cycle="c1")  # fills the pool

    async def boom(*a, **kw):
        raise ConnectionError("network down")

    monkeypatch.setattr(worker, "run_cycle", boom)
    rep = run(db, net, key="k2", cycle="c2")
    assert rep["status"] == "partial"
    assert rep["stages"][0]["status"] == "failed" and "network down" in rep["stages"][0]["error"]
    assert rep["users_ok"] == 2 and rep["users"][0]["items_out"] == 3


def test_everything_failing_marks_the_run_failed(db, net, monkeypatch):
    _setup(db, net)

    async def boom(*a, **kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(worker, "build_issue", boom)
    assert run(db, net, key="k", cycle="c1")["status"] == "failed"


def test_no_pool_and_user_filter_and_inactive_users(db, net):
    alice, bob = _setup(db, net)
    bob.active = False
    db.commit()
    run(db, net, key="k", cycle="c1")
    rep = run(db, net, key="k2", pool=False, usernames=["alice"])
    assert [s["stage"] for s in rep["stages"]] == ["issues"]
    assert [u["user"] for u in rep["users"]] == ["alice"]
    assert db.scalar(select(func.count()).select_from(Issue).where(Issue.user_id == bob.id)) == 0


def test_interrupted_runs_are_closed_on_startup(db):
    db.add(WorkerRun(trigger="schedule", status="running")); db.commit()
    assert worker.mark_interrupted(db) == 1
    r = db.scalar(select(WorkerRun))
    assert r.status == "interrupted" and r.finished_at is not None and r.errors
    assert worker.mark_interrupted(db) == 0


def test_time_and_tz_parsing_and_local_issue_key():
    assert worker.parse_times("19:30, 7:05,07:05") == [(7, 5), (19, 30)]
    for bad in ("", "7", "24:00", "07:60", "x"):
        with pytest.raises(ValueError):
            worker.parse_times(bad)
    with pytest.raises(ValueError):
        worker.parse_tz("Mars/Olympus")
    sh = ZoneInfo("Asia/Shanghai")
    # 23:30 UTC on the 20th is already the 21st in Shanghai
    assert worker.local_key(sh, datetime(2026, 10, 20, 23, 30)) == "2026-10-21"
    assert worker.local_key(ZoneInfo("UTC"), datetime(2026, 10, 20, 23, 30)) == "2026-10-20"


def test_scheduler_registers_one_cron_job_per_time(db, monkeypatch):
    monkeypatch.setenv("NEWSD_WORKER_TIMES", "07:30,19:00")
    monkeypatch.setenv("NEWSD_WORKER_TZ", "Asia/Shanghai")
    from newsd.settings import reset_settings
    reset_settings()
    sched = worker.build_scheduler(blocking=False, job=lambda: None)
    jobs = sorted(sched.get_jobs(), key=lambda j: j.id)
    assert [j.id for j in jobs] == ["pipeline-07:30", "pipeline-19:00", "push-dispatch"]  # M4 adds the per-minute push dispatcher
    assert all(j.max_instances == 1 and j.coalesce for j in jobs)
    now = datetime(2026, 10, 20, 22, 0, tzinfo=timezone.utc)  # 06:00 Shanghai on the 21st
    nxt = jobs[0].trigger.get_next_fire_time(None, now)
    assert nxt.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M") == "2026-10-21 07:30"
    monkeypatch.setenv("NEWSD_WORKER_TIMES", "nope")
    reset_settings()
    with pytest.raises(ValueError):
        worker.build_scheduler(blocking=False)


def test_apscheduler_fires_tick_and_records_run(db, net, monkeypatch):
    """The real scheduler thread fires the real tick(): collect (mocked internet) -> per-user issues -> run row."""
    _setup(db, net)
    real_run_cycle = worker.run_cycle

    async def with_mock_net(db_, cycle, **kw):
        async with make_fetcher(net) as f:
            kw["fetcher"] = f
            return await real_run_cycle(db_, cycle, **kw)

    monkeypatch.setattr(worker, "run_cycle", with_mock_net)
    done = threading.Event()
    real_tick = worker.tick

    def job():
        try:
            real_tick()
        finally:
            done.set()

    sched = worker.build_scheduler(blocking=False, job=job)
    sched.remove_all_jobs()
    from datetime import timedelta
    sched.add_job(job, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=0.3), id="now")
    sched.start()
    try:
        assert done.wait(30), "scheduler never fired the job"
    finally:
        sched.shutdown(wait=True)
    db.expire_all()
    run_ = db.scalar(select(WorkerRun))
    assert run_.status == "ok" and run_.trigger == "schedule" and run_.finished_at is not None
    assert [s["stage"] for s in run_.stages] == ["collect", "issues"]
    assert db.scalar(select(func.count()).select_from(Issue)) == 2
    assert net.count(FEED) == 1


def test_cli_run_once_and_runs_listing(db, net, capsys, monkeypatch):
    _setup(db, net, users=("alice",))
    real_run_cycle = worker.run_cycle

    async def with_mock_net(db_, cycle, **kw):
        async with make_fetcher(net) as f:
            kw["fetcher"] = f
            return await real_run_cycle(db_, cycle, **kw)

    monkeypatch.setattr(worker, "run_cycle", with_mock_net)
    from newsd.cli import main
    assert main(["worker", "run-once", "--no-ai", "--key", "2026-10-21"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok" and out["trigger"] == "manual" and out["users"][0]["items_out"] == 3
    assert main(["worker", "runs"]) == 0
    listing = capsys.readouterr().out
    assert "ok" in listing and "key=2026-10-21" in listing
