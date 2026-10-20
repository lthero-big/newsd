"""Multi-provider LLM client with one interface (modeled on Horizon's ai client).

A model is configured with three things only: ``provider``, ``model`` and ``api_key``. Base URLs are
fixed per provider; an admin may override one with ``NEWSD_LLM_BASE_<PROVIDER>`` (e.g. a corporate
gateway, or where Ollama runs). Users never supply URLs, so a user-provided config cannot point the
server at an internal address.

Providers:
  openai, deepseek, tongyi, doubao, openai_compatible  -> OpenAI ``/chat/completions`` wire format
  anthropic                                           -> ``/v1/messages``
  gemini                                              -> ``/v1beta/models/{model}:generateContent``
  ollama                                              -> ``/api/chat`` (no key)

Every ``LLMClient`` enforces a request rate (requests per minute), a concurrency cap and retries
with exponential backoff (timeouts, connection errors, 429, 5xx; ``Retry-After`` honoured).
``complete_json`` parses the reply as JSON, asks the model to repair it once if it is not valid,
and returns ``None`` if it still is not (callers record that as an empty result).
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from dataclasses import dataclass, field

import httpx

DEFAULT_BASES = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "tongyi": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "doubao": "https://ark.cn-beijing.volces.com/api/v3",
    "openai_compatible": "",  # admin must set NEWSD_LLM_BASE_OPENAI_COMPATIBLE
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "ollama": "http://localhost:11434",
}
OPENAI_WIRE = {"openai", "deepseek", "tongyi", "doubao", "openai_compatible"}
JSON_MODE = {"openai", "deepseek", "tongyi"}  # providers known to accept response_format=json_object
KEYLESS = {"ollama"}
PROVIDERS = tuple(DEFAULT_BASES)
PROVIDER_LABELS = {
    "openai": "OpenAI", "deepseek": "DeepSeek", "tongyi": "通义千问 (DashScope)", "doubao": "豆包 (火山方舟)",
    "openai_compatible": "OpenAI 兼容（管理员配置的地址）", "anthropic": "Anthropic Claude", "gemini": "Google Gemini",
    "ollama": "Ollama（本地）",
}
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
ANTHROPIC_VERSION = "2023-06-01"


class LLMError(Exception):
    """kind: config | auth | rate_limit | http | network | bad_response. Messages never contain the key."""

    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status

    def __str__(self) -> str:
        return f"{self.kind}: {self.args[0]}"


@dataclass
class ModelConfig:
    provider: str
    model: str
    api_key: str = ""

    def validate(self) -> None:
        if self.provider not in DEFAULT_BASES:
            raise LLMError("config", f"unknown provider {self.provider!r}")
        if not self.model or len(self.model) > 128 or not re.fullmatch(r"[\w.:/@+\-]+", self.model) or ".." in self.model:
            raise LLMError("config", "model name is missing or invalid")
        if self.provider == "gemini" and "/" in self.model:
            raise LLMError("config", "model name is missing or invalid")
        if self.provider not in KEYLESS and not self.api_key:
            raise LLMError("config", "api key is required for this provider")
        if not base_url(self.provider):
            raise LLMError("config", f"provider {self.provider} has no base URL; the admin must set "
                                     f"NEWSD_LLM_BASE_{self.provider.upper()}")

    def __repr__(self) -> str:  # never print the key
        return f"ModelConfig(provider={self.provider!r}, model={self.model!r}, api_key={'***' if self.api_key else ''!r})"


def base_url(provider: str) -> str:
    return (os.environ.get(f"NEWSD_LLM_BASE_{provider.upper()}") or DEFAULT_BASES.get(provider, "")).rstrip("/")


# ------------------------------------------------------------------------------------------- wire formats

def build_request(cfg: ModelConfig, system: str, user: str, *, json_mode: bool, max_tokens: int,
                  temperature: float) -> tuple[str, dict, dict]:
    """Return (url, headers, json body) for one chat completion."""
    base, p = base_url(cfg.provider), cfg.provider
    if p in OPENAI_WIRE:
        body = {"model": cfg.model, "temperature": temperature, "max_tokens": max_tokens,
                "messages": ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]}
        if json_mode and p in JSON_MODE:
            body["response_format"] = {"type": "json_object"}
        return f"{base}/chat/completions", {"Authorization": f"Bearer {cfg.api_key}"}, body
    if p == "anthropic":
        body = {"model": cfg.model, "max_tokens": max_tokens, "temperature": temperature,
                "messages": [{"role": "user", "content": user}]}
        if system:
            body["system"] = system
        return f"{base}/v1/messages", {"x-api-key": cfg.api_key, "anthropic-version": ANTHROPIC_VERSION}, body
    if p == "gemini":
        gen = {"temperature": temperature, "maxOutputTokens": max_tokens}
        if json_mode:
            gen["responseMimeType"] = "application/json"
        body = {"contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": gen}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        return f"{base}/v1beta/models/{cfg.model}:generateContent", {"x-goog-api-key": cfg.api_key}, body
    if p == "ollama":
        body = {"model": cfg.model, "stream": False, "options": {"temperature": temperature, "num_predict": max_tokens},
                "messages": ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]}
        if json_mode:
            body["format"] = "json"
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        return f"{base}/api/chat", headers, body
    raise LLMError("config", f"unknown provider {p!r}")


def parse_response(provider: str, data: dict) -> str:
    try:
        if provider in OPENAI_WIRE:
            return data["choices"][0]["message"]["content"] or ""
        if provider == "anthropic":
            return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        if provider == "gemini":
            return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        if provider == "ollama":
            return data["message"]["content"] or ""
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise LLMError("bad_response", f"unexpected response shape ({type(exc).__name__})") from exc
    raise LLMError("config", f"unknown provider {provider!r}")


def _error_detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error") if isinstance(data, dict) else None
        msg = err.get("message") if isinstance(err, dict) else (err if isinstance(err, str) else None)
        if msg:
            return str(msg)[:200]
    except ValueError:
        pass
    return resp.reason_phrase or "error"


# ------------------------------------------------------------------------------------------- JSON helpers

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S | re.I)


def loads_lenient(text: str):
    """Parse model output as JSON: plain, inside a ```json fence, or the outermost {...}/[...] span."""
    if not text:
        raise ValueError("empty")
    s = text.strip()
    m = _FENCE.match(s)
    if m:
        s = m.group(1)
    try:
        return json.loads(s)
    except ValueError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = s.find(open_c), s.rfind(close_c)
        if i != -1 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except ValueError:
                continue
    raise ValueError("not json")


REPAIR_SYSTEM = "You fix malformed JSON. Reply with the corrected JSON only: no prose, no code fences."


# ------------------------------------------------------------------------------------------- client

@dataclass
class Usage:
    requests: int = 0
    retries: int = 0
    repairs: int = 0
    invalid_json: int = 0
    errors: dict = field(default_factory=dict)


class RateLimiter:
    """Spaces request starts so at most ``rpm`` begin per minute (0 = unlimited)."""

    def __init__(self, rpm: float, sleep=asyncio.sleep, clock=time.monotonic):
        self.interval = 60.0 / rpm if rpm and rpm > 0 else 0.0
        self.sleep, self.clock = sleep, clock
        self._lock = asyncio.Lock()
        self._next: float | None = None
        self.waited = 0.0

    async def acquire(self) -> None:
        if not self.interval:
            return
        async with self._lock:
            now = self.clock()
            start = now if self._next is None else max(now, self._next)
            if start > now:
                self.waited += start - now
                await self.sleep(start - now)
            self._next = start + self.interval


class LLMClient:
    def __init__(self, cfg: ModelConfig, *, rpm: float | None = None, concurrency: int | None = None,
                 retries: int = 3, backoff: float = 1.0, max_backoff: float = 30.0, timeout: float = 60.0,
                 transport: httpx.AsyncBaseTransport | None = None, sleep=asyncio.sleep, clock=time.monotonic):
        cfg.validate()
        self.cfg = cfg
        self.retries, self.backoff, self.max_backoff = retries, backoff, max_backoff
        self.sleep = sleep
        rpm = float(os.environ.get("NEWSD_LLM_RPM", "30")) if rpm is None else rpm
        concurrency = int(os.environ.get("NEWSD_LLM_CONCURRENCY", "3")) if concurrency is None else concurrency
        self.limiter = RateLimiter(rpm, sleep=sleep, clock=clock)
        self.max_concurrency = max(1, concurrency)
        self.sem = asyncio.Semaphore(self.max_concurrency)
        self.in_flight = 0
        self.peak_in_flight = 0
        self.usage = Usage()
        self.client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(timeout), trust_env=False,
                                        follow_redirects=False, headers={"User-Agent": "newsd-llm/0.1"})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self) -> None:
        await self.client.aclose()

    @property
    def label(self) -> str:
        return f"{self.cfg.provider}/{self.cfg.model}"

    def _delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(self.max_backoff, max(0.0, float(retry_after)))
            except ValueError:
                pass
        base = self.backoff * (2 ** attempt)
        return min(self.max_backoff, base + random.uniform(0, base / 4))

    def _note_error(self, kind: str) -> None:
        self.usage.errors[kind] = self.usage.errors.get(kind, 0) + 1

    async def _post(self, url: str, headers: dict, body: dict) -> dict:
        attempt = 0
        while True:
            await self.limiter.acquire()
            resp, err = None, None
            async with self.sem:
                self.in_flight += 1
                self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
                self.usage.requests += 1
                try:
                    resp = await self.client.post(url, headers=headers, json=body)
                except httpx.TimeoutException:
                    err = LLMError("network", "timeout")
                except httpx.TransportError as exc:
                    err = LLMError("network", f"connection error ({type(exc).__name__})")
                finally:
                    self.in_flight -= 1
            retry_after = None
            if resp is not None:
                if resp.status_code < 300:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        self._note_error("bad_response")
                        raise LLMError("bad_response", "provider returned a non-JSON body") from exc
                retry_after = resp.headers.get("retry-after")
                kind = "auth" if resp.status_code in (401, 403) else "rate_limit" if resp.status_code == 429 else "http"
                err = LLMError(kind, f"HTTP {resp.status_code}: {_error_detail(resp)}", resp.status_code)
                if resp.status_code not in RETRY_STATUS:
                    self._note_error(kind)
                    raise err
            if attempt >= self.retries:
                self._note_error(err.kind)
                raise err
            self.usage.retries += 1
            await self.sleep(self._delay(attempt, retry_after))
            attempt += 1

    async def complete(self, system: str, user: str, *, json_mode: bool = False, max_tokens: int = 1024,
                       temperature: float = 0.2) -> str:
        url, headers, body = build_request(self.cfg, system, user, json_mode=json_mode, max_tokens=max_tokens,
                                           temperature=temperature)
        data = await self._post(url, headers, body)
        return parse_response(self.cfg.provider, data)

    async def complete_json(self, system: str, user: str, *, max_tokens: int = 2048, temperature: float = 0.0):
        """Return parsed JSON, repairing invalid output once; None if it is still invalid.
        Network/HTTP errors still raise LLMError (callers decide whether to skip)."""
        text = await self.complete(system, user, json_mode=True, max_tokens=max_tokens, temperature=temperature)
        try:
            return loads_lenient(text)
        except ValueError:
            pass
        self.usage.repairs += 1
        fixed = await self.complete(REPAIR_SYSTEM, f"Fix this into valid JSON:\n\n{(text or '')[:12000]}",
                                    json_mode=True, max_tokens=max_tokens, temperature=0.0)
        try:
            return loads_lenient(fixed)
        except ValueError:
            self.usage.invalid_json += 1
            return None

    async def test_connection(self) -> dict:
        """One tiny request, no retries. Returns {ok, latency_ms, reply} or {ok: False, kind, error}."""
        saved, self.retries = self.retries, 0
        t0 = time.monotonic()
        try:
            reply = await self.complete("", "Reply with the single word: OK", max_tokens=16, temperature=0.0)
            return {"ok": True, "latency_ms": int((time.monotonic() - t0) * 1000), "reply": reply.strip()[:80],
                    "provider": self.cfg.provider, "model": self.cfg.model}
        except LLMError as exc:
            return {"ok": False, "kind": exc.kind, "error": exc.args[0], "provider": self.cfg.provider,
                    "model": self.cfg.model}
        finally:
            self.retries = saved
