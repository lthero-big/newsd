import asyncio

from sqlalchemy import func, select

from newsd.models import Extraction, PoolItem, PoolItemSource, Source, SourceFetch, Subscription, User
from newsd.pool import cycle_key, item_text, run_cycle, user_items
from poolkit import ARTICLE, JS_SHELL, make_fetcher, rss

FEED = "https://feeds.example/ai.xml"
FEED2 = "https://other.example/rss"
LONG = "A detailed summary of the release that is long enough to be distinctive for content hashing. " * 3


def _users(db, n):
    users = [User(username=f"u{i}", password_hash="x") for i in range(n)]
    db.add_all(users); db.commit()
    return users


def _source(db, key, url, extractor=False, params=None, kind="rss"):
    s = Source(key=key, kind=kind, name=key, url=url, config={}, content_extractor=extractor, extractor_params=params)
    db.add(s); db.commit()
    return s


def _sub(db, users, *sources):
    for u in users:
        for s in sources:
            db.add(Subscription(user_id=u.id, source_id=s.id))
    db.commit()


def cycle(db, net, key, **kw):
    async def go():
        async with make_fetcher(net) as f:
            return await run_cycle(db, key, fetcher=f, **kw)

    return asyncio.run(go())


def test_cycle_key_buckets():
    from datetime import datetime, timezone

    t = datetime(2026, 10, 20, 9, 47, tzinfo=timezone.utc)
    assert cycle_key(t) == "2026-10-20T09:00"
    assert cycle_key(t, period_minutes=30) == "2026-10-20T09:30"
    assert cycle_key(t, period_minutes=240) == "2026-10-20T08:00"


def test_many_subscribers_one_request_per_cycle(db, net):
    net.routes[FEED] = (200, "application/rss+xml", rss([("A", "https://news.example/a", "a"), ("B", "https://news.example/b", "b")]))
    users = _users(db, 5)
    src = _source(db, "rss:ai", FEED)
    _sub(db, users, src)
    r1 = cycle(db, net, "2026-10-20T09:00", extract=False)
    assert net.count(FEED) == 1 and r1["fetched_sources"] == 1 and r1["new_items"] == 2
    # same cycle again (another worker / a re-run): served from the pool, no request
    r2 = cycle(db, net, "2026-10-20T09:00", extract=False)
    assert net.count(FEED) == 1 and r2["sources"]["rss:ai"] == "shared" and r2["requests"] == 0
    # every subscriber sees the same pooled items
    for u in users:
        assert {i.canonical_url for i in user_items(db, u.id)} == {"https://news.example/a", "https://news.example/b"}
    # next cycle fetches again, items are not duplicated
    r3 = cycle(db, net, "2026-10-20T10:00", extract=False)
    assert net.count(FEED) == 2 and r3["new_items"] == 0 and r3["seen_items"] == 2
    assert db.scalar(select(func.count()).select_from(PoolItem)) == 2
    assert db.scalar(select(func.count()).select_from(SourceFetch)) == 2


def test_unsubscribed_or_disabled_sources_are_not_fetched(db, net):
    net.routes[FEED] = (200, "application/rss+xml", rss([]))
    net.routes[FEED2] = (200, "application/rss+xml", rss([]))
    (u,) = _users(db, 1)
    a = _source(db, "rss:a", FEED)
    _source(db, "rss:nobody", FEED2)
    _sub(db, [u], a)
    off = _source(db, "rss:off", "https://off.example/rss")
    off.enabled = False; db.commit()
    _sub(db, [u], off)
    cycle(db, net, "c1", extract=False)
    assert net.calls == [FEED]


def test_dedupe_by_normalized_url_and_content_hash(db, net):
    net.routes[FEED] = (200, "application/rss+xml", rss([
        ("Model X released", "https://news.example/x?utm_source=rss", LONG),
        ("Model X released", "https://mirror.example/x", LONG),  # syndicated copy: same title + text
        ("Model X released", "https://other-site.example/x", "short text is not hashed"),
    ]))
    net.routes[FEED2] = (200, "application/rss+xml", rss([("Model X released", "https://www.news.example/x/", "short")]))
    users = _users(db, 2)
    a, b = _source(db, "rss:a", FEED), _source(db, "rss:b", FEED2)
    _sub(db, users[:1], a); _sub(db, users[1:], b)
    cycle(db, net, "c1", extract=False)
    items = db.scalars(select(PoolItem)).all()
    assert len(items) == 2  # utm/www/trailing slash normalised; mirror merged by content hash; short text never merged
    items = [i for i in items if i.canonical_url != "https://other-site.example/x"]
    it = items[0]
    assert it.canonical_url == "https://news.example/x" and it.first_source_id == a.id
    links = db.scalars(select(PoolItemSource).where(PoolItemSource.item_id == it.id)).all()
    assert {l.source_id for l in links} == {a.id, b.id} and all(l.cycle == "c1" and l.fetched_at for l in links)
    assert it.published_at is not None and it.first_seen_at is not None


def test_extraction_success_metadata_and_shared_cache(db, net):
    url = "https://news.example/qwen"
    net.routes[FEED] = (200, "application/rss+xml", rss([("Qwen", url, "short teaser")]))
    net.routes[FEED2] = (200, "application/rss+xml", rss([("Qwen (via other)", url + "?utm_medium=x", "teaser")]))
    net.routes[url] = (200, "text/html; charset=utf-8", ARTICLE)
    users = _users(db, 3)
    a = _source(db, "rss:a", FEED, extractor=True)
    b = _source(db, "rss:b", FEED2, extractor=True, params={"favor_precision": True})
    _sub(db, users, a, b)
    r = cycle(db, net, "c1")
    assert net.count(url) == 1 and r["extracted"] == 1
    item = db.scalars(select(PoolItem)).one()
    ex = db.get(Extraction, item.extraction_id)
    assert ex.status == "ok" and ex.extractor == "trafilatura" and "open-weights" in ex.text
    assert item.image == "https://cdn.example.com/img/qwen-launch-hero.jpg" and item.author == "Jane Writer"
    assert "open-weights" in item_text(db, item)  # internal full text
    assert item.summary == "short teaser"  # what pages show stays the source summary
    # next cycle: feed fetched again, article is a cache hit (no page request, no re-extraction)
    r2 = cycle(db, net, "c2")
    assert net.count(url) == 1 and r2["extract_cache_hits"] == 0 and r2["extracted"] == 0
    assert db.scalar(select(func.count()).select_from(Extraction)) == 1


def test_extraction_cache_hit_for_new_item_same_url(db, net):
    url = "https://news.example/qwen"
    net.routes[url] = (200, "text/html", ARTICLE)
    net.routes[FEED] = (200, "application/rss+xml", rss([("Qwen", url, "t")]))
    (u,) = _users(db, 1)
    _sub(db, [u], _source(db, "rss:a", FEED, extractor=True))
    cycle(db, net, "c1")
    # the item is dropped from the pool (e.g. retention) but the extraction stays cached
    db.query(PoolItemSource).delete(); db.query(PoolItem).delete(); db.commit()
    r = cycle(db, net, "c2")
    assert net.count(url) == 1 and r["extract_cache_hits"] == 1
    assert db.scalars(select(PoolItem)).one().extraction_id is not None


def test_extraction_cache_hit_by_html_hash(db, net):
    net.routes["https://news.example/a"] = (200, "text/html", ARTICLE)
    net.routes["https://amp.example/a"] = (200, "text/html", ARTICLE.replace("\n", "\n  "))
    net.routes[FEED] = (200, "application/rss+xml", rss([("Qwen", "https://news.example/a", "t1")]))
    net.routes[FEED2] = (200, "application/rss+xml", rss([("Qwen AMP", "https://amp.example/a", "t2")]))
    (u,) = _users(db, 1)
    _sub(db, [u], _source(db, "rss:a", FEED, extractor=True))
    cycle(db, net, "c1")
    _sub(db, [u], _source(db, "rss:b", FEED2, extractor=True))
    r = cycle(db, net, "c2")
    assert r["extract_cache_hits"] == 1 and r["extracted"] == 0
    rows = db.scalars(select(Extraction).order_by(Extraction.id)).all()
    assert len(rows) == 2 and rows[0].html_hash == rows[1].html_hash and rows[1].text == rows[0].text


def test_extraction_failure_falls_back_to_summary_and_cycle_continues(db, net):
    net.routes[FEED] = (200, "application/rss+xml", rss([
        ("JS app", "https://app.example/post", "Summary from the feed for the JS page"),
        ("Paywalled", "https://paywall.example/story", "Summary for the paywalled story"),
        ("Blocked", "https://blocked.example/x", "Summary for the bot-walled page"),
        ("Fine", "https://news.example/qwen", "teaser"),
    ]))
    net.routes["https://app.example/post"] = (200, "text/html", JS_SHELL)
    net.routes["https://paywall.example/story"] = (402, "text/html", "pay")
    net.routes["https://blocked.example/x"] = (403, "text/html", "no bots")
    net.routes["https://news.example/qwen"] = (200, "text/html", ARTICLE)
    (u,) = _users(db, 1)
    _sub(db, [u], _source(db, "rss:a", FEED, extractor=True))
    r = cycle(db, net, "c1")
    assert r["sources"]["rss:a"] == "ok" and r["extracted"] == 1 and r["extract_failed"] == 3
    by_url = {i.canonical_url: i for i in db.scalars(select(PoolItem))}
    js = by_url["https://app.example/post"]
    assert db.get(Extraction, js.extraction_id).status == "failed:js_shell"
    assert item_text(db, js) == "Summary from the feed for the JS page"
    assert db.get(Extraction, by_url["https://paywall.example/story"].extraction_id).status == "failed:http:402"
    assert item_text(db, by_url["https://blocked.example/x"]) == "Summary for the bot-walled page"
    assert "open-weights" in item_text(db, by_url["https://news.example/qwen"])


def test_extractor_is_per_source_switch(db, net):
    net.routes[FEED] = (200, "application/rss+xml", rss([("A", "https://news.example/qwen", "teaser")]))
    net.routes["https://news.example/qwen"] = (200, "text/html", ARTICLE)
    (u,) = _users(db, 1)
    _sub(db, [u], _source(db, "rss:a", FEED, extractor=False))
    r = cycle(db, net, "c1")
    assert net.calls == [FEED] and r["extracted"] == 0
    assert db.scalars(select(PoolItem)).one().extraction_id is None


def test_bad_source_does_not_fail_cycle_and_private_feed_refused(db, net):
    net.routes[FEED] = (200, "application/rss+xml", rss([("A", "https://news.example/a", "a")]))
    net.routes["https://broken.example/feed"] = (500, "text/plain", "x")
    net.routes["https://html.example/feed"] = (200, "text/html", "<html>not a feed</html>")
    (u,) = _users(db, 1)
    _sub(db, [u], _source(db, "rss:ok", FEED), _source(db, "rss:broken", "https://broken.example/feed"),
         _source(db, "rss:html", "https://html.example/feed"), _source(db, "rss:lan", "http://192.168.1.61:7862/feed"))
    r = cycle(db, net, "c1", extract=False)
    assert r["sources"]["rss:ok"] == "ok"
    assert r["sources"]["rss:broken"] == "failed:http:500"
    assert r["sources"]["rss:html"] == "failed:not_a_feed"
    assert r["sources"]["rss:lan"] == "failed:private-ip"
    assert not any("192.168" in c for c in net.calls)
    assert db.scalar(select(func.count()).select_from(PoolItem)) == 1


def test_x_and_rsshub_need_self_hosted_rsshub(db, net, monkeypatch):
    monkeypatch.delenv("RSSHUB_BASE", raising=False); monkeypatch.delenv("RSSHUB_ACCESS_KEY", raising=False)
    (u,) = _users(db, 1)
    s = Source(key="xa:someone", kind="x_account", name="@someone", url="https://x.com/someone", config={})
    db.add(s); db.commit(); _sub(db, [u], s)
    r = cycle(db, net, "c1", extract=False)
    assert r["sources"]["xa:someone"] == "skipped:rsshub-not-configured" and net.calls == []
    monkeypatch.setenv("RSSHUB_BASE", "https://rsshub.example"); monkeypatch.setenv("RSSHUB_ACCESS_KEY", "sekret")
    url = "https://rsshub.example/twitter/user/someone/excludeReplies%3D1%26includeRts%3D0?key=sekret&limit=10"
    net.routes[url] = (200, "application/rss+xml", rss([("post", "https://x.com/someone/status/1", "hello")]))
    r = cycle(db, net, "c2", extract=False)
    assert r["sources"]["xa:someone"] == "ok" and net.count(url) == 1
    row = db.scalars(select(SourceFetch).where(SourceFetch.cycle == "c2")).one()
    assert "sekret" not in (row.url or "")


def test_old_entries_not_pooled_and_extraction_limited_to_recent(db, net):
    net.routes[FEED] = (200, "application/rss+xml", rss([("old", "https://news.example/old", "x")], age_hours=24 * 30))
    net.routes[FEED2] = (200, "application/rss+xml", rss([("week", "https://news.example/week", "x")], age_hours=24 * 3)
                         .replace("</channel>", rss([(f"n{i}", f"https://news.example/n{i}", "x") for i in range(5)])
                                  .split("<title>t</title>", 1)[1].split("</channel>")[0] + "</channel>"))
    for i in range(5):
        net.routes[f"https://news.example/n{i}"] = (200, "text/html", ARTICLE.replace("Qwen", f"Qwen{i}"))
    (u,) = _users(db, 1)
    _sub(db, [u], _source(db, "rss:a", FEED, extractor=True), _source(db, "rss:b", FEED2, extractor=True))
    r = cycle(db, net, "c1", max_extract_per_source=3)
    assert r["too_old"] == 1 and r["new_items"] == 6
    assert net.count("https://news.example/week") == 0  # pooled (3 days old) but outside the 26 h extraction window
    assert sum(net.count(f"https://news.example/n{i}") for i in range(5)) == 3 and r["extracted"] == 3
