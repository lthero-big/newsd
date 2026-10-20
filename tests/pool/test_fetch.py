import asyncio
import time

import httpx
import pytest

from newsd.collector.collect import FetchError, UnsafeURL
from poolkit import make_fetcher


def run(coro):
    return asyncio.run(coro)


async def _get(fetcher, url):
    async with fetcher:
        return await fetcher.get(url)


def test_get_ok(net):
    net.routes["https://a.example/feed"] = (200, "application/rss+xml", "<rss/>")
    r = run(_get(make_fetcher(net), "https://a.example/feed"))
    assert r.status == 200 and r.body == b"<rss/>" and "rss" in r.content_type


@pytest.mark.parametrize("url,reason", [
    ("http://127.0.0.1/x", "private-ip"),
    ("http://10.1.2.3/x", "private-ip"),
    ("http://[::1]/x", "private-ip"),
    ("http://169.254.169.254/latest/meta-data", "private-ip"),
    ("http://localhost:8080/", "local-host"),
    ("file:///etc/passwd", "non-http:file"),
    ("https://user:pw@a.example/", "credentials-in-url"),
])
def test_ssrf_literal_and_scheme(net, url, reason):
    with pytest.raises(UnsafeURL) as exc:
        run(_get(make_fetcher(net), url))
    assert exc.value.reason == reason
    assert net.calls == []  # refused before any request


def test_ssrf_resolves_private(net):
    f = make_fetcher(net, resolve=lambda host: ["192.168.1.61"])
    with pytest.raises(UnsafeURL) as exc:
        run(_get(f, "https://intranet.example/"))
    assert exc.value.reason == "resolves-private" and net.calls == []


def test_ssrf_redirect_to_private_is_blocked(net):
    net.routes["https://a.example/r"] = (302, "text/html", "", {"location": "http://127.0.0.1:7861/admin"})
    with pytest.raises(UnsafeURL):
        run(_get(make_fetcher(net), "https://a.example/r"))
    assert net.calls == ["https://a.example/r"]


def test_redirect_followed_and_capped(net):
    net.routes["https://a.example/1"] = (301, "text/html", "", {"location": "/2"})
    net.routes["https://a.example/2"] = (200, "text/plain", "done")
    assert run(_get(make_fetcher(net), "https://a.example/1")).body == b"done"
    loop = {f"https://b.example/{i}": (302, "text/html", "", {"location": f"/{i + 1}"}) for i in range(10)}
    net.routes.update(loop)
    with pytest.raises(FetchError) as exc:
        run(_get(make_fetcher(net, max_redirects=3), "https://b.example/0"))
    assert exc.value.reason == "too-many-redirects"


def test_retry_on_503_then_ok(net):
    state = {"n": 0}

    def flaky(_req):
        state["n"] += 1
        return (503, "text/plain", "busy") if state["n"] < 3 else (200, "text/plain", "ok")

    net.routes["https://a.example/f"] = flaky
    assert run(_get(make_fetcher(net, retries=2), "https://a.example/f")).body == b"ok"
    assert state["n"] == 3


def test_retry_exhausted_gives_http_error(net):
    net.routes["https://a.example/f"] = (500, "text/plain", "x")
    with pytest.raises(FetchError) as exc:
        run(_get(make_fetcher(net, retries=1), "https://a.example/f"))
    assert exc.value.reason == "http:500" and net.count("https://a.example/f") == 2


def test_timeout_is_retried_then_reported(net):
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    net.routes["https://a.example/slow"] = boom
    with pytest.raises(FetchError) as exc:
        run(_get(make_fetcher(net, retries=2), "https://a.example/slow"))
    assert exc.value.reason == "timeout" and net.count("https://a.example/slow") == 3


def test_404_not_retried(net):
    with pytest.raises(FetchError) as exc:
        run(_get(make_fetcher(net, retries=3), "https://a.example/missing"))
    assert exc.value.reason == "http:404" and len(net.calls) == 1


def test_body_capped(net):
    net.routes["https://a.example/big"] = (200, "text/plain", "x" * 5000)
    r = run(_get(make_fetcher(net, max_bytes=1000), "https://a.example/big"))
    assert len(r.body) == 1000 and r.truncated


def test_single_flight_same_url(net):
    net.routes["https://a.example/feed"] = (200, "text/plain", "ok")

    async def go():
        async with make_fetcher(net) as f:
            rs = await asyncio.gather(*(f.get("https://a.example/feed") for _ in range(5)))
            return rs, f.request_count

    rs, n = run(go())
    assert all(r.body == b"ok" for r in rs) and n == 1 and net.count("https://a.example/feed") == 1


def test_per_domain_rate_limit(net):
    for i in range(3):
        net.routes[f"https://slow.example/{i}"] = (200, "text/plain", "ok")
        net.routes[f"https://other.example/{i}"] = (200, "text/plain", "ok")
    waits = []

    async def fake_sleep(s):
        waits.append(s)

    async def go():
        async with make_fetcher(net, min_interval=0.5, per_host=1, sleep=fake_sleep) as f:
            await asyncio.gather(*(f.get(f"https://slow.example/{i}") for i in range(3)))
            await f.get("https://other.example/0")  # a different host is not throttled by slow.example

    run(go())
    # first request on a host goes straight away; the next two wait ~min_interval each
    assert len(waits) == 2 and all(0.4 < w <= 0.5 for w in waits)


def test_real_spacing_with_asyncio_sleep(net):
    for i in range(3):
        net.routes[f"https://pace.example/{i}"] = (200, "text/plain", "ok")

    async def go():
        async with make_fetcher(net, min_interval=0.05, per_host=4, sleep=asyncio.sleep) as f:
            t = time.monotonic()
            await asyncio.gather(*(f.get(f"https://pace.example/{i}") for i in range(3)))
            return time.monotonic() - t

    assert run(go()) >= 0.09
