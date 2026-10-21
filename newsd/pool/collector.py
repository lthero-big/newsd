"""Shared collection pool.

``run_cycle(db, cycle)`` fetches every enabled source that at least one active user has an enabled
subscription to -- once, no matter how many users subscribe -- and upserts the entries into
``pool_items``. The once-per-cycle guarantee is the unique ``(source_id, cycle)`` row in
``source_fetches``: a source that already has a row for this cycle (fetched by an earlier run or
another process) is skipped and its items are served from the pool.

After fetching, items from sources with ``content_extractor`` on get full text + metadata via
trafilatura, cached in ``extractions`` by canonical URL (and by HTML hash), so an article is
extracted once for everybody. Extraction failures (JS-rendered pages, paywalls, bot walls, HTTP
errors) are recorded and the item keeps the summary that came with the source; they never fail
the cycle.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from newsd.collector import collect as core
from newsd.models import Extraction, PoolItem, PoolItemSource, Source, SourceFetch, Subscription, User

from .extract import extract_page, html_hash
from .fetch import Fetcher

SUMMARY_LIMIT = 4000
HASH_MIN_TEXT = 200  # content-hash dedupe only when the summary is long enough to be distinctive
RETRY_FAILED_AFTER = timedelta(hours=24)
MAX_AGE_DAYS = 7  # feed entries older than this are not pooled (archive-style feeds list hundreds of old posts)
MAX_EXTRACT_PER_SOURCE = 30  # newest first; only items inside the lookback window are extracted
PLUGIN_NAMES = {"hn": "hn", "github_search": "github", "hf_trending": "hf", "ossinsight": "ossinsight"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def cycle_key(now: datetime | None = None, period_minutes: int = 60) -> str:
    """Cycle id = UTC start of the current period, e.g. "2026-10-20T09:00" for hourly cycles."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    period = max(1, int(period_minutes))
    minutes = (now.hour * 60 + now.minute) // period * period
    start = now.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)
    return start.strftime("%Y-%m-%dT%H:%M")


def url_key(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def text_hash(title: str, summary: str) -> str | None:
    body = " ".join((summary or "").split())
    if len(body) < HASH_MIN_TEXT:
        return None
    raw = f"{core.fold(title or '')}\n{core.fold(body)[:2000]}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _naive(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def subscribed_sources(db) -> list[Source]:
    """Enabled sources with at least one enabled subscription by an active user (each source once)."""
    stmt = (
        select(Source)
        .join(Subscription, Subscription.source_id == Source.id)
        .join(User, User.id == Subscription.user_id)
        .where(Source.enabled.is_(True), Subscription.enabled.is_(True), User.active.is_(True))
        .distinct()
        .order_by(Source.id)
    )
    return list(db.scalars(stmt))


def user_items(db, user_id: int, since: datetime | None = None, limit: int = 500) -> list[PoolItem]:
    """Pool items carried by any source the user is subscribed to (the per-user view of the shared pool).
    Sources in a disabled zone (DEPTAI-216 3b) count as off."""
    from ..zones import active_filter
    stmt = (
        select(PoolItem)
        .join(PoolItemSource, PoolItemSource.item_id == PoolItem.id)
        .join(Subscription, Subscription.source_id == PoolItemSource.source_id)
        .where(Subscription.user_id == user_id, Subscription.enabled.is_(True), active_filter())
        .distinct()
        .order_by(PoolItem.first_seen_at.desc())
        .limit(limit)
    )
    if since is not None:
        stmt = stmt.where(PoolItem.first_seen_at >= since)
    return list(db.scalars(stmt))


def item_text(db, item: PoolItem) -> str:
    """Internal text for scoring/summary/translation/dedupe/search: extracted full text, else the source summary."""
    if item.extraction_id:
        ex = db.get(Extraction, item.extraction_id)
        if ex is not None and ex.status == "ok" and ex.text:
            return ex.text
    return item.summary or ""


# ------------------------------------------------------------------------------------------ source -> request

def _rsshub() -> tuple[str, str]:
    return os.environ.get("RSSHUB_BASE", "").rstrip("/"), os.environ.get("RSSHUB_ACCESS_KEY", "")


def _rsshub_url(route: str, limit: int = 30) -> str | None:
    base, key = _rsshub()
    if not base or not key:
        return None
    return f"{base}{urllib.parse.quote(route)}?" + urllib.parse.urlencode({"key": key, "limit": limit})


def feed_url(src: Source) -> tuple[str | None, str | None]:
    """(url, skip_reason) for feed-like kinds. X goes through self-hosted RSSHub only (no X connector)."""
    cfg = src.config or {}
    if src.kind == "rss":
        return (src.url, None) if src.url else (None, "skipped:no-url")
    if src.kind == "reddit":
        sub = src.key.split(":", 1)[1]
        return core._reddit_rss_url({"subreddit": sub, **cfg}), None
    if src.kind == "rsshub":
        url = _rsshub_url(cfg.get("route") or "", int(cfg.get("limit", 30))) if cfg.get("route") else None
        return (url, None) if url else (None, "skipped:rsshub-not-configured")
    if src.kind == "x_account":
        handle = src.key.split(":", 1)[1]
        url = _rsshub_url(f"/twitter/user/{handle}/excludeReplies=1&includeRts=0", 10)
        return (url, None) if url else (None, "skipped:rsshub-not-configured")
    if src.kind == "x_query":
        terms = cfg.get("terms") or []
        if not terms:
            return None, "skipped:no-terms"
        q = f"({' OR '.join(terms)})" + (f" ({' OR '.join(cfg['and'])})" if cfg.get("and") else "")
        url = _rsshub_url(f"/twitter/keyword/{q}", 20)
        return (url, None) if url else (None, "skipped:rsshub-not-configured")
    return None, f"skipped:unsupported-kind:{src.kind}"


def _scrub(text: str) -> str:
    _, key = _rsshub()
    return text.replace(key, "***") if key else text


def _feed_rows(body: bytes, ctype: str) -> list[dict] | None:
    if not core._looks_like_feed(body, ctype):
        return None
    rows = []
    for e in core.parse_feed(body):
        if not e.get("url"):
            continue
        rows.append({
            "url": e["url"], "title": e.get("title") or "", "published_at": e.get("published_at"),
            "listing_text": e.get("listing_text") or "", "kind": "release" if "/releases" in e["url"] else "post",
            "extra": {"enclosure": e["enclosure"]} if e.get("enclosure") else None,
        })
    return rows


def _legacy_plugins():
    here = str(Path(core.__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)  # legacy collector modules import each other flat (import collect as core)
    import plugins  # type: ignore

    return plugins


async def _run_plugin(src: Source, fetcher: Fetcher, start: datetime, end: datetime, timeout: float) -> list[dict]:
    """Legacy Horizon-style plugins (hn/github/hf/ossinsight) run in a worker thread; every HTTP call they make
    is routed through the shared async client, so rate limits, SSRF checks and request counting still apply."""
    name = src.key.split(":", 1)[1]
    plugin = PLUGIN_NAMES.get(name)
    if not plugin:
        raise core.FetchError(f"unknown-plugin:{name}")
    plugins = _legacy_plugins()
    loop = asyncio.get_running_loop()

    def bridged(url: str) -> dict:
        r = asyncio.run_coroutine_threadsafe(fetcher.get(url), loop).result(max(300, timeout * 10))
        return {"status": r.status, "body": r.body, "content_type": r.content_type, "final_url": r.final_url, "truncated": r.truncated}

    ctx = plugins.Context(start=start.replace(tzinfo=timezone.utc), end=end.replace(tzinfo=timezone.utc),
                          sources={name: src.config or {}}, timeout=int(timeout), cache=bridged)
    items, stats = await asyncio.to_thread(plugins.REGISTRY[plugin], ctx)
    if not items and stats.get("errors"):
        raise core.FetchError(";".join(str(e) for e in stats["errors"][:3]))
    return [{"url": it.get("url") or "", "title": it.get("title") or "", "published_at": it.get("published_at"),
             "listing_text": it.get("listing_text") or "", "kind": it.get("kind"),
             "extra": {k: it[k] for k in ("points", "comments", "downloads", "language", "topics") if it.get(k) is not None} or None}
            for it in items if it.get("url")]


async def _fetch_source(src: Source, fetcher: Fetcher, start: datetime, end: datetime) -> tuple[str, str | None, list[dict]]:
    """Returns (status, url, rows). Never raises."""
    try:
        if src.kind == "plugin":
            return "ok", None, await _run_plugin(src, fetcher, start, end, fetcher.timeout)
        url, skip = feed_url(src)
        if skip:
            return skip, None, []
        resp = await fetcher.get(url)
        rows = _feed_rows(resp.body, resp.content_type)
        if rows is None:
            return "failed:not_a_feed", _scrub(url), []
        return "ok", _scrub(url), rows
    except (core.UnsafeURL, core.FetchError) as exc:
        return _scrub(f"failed:{exc.reason}")[:64], None, []
    except Exception as exc:  # noqa: BLE001 - one bad source never aborts the cycle
        return f"failed:{type(exc).__name__}"[:64], None, []


# ----------------------------------------------------------------------------------------------- pool writes

def _claim(db, src: Source, cycle: str) -> SourceFetch | None:
    row = SourceFetch(source_id=src.id, cycle=cycle, status="running")
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    return row


def upsert_item(db, src: Source, row: dict, cycle: str, now: datetime) -> tuple[PoolItem, bool]:
    canonical = core.normalize_url(row["url"])
    key = url_key(canonical)
    summary = core.html_to_text(row.get("listing_text") or "") if "<" in (row.get("listing_text") or "") else " ".join((row.get("listing_text") or "").split())
    summary = summary[:SUMMARY_LIMIT]
    title = (row.get("title") or "").strip()
    chash = text_hash(title, summary)
    item = db.scalar(select(PoolItem).where(PoolItem.url_key == key))
    if item is None and chash:
        item = db.scalar(select(PoolItem).where(PoolItem.content_hash == chash))
    created = item is None
    published = _naive(core.parse_time(row.get("published_at")))
    if created:
        item = PoolItem(url_key=key, canonical_url=canonical, url=row["url"], content_hash=chash, title=title,
                        summary=summary, published_at=published, kind=row.get("kind"), extra=row.get("extra"),
                        first_source_id=src.id, first_seen_at=now, last_seen_at=now)
        db.add(item)
        db.flush()
    else:
        item.last_seen_at = now
        if not item.summary and summary:
            item.summary = summary
        if item.content_hash is None and chash:
            item.content_hash = chash
        if item.published_at is None and published:
            item.published_at = published
        if not item.title and title:
            item.title = title
    link = db.scalar(select(PoolItemSource).where(PoolItemSource.item_id == item.id, PoolItemSource.source_id == src.id))
    if link is None:
        db.add(PoolItemSource(item_id=item.id, source_id=src.id, url=row["url"], fetched_at=now, cycle=cycle))
    return item, created


def _params_key(params: dict | None) -> tuple[str, bool, bool]:
    params = params or {}
    p, r = bool(params.get("favor_precision")), bool(params.get("favor_recall"))
    return ("precision" if p else "recall" if r else ""), p, r


def _apply_extraction(item: PoolItem, ex: Extraction) -> None:
    item.extraction_id = ex.id
    if ex.published_at and item.published_at is None:
        item.published_at = _naive(core.parse_time(ex.published_at))
    if ex.image and not item.image:
        item.image = ex.image
    if ex.author and not item.author:
        item.author = ex.author[:191]
    if ex.title and not item.title:
        item.title = ex.title


async def _extract_items(db, todo: list[tuple[PoolItem, Source]], fetcher: Fetcher, concurrency: int, report: dict) -> None:
    now = _utcnow()
    pending: dict[str, tuple[PoolItem, Source, list[PoolItem]]] = {}
    for item, src in todo:
        cached = db.scalar(select(Extraction).where(Extraction.url_key == item.url_key))
        if cached is not None and (cached.status == "ok" or now - cached.created_at < RETRY_FAILED_AFTER):
            _apply_extraction(item, cached)
            report["extract_cache_hits"] += 1
            continue
        if item.url_key in pending:
            pending[item.url_key][2].append(item)
            continue
        pending[item.url_key] = (item, src, [item])
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(item: PoolItem, src: Source):
        async with sem:
            try:
                resp = await fetcher.get(item.url)
            except (core.UnsafeURL, core.FetchError) as exc:
                return item, src, None, None, f"failed:{exc.reason}"[:64]
            except Exception as exc:  # noqa: BLE001
                return item, src, None, None, f"failed:{type(exc).__name__}"[:64]
            ctype = resp.content_type.lower()
            if ctype and "html" not in ctype and "xml" not in ctype and not ctype.startswith("text/"):
                return item, src, None, None, "failed:unsupported_type"
            html = resp.text()
            hsh = html_hash(html)
            return item, src, html, hsh, None

    fetched = await asyncio.gather(*(one(item, src) for item, src, _ in pending.values()))
    for item, src, html, hsh, error in fetched:
        group = pending[item.url_key][2]
        pkey, fp, fr = _params_key(src.extractor_params)
        ex = db.scalar(select(Extraction).where(Extraction.url_key == item.url_key))
        if ex is None:
            ex = Extraction(url_key=item.url_key, canonical_url=item.canonical_url, status="running")
            db.add(ex)
        ex.params, ex.created_at, ex.html_hash = pkey, now, hsh
        same = None
        if hsh:
            same = db.scalar(select(Extraction).where(Extraction.html_hash == hsh, Extraction.status == "ok",
                                                      Extraction.params == pkey, Extraction.url_key != item.url_key))
        if error:
            ex.status, ex.extractor, ex.text = error, "none", ""
            report["extract_failed"] += 1
        elif same is not None:
            for f in ("status", "extractor", "text", "title", "author", "published_at", "sitename", "image", "meta_from"):
                setattr(ex, f, getattr(same, f))
            report["extract_cache_hits"] += 1
        else:
            res = await asyncio.to_thread(extract_page, html, item.url, favor_precision=fp, favor_recall=fr)
            ex.status, ex.extractor, ex.text = res.status, res.extractor, res.text
            ex.title, ex.author, ex.published_at = res.title, (res.author or None) and res.author[:191], res.published_at
            ex.sitename, ex.image, ex.meta_from = (res.sitename or None) and res.sitename[:191], res.image, res.meta_from
            report["extracted" if res.ok else "extract_failed"] += 1
        db.flush()
        for it in group:
            _apply_extraction(it, ex)
    db.commit()


async def run_cycle(db, cycle: str | None = None, *, fetcher: Fetcher | None = None, kinds: set[str] | None = None,
                    extract: bool = True, lookback_hours: float = 26, concurrency: int = 8,
                    max_age_days: float = MAX_AGE_DAYS, max_extract_per_source: int = MAX_EXTRACT_PER_SOURCE) -> dict:
    cycle = cycle or cycle_key()
    now = _utcnow()
    end, start = now, now - timedelta(hours=lookback_hours)
    own = fetcher is None
    fetcher = fetcher or Fetcher()
    report = {"cycle": cycle, "sources": {}, "fetched_sources": 0, "shared_sources": 0, "new_items": 0, "seen_items": 0,
              "too_old": 0, "extracted": 0, "extract_failed": 0, "extract_cache_hits": 0}
    oldest = now - timedelta(days=max_age_days)
    try:
        sources = [s for s in subscribed_sources(db) if not kinds or s.kind in kinds]
        claimed: list[tuple[Source, SourceFetch]] = []
        for src in sources:
            row = _claim(db, src, cycle)
            if row is None:
                report["sources"][src.key] = "shared"  # already fetched this cycle
                report["shared_sources"] += 1
            else:
                claimed.append((src, row))
        results = await asyncio.gather(*(_fetch_source(src, fetcher, start, end) for src, _ in claimed))
        todo: list[tuple[PoolItem, Source]] = []
        for (src, frow), (status, url, rows) in zip(claimed, results):
            new, kept, candidates = 0, 0, []
            for r in rows:
                published = _naive(core.parse_time(r.get("published_at")))
                if published is not None and published < oldest:
                    report["too_old"] += 1
                    continue
                kept += 1
                item, created = upsert_item(db, src, r, cycle, now)
                new += int(created)
                recent = item.published_at >= start if item.published_at else item.first_seen_at >= now
                if src.content_extractor and item.extraction_id is None and recent:
                    candidates.append(item)
            candidates.sort(key=lambda i: i.published_at or now, reverse=True)
            todo.extend((item, src) for item in candidates[:max(0, max_extract_per_source)])
            frow.status, frow.url, frow.entries, frow.new_items, frow.finished_at = status, url, len(rows), new, _utcnow()
            db.commit()
            report["sources"][src.key] = status
            report["fetched_sources"] += 1
            report["new_items"] += new
            report["seen_items"] += kept - new
        if extract and todo:
            await _extract_items(db, todo, fetcher, concurrency, report)
        report["requests"] = fetcher.request_count
        return report
    finally:
        if own:
            await fetcher.aclose()


def run_cycle_sync(db, cycle: str | None = None, **kw) -> dict:
    return asyncio.run(run_cycle(db, cycle, **kw))
