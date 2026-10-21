from newsd.db import SessionLocal
from newsd.models import WorkerRun


def _login(c, name="root", pw="rootpass123"):
    c.cookies.clear()
    assert c.post("/login", data={"username": name, "password": pw}, follow_redirects=False).status_code == 303


def _seed():
    db = SessionLocal()
    db.add(WorkerRun(trigger="schedule", cycle="2026-10-21T07:00", issue_key="2026-10-21", status="partial",
                     stages=[{"stage": "collect", "status": "partial", "counts": {"new_items": 4}, "error": None}],
                     users=[{"user": "alice", "status": "ok", "ai": "on", "items_in": 5, "items_out": 3},
                            {"user": "<b>evil</b>", "status": "failed", "error": "boom <script>"}],
                     errors=["source rss:bad: failed:http-500"]))
    db.commit(); db.close()


def test_admin_can_list_and_view_runs(client):
    _seed()
    _login(client)
    rows = client.get("/api/admin/worker-runs").json()
    assert len(rows) == 1 and rows[0]["status"] == "partial" and rows[0]["users_ok"] == 1 and rows[0]["users_failed"] == 1
    assert "users" not in rows[0]
    full = client.get(f"/api/admin/worker-runs/{rows[0]['id']}").json()
    assert full["stages"][0]["counts"] == {"new_items": 4} and full["errors"] == ["source rss:bad: failed:http-500"]
    assert client.get("/api/admin/worker-runs/999").status_code == 404
    page = client.get("/admin")
    assert page.status_code == 200 and "2026-10-21" in page.text and "new_items=4" in page.text
    assert "boom <script>" not in page.text and "boom &lt;script&gt;" in page.text and "&lt;b&gt;evil" in page.text  # escaped


def test_runs_are_admin_only(client):
    _seed()
    assert client.get("/api/admin/worker-runs").status_code == 401
    assert client.get("/admin", follow_redirects=False).status_code == 303
    client.post("/api/register", data={"username": "amy", "password": "amypass123", "invite": "x"})
    from newsd.cli import main
    main(["admin", "registration", "open"])
    client.post("/api/register", data={"username": "amy", "password": "amypass123"})
    _login(client, "amy", "amypass123")
    assert client.get("/api/admin/worker-runs").status_code == 403
    assert client.get("/api/admin/worker-runs/1").status_code == 403
    assert client.get("/admin").status_code == 403
