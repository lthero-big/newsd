from newsd.security import decrypt_secret, encrypt_secret, hash_password, verify_password


def login(c, u, p):
    return c.post("/login", data={"username": u, "password": p}, follow_redirects=False)


def test_password_hash_roundtrip():
    h = hash_password("hunter2hunter2")
    assert verify_password("hunter2hunter2", h) and not verify_password("x", h)


def test_secret_encryption_roundtrip(client):
    enc = encrypt_secret("sk-abc")
    assert enc != "sk-abc" and decrypt_secret(enc) == "sk-abc"


def test_admin_login_invite_register_flow(client):
    assert login(client, "root", "rootpass123").status_code == 303
    assert client.get("/api/me").json()["role"] == "admin"
    inv = client.post("/api/admin/invites").json()["invite"]
    client.cookies.clear()
    assert client.post("/api/register", data={"username": "bob", "password": "bobpass123", "invite": "bad"}).status_code == 403
    r = client.post("/api/register", data={"username": "bob", "password": "bobpass123", "invite": inv})
    assert r.status_code == 200
    # invite is single-use
    assert client.post("/api/register", data={"username": "eve", "password": "evepass123", "invite": inv}).status_code == 403
    assert login(client, "bob", "bobpass123").status_code == 303
    assert client.get("/api/me").json()["role"] == "user"
    assert client.get("/api/admin/users").status_code == 403


def test_api_token_auth_and_unknown_key_rejected(client):
    login(client, "root", "rootpass123")
    tok = client.post("/api/tokens", data={"kind": "api"}).json()["token"]
    client.cookies.clear()
    assert client.get("/api/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    assert client.get("/api/me", headers={"X-News-API-Key": tok}).status_code == 200
    assert client.get("/api/me", headers={"Authorization": "Bearer nd_unknown"}).status_code == 401


def test_three_failed_logins_lock_ip(client):
    for _ in range(3):
        assert login(client, "root", "wrong-password").status_code == 401
    assert login(client, "root", "rootpass123").status_code == 429


def test_open_registration_accepts_public_and_invite(client):
    login(client, "root", "rootpass123")
    assert client.post("/api/admin/registration", data={"mode": "open"}).json()["mode"] == "open"
    inv = client.post("/api/admin/invites").json()["invite"]
    client.cookies.clear()
    assert client.get("/api/registration").json()["mode"] == "open"
    # public sign-up without a code
    assert client.post("/api/register", data={"username": "pub", "password": "pubpass123"}).status_code == 200
    # an invite code still works in open mode, and a wrong one is rejected rather than ignored
    assert client.post("/api/register", data={"username": "inv", "password": "invpass123", "invite": inv}).status_code == 200
    assert client.post("/api/register", data={"username": "bad", "password": "badpass123", "invite": "nope"}).status_code == 403
    assert client.post("/api/register", data={"username": "pub", "password": "pubpass123"}).status_code == 409
    assert login(client, "pub", "pubpass123").status_code == 303


def test_registration_mode_switch_and_web_form(client):
    assert client.get("/api/registration").json()["mode"] == "invite"
    assert client.post("/api/register", data={"username": "x1", "password": "x1pass123"}).status_code == 403
    login(client, "root", "rootpass123")
    assert client.post("/api/admin/registration", data={"mode": "bogus"}).status_code == 400
    client.post("/api/admin/registration", data={"mode": "closed"})
    client.cookies.clear()
    assert client.post("/api/register", data={"username": "x2", "password": "x2pass123"}).status_code == 403
    assert "不开放注册" in client.get("/register").text
    login(client, "root", "rootpass123")
    client.post("/api/admin/registration", data={"mode": "open"})
    client.cookies.clear()
    assert "/register" in client.get("/login").text
    r = client.post("/register", data={"username": "web", "password": "webpass123"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    # non-admin cannot change the mode
    login(client, "web", "webpass123")
    assert client.post("/api/admin/registration", data={"mode": "invite"}).status_code == 403


def test_cli_registration_mode(client, capsys):
    from newsd.cli import main
    main(["admin", "registration", "open"])
    assert capsys.readouterr().out.strip() == "open"
    assert client.get("/api/registration").json()["mode"] == "open"
