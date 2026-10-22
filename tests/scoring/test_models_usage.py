"""DEPTAI-216: fetch models from the provider, token usage per build, background build status."""
import time

import httpx
from sqlalchemy import select

from llmkit import FakeLLM, wrap

KEY = "sk-live-abcdefghijklmnop1234"


def _user(client, name):
    from newsd.cli import main
    main(["admin", "registration", "open"])
    client.cookies.clear()
    assert client.post("/api/register", data={"username": name, "password": name + "pass123"}).status_code == 200
    assert client.post("/login", data={"username": name, "password": name + "pass123"}, follow_redirects=False).status_code == 303


def _transport(monkeypatch, handler):
    from newsd.llm import keys
    monkeypatch.setattr(keys, "TRANSPORT", httpx.MockTransport(handler))


def test_parse_tokens_every_wire():
    from newsd.llm.client import parse_tokens
    assert parse_tokens("openai", {"usage": {"prompt_tokens": 36000, "completion_tokens": 6300}}) == (36000, 6300)
    assert parse_tokens("anthropic", {"usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 3}}) == (15, 3)
    assert parse_tokens("gemini", {"usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 2}}) == (7, 2)
    assert parse_tokens("ollama", {"prompt_eval_count": 4, "eval_count": 1}) == (4, 1)
    assert parse_tokens("openai", {}) == (0, 0)


def test_parse_models():
    from newsd.llm.client import parse_models
    assert parse_models("openai", {"data": [{"id": "b-model"}, {"id": "A-model"}, {"id": "b-model"}]}) == ["A-model", "b-model"]
    assert parse_models("gemini", {"models": [{"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
                                              {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]}]}) == ["gemini-2.5-flash"]
    assert parse_models("ollama", {"models": [{"name": "qwen3:8b"}]}) == ["qwen3:8b"]


def test_fetch_models_endpoint(client, monkeypatch):
    _user(client, "mia")
    seen = []

    def h(req):
        seen.append((req.method, str(req.url), req.headers.get("authorization")))
        return httpx.Response(200, json={"object": "list", "data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]})
    _transport(monkeypatch, h)
    r = client.post("/api/me/llm/providers/models", json={"preset": "deepseek", "api_key": KEY}).json()
    assert r["ok"] and r["models"] == ["deepseek-chat", "deepseek-reasoner"] and r["total"] == 2
    assert seen[0][0] == "GET" and seen[0][1].endswith("/models") and seen[0][2] == f"Bearer {KEY}"

    # editing a saved provider reuses its stored key
    pid = client.post("/api/me/llm/providers", json={"preset": "deepseek", "model": "deepseek-chat", "api_key": KEY}).json()["id"]
    seen.clear()
    assert client.post("/api/me/llm/providers/models", json={"preset": "deepseek", "provider_id": pid}).json()["ok"]
    assert seen[0][2] == f"Bearer {KEY}"

    _transport(monkeypatch, lambda req: httpx.Response(401, json={"error": {"message": "bad key"}}))
    r = client.post("/api/me/llm/providers/models", json={"preset": "deepseek", "api_key": "sk-wrong-000000000000"}).json()
    assert r["ok"] is False and r["kind"] == "auth" and "bad key" in r["error"]
    r = client.post("/api/me/llm/providers/models", json={"preset": "deepseek"}).json()  # no key at all
    assert r["ok"] is False and r["kind"] == "config"


def _seed(name):
    from newsd.db import SessionLocal
    from newsd.models import Keyword, Subscription, User
    from poolseed import Pool
    db = SessionLocal()
    p = Pool(db)
    p.item("Qwen 3 released", summary="new model")
    u = db.scalar(select(User).where(User.username == name))
    for src in p.sources.values():
        db.add(Subscription(user_id=u.id, source_id=src.id))
    db.add(Keyword(user_id=u.id, kind="interest", term="Qwen"))
    db.commit(); db.close()


def test_build_records_usage_and_background_status(client, monkeypatch):
    _user(client, "nia")
    _seed("nia")
    fake = FakeLLM("openai")

    def responder(system, user, body, req):
        out = wrap("openai", '{"items": []}' if body.get("response_format") else "翻译")
        out["usage"] = {"prompt_tokens": 1200, "completion_tokens": 300}
        return out
    fake.responder = responder
    from newsd.llm import keys
    monkeypatch.setattr(keys, "TRANSPORT", fake.transport)
    assert client.post("/api/me/llm/providers", json={"preset": "openai", "model": "gpt-4o-mini", "api_key": KEY}).status_code == 200

    rep = client.post("/api/me/issues/build", json={"key": "2026-10-21"}).json()
    n = len(fake.calls)
    assert n >= 1 and rep["usage"]["requests"] == n
    assert rep["usage"]["input_tokens"] == 1200 * n and rep["usage"]["output_tokens"] == 300 * n

    u = client.get("/api/me/llm/usage").json()
    assert u["runs"][0]["key"] == "2026-10-21" and u["runs"][0]["trigger"] == "manual" and u["runs"][0]["status"] == "ok"
    assert u["runs"][0]["input_tokens"] == 1200 * n and u["total"]["output_tokens"] == 300 * n

    assert client.get("/api/me/issues/build/status").json() == {"state": "idle"}
    r = client.post("/api/me/issues/build", json={"key": "2026-10-22", "background": True}).json()
    assert r["started"] is True and r["state"] == "running"
    for _ in range(100):
        st = client.get("/api/me/issues/build/status").json()
        if st["state"] != "running":
            break
        time.sleep(0.05)
    assert st["state"] == "done", st
    assert st["key"] == "2026-10-22" and st["stage_label"] == "已完成" and st["usage"]["input_tokens"] > 0
    assert client.get("/api/me/issues/2026-10-22").status_code == 200
    assert len(client.get("/api/me/llm/usage").json()["runs"]) == 2

    _user(client, "oli")  # other users see neither the status nor the usage
    assert client.get("/api/me/issues/build/status").json() == {"state": "idle"}
    assert client.get("/api/me/llm/usage").json()["runs"] == []
