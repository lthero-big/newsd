"""DEPTAI-267: X source fed by Grok Bot — admin switch, per-user 「X 关注」, import API, dedupe, daily cap."""


def login(c, u, p):
    return c.post("/login", data={"username": u, "password": p}, follow_redirects=False)


def _user(client, name):
    from newsd.cli import main
    main(["admin", "registration", "open"])
    client.cookies.clear()
    uid = client.post("/api/register", data={"username": name, "password": name + "pass123"}).json()["id"]
    assert login(client, name, name + "pass123").status_code == 303
    return uid


def _root(client):
    client.cookies.clear()
    assert login(client, "root", "rootpass123").status_code == 303


def _bot_token(client):
    _root(client)
    tok = client.post("/api/tokens", data={"kind": "api", "name": "grok-bot"}).json()["token"]
    client.cookies.clear()
    return {"Authorization": f"Bearer {tok}"}


POST = {"url": "https://x.com/karpathy/status/1850000000000000001", "text": "A long enough post about a new open weights model release and its benchmark numbers.",
        "author": "karpathy", "created_at": "2026-10-29T08:00:00Z", "likes": 1200,
        "media": [{"url": "https://pbs.twimg.com/media/abc.jpg", "width": 1200, "height": 675}]}


def test_off_by_default_feature_invisible(client):
    _user(client, "alice")
    assert client.get("/api/me/x").status_code == 404
    assert client.post("/api/me/x", json={"value": "@karpathy"}).status_code == 404
    assert 'id="xFollow"' not in client.get("/sources").text
    h = _bot_token(client)
    assert client.get("/api/admin/x-import/wanted", headers=h).json() == {"enabled": False}
    assert client.post("/api/admin/x-import/items", headers=h, json={"source": "xa:karpathy", "items": [POST]}).status_code == 409


def test_admin_only(client):
    _user(client, "bob")
    assert client.get("/api/admin/x-import").status_code == 403
    assert client.put("/api/admin/x-import", json={"enabled": True}).status_code == 403
    assert client.get("/api/admin/x-import/wanted").status_code == 403


def test_scope_users_and_limit(client):
    alice = _user(client, "alice")
    _user(client, "carol")
    _root(client)
    r = client.put("/api/admin/x-import", json={"enabled": True, "scope": "users", "users": [alice], "max_per_user": 2, "daily_cap": 50})
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert client.put("/api/admin/x-import", json={"enabled": True, "scope": "users", "users": [9999]}).status_code == 400
    client.cookies.clear(); login(client, "carol", "carolpass123")
    assert client.get("/api/me/x").status_code == 404  # out of scope: not shown
    client.cookies.clear(); login(client, "alice", "alicepass123")
    assert 'id="xFollow"' in client.get("/sources").text
    assert client.post("/api/me/x", json={"value": "@Karpathy"}).status_code == 200
    j = client.post("/api/me/x", json={"value": "nano banana prompt"}).json()
    assert [f["kind"] for f in j["follows"]] == ["x_account", "x_query"]
    assert client.post("/api/me/x", json={"value": "@karpathy"}).status_code == 200  # already followed: no new slot
    r = client.post("/api/me/x", json={"value": "@sama"})
    assert r.status_code == 400  # over max_per_user
    # placed into the X category of the zone view
    zones = client.get("/api/me/zones").json()
    xcat = next(c for c in zones["cats"] if c["key"] == "x")
    assert {s["key"] for z in xcat["zones"] for s in z["sources"]} >= {"xa:Karpathy", j["follows"][1]["key"]}
    key = j["follows"][1]["key"]
    assert len(client.delete("/api/me/x/" + key).json()["follows"]) == 1


def test_import_dedupe_and_distribution(client):
    alice = _user(client, "alice")
    assert client.post("/api/me/x", json={"value": "@karpathy"}).status_code == 404
    _root(client)
    client.put("/api/admin/x-import", json={"enabled": True, "scope": "all", "max_per_user": 5, "daily_cap": 3})
    client.cookies.clear(); login(client, "alice", "alicepass123")
    assert client.post("/api/me/x", json={"value": "@karpathy"}).status_code == 200
    h = _bot_token(client)
    w = client.get("/api/admin/x-import/wanted", headers=h).json()
    assert w["enabled"] and w["remaining"] == 3
    assert w["sources"] == [{"source": "xa:karpathy", "kind": "account", "handle": "karpathy", "subscribers": 1}]
    bad = {"url": "https://example.com/not-x", "text": "nope"}
    r = client.post("/api/admin/x-import/items", headers=h, json={"source": "xa:karpathy", "items": [POST, bad]}).json()
    assert (r["new"], r["seen"], r["rejected"], r["remaining"]) == (1, 0, 1, 2)
    # same post again (twitter.com form, trailing query) -> no new row
    again = {**POST, "url": POST["url"].replace("x.com", "twitter.com") + "?s=20"}
    r = client.post("/api/admin/x-import/items", headers=h, json={"source": "xa:karpathy", "items": [again]}).json()
    assert (r["new"], r["seen"]) == (0, 1)
    from newsd.db import SessionLocal
    from newsd.models import PoolItem, User
    from newsd.pool.collector import user_items
    db = SessionLocal()
    try:
        items = db.query(PoolItem).all()
        assert len(items) == 1 and items[0].extra["transport"] == "grokbot" and items[0].image
        assert items[0].extra["points"] == 1200  # scoring reads likes as engagement
        assert [i.id for i in user_items(db, alice)] == [items[0].id]
        root = db.query(User).filter_by(username="root").one()
        assert user_items(db, root.id) == []  # not subscribed -> not distributed
    finally:
        db.close()
    # daily cap: 2 used, 1 left; a 2-call batch is refused and flagged
    r = client.post("/api/admin/x-import/items", headers=h, json={"source": "xa:karpathy", "calls": 2, "items": [POST]})
    assert r.status_code == 429
    _root(client)
    adm = client.get("/api/admin/x-import").json()
    assert adm["usage"]["calls"] == 2 and adm["usage"]["exhausted_at"]
    assert client.post("/api/admin/x-import/items", headers=h, json={"source": "rss:nope", "items": []}).status_code == 404
