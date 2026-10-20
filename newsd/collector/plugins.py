"""Horizon-style plugin fetchers for the news-daily pipeline.

Each plugin is a function ``fetch(ctx) -> (items, stats)`` registered under a
name.  Items share the raw-candidate shape used by ``collect.py``
(source/url/title/published_at/points/comments/listing_text/source_links/feed)
plus an optional ``kind`` hint ("repo", "model", "release", "post").
Plugins never raise: failures are reported in stats so one bad source cannot
abort the daily run (same contract as Horizon's BaseScraper).
"""
from __future__ import annotations

import json
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import collect as core

REGISTRY: dict[str, Callable] = {}


def register(name: str):
    def wrap(fn):
        REGISTRY[name] = fn
        return fn
    return wrap


@dataclass
class Context:
    start: datetime
    end: datetime
    sources: dict
    timeout: int = core.DEFAULT_TIMEOUT
    cache: object | None = None
    x_items: Path | None = None
    extra: dict = field(default_factory=dict)


def _json(url: str, ctx: Context):
    body, _ = core._get_bytes(url, ctx.timeout, ctx.cache)
    return json.loads(body)


@register("hn")
def hn(ctx: Context):
    items, stats = core.fetch_hn(ctx.sources, ctx.start, ctx.end, ctx.timeout, ctx.cache)
    for item in items:
        item["kind"] = "post"
    return items, stats


@register("reddit")
def reddit(ctx: Context):
    items, stats = core.fetch_reddit(ctx.sources, ctx.start, ctx.end, ctx.timeout, ctx.cache)
    for item in items:
        item["kind"] = "post"
    return items, stats


@register("rss")
def rss(ctx: Context):
    items, stats = core.fetch_rss(ctx.sources, ctx.start, ctx.end, ctx.timeout, ctx.cache)
    for item in items:
        item["kind"] = "release" if "/releases" in item["url"] else "post"
    return items, stats


@register("rsshub")
def rsshub(ctx: Context):
    """Self-hosted RSSHub routes (config.rsshub.routes). Deterministic; the access key never leaves this function:
    items carry the article URL only, and errors are scrubbed. Skips quietly when RSSHUB_BASE/KEY are unset."""
    import os
    base, key = os.environ.get("RSSHUB_BASE", "").rstrip("/"), os.environ.get("RSSHUB_ACCESS_KEY", "")
    cfg = ctx.sources.get("rsshub") or {}
    if not base or not key or not cfg.get("routes"):
        return [], {"fetched": 0, "errors": [], "skipped": "RSSHUB_BASE/RSSHUB_ACCESS_KEY 未配置"}
    known = {f.get("url") for f in ctx.sources.get("rss", [])}
    feeds, caps = [], {}
    for r in cfg["routes"]:
        q = urllib.parse.urlencode({"key": key, "limit": r.get("limit", 30)})
        feeds.append({"id": r["id"], "url": f"{base}{urllib.parse.quote(r['route'])}?{q}"})
        caps[r["id"]] = r.get("max", 15)
    labels = {r["id"]: r.get("label", r["id"]) for r in cfg["routes"]}
    items, stats = core.fetch_rss({"rss": feeds}, ctx.start, ctx.end, ctx.timeout, ctx.cache)
    out, per = [], {}
    for it in items:
        if it["url"] in known or per.get(it["feed"], 0) >= caps.get(it["feed"], 15):
            continue
        per[it["feed"]] = per.get(it["feed"], 0) + 1
        it["kind"] = "post"
        it["transport"] = "rsshub"
        it["feed_label"] = labels.get(it["feed"], it["feed"])
        out.append(it)
    scrub = lambda v: v.replace(key, "***") if isinstance(v, str) else v
    stats = json.loads(scrub(json.dumps(stats, ensure_ascii=False, default=str)))
    stats["fetched"] = len(out)
    return out, stats


def _gh_search(query: str, ctx: Context) -> dict:
    """Prefer the gh CLI (authenticated, higher rate limit); fall back to the public API."""
    path = "search/repositories?" + urllib.parse.urlencode({"q": query, "sort": "stars", "order": "desc", "per_page": ctx.sources["github_search"].get("per_query", 15)})
    try:
        out = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=ctx.timeout * 2)
        if out.returncode == 0:
            return json.loads(out.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return _json("https://api.github.com/" + path, ctx)


@register("github")
def github(ctx: Context):
    cfg = ctx.sources.get("github_search") or {}
    since = (ctx.end - timedelta(days=int(cfg.get("created_days", 14)))).date().isoformat()
    stats = {"fetched": 0, "errors": [], "queries": {}}
    items, seen = [], set()
    for q in cfg.get("queries", []):
        query = f"{q} created:>={since} stars:>={cfg.get('min_stars', 50)}"
        try:
            payload = _gh_search(query, ctx)
        except Exception as exc:  # noqa: BLE001
            stats["errors"].append(f"{q}:{type(exc).__name__}")
            continue
        repos = payload.get("items") or []
        stats["queries"][q] = len(repos)
        for repo in repos:
            url = repo.get("html_url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            items.append({
                "source": "github", "kind": "repo", "url": url,
                "title": repo.get("full_name") or "",
                "published_at": repo.get("created_at"),
                "points": repo.get("stargazers_count"), "comments": repo.get("forks_count"),
                "listing_text": repo.get("description") or "",
                "language": repo.get("language"), "topics": repo.get("topics") or [],
                "source_links": [core._link("github", url)], "feed": f"github:{q}",
            })
    stats["fetched"] = len(items)
    return items, stats


@register("hf")
def hf(ctx: Context):
    cfg = ctx.sources.get("hf_trending") or {}
    stats = {"fetched": 0, "errors": []}
    try:
        payload = _json(f"https://huggingface.co/api/trending?type=model&limit={min(20, int(cfg.get('limit', 20)))}", ctx)
    except Exception as exc:  # noqa: BLE001
        return [], {**stats, "errors": [f"{type(exc).__name__}:{exc}"]}
    items = []
    for org in cfg.get("orgs") or []:  # owner-followed HF orgs (/sources): recently updated models
        try:
            rows = _json(f"https://huggingface.co/api/models?author={urllib.parse.quote(org)}&sort=lastModified&direction=-1&limit=5", ctx)
        except Exception as exc:  # noqa: BLE001
            stats["errors"].append(f"org {org}: {type(exc).__name__}")
            continue
        for r in rows if isinstance(rows, list) else []:
            dt = core.parse_time(r.get("lastModified") or r.get("createdAt"))
            if r.get("id") and dt and core.in_window(dt, ctx.start, ctx.end):
                payload.setdefault("recentlyTrending", []).append({"repoData": {"id": r["id"], "lastModified": r.get("lastModified"), "likes": r.get("likes"), "downloads": r.get("downloads"), "pipeline_tag": r.get("pipeline_tag"), "authorData": {"fullname": org}}})
    for row in payload.get("recentlyTrending") or []:
        repo = row.get("repoData") or {}
        mid = repo.get("id")
        if not mid:
            continue
        url = f"https://huggingface.co/{mid}"
        items.append({
            "source": "hf", "kind": "model", "url": url, "title": mid,
            "published_at": repo.get("lastModified"),
            "points": repo.get("likes"), "comments": None, "downloads": repo.get("downloads"),
            "listing_text": " ".join(filter(None, [repo.get("pipeline_tag"), (repo.get("authorData") or {}).get("fullname")])),
            "source_links": [core._link("hf", url)], "feed": "hf:trending",
        })
    stats["fetched"] = len(items)
    return items, stats


@register("ossinsight")
def ossinsight(ctx: Context):
    cfg = ctx.sources.get("ossinsight") or {}
    stats = {"fetched": 0, "errors": []}
    url = "https://api.ossinsight.io/v1/trends/repos/?" + urllib.parse.urlencode({"period": cfg.get("period", "past_24_hours"), "language": cfg.get("language", "All")})
    try:
        payload = _json(url, ctx)
    except Exception as exc:  # noqa: BLE001
        return [], {**stats, "errors": [f"{type(exc).__name__}"]}
    quality = (payload.get("data_quality") or {}).get("status")
    if quality == "unavailable":
        return [], {**stats, "skipped": "ossinsight data_quality=unavailable"}
    items = []
    for row in ((payload.get("data") or {}).get("rows") or [])[: int(cfg.get("limit", 25))]:
        name = row.get("repo_name")
        if not name:
            continue
        link = f"https://github.com/{name}"
        items.append({
            "source": "ossinsight", "kind": "repo", "url": link, "title": name,
            "published_at": core.to_iso(ctx.end), "points": int(float(row.get("stars") or 0)),
            "comments": int(float(row.get("forks") or 0)), "listing_text": row.get("description") or "",
            "source_links": [core._link("ossinsight", link)], "feed": "ossinsight",
        })
    stats["fetched"] = len(items)
    return items, stats


@register("x")
def x_items(ctx: Context):
    """Merge X posts gathered out-of-band by the agent's X connector.

    Expected file: ``{"items": [{"url", "title"|"text", "author", "created_at",
    "likes", "reposts", "replies"}]}``.  Missing file means the step is skipped.
    """
    path = ctx.x_items
    if not path or not Path(path).is_file():
        return [], {"fetched": 0, "errors": [], "skipped": "no x-items.json"}
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError as exc:
        return [], {"fetched": 0, "errors": [f"invalid json: {exc}"]}
    rows = rows.get("items", []) if isinstance(rows, dict) else rows
    items = []
    for row in rows:
        url = row.get("url") or ""
        text = (row.get("title") or row.get("text") or "").strip()
        if not url.startswith("https://") or not text:
            continue
        dt = core.parse_time(row.get("created_at"))
        if dt and not core.in_window(dt, ctx.start - timedelta(hours=24), ctx.end):
            continue
        items.append({
            "source": "x", "kind": "post", "url": url,
            "title": text.split("\n")[0][:160], "published_at": core.to_iso(dt),
            "points": (row.get("likes") or 0) + 2 * (row.get("reposts") or 0), "comments": row.get("replies"),
            "listing_text": text, "author": row.get("author"), "author_name": row.get("author_name"),
            "avatar": row.get("avatar"), "verified": row.get("verified"), "likes": row.get("likes"), "reposts": row.get("reposts"),
            "source_links": [core._link("x", url)], "feed": "x",
            "media": [m for m in (row.get("media") or []) if isinstance(m, dict)],
            **({"links_out": row["links_out"]} if isinstance(row.get("links_out"), list) else {}),
            **({"prompt_q": True} if row.get("prompt_q") else {}), **({"prompt": row["prompt"]} if isinstance(row.get("prompt"), dict) else {}),
        })
    return items, {"fetched": len(items), "errors": []}


def run_all(ctx: Context, enabled: list[str]) -> tuple[list[dict], dict]:
    raw, stats = [], {}
    for name in enabled:
        fn = REGISTRY.get(name)
        if not fn:
            stats[name] = {"errors": ["unknown plugin"]}
            continue
        try:
            items, st = fn(ctx)
        except Exception as exc:  # noqa: BLE001 - plugin isolation
            items, st = [], {"errors": [f"{type(exc).__name__}:{exc}"]}
        raw.extend(items)
        stats[name] = st
    return raw, stats
