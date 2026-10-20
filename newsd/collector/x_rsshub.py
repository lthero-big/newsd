"""X account timelines via self-hosted RSSHub (/twitter/user/<handle>), deterministic, no AI.

    python3 scripts/collect/x_rsshub.py --out var/x-rsshub/<date>-am.json [--hours 26] [--per 2] [--pace 2]

Enabled accounts from /sources (var/state/sources.json via sync-state) in fixed order, one request each with
polite pacing (20 s timeout, 2 retries with backoff on TLS/connection errors, circuit breaker after 5 accounts fail in a
row so a broken proxy costs ~1 min, not 9); keeps the newest `--per` original posts (no replies / retweets) inside the window (特别关注 gets
per+1). Output rows use the x-items format (merged by x_from_connector.py --extra) plus {"ok": [...], "failed": [...]}:
accounts in "ok" are dropped from the connector's from: queries (x_queries.py --skip-ok), failed ones fall back to it.
RSSHub's feed has no like counts, so like thresholds cannot apply on this path (likes=null); volume is bounded by
--per and the X cap per module (2) in generate.py. The access key never leaves this process (errors are scrubbed).
"""
from __future__ import annotations

import argparse
import html
import http.client
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import source_config as sc  # noqa: E402


def parse_item(it: dict, handle: str) -> dict | None:
    body = it.get("content_html") or ""
    text = html.unescape(re.sub(r"<br\s*/?>", "\n", body))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text).strip()
    url = (it.get("url") or "").replace("twitter.com", "x.com")
    if not re.match(rf"https://x\.com/{re.escape(handle)}/status/\d+$", url, re.I):
        return None  # retweets/quotes of other accounts
    if len(re.sub(r"(https?://\S+|#\w+|@\w+)", "", text).strip()) < 40:
        return None
    media = []
    for m in re.finditer(r"<(img|video)\b([^>]*)>", body):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(2)))
        src = html.unescape(attrs.get("poster") if m.group(1) == "video" else attrs.get("src") or "")
        w, h = int(attrs.get("width") or 0), int(attrs.get("height") or 0)
        if src.startswith("https://pbs.twimg.com/") and "profile_images" not in src and w and h:
            row = {"url": src, "width": w, "height": h, "type": "video" if m.group(1) == "video" else "photo"}
            vs = html.unescape(attrs.get("src") or "") if m.group(1) == "video" else ""
            if vs.startswith("https://video.twimg.com/"):
                row["video"] = vs
            media.append(row)
    au = (it.get("authors") or [{}])[0]
    return {"url": url, "text": text, "author": handle, "author_name": au.get("name"),
            "avatar": (au.get("avatar") or "").replace("_normal.", "_400x400.") or None, "verified": False,
            "created_at": it.get("date_published"), "likes": None, "reposts": None, "replies": None,
            "media": media, "transport": "rsshub"}


def fetch(base: str, key: str, handle: str, timeout: int) -> list[dict]:
    route = f"/twitter/user/{urllib.parse.quote(handle)}/excludeReplies=1&includeRts=0"
    url = f"{base}{route}?" + urllib.parse.urlencode({"key": key, "format": "json", "limit": 10})
    req = urllib.request.Request(url, headers={"User-Agent": "news-daily-x/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()).get("items") or []


TIMEOUT = 20          # per request; a healthy RSSHub answers in 1–3 s
RETRY_BACKOFF = (2.0, 5.0)  # sleeps before attempts 2 and 3, only for transient errors (TLS EOF, reset, timeout, 5xx/429)
BREAKER = 5           # this many accounts failing in a row → stop, the rest go to the X connector (x_queries.py)


def transient(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500 or exc.code == 429
    return isinstance(exc, (urllib.error.URLError, OSError, TimeoutError, ConnectionError, ssl.SSLError, http.client.HTTPException))


def fetch_retry(fetcher, base, key, h, timeout, backoff=RETRY_BACKOFF, sleep=time.sleep):
    for n, wait in enumerate((0.0, *backoff)):
        if wait:
            sleep(wait)
        try:
            return fetcher(base, key, h, timeout)
        except Exception as exc:  # noqa: BLE001
            if n == len(backoff) or not transient(exc):
                raise


def run(accts, base, key, hours=26, per=2, pace=2.0, timeout=TIMEOUT, low_groups=("特别关注",), fetcher=fetch, now=None,
        backoff=RETRY_BACKOFF, breaker=BREAKER, sleep=time.sleep):
    since = (now or datetime.now(timezone.utc)) - timedelta(hours=hours)
    out, ok, failed = [], [], []
    scrub = (lambda e: str(e).replace(key, "***")) if key else str
    streak, tripped = 0, None
    for n, (h, g, _) in enumerate(accts):
        if tripped:
            failed.append({"handle": h, "error": f"skipped: {breaker} accounts failed in a row ({tripped})"[:160]})
            continue
        if n and pace:
            sleep(pace)
        try:
            raw = fetch_retry(fetcher, base, key, h, timeout, backoff, sleep)
        except Exception as exc:  # noqa: BLE001
            failed.append({"handle": h, "error": scrub(exc)[:160]})
            streak += 1
            if breaker and streak >= breaker:
                tripped = scrub(exc)[:80]
                print(f"x-rsshub: circuit open after {streak} consecutive failures, {len(accts) - n - 1} accounts left to the connector: {tripped}", file=sys.stderr)
            continue
        streak = 0
        ok.append(h)
        keep = per + (1 if g in low_groups else 0)
        for it in raw:
            row = parse_item(it, h)
            try:
                dt = datetime.fromisoformat((row or {}).get("created_at", "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if row and dt >= since and keep > 0:
                row["group"] = g
                out.append(row); keep -= 1
    res = {"items": out, "ok": ok, "failed": failed, "likes_available": False}
    if tripped:
        res["circuit_open"] = tripped
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--hours", type=float, default=26)
    ap.add_argument("--per", type=int, default=2)
    ap.add_argument("--pace", type=float, default=2.0)
    a = ap.parse_args(argv)
    base, key = os.environ.get("RSSHUB_BASE", "").rstrip("/"), os.environ.get("RSSHUB_ACCESS_KEY", "")
    sources = json.loads((sc.ROOT / "config/collect-sources.json").read_text(encoding="utf-8"))
    accts = sc.x_accounts(sources, sc.load_settings())
    if not base or not key:
        res = {"items": [], "ok": [], "failed": [], "skipped": "RSSHUB_BASE/RSSHUB_ACCESS_KEY 未配置"}
    else:
        res = run(accts, base, key, a.hours, a.per, a.pace, low_groups=tuple((sources.get("x") or {}).get("low_threshold_groups") or ["特别关注"]))
        if accts and not res["ok"]:
            res["skipped"] = "RSSHub 全部失败，账号走连接器"
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"x-rsshub: {len(res['items'])} items, ok {len(res['ok'])}/{len(accts)}, failed {len(res['failed'])} -> {a.out}")


if __name__ == "__main__":
    sys.exit(main())
