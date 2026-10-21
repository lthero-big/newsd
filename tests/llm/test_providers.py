"""DEPTAI-216 item 2: presets, several providers per user, priority + failover, custom base URLs
(SSRF guard), test connection, options, migration of the v1 single-model config."""
import asyncio

import httpx
import pytest
from sqlalchemy import select

from llmkit import wrap

KEY = "sk-live-abcdefghijklmnop1234"
KEY2 = "sk-back-qrstuvwxyz98765432"
PUBLIC_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """Hostnames resolve to a public address unless a test maps them elsewhere. No real DNS."""
    from newsd.llm import netguard
    table = {"localhost": ["127.0.0.1"], "intranet.corp": ["10.1.2.3"], "meta.internal": ["169.254.169.254"],
             "mixed.example.com": [PUBLIC_IP, "192.168.1.5"]}

    def resolve(host, port):
        try:
            import ipaddress
            ipaddress.ip_address(host.strip("[]"))
            return [host.strip("[]")]
        except ValueError:
            return table.get(host, [PUBLIC_IP])
    monkeypatch.setattr(netguard, "RESOLVER", resolve)
    monkeypatch.delenv("NEWSD_LLM_ALLOW_PRIVATE_URLS", raising=False)
    return table


class Router:
    """httpx transport dispatching on host: host -> status code or reply text."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def handler(self, request):
        self.calls.append(str(request.url))
        out = self.routes.get(request.url.host, 404)
        if isinstance(out, int):
            return httpx.Response(out, json={"error": {"message": f"status {out}"}})
        if "anthropic" in request.url.host:
            return httpx.Response(200, json=wrap("anthropic", out))
        return httpx.Response(200, json=wrap("openai", out))

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


def _route(monkeypatch, routes):
    from newsd.llm import keys
    r = Router(routes)
    monkeypatch.setattr(keys, "TRANSPORT", r.transport)
    return r


def _user(client, name, admin=False):
    from newsd.cli import main
    main(["admin", "registration", "open"])
    client.cookies.clear()
    assert client.post("/api/register", data={"username": name, "password": name + "pass123"}).status_code == 200
    if admin:
        main(["set-role", name, "admin"])
    assert client.post("/login", data={"username": name, "password": name + "pass123"}, follow_redirects=False).status_code == 303


def _uid(name):
    from newsd.db import SessionLocal
    from newsd.models import User
    db = SessionLocal()
    try:
        return db.scalar(select(User.id).where(User.username == name))
    finally:
        db.close()


def test_presets_cover_domestic_foreign_local(client):
    _user(client, "pat")
    body = client.get("/api/llm/presets").json()
    ids = {p["id"] for p in body["presets"]}
    assert {"openai", "anthropic", "gemini", "xai", "mistral", "groq", "openrouter", "deepseek", "qwen", "glm", "kimi",
            "doubao", "qianfan", "hunyuan", "minimax", "stepfun", "yi", "siliconflow", "ollama", "custom"} <= ids
    assert "tongyi" not in ids and "openai_compatible" not in ids  # legacy ids stay hidden
    assert {r["id"] for r in body["regions"]} == {"intl", "cn", "local"}
    by = {p["id"]: p for p in body["presets"]}
    assert by["xai"]["base_url"] == "https://api.x.ai/v1" and "grok-4" in by["xai"]["models"]
    assert by["gemini"]["region"] == "intl" and by["glm"]["region"] == "cn" and by["ollama"]["needs_key"] is False
    assert by["custom"]["needs_base_url"] is True
    for p in body["presets"]:
        assert p["id"] in ("ollama", "custom") or p["base_url"].startswith("https://")


def test_provider_crud_order_enable_and_isolation(client):
    _user(client, "ann")
    r = client.post("/api/me/llm/providers", json={"preset": "deepseek", "model": "deepseek-chat", "api_key": KEY})
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["name"] == "DeepSeek" and a["priority"] == 0 and a["key_last4"] == "1234" and KEY not in r.text
    b = client.post("/api/me/llm/providers", json={"preset": "xai", "model": "grok-4-fast", "api_key": KEY2,
                                                   "name": "Grok 备份"}).json()
    c = client.post("/api/me/llm/providers", json={"preset": "ollama", "model": "qwen3:8b"}).json()
    assert (b["priority"], c["priority"], c["has_key"], c["usable"]) == (1, 2, False, True)
    view = client.get("/api/me/llm/providers").json()
    assert [p["name"] for p in view["chain"]] == ["DeepSeek", "Grok 备份", "Ollama · 本地"] and view["ai_enabled"]
    assert KEY not in str(view) and KEY2 not in str(view)

    from newsd.db import SessionLocal
    from newsd.models import LLMProvider
    from newsd.security import decrypt_secret
    db = SessionLocal()
    rows = list(db.scalars(select(LLMProvider).order_by(LLMProvider.priority)))
    assert KEY not in rows[0].secret and decrypt_secret(rows[0].secret) == KEY and rows[2].secret is None
    db.close()

    # reorder, toggle, edit without resending the key, bad input
    assert client.put("/api/me/llm/order", json={"ids": [b["id"], a["id"]]}).status_code == 400
    assert client.put("/api/me/llm/order", json={"ids": [b["id"], a["id"], a["id"]]}).status_code == 400
    v = client.put("/api/me/llm/order", json={"ids": [c["id"], b["id"], a["id"]]}).json()
    assert [p["id"] for p in v["providers"]] == [c["id"], b["id"], a["id"]]
    assert client.patch(f"/api/me/llm/providers/{c['id']}", json={"enabled": False}).json()["enabled"] is False
    assert [p["id"] for p in client.get("/api/me/llm/providers").json()["chain"]] == [b["id"], a["id"]]
    e = client.patch(f"/api/me/llm/providers/{b['id']}", json={"model": "grok-4"}).json()
    assert e["model"] == "grok-4" and e["has_key"] and e["key_last4"] == "5432"
    assert client.patch(f"/api/me/llm/providers/{b['id']}", json={"api_key": ""}).status_code == 400  # key required
    assert client.patch(f"/api/me/llm/providers/{b['id']}", json={"preset": "nope"}).status_code == 400
    assert client.post("/api/me/llm/providers", json={"preset": "openai", "model": "bad model", "api_key": KEY}).status_code == 400
    assert client.post("/api/me/llm/providers", json={"preset": "openai", "model": "m"}).status_code == 400  # no key
    assert client.post("/api/me/llm/providers", json={"preset": "custom", "model": "m", "api_key": KEY}).status_code == 400

    # v1 API shows the primary (first by priority); the legacy view still works
    legacy = client.get("/api/me/llm").json()
    assert legacy["provider"] == "ollama" and legacy["ai_enabled"] is False  # disabled primary

    _user(client, "ben")  # other users see and touch nothing
    assert client.get("/api/me/llm/providers").json()["providers"] == []
    assert client.patch(f"/api/me/llm/providers/{a['id']}", json={"enabled": False}).status_code == 404
    assert client.delete(f"/api/me/llm/providers/{a['id']}").status_code == 404
    assert client.post(f"/api/me/llm/providers/{a['id']}/test").status_code == 404
    assert client.put("/api/me/llm/order", json={"ids": [a["id"]]}).status_code == 400

    client.cookies.clear()
    _login = client.post("/login", data={"username": "ann", "password": "annpass123"}, follow_redirects=False)
    assert _login.status_code == 303
    assert client.delete(f"/api/me/llm/providers/{c['id']}").json() == {"ok": True}
    assert [p["priority"] for p in client.get("/api/me/llm/providers").json()["providers"]] == [0, 1]


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080/v1", "http://localhost:11434/v1", "http://intranet.corp/v1",
                                 "http://169.254.169.254/latest", "http://[::1]/v1", "https://mixed.example.com/v1",
                                 "http://10.0.0.8/v1", "http://100.64.0.1/v1", "http://[::ffff:127.0.0.1]/v1"])
def test_private_base_urls_rejected_for_users(client, url):
    _user(client, "eve")
    r = client.post("/api/me/llm/providers", json={"preset": "custom", "model": "m", "api_key": KEY, "base_url": url})
    assert r.status_code == 400 and "private" in r.json()["error"]
    t = client.post("/api/me/llm/providers/test", json={"preset": "custom", "model": "m", "api_key": KEY, "base_url": url}).json()
    assert t["ok"] is False and t["kind"] == "config"


@pytest.mark.parametrize("url", ["ftp://gw.example.com/v1", "https://user:pw@gw.example.com/v1", "https://gw.example.com/v1?x=1",
                                 "https://gw.example.com/v1#f", "gw.example.com/v1", "https:///v1", "https://gw example.com/v1"])
def test_malformed_base_urls_rejected(client, url):
    _user(client, "mal")
    r = client.post("/api/me/llm/providers", json={"preset": "custom", "model": "m", "api_key": KEY, "base_url": url})
    assert r.status_code == 400


def test_custom_base_url_used_and_admin_may_use_private(client, monkeypatch):
    _user(client, "cat")
    router = _route(monkeypatch, {"gw.example.com": "OK", "10.9.9.9": "OK"})
    p = client.post("/api/me/llm/providers", json={"preset": "custom", "model": "Qwen/Qwen3-8B", "api_key": KEY,
                                                   "base_url": "https://gw.example.com/v1/"}).json()
    assert p["base_url"] == "https://gw.example.com/v1" and p["custom_base_url"] == "https://gw.example.com/v1"
    r = client.post(f"/api/me/llm/providers/{p['id']}/test").json()
    assert r["ok"] is True and router.calls == ["https://gw.example.com/v1/chat/completions"]
    # a preset with its default URL stores no override; changing the preset resets an override
    d = client.post("/api/me/llm/providers", json={"preset": "kimi", "model": "moonshot-v1-32k", "api_key": KEY,
                                                   "base_url": "https://api.moonshot.cn/v1"}).json()
    assert d["custom_base_url"] is None
    assert client.patch(f"/api/me/llm/providers/{p['id']}", json={"preset": "openai"}).json()["custom_base_url"] is None

    _user(client, "root2", admin=True)
    a = client.post("/api/me/llm/providers", json={"preset": "custom", "model": "m", "api_key": KEY,
                                                   "base_url": "http://10.9.9.9:8000/v1"})
    assert a.status_code == 200, a.text
    assert client.post(f"/api/me/llm/providers/{a.json()['id']}/test").json()["ok"] is True

    _user(client, "dan")
    monkeypatch.setenv("NEWSD_LLM_ALLOW_PRIVATE_URLS", "1")  # server-wide opt-in (e.g. a LAN Ollama)
    assert client.post("/api/me/llm/providers", json={"preset": "ollama", "model": "qwen3:8b",
                                                      "base_url": "http://intranet.corp:11434/v1"}).status_code == 200


def test_dns_rebinding_is_rechecked_and_fails_over(db, fake_dns, monkeypatch):
    from newsd.llm import keys
    from newsd.models import User
    u = User(username="reb", password_hash="x")
    db.add(u); db.commit()
    keys.add_provider(db, u.id, preset="custom", model="m", api_key=KEY, base_url="https://gw.example.com/v1")
    keys.add_provider(db, u.id, preset="deepseek", model="deepseek-chat", api_key=KEY2)
    router = Router({"gw.example.com": "from-gw", "api.deepseek.com": "from-ds"})
    keys.TRANSPORT = router.transport
    fake_dns["gw.example.com"] = ["127.0.0.1"]  # DNS now points inside

    async def go():
        c = keys.client_for_user(db, u.id)
        try:
            return await c.complete("", "hi"), c.usage, c.label
        finally:
            await c.aclose()
    out, usage, label = asyncio.run(go())
    assert out == "from-ds" and label == "deepseek/deepseek-chat" and usage.failovers == 1
    assert router.calls == ["https://api.deepseek.com/v1/chat/completions"]  # nothing was sent to the rebound host


def test_failover_order_cooldown_retries_and_all_fail(db, monkeypatch):
    from newsd.llm import keys
    from newsd.llm.client import FailoverClient, LLMError
    from newsd.models import User
    u = User(username="fo", password_hash="x")
    db.add(u); db.commit()
    p1 = keys.add_provider(db, u.id, preset="openai", model="gpt-4.1", api_key=KEY)
    keys.add_provider(db, u.id, preset="anthropic", model="claude-haiku-4-5", api_key=KEY2)
    keys.add_provider(db, u.id, preset="glm", model="glm-4.6", api_key=KEY, enabled=False)
    keys.set_options(db, u.id, retries=1, timeout=30)
    router = Router({"api.openai.com": 503, "api.anthropic.com": "claude-ok", "open.bigmodel.cn": "glm"})
    keys.TRANSPORT = router.transport

    async def go():
        c = keys.client_for_user(db, u.id, backoff=0)
        assert isinstance(c, FailoverClient) and c.labels == ["openai/gpt-4.1", "anthropic/claude-haiku-4-5"]
        assert c.clients[0].retries == 1 and c.clients[0].client.timeout.read == 30
        assert c.label == "openai/gpt-4.1"
        first = await c.complete("", "a")
        second = await c.complete("", "b")  # primary is cooling down: goes straight to the backup
        js = await c.complete_json("", "c")
        u_ = c.usage
        await c.aclose()
        return first, second, js, u_, c.label, c.events
    first, second, js, usage, label, events = asyncio.run(go())
    assert first == second == "claude-ok" and js is None
    assert label == "anthropic/claude-haiku-4-5" and usage.failovers == 1 and events[0]["kind"] == "http"
    assert router.calls.count("https://api.openai.com/v1/chat/completions") == 2  # 1 try + 1 retry, then cooldown
    assert usage.retries == 1 and usage.errors.get("http") == 1
    assert not any("bigmodel" in x for x in router.calls)  # disabled provider never used

    # every provider failing raises the last error
    keys.update_provider(db, u.id, p1["id"], enabled=False)
    keys.TRANSPORT = Router({"api.anthropic.com": 401}).transport

    async def bad():
        c = keys.client_for_user(db, u.id)
        try:
            await c.complete("", "x")
        finally:
            await c.aclose()
    with pytest.raises(LLMError) as e:
        asyncio.run(bad())
    assert e.value.kind == "auth"
    keys.update_provider(db, u.id, p1["id"], enabled=True)
    keys.delete_settings(db, u.id)
    assert keys.client_for_user(db, u.id) is None


def test_failover_json_repair_uses_chain(db):
    from newsd.llm import keys
    from newsd.models import User
    u = User(username="js", password_hash="x")
    db.add(u); db.commit()
    keys.add_provider(db, u.id, preset="deepseek", model="deepseek-chat", api_key=KEY)
    replies = iter(["not json", '{"ok": true}'])
    keys.TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(200, json=wrap("openai", next(replies))))

    async def go():
        async with keys.client_for_user(db, u.id) as c:
            return await c.complete_json("", "x"), c.usage
    out, usage = asyncio.run(go())
    assert out == {"ok": True} and usage.repairs == 1 and usage.requests == 2


def test_connection_tests_unsaved_and_saved(client, monkeypatch):
    _user(client, "tia")
    router = _route(monkeypatch, {"api.x.ai": "OK", "generativelanguage.googleapis.com": 403})
    r = client.post("/api/me/llm/providers/test", json={"preset": "xai", "model": "grok-4-fast", "api_key": KEY}).json()
    assert r["ok"] is True and router.calls[-1] == "https://api.x.ai/v1/chat/completions"
    assert client.get("/api/me/llm/providers").json()["providers"] == []  # testing does not save
    g = client.post("/api/me/llm/providers", json={"preset": "gemini", "model": "gemini-2.5-flash", "api_key": KEY}).json()
    r = client.post(f"/api/me/llm/providers/{g['id']}/test").json()
    assert r["ok"] is False and r["kind"] == "auth" and KEY not in str(r)
    assert router.calls[-1] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    lt = client.get("/api/me/llm/providers").json()["providers"][0]["last_test"]
    assert lt["ok"] is False and lt["error"].startswith("auth")
    # editing a saved provider: its key is reused only for the same preset
    r = client.post("/api/me/llm/providers/test", json={"preset": "gemini", "model": "gemini-2.5-pro", "provider_id": g["id"]}).json()
    assert r["kind"] == "auth"  # reached the provider with the stored key
    r = client.post("/api/me/llm/providers/test", json={"preset": "xai", "model": "grok-4", "provider_id": g["id"]}).json()
    assert r["ok"] is False and r["kind"] == "config"
    codes = [client.post("/api/me/llm/providers/test", json={"preset": "xai", "model": "x", "api_key": KEY}).status_code
             for _ in range(8)]
    assert 429 in codes


def test_wire_endpoints_with_custom_bases():
    from newsd.llm.client import ModelConfig, build_request
    kw = dict(json_mode=False, max_tokens=10, temperature=0)
    assert build_request(ModelConfig("anthropic", "c", KEY, "https://proxy.example.com"), "", "u", **kw)[0] == \
        "https://proxy.example.com/v1/messages"
    assert build_request(ModelConfig("anthropic", "c", KEY, "https://proxy.example.com/v1"), "", "u", **kw)[0] == \
        "https://proxy.example.com/v1/messages"
    assert build_request(ModelConfig("gemini", "g", KEY, "https://generativelanguage.googleapis.com/v1beta/openai"),
                         "", "u", **kw)[0] == "https://generativelanguage.googleapis.com/v1beta/models/g:generateContent"
    assert build_request(ModelConfig("ollama", "q", "", "http://box:11434/v1"), "", "u", **kw)[0] == "http://box:11434/api/chat"
    assert build_request(ModelConfig("glm", "glm-4.6", KEY), "", "u", **kw)[0] == \
        "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    assert build_request(ModelConfig("qwen", "qwen-plus", KEY), "", "u", json_mode=True, max_tokens=5,
                         temperature=0)[2]["response_format"] == {"type": "json_object"}


def test_options_api(client):
    _user(client, "opt")
    assert client.get("/api/me/llm/providers").json()["options"] == {"translate_to": "zh", "retries": 2, "timeout": 60}
    assert client.put("/api/me/llm/options", json={"retries": 3, "timeout": 120, "translate_to": "en"}).json() == \
        {"translate_to": "en", "retries": 3, "timeout": 120}
    for bad in ({"retries": 9}, {"timeout": 5}, {"translate_to": "xx"}):
        assert client.put("/api/me/llm/options", json=bad).status_code == 400
    assert client.get("/api/me/llm").json()["translate_to"] == "en"


def test_settings_page_renders_chain(client):
    _user(client, "pg")
    client.post("/api/me/llm/providers", json={"preset": "deepseek", "model": "pg-model-a", "api_key": KEY, "name": "主力"})
    client.post("/api/me/llm/providers", json={"preset": "xai", "model": "grok-4", "api_key": KEY2, "name": "后备"})
    t = client.get("/models").text
    assert "pg-model-a" in t and "主用 · 主力" in t and "备份 1 · 后备" in t and '"key_last4": "1234"' in t and KEY not in t and KEY2 not in t
    assert "xAI Grok" in t and "通义千问" in t and "失败重试" in t and 'href="/models" data-k="models" aria-current="page"' in t
    assert "/api/me/llm/order" in client.get("/static/v2/models.js").text
    assert "pg-model-a" not in client.get("/settings").text  # no longer on the settings page


def test_migration_moves_v1_config(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWSD_DATABASE_URL", f"sqlite:///{tmp_path}/mig.db")
    monkeypatch.setenv("NEWSD_SECRET_KEY", "test-secret")
    monkeypatch.setenv("NEWSD_ENV_FILE", str(tmp_path / "none.env"))
    from sqlalchemy import text

    from newsd.db import get_engine, reset_engine, upgrade_db
    from newsd.security import decrypt_secret, encrypt_secret
    from newsd.settings import reset_settings
    reset_settings(); reset_engine()
    upgrade_db("0008_m4_push")
    ct = encrypt_secret(KEY)
    with get_engine().begin() as c:
        c.execute(text("INSERT INTO users (id, username, password_hash, role, active, created_at) "
                       "VALUES (1, 'old', 'x', 'user', 1, '2026-01-01')"))
        c.execute(text("INSERT INTO users (id, username, password_hash, role, active, created_at) "
                       "VALUES (2, 'none', 'x', 'user', 1, '2026-01-01')"))
        c.execute(text("INSERT INTO user_settings (user_id, key, value, secret) VALUES (1, 'llm', :v, :s)"),
                  {"v": '{"provider": "tongyi", "model": "qwen-plus", "last4": "1234", "translate_to": "ja"}', "s": ct})
        c.execute(text("INSERT INTO user_settings (user_id, key, value) VALUES (2, 'llm', :v)"), {"v": '{"translate_to": "en"}'})
    upgrade_db("head")
    from newsd.db import SessionLocal
    from newsd.llm import keys
    db = SessionLocal()
    view = keys.overview(db, 1)
    assert [(p["preset"], p["model"], p["key_last4"], p["enabled"]) for p in view["providers"]] == [("tongyi", "qwen-plus", "1234", True)]
    assert view["options"]["translate_to"] == "ja" and keys.overview(db, 2)["providers"] == []
    assert keys.translate_to(db, 2) == "en"
    cfg = keys.model_config(db, 1)
    assert cfg.provider == "tongyi" and cfg.api_key == KEY
    with get_engine().connect() as c:
        assert c.execute(text("SELECT secret FROM llm_providers")).scalar() == ct  # ciphertext moved, not re-encrypted
        assert c.execute(text("SELECT secret FROM user_settings WHERE user_id = 1")).scalar() is None
    db.close()
    from alembic import command

    from newsd.db import alembic_config
    command.downgrade(alembic_config(), "0008_m4_push")  # downgrade restores the v1 row
    with get_engine().connect() as c:
        v, s = c.execute(text("SELECT value, secret FROM user_settings WHERE user_id = 1")).one()
        assert '"qwen-plus"' in v and decrypt_secret(s) == KEY
    reset_engine(); reset_settings()
