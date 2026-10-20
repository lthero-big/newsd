"""Before/after comparison of publish-date, lead-image and text hit rates on real RSS sources (DEPTAI-218).

    .venv/bin/python scripts/compare_extraction.py [--per-feed 6] [--out var/compare-m2-2.json]

For each feed the newest N entries are taken and each article page is fetched once with the pool's
shared client. On the same HTML:

* before: regex date (linkdates.from_html_regex), regex og:image (media.og_image_from_html) and the old
  text path (trafilatura.extract with default arguments, falling back to tag stripping);
* after: newsd.pool.extract.extract_page (trafilatura text + metadata in one pass, regex fallbacks).

"summary" = the item ends up with >= 200 chars of usable text: the feed's own text when it is long enough,
otherwise the page text (this is what annotate_bodies did before, and what the pool does after).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from newsd.collector import collect as core  # noqa: E402
from newsd.collector import linkdates, media  # noqa: E402
from newsd.pool.extract import extract_page  # noqa: E402
from newsd.pool.fetch import Fetcher  # noqa: E402

FEEDS = [
    ("hf-blog", "https://huggingface.co/blog/feed.xml"),
    ("openai-news", "https://openai.com/news/rss.xml"),
    ("google-ai-blog", "https://blog.google/technology/ai/rss/"),
    ("ollama-releases", "https://github.com/ollama/ollama/releases.atom"),
    ("vllm-releases", "https://github.com/vllm-project/vllm/releases.atom"),
    ("simonwillison", "https://simonwillison.net/atom/everything/"),
    ("mit-tech-review-ai", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
    ("techcrunch-ai", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("theverge-ai", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("arstechnica", "https://feeds.arstechnica.com/arstechnica/technology-lab"),
    ("marktechpost", "https://www.marktechpost.com/feed/"),
    ("infoq-cn", "https://www.infoq.cn/feed"),
]
MIN_BODY = 200


def old_text(html: str) -> str:
    text = ""
    try:
        import trafilatura

        extracted = trafilatura.extract(html) or ""
        if len(extracted) >= 80:
            text = extracted
    except Exception:  # noqa: BLE001
        pass
    return text or core.html_to_text(html)


async def run(per_feed: int) -> dict:
    rows, feeds = [], {}
    async with Fetcher(min_interval=1.0, per_host=2, retries=1, timeout=15) as f:
        async def feed(fid, url):
            try:
                r = await f.get(url)
                entries = core.parse_feed(r.body)[:per_feed]
                feeds[fid] = {"status": "ok", "entries": len(entries)}
            except Exception as exc:  # noqa: BLE001
                feeds[fid] = {"status": f"failed:{getattr(exc, 'reason', type(exc).__name__)}", "entries": 0}
                return []
            return [(fid, e) for e in entries if e.get("url")]

        entries = [x for chunk in await asyncio.gather(*(feed(fid, u) for fid, u in FEEDS)) for x in chunk]

        async def page(fid, e):
            listing = core.html_to_text(e.get("listing_text") or "")
            row = {"feed": fid, "url": e["url"], "feed_date": bool(e.get("published_at")), "listing_len": len(listing)}
            try:
                r = await f.get(e["url"])
                html = r.text() if "html" in r.content_type.lower() or not r.content_type else ""
                row["page"] = "ok" if html else f"type:{r.content_type[:30]}"
            except Exception as exc:  # noqa: BLE001
                html, row["page"] = "", f"failed:{getattr(exc, 'reason', type(exc).__name__)}"
            ot = await asyncio.to_thread(old_text, html) if html else ""
            new = await asyncio.to_thread(extract_page, html, e["url"]) if html else None
            row["before"] = {
                "date": bool(linkdates.from_html_regex(html)) if html else False,
                "image": bool(media.og_image_from_html(html, e["url"])) if html else False,
                "page_text": len(ot) >= MIN_BODY,
                "summary": len(listing) >= MIN_BODY or len(ot) >= MIN_BODY,
            }
            row["after"] = {
                "date": bool(new and new.published_at),
                "image": bool(new and new.image),
                "page_text": bool(new and new.ok and len(new.text) >= MIN_BODY),
                "summary": len(listing) >= MIN_BODY or bool(new and new.ok and len(new.text) >= MIN_BODY),
                "status": new.status if new else "no-html",
                "date_from": (new.meta_from or {}).get("published_at") if new else None,
            }
            return row

        rows = await asyncio.gather(*(page(fid, e) for fid, e in entries))
        requests = f.request_count
    return {"feeds": feeds, "rows": rows, "requests": requests}


def table(result: dict) -> str:
    rows = result["rows"]
    keys = ("date", "image", "page_text", "summary")
    out = ["| 源 | 条目 | 发布日期 前→后 | 配图 前→后 | 全文≥200字 前→后 | 摘要可用 前→后 |", "|---|---|---|---|---|---|"]
    by = {}
    for r in rows:
        by.setdefault(r["feed"], []).append(r)

    def cell(rs, k):
        b = sum(r["before"][k] for r in rs); a = sum(r["after"][k] for r in rs)
        return f"{b}→{a}"

    for fid, _ in FEEDS:
        rs = by.get(fid, [])
        if not rs:
            out.append(f"| {fid} | 0 ({result['feeds'].get(fid, {}).get('status', '?')}) | – | – | – | – |")
            continue
        out.append(f"| {fid} | {len(rs)} | " + " | ".join(cell(rs, k) for k in keys) + " |")
    n = len(rows)
    tot = []
    for k in keys:
        b = sum(r["before"][k] for r in rows); a = sum(r["after"][k] for r in rows)
        tot.append(f"{b}/{n} ({b / n:.0%}) → {a}/{n} ({a / n:.0%})" if n else "–")
    out.append(f"| **合计** | {n} | " + " | ".join(tot) + " |")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-feed", type=int, default=6)
    ap.add_argument("--out", default=str(ROOT / "var/compare-m2-2.json"))
    args = ap.parse_args(argv)
    result = asyncio.run(run(args.per_feed))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(table(result))
    print(f"\n页面请求 {result['requests']} 次；明细 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
