"""M3 acceptance: two logged-in users each see their own daily report, favorites, trash, calendar and settings,
and nothing one of them does shows up for the other."""
from pagekit import login, register, subscribe_all
from poolseed import Pool


def _setup(client):
    """alice follows Qwen, bob follows Sora; both read the same shared pool."""
    from newsd.db import SessionLocal
    register(client, "alice")
    register(client, "bob")
    db = SessionLocal()
    p = Pool(db)
    qwen = p.item("Qwen 3 released today", summary="a new open model")
    sora = p.item("Sora gets sound", summary="video generation with audio")
    both = p.item("Qwen and Sora compared", summary="both", url="https://news.example/both")
    subscribe_all(db, "alice", interest=["Qwen"])
    subscribe_all(db, "bob", interest=["Sora"])
    db.close()
    for name in ("alice", "bob"):
        login(client, name)
        assert client.post("/api/me/issues/build", json={"key": "2026-10-20"}).status_code == 200
    return qwen, sora, both


def test_each_user_gets_their_own_daily_report(client):
    qwen, sora, both = _setup(client)
    login(client, "alice")
    page = client.get("/")
    assert page.status_code == 200
    assert "Qwen 3 released today" in page.text and "Qwen and Sora compared" in page.text
    assert "Sora gets sound" not in page.text
    assert "alice" in page.text and "bob" not in page.text
    login(client, "bob")
    page = client.get("/")
    assert "Sora gets sound" in page.text and "Qwen and Sora compared" in page.text
    assert "Qwen 3 released today" not in page.text
    # the dated URL is also scoped to the viewer
    assert client.get("/issue/2026-10-20").status_code == 200
    assert client.get("/issue/2026-01-01").status_code == 404


def test_settings_pages_are_independent(client):
    _setup(client)
    login(client, "alice")
    assert client.post("/api/me/keywords", json={"kind": "block", "term": "alice-secret-block"}).status_code == 200
    assert client.put("/api/me/quotas", json=[{"max_items": 11}, {"module": "news", "max_items": 4}]).status_code == 200
    assert client.put("/api/me/push", json={"enabled": True, "time": "07:30", "timezone": "Asia/Shanghai",
                                           "channels": {"email": {"enabled": True, "target": "alice@example.com"}}}).status_code == 200
    assert client.put("/api/me/theme", json={"palette": "arch-crt"}).json() == {"palette": "arch-crt"}
    assert client.put("/api/me/llm", json={"provider": "deepseek", "model": "alice-model", "api_key": "sk-alice-12345678"}).status_code == 200
    a = client.get("/settings")
    assert a.status_code == 200
    assert "alice-secret-block" in a.text and "alice@example.com" in a.text and "07:30" in a.text
    assert 'value="11"' in a.text and 'data-theme="arch-crt"' in a.text and "alice-model" in a.text
    assert "sk-alice-12345678" not in a.text and "5678" in a.text  # only the last 4 digits ever reach the page

    login(client, "bob")
    b = client.get("/settings")
    assert b.status_code == 200
    assert "alice-secret-block" not in b.text and "alice@example.com" not in b.text and "alice-model" not in b.text
    assert 'data-theme="arch-editorial"' in b.text and 'value="11"' not in b.text
    assert any(k["term"] == "Sora" for k in client.get("/api/me/keywords").json())
    assert client.get("/api/me/quotas").json() == [] and client.get("/api/me/push").json()["enabled"] is False
    # bob editing his own settings leaves alice's alone
    client.put("/api/me/theme", json={"palette": "arch-swiss"})
    client.put("/api/me/quotas", json=[{"max_items": 3}])
    client.put("/api/me/push", json={"enabled": True, "time": "21:00", "timezone": "UTC", "channels": {}})
    login(client, "alice")
    assert client.get("/api/me/theme").json()["palette"] == "arch-crt"
    assert client.get("/api/me/push").json()["time"] == "07:30"
    assert sorted(q["max_items"] for q in client.get("/api/me/quotas").json()) == [4, 11]
    assert 'data-theme="arch-crt"' in client.get("/").text


def test_favorites_and_trash_are_per_user(client):
    qwen, sora, both = _setup(client)
    login(client, "alice")
    assert client.put(f"/api/me/items/{qwen.id}/mark", json={"state": "favorite"}).json()["state"] == "favorite"
    assert client.put(f"/api/me/items/{both.id}/mark", json={"state": "trash"}).status_code == 200
    # alice can't touch an item that is not in her issues
    assert client.put(f"/api/me/items/{sora.id}/mark", json={"state": "favorite"}).status_code == 404
    assert client.put(f"/api/me/items/{qwen.id}/mark", json={"state": "bogus"}).status_code == 400
    home = client.get("/").text
    assert "Qwen 3 released today" in home and 'aria-pressed="true" title="取消收藏"' in home
    assert "Qwen and Sora compared" not in home and "本期删了 1 条" in home and 'data-trash="1"' in home
    assert "Qwen 3 released today" in client.get("/favorites").text
    assert "Qwen and Sora compared" in client.get("/trash").text
    states = {i["item_id"]: i["state"] for i in client.get("/api/me/issues/latest").json()["items"]}
    assert states == {qwen.id: "favorite", both.id: "trash"}

    login(client, "bob")
    assert "Qwen 3 released today" not in client.get("/favorites").text
    assert "Qwen and Sora compared" not in client.get("/trash").text
    assert "Qwen and Sora compared" in client.get("/").text  # alice's trash does not hide it from bob
    assert client.get("/api/me/marks", params={"state": "favorite"}).json() == []
    assert client.put(f"/api/me/items/{qwen.id}/mark", json={"state": "trash"}).status_code == 404

    login(client, "alice")
    assert client.put(f"/api/me/items/{both.id}/mark", json={"state": None}).json()["state"] is None  # restore
    assert "Qwen and Sora compared" in client.get("/").text
    assert client.delete("/api/me/marks", params={"state": "favorite"}).json() == {"cleared": 1}
    assert "Qwen 3 released today" not in client.get("/favorites").text


def test_calendar_lists_only_own_issues(client):
    _setup(client)
    login(client, "alice")
    client.post("/api/me/issues/build", json={"key": "2026-10-21"})
    login(client, "bob")
    client.post("/api/me/issues/build", json={"key": "weekly-special"})
    cal = client.get("/calendar", params={"month": "2026-10"}).text
    assert 'href="/issue/2026-10-20"' in cal and 'href="/issue/2026-10-21"' not in cal and "weekly-special" in cal
    login(client, "alice")
    cal = client.get("/calendar", params={"month": "2026-10"}).text
    assert 'href="/issue/2026-10-21"' in cal and "weekly-special" not in cal
    assert client.get("/calendar", params={"month": "garbage"}).status_code == 200  # falls back to this month
    assert 'href="/calendar?month=2026-09"' in client.get("/calendar", params={"month": "2026-10"}).text
    assert 'href="/calendar?month=2027-01"' in client.get("/calendar", params={"month": "2026-12"}).text


def test_pages_need_login(client):
    for path in ("/", "/issue/2026-10-20", "/calendar", "/favorites", "/trash", "/settings", "/welcome", "/admin"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    for path in ("/api/me/theme", "/api/me/push", "/api/me/marks?state=trash", "/api/onboarding", "/api/admin/sources"):
        assert client.get(path).status_code == 401, path


def test_only_http_links_become_hrefs(client):
    from newsd.db import SessionLocal
    register(client, "carol")
    db = SessionLocal()
    p = Pool(db)
    p.item("Evil link <script>alert(1)</script>", url="javascript:alert(1)")
    subscribe_all(db, "carol")
    db.close()
    client.post("/api/me/issues/build", json={"key": "k"})
    html = client.get("/").text
    assert "javascript:" not in html and "<script>alert(1)" not in html and "&lt;script&gt;alert(1)" in html


def test_theme_and_push_validation(client):
    register(client, "dave")
    assert client.put("/api/me/theme", json={"palette": "neon"}).status_code == 400
    assert client.get("/api/me/theme").json()["palette"] == "arch-editorial"
    ok = {"enabled": True, "time": "08:00", "timezone": "UTC", "channels": {}}
    assert client.put("/api/me/push", json={**ok, "time": "25:00"}).status_code == 400
    assert client.put("/api/me/push", json={**ok, "timezone": "Mars/Base"}).status_code == 400
    assert client.put("/api/me/push", json={**ok, "channels": {"sms": {"enabled": True, "target": "1"}}}).status_code == 400
    assert client.put("/api/me/push", json={**ok, "channels": {"email": {"enabled": True, "target": "nope"}}}).status_code == 400
    assert client.put("/api/me/push", json={**ok, "channels": {"webhook": {"enabled": True, "target": "ftp://x"}}}).status_code == 400
    assert client.put("/api/me/push", json={**ok, "channels": {"telegram": {"enabled": True, "target": "@mychannel"}}}).status_code == 400  # needs a bot token
    tg = {"enabled": True, "target": "@mychannel", "token": "123456:ABCDEFGHIJKLMNOPQRSTUV"}
    assert client.put("/api/me/push", json={**ok, "channels": {"telegram": tg}}).status_code == 200
    got = client.get("/api/me/push").json()
    assert got["channels"]["telegram"]["enabled"] is True and got["channels"]["telegram"]["target"] == "@mychannel"
    assert got["channels"]["telegram"]["has_token"] and "ABCDEFGH" not in str(got) and got["channels"]["email"]["enabled"] is False
    # enabling a channel needs a destination; a disabled, empty one is fine and clears it
    assert client.put("/api/me/push", json={**ok, "channels": {"telegram": {"enabled": True, "target": ""}}}).status_code == 400
    got = client.put("/api/me/push", json={**ok, "channels": {"telegram": {"enabled": False, "target": ""}}}).json()
    assert got["channels"]["telegram"]["enabled"] is False and got["channels"]["telegram"]["target"] == ""
    # the login page follows the last used palette via a cookie
    client.put("/api/me/theme", json={"palette": "taste-moat"})
    assert 'data-theme="taste-moat"' in client.get("/login").text
    # v1 palette names still work and map onto the closest v2 theme
    assert client.put("/api/me/theme", json={"palette": "amber"}).json() == {"palette": "now-playing"}
    assert 'data-theme="now-playing"' in client.get("/login").text
