"""DEPTAI-216 item 1: roles. `newsd create-user --role`, `set-role`, and every admin surface is 403 for non-admins."""
import re

import pytest
from fastapi.routing import APIRoute


def login(c, u, p):
    r = c.post("/login", data={"username": u, "password": p}, follow_redirects=False)
    assert r.status_code in (302, 303), r.text


def _role(username):
    from sqlalchemy import select
    from newsd.db import SessionLocal
    from newsd.models import User
    db = SessionLocal()
    try:
        return db.scalar(select(User.role).where(User.username == username))
    finally:
        db.close()


def test_create_user_defaults_to_user(client, capsys):
    from newsd.cli import main
    assert main(["create-user", "alice", "--password", "alicepass123"]) == 0
    assert "user alice created" in capsys.readouterr().out
    assert _role("alice") == "user"


def test_create_user_role_admin_and_create_admin_aliases(client, tmp_path):
    from newsd.cli import main
    assert main(["create-user", "bob", "--role", "admin", "--password", "bobpass1234"]) == 0
    assert _role("bob") == "admin"
    assert main(["create-admin", "carol", "--password", "carolpass12"]) == 0
    assert _role("carol") == "admin"
    pw = tmp_path / "pw"; pw.write_text("davepass123\n")
    assert main(["admin", "create", "dave", "--password-file", str(pw)]) == 0  # legacy command still works
    assert _role("dave") == "admin"
    login(client, "dave", "davepass123")


def test_create_user_rejects_bad_input(client, capsys):
    from newsd.cli import main
    with pytest.raises(SystemExit):
        main(["create-user", "x", "--role", "root", "--password", "xxxxxxxx1"])
    assert main(["create-user", "short", "--password", "abc"]) == 1
    assert main(["create-user", "root", "--password", "rootpass123"]) == 1  # exists
    assert "user exists" in capsys.readouterr().err


def test_set_role_demote_and_last_admin_guard(client, capsys):
    from newsd.cli import main
    main(["create-user", "demo", "--role", "admin", "--password", "demopass123"])
    assert main(["set-role", "demo", "user"]) == 0
    assert _role("demo") == "user"
    assert main(["admin", "set-role", "demo", "admin"]) == 0 and _role("demo") == "admin"
    assert main(["set-role", "demo", "user"]) == 0
    assert main(["set-role", "root", "user"]) == 1  # root is the last admin
    assert "last active admin" in capsys.readouterr().err
    assert _role("root") == "admin"
    assert main(["set-role", "nobody", "user"]) == 1


def api_routes(routes):
    """Every APIRoute, also inside routers that newer FastAPI keeps as lazy `_IncludedRouter` entries
    (routers here are included without an extra prefix, so the inner paths are already complete)."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif getattr(r, "original_router", None) is not None:
            yield from api_routes(r.original_router.routes)


def _admin_routes(app):
    out = []
    for r in api_routes(app.routes):
        if (r.path == "/admin" or r.path.startswith("/api/admin/")):
            for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
                out.append((m, re.sub(r"\{[^}:]+(:path)?\}", "1", r.path)))
    return out


def test_every_admin_route_is_403_for_users(client):
    from newsd.cli import main
    main(["create-user", "demo", "--password", "demopass123"])
    routes = _admin_routes(client.app)
    assert len(routes) >= 15, routes  # guard against the enumeration silently matching nothing
    assert ("GET", "/admin") in routes and ("GET", "/api/admin/users") in routes
    login(client, "demo", "demopass123")
    for m, path in routes:
        r = client.request(m, path)
        assert r.status_code == 403, (m, path, r.status_code)
    # the nav never shows the admin link to a plain user
    assert 'href="/admin"' not in client.get("/").text


def test_admin_routes_need_login_and_admin_passes(client):
    routes = _admin_routes(client.app)
    for m, path in routes:
        r = client.request(m, path, follow_redirects=False)
        assert r.status_code in (401, 303, 302, 307), (m, path, r.status_code)
    login(client, "root", "rootpass123")
    for m, path in routes:
        if m == "GET":
            assert client.get(path).status_code in (200, 404), path


def test_every_require_admin_route_lives_under_admin_prefix(client):
    from newsd.auth import require_admin

    def uses_admin(dep):
        return dep.call is require_admin or any(uses_admin(d) for d in dep.dependencies)

    for r in api_routes(client.app.routes):
        under = r.path == "/admin" or r.path.startswith("/api/admin/")
        if uses_admin(r.dependant):
            assert under, r.path
        elif r.path.startswith("/api/admin/"):
            pytest.fail(f"{r.path} is under /api/admin but does not depend on require_admin")


def test_role_api(client):
    from newsd.cli import main
    main(["create-user", "demo", "--role", "admin", "--password", "demopass123"])
    login(client, "root", "rootpass123")
    uid = next(u["id"] for u in client.get("/api/admin/users").json() if u["username"] == "demo")
    assert client.post(f"/api/admin/users/{uid}/role", data={"role": "user"}).json() == {"id": uid, "role": "user"}
    assert client.post(f"/api/admin/users/{uid}/role", data={"role": "god"}).status_code == 400
    me = client.get("/api/me").json()
    assert client.post(f"/api/admin/users/{me['id']}/role", data={"role": "user"}).status_code == 400
    login(client, "demo", "demopass123")
    assert client.get("/api/me").json()["role"] == "user"
    assert client.post(f"/api/admin/users/{uid}/role", data={"role": "admin"}).status_code == 403
