"""Deterministic helpers: canonical URLs (cross-edition dedupe) and publish dates of linked content (旧闻重提).

published(url) tries, in order: YouTube (watch-page uploadDate/publishDate → oEmbed author → channel RSS),
GitHub release (API published_at), article HTML (article:published_time / JSON-LD datePublished / itemprop / <time>).
Hits are cached in var/cache/linkdates.json. Unknown -> None (caller skips)."""
import json, re, threading, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "var/cache/linkdates.json"
SEEN = ROOT / "data/state/seen-links.json"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36", "Accept-Language": "en"}
TRACK = re.compile(r"^(utm_\w+|fbclid|gclid|dclid|mc_cid|mc_eid|igshid|si|ref|ref_src|ref_url|s|t|feature|share|source|spm|from|cmpid|ocid|__twitter_impression)$", re.I)
_lock = threading.Lock()


def canon(url: str) -> str:
    try:
        u = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return url or ""
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    path, q = u.path or "/", urllib.parse.parse_qsl(u.query, keep_blank_values=False)
    if host == "youtu.be":
        host, q, path = "youtube.com", [("v", path.strip("/"))], "/watch"
    elif host == "youtube.com" and path.startswith(("/shorts/", "/live/")):
        q, path = [("v", path.split("/")[2])], "/watch"
    if host == "youtube.com" and path == "/watch":
        q = [(k, v) for k, v in q if k == "v"]
    elif host in ("x.com", "twitter.com", "mobile.twitter.com"):
        host, q = "x.com", []
        m = re.match(r"/[^/]+/status/(\d+)", path)
        if m:
            path = f"/i/status/{m.group(1)}"
    else:
        q = [(k, v) for k, v in q if not TRACK.match(k)]
    path = re.sub(r"/+$", "", path) or ""
    return urllib.parse.urlunsplit(("https", host, path, urllib.parse.urlencode(sorted(q)), ""))


def _get(url: str, timeout: int = 8, n: int = 400_000) -> str:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read(n).decode("utf-8", "replace")


def _iso(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s.replace("Z", "+00:00"), fmt)
            return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            continue
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


META = [r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|og:published_time|datePublished|pubdate|publish-date|parsely-pub-date|date)["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:article:published_time|datePublished)["\']',
        r'"datePublished"\s*:\s*"([^"]+)"', r'"uploadDate"\s*:\s*"([^"]+)"', r'itemprop=["\'](?:datePublished|uploadDate)["\'][^>]+content=["\']([^"\']+)',
        r'<time[^>]+datetime=["\']([^"\']+)']


def from_html_regex(html: str) -> str | None:
    """Pre-trafilatura regex parser; now the fallback when trafilatura finds no date."""
    for pat in META:
        m = re.search(pat, html or "", re.I)
        if m and _iso(m.group(1)):
            return _iso(m.group(1))
    return None


def from_html(html: str) -> str | None:
    """Publish date of an article page: trafilatura metadata first (meta tags, JSON-LD, <time>, ...),
    then the regex patterns above."""
    try:
        from trafilatura import extract_metadata

        doc = extract_metadata(html, date_config={"original_date": True, "extensive_search": False,
                                                  "outputformat": "%Y-%m-%dT%H:%M:%S%z"})
        d = _iso(doc.date) if doc is not None and doc.date else None
    except Exception:  # noqa: BLE001 - fall back to the regex parser
        d = None
    return d or from_html_regex(html)


def _youtube(vid: str) -> str | None:
    try:
        d = from_html(_get(f"https://www.youtube.com/watch?v={vid}"))
        if d:
            return d
    except Exception:
        pass
    try:  # watch page blocked/rate-limited: oEmbed author -> channel id -> channel RSS (last 15 uploads)
        au = json.loads(_get(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid}&format=json"))["author_url"]
        cid = re.search(r'"externalId":"(UC[\w-]{22})"', _get(au, n=3_000_000)).group(1)
        rss = _get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
        m = re.search(rf"<yt:videoId>{vid}</yt:videoId>.*?<published>([^<]+)", rss, re.S)
        return _iso(m.group(1)) if m else None
    except Exception:
        return None


def _fetch(url: str) -> str | None:
    c = canon(url)
    m = re.match(r"https://youtube\.com/watch\?v=([\w-]{6,})", c)
    if m:
        return _youtube(m.group(1))
    m = re.match(r"https://github\.com/([\w.-]+)/([\w.-]+)/releases/tag/([^/?#]+)", c)
    if m:
        try:
            return _iso(json.loads(_get(f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}/releases/tags/{m.group(3)}"))["published_at"])
        except Exception:
            return None
    if re.match(r"https://(x\.com|news\.ycombinator\.com|reddit\.com|github\.com/[\w.-]+/[\w.-]+$|huggingface\.co)", c):
        return None  # repo/model pages and discussion pages have no single publish date
    try:
        return from_html(_get(url))
    except Exception:
        return None


def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def published(url: str, cache: dict | None = None) -> str | None:
    c = canon(url)
    cache = _CACHE if cache is None else cache
    if c in cache:
        return cache[c]
    d = _fetch(url)
    with _lock:
        cache[c] = d
    return d


_CACHE = _load(CACHE)


def save_cache() -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({k: v for k, v in _CACHE.items() if v}, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")  # misses are retried next run


# ---- cross-edition seen-links index (committed: data/state/seen-links.json, {canonical_url: first_date}) ----
def card_links(card: dict) -> list[str]:
    out = [card.get("url") or "", card.get("linked") or ""]
    return [canon(u) for u in out if u]


def load_seen() -> dict:
    return _load(SEEN)


def update_seen(daily: dict) -> int:
    seen, n = load_seen(), 0
    for c in daily.get("items", []):
        for u in card_links(c):
            if u not in seen:
                seen[u] = daily["date"]; n += 1
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    SEEN.write_text(json.dumps(dict(sorted(seen.items())), ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    return n


if __name__ == "__main__":  # python3 scripts/collect/linkdates.py update data/<date>.json [...]
    import sys
    if sys.argv[1:2] == ["update"]:
        for f in sys.argv[2:]:
            print(f"seen-links: +{update_seen(json.loads(Path(f).read_text(encoding='utf-8')))} from {f}")
