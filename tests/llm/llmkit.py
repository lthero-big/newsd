"""Fake model endpoints for httpx.MockTransport. No real API calls."""
import json

import httpx


def wrap(provider: str, text: str) -> dict:
    if provider == "anthropic":
        return {"id": "msg_1", "type": "message", "role": "assistant",
                "content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}
    if provider == "gemini":
        return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}]}
    if provider == "ollama":
        return {"model": "m", "message": {"role": "assistant", "content": text}, "done": True}
    return {"id": "chatcmpl-1", "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


def prompt_of(body: dict) -> tuple[str, str]:
    """(system, user) from any provider's request body."""
    if "messages" in body:
        sys_ = "".join(m["content"] for m in body["messages"] if m["role"] == "system")
        user = "".join(m["content"] for m in body["messages"] if m["role"] == "user")
        return body.get("system", "") + sys_, user
    if "contents" in body:
        sys_ = "".join(p["text"] for p in body.get("systemInstruction", {}).get("parts", []))
        return sys_, "".join(p["text"] for c in body["contents"] for p in c["parts"])
    return "", ""


class FakeLLM:
    """responder(system, user, body, request) -> str (reply text) | httpx.Response | dict (raw JSON body)."""

    def __init__(self, provider: str = "openai", responder=None):
        self.provider = provider
        self.responder = responder or (lambda *a: "OK")
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        system, user = prompt_of(body)
        self.calls.append({"url": str(request.url), "headers": dict(request.headers), "body": body,
                           "system": system, "user": user})
        out = self.responder(system, user, body, request)
        if isinstance(out, httpx.Response):
            return out
        if isinstance(out, dict):
            return httpx.Response(200, json=out)
        return httpx.Response(200, json=wrap(self.provider, out))

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


class Clock:
    """Fake monotonic clock + sleep: sleeping advances the clock instantly."""

    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.sleeps.append(round(s, 3))
        self.t += s
