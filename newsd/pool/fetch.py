"""Shared async HTTP client for the collection pool.

One ``Fetcher`` per cycle wraps a single ``httpx.AsyncClient`` and adds what every
outgoing request needs:

* SSRF guard: every hop (redirects are followed by hand) is checked with
  ``collect.check_url`` -- non-http schemes, credentials in the URL, local names
  and literal or resolved private/loopback/link-local/reserved addresses are refused.
* per-domain politeness: at most ``per_host`` concurrent requests per host and at
  least ``min_interval`` seconds between request starts on the same host.
* timeouts, bounded body size, and retries with exponential backoff for timeouts,
  connection errors, 429 and 5xx (``Retry-After`` honoured up to ``max_backoff``).
* single-flight: concurrent ``get`` calls for the same URL share one request.

``request_count`` / ``requests_by_url`` record real network requests (tests use them
to prove a source is fetched once per cycle).
"""
from __future__ import annotations

import asyncio
import os
import random
import time
import urllib.parse
from collections import Counter
from dataclasses import dataclass

import httpx

from newsd.collector.collect import FetchError, UnsafeURL, check_url, default_resolve, iri_to_uri

UA = "newsd-collector/0.2 (+self-hosted news digest)"
REDIRECTS = (301, 302, 303, 307, 308)
RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass
class Response:
    url: str
    final_url: str
    status: int
    content_type: str
    body: bytes
    truncated: bool = False

    def text(self) -> str:
        charset = "utf-8"
        ct = self.content_type.lower()
        if "charset=" in ct:
            charset = ct.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.body.decode(charset, "replace")
        except LookupError:
            return self.body.decode("utf-8", "replace")


class Fetcher:
    def __init__(
        self,
        *,
        timeout: float = 12.0,
        retries: int = 2,
        backoff: float = 0.5,
        max_backoff: float = 8.0,
        per_host: int = 2,
        min_interval: float = 1.0,
        max_bytes: int = 3_000_000,
        max_redirects: int = 3,
        resolve=None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep=asyncio.sleep,
        user_agent: str = UA,
    ):
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.max_backoff = max_backoff
        self.per_host = per_host
        self.min_interval = min_interval
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.resolve = resolve or default_resolve
        self.sleep = sleep
        self.client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            trust_env=False,  # never pick up proxy/netrc credentials from the environment
            headers={"User-Agent": user_agent, "Accept": "*/*"},
        )
        self._host_sem: dict[str, asyncio.Semaphore] = {}
        self._host_lock: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}
        self._inflight: dict[str, asyncio.Future] = {}
        self.request_count = 0
        self.requests_by_url: Counter[str] = Counter()
        self.github_token = os.environ.get("NEWSD_GITHUB_TOKEN", "").strip()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------------ public
    async def get(self, url: str) -> Response:
        """GET a public URL (single-flight per URL)."""
        key = iri_to_uri(url)
        fut = self._inflight.get(key)
        if fut is not None:
            return await asyncio.shield(fut)
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            result = await self._get(key)
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
                fut.exception()  # mark retrieved; waiters re-raise it
            raise
        else:
            fut.set_result(result)
            return result
        finally:
            self._inflight.pop(key, None)

    # ----------------------------------------------------------------- helpers
    async def _check(self, url: str) -> None:
        # DNS lookups block, so they run in a worker thread.
        await asyncio.to_thread(check_url, url, self.resolve)

    def _host(self, url: str) -> str:
        return (urllib.parse.urlsplit(url).hostname or "").lower()

    async def _pace(self, host: str) -> None:
        lock = self._host_lock.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._host_last.get(host)
            now = time.monotonic()
            if last is not None and self.min_interval > 0:
                wait = self.min_interval - (now - last)
                if wait > 0:
                    await self.sleep(wait)
            self._host_last[host] = time.monotonic()

    def _headers(self, url: str) -> dict:
        if self.github_token and self._host(url) == "api.github.com":
            return {"Authorization": f"Bearer {self.github_token}", "Accept": "application/vnd.github+json"}
        return {}

    async def _once(self, url: str) -> tuple[Response, str | None, str | None]:
        host = self._host(url)
        sem = self._host_sem.setdefault(host, asyncio.Semaphore(max(1, self.per_host)))
        async with sem:
            await self._pace(host)
            self.request_count += 1
            self.requests_by_url[url] += 1
            try:
                async with self.client.stream("GET", url, headers=self._headers(url)) as resp:
                    chunks, size, truncated = [], 0, False
                    if resp.status_code not in REDIRECTS:
                        async for chunk in resp.aiter_bytes():
                            chunks.append(chunk)
                            size += len(chunk)
                            if size > self.max_bytes:
                                truncated = True
                                break
                    body = b"".join(chunks)[: self.max_bytes]
                    return Response(
                        url=url,
                        final_url=str(resp.url),
                        status=resp.status_code,
                        content_type=resp.headers.get("content-type", ""),
                        body=body,
                        truncated=truncated,
                    ), resp.headers.get("location"), resp.headers.get("retry-after")
            except httpx.TimeoutException as exc:
                raise _Transient("timeout") from exc
            except httpx.TransportError as exc:
                raise _Transient(f"connect:{type(exc).__name__}") from exc

    def _delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(self.max_backoff, max(0.0, float(retry_after)))
            except ValueError:
                pass
        base = self.backoff * (2 ** attempt)
        return min(self.max_backoff, base + random.uniform(0, base / 4))

    async def _get(self, url: str) -> Response:
        current = url
        for _hop in range(self.max_redirects + 1):
            await self._check(current)
            attempt = 0
            while True:
                try:
                    resp, location, retry_after = await self._once(current)
                except _Transient as exc:
                    if attempt >= self.retries:
                        raise FetchError(exc.reason) from exc
                    await self.sleep(self._delay(attempt, None))
                    attempt += 1
                    continue
                if resp.status in RETRY_STATUS and attempt < self.retries:
                    await self.sleep(self._delay(attempt, retry_after))
                    attempt += 1
                    continue
                break
            if resp.status in REDIRECTS:
                if not location:
                    raise FetchError(f"redirect-without-location:{resp.status}")
                current = iri_to_uri(urllib.parse.urljoin(current, location))
                continue
            if resp.status >= 400:
                raise FetchError(f"http:{resp.status}")
            resp.url = url
            return resp
        raise FetchError("too-many-redirects")


class _Transient(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


__all__ = ["Fetcher", "Response", "FetchError", "UnsafeURL"]
