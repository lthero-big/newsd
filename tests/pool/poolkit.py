import httpx

PUBLIC = "93.184.216.34"

ARTICLE = """<!doctype html><html><head><title>Qwen ships a new open model</title>
<meta property="og:title" content="Qwen ships a new open model">
<meta property="og:image" content="https://cdn.example.com/img/qwen-launch-hero.jpg">
<meta property="og:site_name" content="Example News">
<meta name="author" content="Jane Writer">
<meta property="article:published_time" content="2026-10-19T07:30:00Z">
</head><body><nav>Home | World | Tech</nav><article><h1>Qwen ships a new open model</h1>
<p>The Qwen team released a new open-weights language model on Monday, with a permissive license and a long context window.</p>
<p>According to the announcement, the model beats the previous generation on coding and math benchmarks while using less memory at inference time.</p>
<p>Weights are available on Hugging Face, and the team published a technical report describing the training data mix and evaluation setup.</p>
</article><footer>(c) Example News</footer></body></html>"""

JS_SHELL = """<html><head><title>App</title></head><body><noscript>You need to enable JavaScript to run this app.</noscript>
<div id="root"></div><script src="/static/js/main.js"></script></body></html>"""


def rss(items, age_hours: float = 1):
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    when = format_datetime(datetime.now(timezone.utc) - timedelta(hours=age_hours), usegmt=True)
    body = "".join(
        f"<item><title>{t}</title><link>{u}</link><pubDate>{when}</pubDate><description>{d}</description></item>"
        for t, u, d in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{body}</channel></rss>'


class Net:
    """Fake internet for httpx.MockTransport: routes url -> (status, content-type, body, headers)."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        route = self.routes.get(url)
        if route is None:
            return httpx.Response(404, text="nope")
        if callable(route):
            route = route(request)
        status, ctype, body, headers = (list(route) + [{}])[:4]
        return httpx.Response(status, headers={"content-type": ctype, **headers}, content=body.encode() if isinstance(body, str) else body)

    def count(self, url):
        return sum(1 for c in self.calls if c == url)


def make_fetcher(net, **kw):
    from newsd.pool.fetch import Fetcher

    async def no_sleep(_s):
        return None

    kw.setdefault("min_interval", 0)
    kw.setdefault("sleep", no_sleep)
    kw.setdefault("resolve", lambda host: [PUBLIC])
    return Fetcher(transport=httpx.MockTransport(net.handler), **kw)
