"""DEPTAI-216 item 3c: admin console — groups & permissions, bulk user actions, invites into groups,
group quota, source health, structured logs + run timeline, overview, pages, migration 0012."""
from datetime import timedelta

from pagekit import login, register, subscribe_all
from poolseed import Pool
from sqlalchemy import select


def _admin(client):
    return login(client, "root", "rootpass123")


def _db():
    from newsd.db import SessionLocal
    return SessionLocal()


def _uid(name):
    from newsd.models import User
    db = _db()
    try:
        return db.scalar(select(User.id).where(User.username == name))
    finally:
        db.close()


def _seed_sources():
    db = _db()
    p = Pool(db)
    for k in ("rss:a", "rss:b"):
        p.source(k)
    db.close()


def test_groups_crud_and_validation(client):
    _seed_sources()
    _admin(client)
    r = client.post("/api/admin/groups", json={"name": " 研发  组 ", "color": "#2563eb", "perms": ["read", "src", "logs", "users", "site", "bogus"],
                                               "default_sources": ["rss:a"], "quota": 40})
    assert r.status_code == 200, r.text
    g = r.json()
    assert g["name"] == "研发 组" and g["perms"] == ["read", "src", "logs"]  # users / site are admin-role only; read is implied
    assert client.post("/api/admin/groups", json={"name": "研发 组"}).status_code == 409
    assert client.post("/api/admin/groups", json={"name": "x", "color": "red"}).status_code == 400
    assert client.post("/api/admin/groups", json={"name": "x", "role": "admin"}).status_code == 400
    assert client.post("/api/admin/groups", json={"name": "x", "default_sources": ["rss:nope"]}).status_code == 400
    r = client.patch(f"/api/admin/groups/{g['id']}", json={"description": "AI 方向", "quota": 0, "role": "readonly"}).json()
    assert r["description"] == "AI 方向" and r["quota"] is None and r["role"] == "readonly"
    L = client.get("/api/admin/groups").json()
    assert [x["name"] for x in L["groups"]] == ["研发 组"] and {p["key"] for p in L["perms"] if p["admin_only"]} == {"users", "site"}
    register(client, "erin")
    _admin(client)
    client.post("/api/admin/users/bulk", json={"ids": [_uid("erin")], "action": "add_group", "group_id": g["id"]})
    assert client.delete(f"/api/admin/groups/{g['id']}").json() == {"ok": True}
    erin = next(u for u in client.get("/api/admin/people").json()["users"] if u["username"] == "erin")
    assert erin["groups"] == [] and erin["active"]  # members are kept
    assert client.patch("/api/admin/groups/999", json={"name": "z"}).status_code == 404


def test_bulk_actions_and_safety(client):
    _seed_sources()
    register(client, "erin")
    register(client, "finn")
    _admin(client)
    root, erin, finn = _uid("root"), _uid("erin"), _uid("finn")
    g = client.post("/api/admin/groups", json={"name": "设计", "default_sources": ["rss:a", "rss:b"]}).json()
    r = client.post("/api/admin/users/bulk", json={"ids": [erin, finn], "action": "add_group", "group_id": g["id"]}).json()
    assert r["n"] == 2
    people = {u["username"]: u for u in client.get("/api/admin/people").json()["users"]}
    assert people["erin"]["groups"] == [g["id"]] and people["erin"]["subscriptions"] == 2  # default sources subscribed
    assert client.post("/api/admin/users/bulk", json={"ids": [erin], "action": "add_group", "group_id": g["id"]}).json()["n"] == 0
    assert client.post("/api/admin/users/bulk", json={"ids": [finn], "action": "remove_group", "group_id": g["id"]}).json()["n"] == 1
    # self protection and the last admin
    assert client.post("/api/admin/users/bulk", json={"ids": [root], "action": "disable"}).status_code == 400
    assert client.post("/api/admin/users/bulk", json={"ids": [root, erin], "action": "role", "role": "user"}).status_code == 400
    assert client.post("/api/admin/users/bulk", json={"ids": [root], "action": "reset_password"}).status_code == 400
    assert client.post("/api/admin/users/bulk", json={"ids": [erin], "action": "nuke"}).status_code == 400
    assert client.post("/api/admin/users/bulk", json={"ids": [9999], "action": "logout"}).status_code == 404
    assert client.post("/api/admin/users/bulk", json={"ids": [erin], "action": "role", "role": "readonly"}).json()["n"] == 1
    # reset password: a temporary password that works, old sessions are gone
    from fastapi.testclient import TestClient
    other = TestClient(client.app)
    login(other, "finn")
    assert other.get("/api/me").status_code == 200
    pw = client.post("/api/admin/users/bulk", json={"ids": [finn], "action": "reset_password"}).json()["passwords"]["finn"]
    assert len(pw) >= 10 and other.get("/api/me").status_code == 401
    login(other, "finn", pw)
    assert other.get("/api/me").status_code == 200
    # disable signs the user out and keeps them out; enable lets them back
    assert client.post("/api/admin/users/bulk", json={"ids": [finn], "action": "disable"}).json()["n"] == 1
    assert other.get("/api/me").status_code == 401
    assert other.post("/login", data={"username": "finn", "password": pw}, follow_redirects=False).status_code == 401
    client.post("/api/admin/users/bulk", json={"ids": [finn], "action": "enable"})
    login(other, "finn", pw)
    assert client.post("/api/admin/users/bulk", json={"ids": [finn], "action": "logout"}).json()["n"] >= 1
    assert other.get("/api/me").status_code == 401
    d = client.get(f"/api/admin/users/{erin}/detail").json()
    assert d["role"] == "readonly" and d["groups"] == [g["id"]] and "src" not in d["perms"] and "push" not in d["perms"]
    assert any("重置密码" in a["message"] or "加入分组" in a["message"] for a in client.get(f"/api/admin/users/{root}/detail").json()["activity"])
    assert "passwords" not in str(client.get("/api/admin/logs", params={"module": "admin"}).json())  # never logged


def test_group_permissions_are_enforced(client):
    _seed_sources()
    register(client, "erin")
    db = _db()
    subscribe_all(db, "erin")
    db.close()
    # no group: the old default permissions
    assert client.post("/api/me/zones", json={"category": "rss", "name": "新区"}).status_code == 200
    _admin(client)
    erin = _uid("erin")
    g = client.post("/api/admin/groups", json={"name": "只读组", "perms": ["read", "logs"]}).json()
    client.post("/api/admin/users/bulk", json={"ids": [erin], "action": "add_group", "group_id": g["id"]})
    login(client, "erin")
    assert client.get("/api/me/zones").status_code == 200  # reading is fine
    assert client.post("/api/me/zones", json={"category": "rss", "name": "再来"}).status_code == 403
    assert client.put("/api/me/subscriptions", json={"source": "rss:a"}).status_code == 403
    assert client.post("/api/me/llm/providers", json={"provider": "openai", "model": "m"}).status_code == 403
    assert client.put("/api/me/push", json={}).status_code == 403
    assert client.post("/api/me/feedback", json={}).status_code != 403  # unrelated endpoints untouched
    # the logs permission opens a read-only 日志与任务, nothing else of the console
    assert client.get("/admin/logs").status_code == 200 and 'href="/admin/logs"' in client.get("/settings").text
    assert "推送记录" not in client.get("/admin/logs").text and "手动运行" not in client.get("/admin/logs").text
    assert client.get("/api/admin/logs").status_code == 200 and client.get("/api/admin/timeline").status_code == 200
    for m, p in (("GET", "/admin"), ("GET", "/admin/users"), ("GET", "/api/admin/people"), ("POST", "/api/admin/runs/trigger"),
                 ("POST", "/api/admin/health/retry"), ("GET", "/api/admin/overview")):
        assert client.request(m, p, json={"key": "rss:a"}).status_code == 403, p
    # src back on through a second group (union)
    _admin(client)
    g2 = client.post("/api/admin/groups", json={"name": "源组", "perms": ["src"]}).json()
    client.post("/api/admin/users/bulk", json={"ids": [erin], "action": "add_group", "group_id": g2["id"]})
    login(client, "erin")
    assert client.post("/api/me/zones", json={"category": "rss", "name": "再来"}).status_code == 200
    # ... but the readonly role drops it again
    _admin(client)
    client.post("/api/admin/users/bulk", json={"ids": [erin], "action": "role", "role": "readonly"})
    login(client, "erin")
    assert client.post("/api/me/zones", json={"category": "rss", "name": "第三"}).status_code == 403
    # a group whose role is 只读 makes its members read-only too, unless another group is more generous
    _admin(client)
    client.post("/api/admin/users/bulk", json={"ids": [erin], "action": "role", "role": "user"})
    client.post("/api/admin/users/bulk", json={"ids": [erin], "action": "remove_group", "group_id": g["id"]})
    client.patch(f"/api/admin/groups/{g2['id']}", json={"role": "readonly"})
    login(client, "erin")
    assert client.post("/api/me/zones", json={"category": "rss", "name": "第四"}).status_code == 403
    _admin(client)
    client.post("/api/admin/users/bulk", json={"ids": [erin], "action": "add_group", "group_id": g["id"]})  # role user, no src
    login(client, "erin")
    assert client.post("/api/me/zones", json={"category": "rss", "name": "第四"}).status_code == 200  # union: g2's src counts


def test_invites_into_a_group(client):
    _seed_sources()
    _admin(client)
    g = client.post("/api/admin/groups", json={"name": "运营", "role": "readonly", "default_sources": ["rss:b"], "quota": 30}).json()
    assert client.post("/api/admin/invites/batch", json={"emails": ["not-an-email"]}).status_code == 400
    assert client.post("/api/admin/invites/batch", json={"emails": []}).status_code == 400
    r = client.post("/api/admin/invites/batch", json={"emails": ["Wang@Example.com", "li@example.com", "wang@example.com"], "group_id": g["id"], "days": 7}).json()
    assert [x["email"] for x in r["invites"]] == ["wang@example.com", "li@example.com"] and r["group"] == "运营"
    pending = client.get("/api/admin/people").json()["invites"]
    assert {p["email"] for p in pending} == {"wang@example.com", "li@example.com"} and all(p["group_id"] == g["id"] for p in pending)
    assert "inv-" not in client.get("/api/admin/people").text  # codes are shown once, never listed
    client.cookies.clear()
    assert client.get("/register", params={"invite": r["invites"][0]["code"]}).text.count(r["invites"][0]["code"]) == 1  # prefilled
    assert client.post("/api/register", data={"username": "wang", "password": "wangpass123", "invite": r["invites"][0]["code"]}).status_code == 200
    _admin(client)
    people = client.get("/api/admin/people").json()
    wang = next(u for u in people["users"] if u["username"] == "wang")
    assert wang["email"] == "wang@example.com" and wang["groups"] == [g["id"]] and wang["role"] == "readonly" and wang["subscriptions"] == 1
    assert [p["email"] for p in people["invites"]] == ["li@example.com"]
    li = people["invites"][0]["id"]
    assert client.delete(f"/api/admin/invites/{li}").status_code == 200 and client.get("/api/admin/people").json()["invites"] == []
    anon = client.post("/api/admin/invites/batch", json={"count": 2}).json()["invites"]
    assert len(anon) == 2 and all(x["email"] is None and x["code"].startswith("inv") for x in anon)


def test_group_quota_caps_the_issue(client):
    db = _db()
    p = Pool(db)
    for i, t in enumerate(["Qwen", "DeepSeek", "Gemma", "Llama", "Mistral", "Phi"]):
        p.item(f"{t} releases a new model number {i}", hours=1 + i)
    db.close()
    register(client, "erin")
    db = _db()
    subscribe_all(db, "erin", interest=["Qwen", "DeepSeek", "Gemma", "Llama", "Mistral", "Phi"])
    db.close()
    build = lambda: (client.post("/api/me/issues/build", json={"key": "2026-10-21"}), client.get("/api/me/issues/2026-10-21").json()["items"])[1]  # noqa: E731
    n0 = len(build())
    assert n0 >= 4
    _admin(client)
    g = client.post("/api/admin/groups", json={"name": "限额", "quota": 2}).json()
    client.post("/api/admin/users/bulk", json={"ids": [_uid("erin")], "action": "add_group", "group_id": g["id"]})
    g2 = client.post("/api/admin/groups", json={"name": "宽松", "quota": 3}).json()
    login(client, "erin")
    assert len(build()) == 2
    _admin(client)
    client.post("/api/admin/users/bulk", json={"ids": [_uid("erin")], "action": "add_group", "group_id": g2["id"]})
    login(client, "erin")
    assert len(build()) == 3  # the most generous group wins


def test_source_health_states_and_retry(client, monkeypatch):
    from newsd import admin_console
    from newsd.models import Source, SourceFetch, now
    db = _db()
    p = Pool(db)
    keys = ("rss:ok", "rss:flaky", "rss:dead", "rss:quiet", "rss:off", "rss:lonely")
    for k in keys:
        p.source(k)
    p.user("sub")
    S = {s.key: s for s in db.scalars(select(Source))}
    S["rss:off"].enabled = False
    db.query(__import__("newsd.models", fromlist=["Subscription"]).Subscription).filter_by(source_id=S["rss:lonely"].id).delete()
    t = now()

    def f(key, i, status, entries=5):
        # minutes apart, not hours: the 7-day history buckets by day, so hour spacing split the fixture across midnight (fails 00:00-03:00 UTC)
        db.add(SourceFetch(source_id=S[key].id, cycle=f"c{i}", status=status, entries=entries, started_at=t - timedelta(minutes=3 - i),
                           finished_at=t - timedelta(minutes=3 - i) + timedelta(seconds=2)))
    for i in range(3):
        f("rss:ok", i, "ok")
        f("rss:quiet", i, "ok", 0)
        f("rss:dead", i, "ok" if i == 0 else "failed:http:403")
        f("rss:flaky", i, "failed:timeout" if i == 2 else "ok")
    db.commit(); db.close()
    _admin(client)
    H = {h["key"]: h for h in client.get("/api/admin/health").json()["sources"]}
    assert H["rss:ok"]["st"] == "ok" and H["rss:ok"]["lat"] == 2.0 and H["rss:ok"]["hist"][-1] == ""
    assert H["rss:dead"]["st"] == "err" and "连续 2 次" in H["rss:dead"]["note"] and "http:403" in H["rss:dead"]["note"]
    assert H["rss:flaky"]["st"] == "warn" and H["rss:flaky"]["hist"][-1] == "w"
    assert H["rss:quiet"]["st"] == "warn" and "没有条目" in H["rss:quiet"]["note"]
    assert H["rss:off"]["st"] == "off" and H["rss:lonely"]["note"] == "无人订阅，不抓取" and H["rss:lonely"]["users"] == 0
    calls = []

    def fake(keys, cycle):
        calls.append((keys, cycle))
        db = _db()
        db.add(SourceFetch(source_id=db.scalar(select(Source.id).where(Source.key == "rss:dead")), cycle=cycle, status="ok", entries=3,
                           started_at=now(), finished_at=now()))
        db.commit(); db.close()
        return {"sources": {"rss:dead": "ok"}, "new_items": 3}
    monkeypatch.setattr(admin_console, "fetch_now", fake)
    r = client.post("/api/admin/health/retry", json={"key": "rss:dead"}).json()
    assert r["status"] == "ok" and r["new_items"] == 3 and r["row"]["st"] == "ok" and calls[0][0] == {"rss:dead"} and calls[0][1].startswith("retry-")
    assert client.post("/api/admin/health/retry", json={"key": "rss:off"}).status_code == 409
    assert client.post("/api/admin/health/retry", json={"key": "rss:lonely"}).status_code == 409
    assert client.post("/api/admin/health/retry", json={"key": "rss:nope"}).status_code == 404
    assert any(e["source"] == "rss:dead" and "手动重试" in e["message"] for e in client.get("/api/admin/logs").json()["items"])


def test_logs_filters_histogram_and_paging(client):
    from newsd.eventlog import add
    from newsd.models import WorkerRun, now
    db = _db()
    run = WorkerRun(trigger="manual", status="ok", started_at=now() - timedelta(minutes=5), finished_at=now(),
                    stages=[{"stage": "collect", "status": "ok", "started_at": (now() - timedelta(minutes=5)).isoformat(),
                             "finished_at": (now() - timedelta(minutes=4)).isoformat(), "counts": {}, "error": None}], users=[{"user": "x", "status": "ok", "seconds": 1.5, "items_out": 7}])
    db.add(run); db.commit()
    for i in range(30):
        add(db, "INFO", "fetch", f"ok #{i}", source="rss:a", run_id=run.id, at=now() - timedelta(minutes=30 - i))
    add(db, "ERROR", "push", "推送失败 · webhook https://hook.example/T0/SECRET", user="alice", detail={"stack": "Trace\nboom"}, at=now() - timedelta(minutes=1))
    add(db, "WARN", "llm", "模型切换 1 次（主用失败后改用备份）", user="alice", run_id=run.id)
    add(db, "INFO", "fetch", "old", at=now() - timedelta(days=3))
    db.commit(); db.close()
    _admin(client)
    j = client.get("/api/admin/logs").json()  # + root's own 登录 event
    assert j["total"] == 33 and j["items"][0]["module"] == "auth" and j["items"][1]["module"] == "llm"
    assert j["levels"] == {"INFO": 31, "ERROR": 1, "WARN": 1}
    assert sum(sum(b.values()) for b in j["hist"]["buckets"]) == 33 and len(j["hist"]["buckets"]) == 48
    assert "SECRET" not in client.get("/api/admin/logs").text  # URL paths are redacted when stored
    assert client.get("/api/admin/logs", params={"level": "ERROR"}).json()["total"] == 1
    assert client.get("/api/admin/logs", params={"q": "level:warn"}).json()["total"] == 1
    assert client.get("/api/admin/logs", params={"q": "user:alice mod:push"}).json()["items"][0]["detail"]["stack"] == "Trace\nboom"
    assert client.get("/api/admin/logs", params={"q": f"run:{run.id} #2"}).json()["total"] == 11  # #2, #20..#29
    assert client.get("/api/admin/logs", params={"q": "src:rss"}).json()["total"] == 30
    assert client.get("/api/admin/logs", params={"source": "rss:a", "limit": 10}).json()["more"] is True
    page1 = client.get("/api/admin/logs", params={"source": "rss:a", "limit": 10}).json()["items"]
    page2 = client.get("/api/admin/logs", params={"source": "rss:a", "limit": 10, "before": page1[-1]["id"]}).json()["items"]
    assert page2[0]["id"] < page1[-1]["id"] and len(page2) == 10
    assert client.get("/api/admin/logs", params={"since": (now() - timedelta(days=4)).isoformat()}).json()["total"] == 34
    assert client.get("/api/admin/logs", params={"q": "100%_"}).json()["total"] == 0  # LIKE wildcards are literal
    assert client.get("/api/admin/logs", params={"run": "abc"}).status_code == 400
    assert client.get("/api/admin/logs", params={"since": "2026-10-21T10:00", "until": "2026-10-21T09:00"}).status_code == 400
    one = client.get(f"/api/admin/logs/{page1[0]['id']}").json()
    assert one["run"] == run.id and 1 < len(one["context"]) <= 11
    f = client.get("/api/admin/logs").json()["facets"]
    assert f["sources"] == ["rss:a"] and f["users"] == ["alice", "root"] and f["runs"][0]["id"] == run.id
    tl = client.get("/api/admin/timeline").json()
    assert tl["runs"][0]["id"] == run.id and tl["runs"][0]["users"][0]["items_out"] == 7 and tl["runs"][0]["events"]["INFO"] == 30


def test_login_activity_overview_and_manual_run(client, monkeypatch):
    from newsd import admin_console
    from newsd.models import WorkerRun, now
    register(client, "erin")
    client.get("/api/me")
    client.cookies.clear()
    assert client.post("/login", data={"username": "erin", "password": "wrong-password"}, follow_redirects=False).status_code == 401
    _admin(client)
    ev = client.get("/api/admin/logs", params={"module": "auth", "level": "INFO,WARN"}).json()["items"]
    assert {(e["level"], e["user"], e["message"]) for e in ev} >= {("INFO", "erin", "登录"), ("WARN", "erin", "登录失败"), ("INFO", "root", "登录")}
    assert any(e["message"].startswith("注册") for e in ev)
    ov = client.get("/api/admin/overview").json()
    assert ov["users"] == 2 and ov["active_7d"] == 2 and ov["trend"][-1]["n"] == 2 and len(ov["trend"]) == 14
    assert ov["new_7d"] == 2 and ov["ungrouped"] == 2 and ov["health"] == {"ok": 0, "warn": 0, "err": 0, "off": 0}
    people = {u["username"]: u for u in client.get("/api/admin/people").json()["users"]}
    assert people["erin"]["last_seen_at"] is not None
    ran = []
    monkeypatch.setattr(admin_console, "run_now", lambda: ran.append(1))
    assert client.post("/api/admin/runs/trigger").json() == {"started": True}
    admin_console._bg["run"].join(5)
    assert ran == [1]
    db = _db()
    db.add(WorkerRun(trigger="schedule", status="running", started_at=now()))
    db.commit(); db.close()
    assert client.post("/api/admin/runs/trigger").status_code == 409
    ov = client.get("/api/admin/overview").json()
    assert ov["runs_today"] == 1


def test_admin_pages_render(client):
    _seed_sources()
    register(client, "erin")
    for p in ("/admin", "/admin/users", "/admin/health", "/admin/logs"):
        assert client.get(p).status_code == 403, p
    client.cookies.clear()
    for p in ("/admin", "/admin/users", "/admin/health", "/admin/logs"):
        assert client.get(p, follow_redirects=False).status_code == 303, p
    _admin(client)
    client.post("/api/admin/groups", json={"name": "<b>组</b>", "description": "<script>x</script>"})
    pages = {p: client.get(p) for p in ("/admin", "/admin/users", "/admin/health", "/admin/logs")}
    assert all(r.status_code == 200 for r in pages.values())
    assert "管理概览" in pages["/admin"].text and "需要处理" in pages["/admin"].text and "<b>组</b>" not in pages["/admin"].text
    assert "window.ND_USERS=" in pages["/admin/users"].text and "<script>x</script>" not in pages["/admin/users"].text
    assert "/static/v2/admin-users.js" in pages["/admin/users"].text and "erin" in pages["/admin/users"].text
    assert "window.ND_HEALTH=" in pages["/admin/health"].text and "rss:a" in pages["/admin/health"].text
    assert "任务运行时间线" in pages["/admin/logs"].text and "手动运行" in pages["/admin/logs"].text and "推送记录" in pages["/admin/logs"].text
    nav = pages["/admin"].text
    for h in ("/admin/users", "/admin/health", "/admin/logs"):
        assert f'href="{h}"' in nav
    for f in ("admin-users.js", "admin-health.js", "admin-logs.js"):
        assert client.get(f"/static/v2/{f}").status_code == 200


def test_migration_0012_round_trip_keeps_data(client):
    from alembic import command
    from sqlalchemy import inspect, text

    from newsd.db import alembic_config, get_engine
    _seed_sources()
    register(client, "erin")
    db = _db()
    subscribe_all(db, "erin")
    db.close()
    _admin(client)
    g = client.post("/api/admin/groups", json={"name": "组"}).json()
    client.post("/api/admin/users/bulk", json={"ids": [_uid("erin")], "action": "add_group", "group_id": g["id"]})
    client.post("/api/admin/invites/batch", json={"emails": ["a@b.co"], "group_id": g["id"]})
    eng = get_engine()

    def counts():
        with eng.connect() as c:
            return {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in ("users", "subscriptions", "sessions", "invites", "source_zones")}
    before = counts()
    command.downgrade(alembic_config(), "0011_216_zones")
    eng = get_engine()
    names = inspect(eng).get_table_names()
    assert not {"user_groups", "user_group_members", "user_activity", "log_events"} & set(names)
    assert "email" not in {c["name"] for c in inspect(eng).get_columns("users")}
    assert "group_id" not in {c["name"] for c in inspect(eng).get_columns("invites")}
    assert counts() == before  # dropping columns never rebuilt users (that would cascade-delete everything)
    command.upgrade(alembic_config(), "head")
    assert counts() == before
