"""Admin backend (users, invites, registration mode, source catalog with quality stats, run records) and onboarding."""
from pagekit import login, register, subscribe_all
from poolseed import Pool


def _admin(client):
    return login(client, "root", "rootpass123")


def test_admin_pages_and_api_are_admin_only(client):
    register(client, "erin")
    assert client.get("/admin").status_code == 403
    for path in ("/api/admin/sources", "/api/admin/users", "/api/admin/invites", "/api/admin/runs", "/api/admin/registration"):
        assert client.get(path).status_code == 403, path
    assert client.patch("/api/admin/sources/rss:x", json={"enabled": False}).status_code == 403
    page = client.get("/settings").text
    assert 'href="/admin"' not in page and "管理</div>" not in page  # no 管理 group in the console nav or the avatar menu
    _admin(client)
    r = client.get("/admin")
    assert r.status_code == 200 and "管理概览" in r.text and "erin" not in r.text  # 3c: the overview has numbers only
    r = client.get("/admin/users")
    assert r.status_code == 200 and "erin" in r.text and 'aria-current="page"' in r.text


def test_admin_users_invites_registration(client):
    register(client, "frank")
    _admin(client)
    users = {u["username"]: u for u in client.get("/api/admin/users").json()}
    assert users["frank"]["subscriptions"] == 0 and users["root"]["role"] == "admin"
    me = users["root"]["id"]
    assert client.post(f"/api/admin/users/{me}/active", data={"active": "false"}).status_code == 400  # no self lock-out
    fid = users["frank"]["id"]
    assert client.post(f"/api/admin/users/{fid}/active", data={"active": "false"}).json()["active"] is False
    assert login_status(client, "frank") == 401
    _admin(client)
    client.post(f"/api/admin/users/{fid}/active", data={"active": "true"})
    assert login_status(client, "frank") == 303

    _admin(client)
    code = client.post("/api/admin/invites").json()["invite"]
    inv = client.get("/api/admin/invites").json()
    assert [i["status"] for i in inv] == ["unused"] and code not in str(inv)
    client.cookies.clear()
    client.post("/api/admin/registration", data={"mode": "invite"})  # unauthenticated: ignored
    _admin(client)
    assert client.post("/api/admin/registration", data={"mode": "invite"}).json() == {"mode": "invite"}
    client.cookies.clear()
    assert client.post("/api/register", data={"username": "gina", "password": "ginapass123", "invite": code}).status_code == 200
    _admin(client)
    inv = client.get("/api/admin/invites").json()
    assert inv[0]["status"] == "used" and inv[0]["used_by"] == "gina"
    assert client.delete(f"/api/admin/invites/{inv[0]['id']}").status_code == 409
    client.post("/api/admin/invites")
    spare = client.get("/api/admin/invites").json()[0]
    assert spare["status"] == "unused" and client.delete(f"/api/admin/invites/{spare['id']}").status_code == 200
    assert client.post("/api/admin/registration", data={"mode": "closed"}).status_code == 200
    assert client.get("/api/admin/registration").json()["mode"] == "closed"
    assert "关闭注册" in client.get("/admin/users").text


def login_status(client, name):
    client.cookies.clear()
    return client.post("/login", data={"username": name, "password": name + "pass123"}, follow_redirects=False).status_code


def test_source_catalog_quality_and_runs(client):
    from newsd.db import SessionLocal
    from newsd.models import SourceFetch
    register(client, "hank")
    db = SessionLocal()
    p = Pool(db)
    # "good" carries 2 matching items (both selected), "noisy" carries 4 items of which one matches
    p.item("Qwen 3 released", source="rss:good"); p.item("Llama agents", source="rss:good")
    p.item("Mistral tips", source="rss:noisy")
    for i in range(3):
        p.item(f"Random gossip {i}", source="rss:noisy")
    p.item("Unsubscribed noise", source="rss:silent")
    subscribe_all(db, "hank", interest=["Qwen", "Llama", "Mistral"])
    from newsd.models import Subscription
    from sqlalchemy import delete
    silent = p.sources["rss:silent"]
    db.execute(delete(Subscription).where(Subscription.source_id == silent.id))
    db.add(SourceFetch(source_id=p.sources["rss:good"].id, cycle="2026-10-20T09:00", status="ok", entries=2, new_items=2))
    db.add(SourceFetch(source_id=silent.id, cycle="2026-10-20T09:00", status="failed:timeout"))
    db.commit(); db.close()
    login(client, "hank")
    assert client.post("/api/me/issues/build", json={"key": "2026-10-20"}).json()["items_out"] == 3

    _admin(client)
    rows = {s["key"]: s for s in client.get("/api/admin/sources").json()}
    assert (rows["rss:good"]["collected"], rows["rss:good"]["selected"], rows["rss:good"]["pass_rate"]) == (2, 2, 1.0)
    assert (rows["rss:noisy"]["collected"], rows["rss:noisy"]["selected"], rows["rss:noisy"]["pass_rate"]) == (4, 1, 0.25)
    assert rows["rss:good"]["avg_score"] is not None and rows["rss:good"]["avg_score"] > 0
    assert rows["rss:silent"]["selected"] == 0 and rows["rss:silent"]["pass_rate"] == 0.0 and rows["rss:silent"]["subscribers"] == 0
    assert rows["rss:good"]["subscribers"] == 1 and rows["rss:good"]["last_fetch"]["status"] == "ok"
    assert rows["rss:silent"]["last_fetch"]["status"] == "failed:timeout"
    page = client.get("/admin/health").text  # 3c: source quality lives on 源健康 (rendered from this JSON)
    assert '"pass_rate": 1.0' in page and '"pass_rate": 0.25' in page and "failed:timeout" in page

    runs = client.get("/api/admin/runs").json()
    assert {f["status"] for f in runs["fetches"]} == {"ok", "failed:timeout"}
    assert runs["issues"][0]["user"] == "hank" and runs["issues"][0]["items_out"] == 3 and runs["issues"][0]["items_in"] == 6

    # catalog management: disable hides it from users, add a feed
    assert client.patch("/api/admin/sources/rss:noisy", json={"enabled": False}).json()["enabled"] is False
    assert client.patch("/api/admin/sources/rss:nope", json={"enabled": False}).status_code == 404
    login(client, "hank")
    assert "rss:noisy" not in {s["key"] for s in client.get("/api/sources").json()}
    _admin(client)
    assert client.post("/api/admin/sources", json={"id": "new-feed", "name": "New", "url": "https://x.example/feed.xml"}).json() == {"key": "rss:new-feed"}
    assert client.post("/api/admin/sources", json={"id": "new-feed", "name": "New", "url": "https://x.example/feed.xml"}).status_code == 409
    assert client.post("/api/admin/sources", json={"id": "bad", "name": "Bad", "url": "file:///etc/passwd"}).status_code == 400


def test_onboarding_one_click_presets(client):
    from newsd.db import SessionLocal
    from newsd.catalog import import_config
    db = SessionLocal(); import_config(db); db.close()
    register(client, "iris")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/welcome"
    w = client.get("/welcome")
    assert w.status_code == 200 and "AI 资讯" in w.text and "开源与发布" in w.text
    info = client.get("/api/onboarding").json()
    assert info["done"] is False and {g["name"] for g in info["groups"]} >= {"ai-news", "research", "open-source"}
    assert client.post("/api/onboarding", json={"interests": ["nope"]}).status_code == 400
    assert client.get("/api/me/subscriptions").json() == []  # nothing half-applied
    out = client.post("/api/onboarding", json={"interests": ["open-source", "creative-ai"]}).json()
    subs = {s["source"] for s in client.get("/api/me/subscriptions").json()}
    assert out["added_sources"] == len(subs) and {"rss:ollama-releases", "reddit:StableDiffusion", "plugin:github_search"} <= subs
    assert "rsshub:rsshub-qbitai" not in subs
    assert [p["name"] for p in client.get("/api/me/profiles").json()] == ["open-source", "creative-ai"]
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.post("/api/onboarding", json={"interests": ["open-source"]}).json()["added_sources"] == 0  # idempotent

    # skipping marks onboarding done without subscribing; another user is unaffected
    register(client, "jack")
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.post("/api/onboarding", json={"interests": []}).json()["added_sources"] == 0
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/api/me/subscriptions").json() == []
    login(client, "iris")
    assert len(client.get("/api/me/subscriptions").json()) == len(subs)
