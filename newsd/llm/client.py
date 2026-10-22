"""Multi-provider LLM client with one interface (modeled on Horizon's ai client).

A model is configured with ``provider`` (a preset id from :mod:`newsd.llm.presets`), ``model``,
``api_key`` and optionally ``base`` -- a user-supplied base URL (self-hosted gateway / proxy) that
replaces the preset default. User URLs are checked by :mod:`newsd.llm.netguard` (no private or
internal addresses unless trusted) when saved and again before every request. An admin may override
a preset's default with ``NEWSD_LLM_BASE_<PROVIDER>`` (e.g. a corporate gateway, or where Ollama runs).

Wire formats (per preset): OpenAI ``/chat/completions`` (most providers), Anthropic ``/v1/messages``,
Gemini ``/v1beta/models/{model}:generateContent``, Ollama ``/api/chat`` (no key).

Every ``LLMClient`` enforces a request rate (requests per minute), a concurrency cap and retries
with exponential backoff (timeouts, connection errors, 429, 5xx; ``Retry-After`` honoured).
``complete_json`` parses the reply as JSON, asks the model to repair it once if it is not valid,
and returns ``None`` if it still is not (callers record that as an empty result).
``FailoverClient`` wraps several ``LLMClient`` s in priority order and moves to the next one when a
provider fails (after its own retries).
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

from . import presets as _presets

DEFAULT_BASES = {p.id: p.base for p in _presets.PRESETS.values()}
WIRE = {p.id: p.wire for p in _presets.PRESETS.values()}
OPENAI_WIRE = {p for p, w in WIRE.items() if w == "openai"}
JSON_MODE = {p.id for p in _presets.PRESETS.values() if p.json_mode}  # accept response_format=json_object
KEYLESS = {p.id for p in _presets.PRESETS.values() if p.keyless}
PROVIDERS = tuple(DEFAULT_BASES)
PROVIDER_LABELS = {p.id: p.name for p in _presets.PRESETS.values()}
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
    base: str = ""  # user-supplied base URL (overrides the preset default); checked by netguard
    trusted: bool = False  # skip the private-address check for ``base`` (admin-owned config)

    @property
    def wire(self) -> str:
        return WIRE.get(self.provider, "")

    def effective_base(self) -> str:
        return (self.base or base_url(self.provider)).rstrip("/")

    def validate(self) -> None:
        if self.provider not in DEFAULT_BASES:
            raise LLMError("config", f"unknown provider {self.provider!r}")
        if not self.model or len(self.model) > 128 or not re.fullmatch(r"[\w.:/@+\-]+", self.model) or ".." in self.model:
            raise LLMError("config", "model name is missing or invalid")
        if self.provider == "gemini" and "/" in self.model:
            raise LLMError("config", "model name is missing or invalid")
        if self.provider not in KEYLESS and not self.api_key:
            raise LLMError("config", "api key is required for this provider")
        if self.base:
            from .netguard import normalize
            self.base = normalize(self.base)
        elif not base_url(self.provider):
            if self.provider == "custom":
                raise LLMError("config", "a custom provider needs a base URL")
            raise LLMError("config", f"provider {self.provider} has no base URL; the admin must set "
                                     f"NEWSD_LLM_BASE_{self.provider.upper()}")

    def __repr__(self) -> str:  # never print the key
        return (f"ModelConfig(provider={self.provider!r}, model={self.model!r}, "
                f"api_key={'***' if self.api_key else ''!r}, base={self.base!r})")


def base_url(provider: str) -> str:
    return (os.environ.get(f"NEWSD_LLM_BASE_{provider.upper()}") or DEFAULT_BASES.get(provider, "")).rstrip("/")


# ------------------------------------------------------------------------------------------- wire formats

def build_request(cfg: ModelConfig, system: str, user: str, *, json_mode: bool, max_tokens: int,
                  temperature: float) -> tuple[str, dict, dict]:
    """Return (url, headers, json body) for one chat completion."""
    base, p, wire = cfg.effective_base(), cfg.provider, cfg.wire
    if wire == "openai":
        body = {"model": cfg.model, "temperature": temperature, "max_tokens": max_tokens,
                "messages": ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]}
        if json_mode and p in JSON_MODE:
            body["response_format"] = {"type": "json_object"}
        return f"{base}/chat/completions", {"Authorization": f"Bearer {cfg.api_key}"}, body
    if wire == "anthropic":
        body = {"model": cfg.model, "max_tokens": max_tokens, "temperature": temperature,
                "messages": [{"role": "user", "content": user}]}
        if system:
            body["system"] = system
        root = base if base.endswith("/v1") else base + "/v1"
        return f"{root}/messages", {"x-api-key": cfg.api_key, "anthropic-version": ANTHROPIC_VERSION}, body
    if wire == "gemini":
        gen = {"temperature": temperature, "maxOutputTokens": max_tokens}
        if json_mode:
            gen["responseMimeType"] = "application/json"
        body = {"contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": gen}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        root = base[:-len("/openai")] if base.endswith("/openai") else base
        if not re.search(r"/v1(beta)?$", root):
            root += "/v1beta"
        return f"{root}/models/{cfg.model}:generateContent", {"x-goog-api-key": cfg.api_key}, body
    if wire == "ollama":
        body = {"model": cfg.model, "stream": False, "options": {"temperature": temperature, "num_predict": max_tokens},
                "messages": ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]}
        if json_mode:
            body["format"] = "json"
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        root = base[:-len("/v1")] if base.endswith("/v1") else base
        return f"{root}/api/chat", headers, body
    raise LLMError("config", f"unknown provider {p!r}")


def parse_response(provider: str, data: dict) -> str:
    wire = WIRE.get(provider, provider)
    try:
        if wire == "openai":
            return data["choices"][0]["message"]["content"] or ""
        if wire == "anthropic":
            return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        if wire == "gemini":
            return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        if wire == "ollama":
            return data["message"]["content"] or ""
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise LLMError("bad_response", f"unexpected response shape ({type(exc).__name__})") from exc
    raise LLMError("config", f"unknown provider {provider!r}")


def parse_tokens(provider: str, data: dict) -> tuple[int, int]:
    """(input, output) tokens reported by the provider for one response; (0, 0) when it reports none."""
    wire = WIRE.get(provider, provider)
    try:
        if wire == "openai":
            u = data.get("usage") or {}
            return int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
        if wire == "anthropic":
            u = data.get("usage") or {}
            return (int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
                    + int(u.get("cache_creation_input_tokens") or 0), int(u.get("output_tokens") or 0))
        if wire == "gemini":
            u = data.get("usageMetadata") or {}
            return int(u.get("promptTokenCount") or 0), int(u.get("candidatesTokenCount") or 0) + int(u.get("thoughtsTokenCount") or 0)
        if wire == "ollama":
            return int(data.get("prompt_eval_count") or 0), int(data.get("eval_count") or 0)
    except (AttributeError, TypeError, ValueError):
        pass
    return 0, 0


def models_request(cfg: "ModelConfig") -> tuple[str, dict]:
    """(url, headers) of the provider's model-list endpoint."""
    base, wire = cfg.effective_base(), cfg.wire
    if wire == "openai":
        return f"{base}/models", ({"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {})
    if wire == "anthropic":
        root = base if base.endswith("/v1") else base + "/v1"
        return f"{root}/models?limit=1000", {"x-api-key": cfg.api_key, "anthropic-version": ANTHROPIC_VERSION}
    if wire == "gemini":
        root = base[:-len("/openai")] if base.endswith("/openai") else base
        if not re.search(r"/v1(beta)?$", root):
            root += "/v1beta"
        return f"{root}/models?pageSize=1000", {"x-goog-api-key": cfg.api_key}
    if wire == "ollama":
        root = base[:-len("/v1")] if base.endswith("/v1") else base
        return f"{root}/api/tags", ({"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {})
    raise LLMError("config", f"unknown provider {cfg.provider!r}")


def parse_models(provider: str, data) -> list[str]:
    """Model ids from a model-list response (chat-capable ones only where the provider says so), sorted, unique."""
    wire = WIRE.get(provider, provider)
    out: list[str] = []
    rows = []
    if isinstance(data, dict):
        rows = data.get("data") if wire in ("openai", "anthropic") else data.get("models") if wire in ("gemini", "ollama") else None
        if rows is None:
            rows = data.get("data") or data.get("models") or []
    elif isinstance(data, list):
        rows = data
    for r in rows or []:
        if isinstance(r, str):
            mid = r
        elif isinstance(r, dict):
            if wire == "gemini":
                methods = r.get("supportedGenerationMethods")
                if methods and "generateContent" not in methods:
                    continue
                mid = str(r.get("name") or "").removeprefix("models/")
            else:
                mid = r.get("id") or r.get("name") or r.get("model") or ""
        else:
            continue
        mid = str(mid).strip()
        if mid and len(mid) <= 128 and mid not in out:
            out.append(mid)
    return sorted(out, key=str.lower)


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
    failovers: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def public(self) -> dict:
        return {"requests": self.requests, "retries": self.retries, "repairs": self.repairs,
                "invalid_json": self.invalid_json, "errors": dict(self.errors), "failovers": self.failovers,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


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

    async def _guard(self, url: str) -> None:
        """Re-check a user-supplied base URL right before sending (its DNS may have changed)."""
        if not self.cfg.base or self.cfg.trusted:
            return
        from . import netguard
        try:
            await asyncio.to_thread(netguard.check, url)
        except LLMError:
            self._note_error("config")
            raise

    async def _post(self, url: str, headers: dict, body: dict) -> dict:
        attempt = 0
        while True:
            await self._guard(url)
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
        tin, tout = parse_tokens(self.cfg.provider, data)
        self.usage.input_tokens += tin
        self.usage.output_tokens += tout
        return parse_response(self.cfg.provider, data)

    async def list_models(self) -> list[str]:
        """The provider's model ids (one GET, no retries)."""
        url, headers = models_request(self.cfg)
        await self._guard(url)
        try:
            resp = await self.client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMError("network", "timeout") from exc
        except httpx.TransportError as exc:
            raise LLMError("network", f"connection error ({type(exc).__name__})") from exc
        if resp.status_code >= 300:
            kind = "auth" if resp.status_code in (401, 403) else "rate_limit" if resp.status_code == 429 else "http"
            raise LLMError(kind, f"HTTP {resp.status_code}: {_error_detail(resp)}", resp.status_code)
        try:
            return parse_models(self.cfg.provider, resp.json())
        except ValueError as exc:
            raise LLMError("bad_response", "provider returned a non-JSON body") from exc

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


class FailoverClient:
    """Several providers in priority order behind the ``LLMClient`` interface.

    Each request goes to the first provider that is not cooling down; when it fails (after that
    client's own retries) the provider cools down for ``cooldown`` seconds and the request moves on
    to the next one. If every provider fails, the last error is raised. ``label`` names the provider
    that served the latest request (the primary until something has been served)."""

    def __init__(self, clients: list[LLMClient], *, cooldown: float = 300.0, clock=time.monotonic):
        if not clients:
            raise LLMError("config", "no model provider is enabled")
        self.clients = list(clients)
        self.cooldown, self.clock = cooldown, clock
        self._down: dict[int, float] = {}
        self._current = 0
        self._own = Usage()
        self.events: list[dict] = []  # {"from", "to", "kind"} per failover

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self) -> None:
        for c in self.clients:
            await c.aclose()

    @property
    def label(self) -> str:
        return self.clients[self._current].label

    @property
    def labels(self) -> list[str]:
        return [c.label for c in self.clients]

    @property
    def usage(self) -> Usage:
        u = Usage(repairs=self._own.repairs, invalid_json=self._own.invalid_json, failovers=self._own.failovers)
        for c in self.clients:
            u.requests += c.usage.requests
            u.retries += c.usage.retries
            u.repairs += c.usage.repairs
            u.invalid_json += c.usage.invalid_json
            u.input_tokens += c.usage.input_tokens
            u.output_tokens += c.usage.output_tokens
            for k, v in c.usage.errors.items():
                u.errors[k] = u.errors.get(k, 0) + v
        return u

    def _order(self) -> list[int]:
        now = self.clock()
        up = [i for i in range(len(self.clients)) if self._down.get(i, 0) <= now]
        down = sorted((i for i in range(len(self.clients)) if i not in up), key=lambda i: self._down[i])
        return up + down  # if all are cooling down, still try them (soonest-recovering first)

    async def complete(self, system: str, user: str, **kw) -> str:
        err: LLMError | None = None
        order = self._order()
        for n, i in enumerate(order):
            try:
                out = await self.clients[i].complete(system, user, **kw)
            except LLMError as exc:
                err = exc
                self._down[i] = self.clock() + self.cooldown
                if n + 1 < len(order):
                    self._own.failovers += 1
                    self.events.append({"from": self.clients[i].label, "to": self.clients[order[n + 1]].label,
                                        "kind": exc.kind})
                continue
            self._down.pop(i, None)
            self._current = i
            return out
        raise err

    async def complete_json(self, system: str, user: str, *, max_tokens: int = 2048, temperature: float = 0.0):
        text = await self.complete(system, user, json_mode=True, max_tokens=max_tokens, temperature=temperature)
        try:
            return loads_lenient(text)
        except ValueError:
            pass
        self._own.repairs += 1
        fixed = await self.complete(REPAIR_SYSTEM, f"Fix this into valid JSON:\n\n{(text or '')[:12000]}",
                                    json_mode=True, max_tokens=max_tokens, temperature=0.0)
        try:
            return loads_lenient(fixed)
        except ValueError:
            self._own.invalid_json += 1
            return None
