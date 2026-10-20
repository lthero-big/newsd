import asyncio
import json

import httpx
import pytest

from llmkit import Clock, FakeLLM
from newsd.llm.client import LLMClient, LLMError, ModelConfig, loads_lenient

KEY = "sk-test-0123456789abcdef"


def run(coro):
    return asyncio.run(coro)


def make(provider, fake, **kw):
    clock = Clock()
    kw.setdefault("rpm", 0)
    kw.setdefault("sleep", clock.sleep)
    kw.setdefault("clock", clock)
    c = LLMClient(ModelConfig(provider, kw.pop("model", "m-1"), kw.pop("key", KEY)), transport=fake.transport, **kw)
    return c, clock


async def _complete(c, *a, **kw):
    async with c:
        return await c.complete(*a, **kw)


async def _json(c, *a, **kw):
    async with c:
        return await c.complete_json(*a, **kw)


# ------------------------------------------------------------------------------------ wire formats

def test_openai_request_and_reply():
    fake = FakeLLM("openai", lambda s, u, b, r: "hello")
    c, _ = make("openai", fake, model="gpt-4o-mini")
    assert run(_complete(c, "be brief", "hi", json_mode=True, max_tokens=50)) == "hello"
    call = fake.calls[0]
    assert call["url"] == "https://api.openai.com/v1/chat/completions"
    assert call["headers"]["authorization"] == f"Bearer {KEY}"
    assert call["body"]["model"] == "gpt-4o-mini" and call["body"]["max_tokens"] == 50
    assert call["body"]["messages"] == [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]
    assert call["body"]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("provider,url,json_mode", [
    ("deepseek", "https://api.deepseek.com/v1/chat/completions", True),
    ("tongyi", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", True),
    ("doubao", "https://ark.cn-beijing.volces.com/api/v3/chat/completions", False),
])
def test_openai_compatible_providers(provider, url, json_mode):
    fake = FakeLLM(provider, lambda *a: "ok")
    c, _ = make(provider, fake)
    assert run(_complete(c, "", "hi", json_mode=True)) == "ok"
    assert fake.calls[0]["url"] == url
    assert fake.calls[0]["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert ("response_format" in fake.calls[0]["body"]) is json_mode


def test_openai_compatible_needs_admin_base(monkeypatch):
    monkeypatch.delenv("NEWSD_LLM_BASE_OPENAI_COMPATIBLE", raising=False)
    with pytest.raises(LLMError) as e:
        ModelConfig("openai_compatible", "m", KEY).validate()
    assert e.value.kind == "config"
    monkeypatch.setenv("NEWSD_LLM_BASE_OPENAI_COMPATIBLE", "https://gw.example.com/v1/")
    fake = FakeLLM("openai_compatible", lambda *a: "ok")
    c, _ = make("openai_compatible", fake, model="Qwen/Qwen2.5-72B-Instruct")
    run(_complete(c, "", "hi"))
    assert fake.calls[0]["url"] == "https://gw.example.com/v1/chat/completions"
    assert fake.calls[0]["body"]["model"] == "Qwen/Qwen2.5-72B-Instruct"


def test_anthropic_request_and_reply():
    def reply(s, u, b, r):
        return {"content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Hel"},
                            {"type": "text", "text": "lo"}], "stop_reason": "end_turn"}
    fake = FakeLLM("anthropic", reply)
    c, _ = make("anthropic", fake, model="claude-sonnet-4-5")
    assert run(_complete(c, "sys prompt", "hi", max_tokens=99, temperature=0.1)) == "Hello"
    call = fake.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == KEY and call["headers"]["anthropic-version"] == "2023-06-01"
    assert "authorization" not in call["headers"]
    assert call["body"] == {"model": "claude-sonnet-4-5", "max_tokens": 99, "temperature": 0.1, "system": "sys prompt",
                            "messages": [{"role": "user", "content": "hi"}]}


def test_anthropic_json_with_fence_and_error_body():
    fake = FakeLLM("anthropic", lambda *a: '```json\n{"items": [1, 2]}\n```')
    c, _ = make("anthropic", fake)
    assert run(_json(c, "s", "u")) == {"items": [1, 2]}
    assert len(fake.calls) == 1  # no repair needed

    err = FakeLLM("anthropic", lambda *a: httpx.Response(401, json={"type": "error", "error": {
        "type": "authentication_error", "message": "invalid x-api-key"}}))
    c, _ = make("anthropic", err)
    with pytest.raises(LLMError) as e:
        run(_complete(c, "", "hi"))
    assert e.value.kind == "auth" and "invalid x-api-key" in str(e.value) and KEY not in str(e.value)
    assert len(err.calls) == 1  # auth errors are not retried


def test_gemini_and_ollama_shapes():
    fake = FakeLLM("gemini", lambda *a: "g")
    c, _ = make("gemini", fake, model="gemini-2.5-flash")
    assert run(_complete(c, "sys", "hi", json_mode=True)) == "g"
    call = fake.calls[0]
    assert call["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    assert call["headers"]["x-goog-api-key"] == KEY and KEY not in call["url"]
    assert call["body"]["systemInstruction"] == {"parts": [{"text": "sys"}]}
    assert call["body"]["generationConfig"]["responseMimeType"] == "application/json"

    fake = FakeLLM("ollama", lambda *a: "o")
    c, _ = make("ollama", fake, model="qwen2.5:7b", key="")
    assert run(_complete(c, "", "hi", json_mode=True)) == "o"
    assert fake.calls[0]["url"] == "http://localhost:11434/api/chat"
    assert fake.calls[0]["body"]["format"] == "json" and fake.calls[0]["body"]["stream"] is False
    assert "authorization" not in fake.calls[0]["headers"]


def test_config_validation_and_no_key_leak():
    for bad in [("nope", "m", KEY), ("openai", "", KEY), ("openai", "m", ""), ("gemini", "a/../b", KEY),
                ("gemini", "models/x", KEY), ("openai", "bad model", KEY)]:
        with pytest.raises(LLMError):
            ModelConfig(*bad).validate()
    ModelConfig("ollama", "llama3.1:8b", "").validate()  # keyless
    assert KEY not in repr(ModelConfig("openai", "m", KEY))


def test_unexpected_shape_is_bad_response():
    fake = FakeLLM("openai", lambda *a: {"choices": []})
    c, _ = make("openai", fake)
    with pytest.raises(LLMError) as e:
        run(_complete(c, "", "hi"))
    assert e.value.kind == "bad_response"


# ------------------------------------------------------------------------------------ retries

def test_retry_429_honours_retry_after_then_succeeds():
    seq = [httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "slow down"}}),
           httpx.Response(503, text="busy"), None]
    fake = FakeLLM("openai", lambda *a: seq.pop(0) or "fine")
    c, clock = make("openai", fake, backoff=1.0)
    assert run(_complete(c, "", "hi")) == "fine"
    assert len(fake.calls) == 3
    assert clock.sleeps[0] == 7.0  # Retry-After
    assert 2.0 <= clock.sleeps[1] <= 2.5  # exponential: backoff * 2**1 (+ jitter)
    assert c.usage.retries == 2


def test_retries_exhausted_raises_rate_limit():
    fake = FakeLLM("openai", lambda *a: httpx.Response(429, json={"error": {"message": "quota"}}))
    c, clock = make("openai", fake, retries=2, backoff=0.5)
    with pytest.raises(LLMError) as e:
        run(_complete(c, "", "hi"))
    assert e.value.kind == "rate_limit" and len(fake.calls) == 3
    assert clock.sleeps[0] < clock.sleeps[1]  # backoff grows


def test_timeout_is_retried_and_400_is_not():
    n = {"i": 0}

    def flaky(*a):
        n["i"] += 1
        if n["i"] == 1:
            raise httpx.ReadTimeout("slow")
        return "ok"
    fake = FakeLLM("openai", flaky)
    c, _ = make("openai", fake)
    assert run(_complete(c, "", "hi")) == "ok" and len(fake.calls) == 2

    bad = FakeLLM("openai", lambda *a: httpx.Response(400, json={"error": {"message": "bad model"}}))
    c, _ = make("openai", bad)
    with pytest.raises(LLMError) as e:
        run(_complete(c, "", "hi"))
    assert e.value.kind == "http" and e.value.status == 400 and len(bad.calls) == 1


# ------------------------------------------------------------------------------------ JSON repair

def test_invalid_json_repaired_once():
    replies = ["Sure! here: {items: [1,2,]}", '{"items": [1, 2]}']
    fake = FakeLLM("openai", lambda *a: replies.pop(0))
    c, _ = make("openai", fake)
    assert run(_json(c, "s", "u")) == {"items": [1, 2]}
    assert len(fake.calls) == 2 and "fix malformed JSON" in fake.calls[1]["system"]
    assert "{items: [1,2,]}" in fake.calls[1]["user"]
    assert c.usage.repairs == 1 and c.usage.invalid_json == 0


def test_still_invalid_after_repair_returns_none():
    fake = FakeLLM("anthropic", lambda *a: "no json here")
    c, _ = make("anthropic", fake)
    assert run(_json(c, "s", "u")) is None
    assert len(fake.calls) == 2  # one original + exactly one repair
    assert c.usage.invalid_json == 1


def test_loads_lenient():
    assert loads_lenient('noise {"a": 1} trailing') == {"a": 1}
    assert loads_lenient("[1, 2]") == [1, 2]
    with pytest.raises(ValueError):
        loads_lenient("")


# ------------------------------------------------------------------------------------ limits

def test_concurrency_cap():
    state = {"now": 0, "peak": 0}

    async def slow(request):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.01)
        state["now"] -= 1
        return httpx.Response(200, json={"choices": [{"message": {"content": json.loads(request.content)["messages"][-1]["content"]}}]})

    async def go():
        async with LLMClient(ModelConfig("openai", "m", KEY), rpm=0, concurrency=2, transport=httpx.MockTransport(slow)) as c:
            out = await asyncio.gather(*(c.complete("", str(i)) for i in range(8)))
            return out, c.peak_in_flight
    out, peak = asyncio.run(go())
    assert out == [str(i) for i in range(8)]
    assert state["peak"] == 2 and peak == 2


def test_rate_limit_spaces_requests():
    fake = FakeLLM("openai", lambda *a: "ok")
    c, clock = make("openai", fake, rpm=30)  # one request start every 2s

    async def go():
        async with c:
            await asyncio.gather(*(c.complete("", "x") for _ in range(4)))
    run(go())
    assert len(fake.calls) == 4
    assert clock.sleeps == [2.0, 2.0, 2.0]


def test_test_connection_reports_without_retry():
    fake = FakeLLM("openai", lambda *a: httpx.Response(503, text="down"))
    c, _ = make("openai", fake)

    async def go():
        async with c:
            return await c.test_connection()
    res = run(go())
    assert res["ok"] is False and res["kind"] == "http" and len(fake.calls) == 1 and c.retries == 3
