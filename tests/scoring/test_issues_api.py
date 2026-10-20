from sqlalchemy import select


def _login(client, name):
    from newsd.cli import main
    main(["admin", "registration", "open"])
    client.cookies.clear()
    client.post("/api/register", data={"username": name, "password": name + "pass123"})
    assert client.post("/login", data={"username": name, "password": name + "pass123"}, follow_redirects=False).status_code == 303


def test_build_and_read_issue_per_user(client):
    from newsd.db import SessionLocal
    from newsd.models import Keyword, Subscription, User
    from poolseed import Pool

    _login(client, "amy")
    _login(client, "ben")
    db = SessionLocal()
    p = Pool(db)
    q = p.item("Qwen 3 released", summary="new model")
    s = p.item("Sora gets sound", summary="video")
    for name, term in (("amy", "Qwen"), ("ben", "Sora")):
        u = db.scalar(select(User).where(User.username == name))
        for src in p.sources.values():
            db.add(Subscription(user_id=u.id, source_id=src.id))
        db.add(Keyword(user_id=u.id, kind="interest", term=term))
    db.commit(); db.close()

    _login(client, "amy")
    r = client.post("/api/me/issues/build", json={"key": "2026-10-20", "ai": True})  # no key stored: rules only
    assert r.status_code == 200 and r.json()["ai"] == "skipped: no model key" and r.json()["items_out"] == 1
    view = client.get("/api/me/issues/latest").json()
    assert [i["item_id"] for i in view["items"]] == [q.id] and view["items"][0]["matched"] == ["Qwen"]
    assert view["items"][0]["url"].startswith("https://") and view["items"][0]["sources"] == ["rss:feed"]
    assert client.get("/api/me/issues").json()[0]["key"] == "2026-10-20"
    ex = client.get("/api/me/issues/2026-10-20?explain=1").json()
    assert ex["dropped_items"] == [{"item_id": s.id, "title": "Sora gets sound", "reason": "no-keyword-match"}]
    assert client.post("/api/me/issues/build", json={"key": "bad key!"}).status_code == 422

    _login(client, "ben")
    assert client.get("/api/me/issues/2026-10-20").status_code == 404  # amy's issue is not visible
    client.post("/api/me/issues/build", json={"key": "2026-10-20"})
    assert [i["item_id"] for i in client.get("/api/me/issues/2026-10-20").json()["items"]] == [s.id]
    client.cookies.clear()
    assert client.get("/api/me/issues").status_code == 401


def test_cli_build_all(client, capsys):
    from newsd.cli import main
    assert main(["issue", "build", "--all", "--key", "k1", "--no-ai"]) == 0
    out = capsys.readouterr().out
    assert '"user": "root"' in out
    assert main(["issue", "show", "root", "--key", "k1"]) == 0
