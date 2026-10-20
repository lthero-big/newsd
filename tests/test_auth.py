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
