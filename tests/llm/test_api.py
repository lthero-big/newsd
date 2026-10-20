import httpx
from sqlalchemy import select

from llmkit import FakeLLM

KEY = "sk-live-abcdefghijklmnop1234"


def _user(client, name):
    from newsd.cli import main
    main(["admin", "registration", "open"])
    client.cookies.clear()
    assert client.post("/api/register", data={"username": name, "password": name + "pass123"}).status_code == 200
    assert client.post("/login", data={"username": name, "password": name + "pass123"}, follow_redirects=False).status_code == 303


def _fake(monkeypatch, provider, responder):
    from newsd.llm import keys
    fake = FakeLLM(provider, responder)
    monkeypatch.setattr(keys, "TRANSPORT", fake.transport)
    return fake


def test_key_stored_encrypted_and_never_returned(client):
    _user(client, "alice")
    r = client.put("/api/me/llm", json={"provider": "deepseek", "model": "deepseek-chat", "api_key": KEY})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_key"] and body["key_last4"] == "1234" and body["ai_enabled"] and KEY not in r.text
    assert KEY not in client.get("/api/me/llm").text

    from newsd.db import SessionLocal
    from newsd.models import UserSetting
    from newsd.security import decrypt_secret
    db = SessionLocal()
    row = db.scalar(select(UserSetting).where(UserSetting.key == "llm"))
    assert row.secret and KEY not in row.secret and KEY not in str(row.value)
    assert decrypt_secret(row.secret) == KEY  # M1 server-key Fernet
    db.close()

    # change model without resending the key keeps the key; bad input is rejected without partial writes
    assert client.put("/api/me/llm", json={"provider": "deepseek", "model": "deepseek-reasoner"}).json()["has_key"]
    assert client.put("/api/me/llm", json={"provider": "anthropic", "model": "claude-x"}).status_code == 200  # stored key reused
    assert client.put("/api/me/llm", json={"provider": "nope", "model": "m", "api_key": "k"}).status_code == 400
    assert client.put("/api/me/llm", json={"provider": "openai", "model": "m", "api_key": ""}).status_code == 400
    assert client.put("/api/me/llm", json={"provider": "openai", "model": "m", "translate_to": "xx"}).status_code == 400
    assert client.get("/api/me/llm").json()["model"] == "claude-x"

    _user(client, "bob")  # other users see nothing
    assert client.get("/api/me/llm").json() == {"configured": False, "provider": None, "model": None, "has_key": False,
                                                "key_last4": None, "key_unreadable": False, "translate_to": "zh",
                                                "ai_enabled": False}
    assert client.delete("/api/me/llm").status_code == 200


def test_connection_endpoint_openai_ok_and_anthropic_auth_error(client, monkeypatch):
    _user(client, "carol")
    fake = _fake(monkeypatch, "openai", lambda *a: "OK")
    r = client.post("/api/me/llm/test", json={"provider": "openai", "model": "gpt-4o-mini", "api_key": KEY})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["reply"] == "OK"
    assert fake.calls[0]["headers"]["authorization"] == f"Bearer {KEY}"
    assert client.get("/api/me/llm").json()["configured"] is False  # testing does not save

    fake = _fake(monkeypatch, "anthropic", lambda *a: httpx.Response(401, json={"error": {"message": "invalid x-api-key"}}))
    r = client.post("/api/me/llm/test", json={"provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": KEY}).json()
    assert r["ok"] is False and r["kind"] == "auth" and "invalid x-api-key" in r["error"] and KEY not in str(r)

    # stored settings are used when the body is empty
    fake = _fake(monkeypatch, "anthropic", lambda *a: "OK")
    client.put("/api/me/llm", json={"provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": KEY})
    assert client.post("/api/me/llm/test").json()["ok"] is True
    assert fake.calls[0]["headers"]["x-api-key"] == KEY
    # a stored key is never sent to a different provider
    r = client.post("/api/me/llm/test", json={"provider": "openai", "model": "gpt-4o"}).json()
    assert r["ok"] is False and r["kind"] == "config"
    # rate limited
    codes = [client.post("/api/me/llm/test", json={"provider": "openai", "model": "x"}).status_code for _ in range(8)]
    assert 429 in codes


def test_profiles_api_and_settings_page(client):
    _user(client, "dave")
    profs = client.get("/api/profiles").json()
    names = {p["name"] for p in profs}
    assert {"ai-news", "research", "open-source", "creative-ai", "agents"} <= names
    for p in profs:
        assert "## 打分标准" in p["prompt"] and "## 是否属于本类" in p["prompt"] and 0 <= p["threshold"] <= 10 and p["sections"]
    assert client.get("/api/me/profiles").json() == [{"name": "ai-news", "title": "AI 资讯", "default_threshold": 6.0, "threshold": 6.0}]
    r = client.put("/api/me/profiles", json=[{"name": "research", "threshold": 8.5}, {"name": "agents"}])
    assert [(p["name"], p["threshold"]) for p in r.json()] == [("research", 8.5), ("agents", 6.0)]
    assert client.put("/api/me/profiles", json=[{"name": "nope"}]).status_code == 400
    assert client.put("/api/me/profiles", json=[{"name": "research", "threshold": 11}]).status_code == 422
    assert client.put("/api/me/profiles", json=[{"name": "research"}, {"name": "research"}]).status_code == 400

    page = client.get("/settings")
    assert page.status_code == 200 and "测试连接" in page.text and "/api/me/llm/test" in page.text and "Anthropic" in page.text
    assert {p["provider"] for p in client.get("/api/llm/providers").json()} >= {
        "openai", "anthropic", "gemini", "deepseek", "tongyi", "doubao", "ollama"}
    client.cookies.clear()
    assert client.get("/settings", follow_redirects=False).status_code == 303
    assert client.post("/api/me/llm/test").status_code == 401
