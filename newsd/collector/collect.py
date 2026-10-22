#!/usr/bin/env python3
"""Deterministic HN / Reddit / RSS collector for the news-daily keyword list.

Safety: public http(s) only, no credentials, no netrc, no Authorization header.
Interest rules come from config/interests.json. Sources come from
config/collect-sources.json. Items that match no term are counted and sampled
into dropped.jsonl; they are not written to candidates.json.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from xml.etree import ElementTree as ET

UA = "newsd-collect/0.1 (news digest)"
MAX_BYTES = 1_500_000
MAX_REDIRECTS = 3
DEFAULT_TIMEOUT = 12
ATOM = "http://www.w3.org/2005/Atom"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
TRACKING_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "ref",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "si",
}
TITLE_STOP = {
    "the", "a", "an", "of", "and", "to", "for", "on", "in", "with", "from",
    "by", "at", "as", "is", "are", "was", "were", "be", "new", "show", "hn",
}
MEDIA_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".webm", ".svg", ".avif")
REMOVED_TITLES = {"[ removed by moderator ]", "[deleted]", "[removed]"}


class UnsafeURL(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class FetchError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _fakeip_allowed(ip) -> bool:
    """Opt-in for sandboxes whose DNS returns proxy fake-IPs (e.g. 198.18.0.0/15).

    Only applies to *resolved hostnames*; literal private IPs in URLs stay blocked.
    Unset in production, so behaviour there is unchanged.
    """
    raw = os.environ.get("NEWS_COLLECT_FAKEIP_CIDR", "").strip()
    if not raw:
        return False
    try:
        return any(ip in ipaddress.ip_network(part.strip(), strict=False) for part in raw.split(",") if part.strip())
    except ValueError:
        return False


def default_resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None)
    return [item[4][0] for item in infos]


def iri_to_uri(url: str) -> str:
    """Percent-encode non-ASCII so urllib can put the URL on the wire."""
    parts = urllib.parse.urlsplit((url or "").strip())
    host = parts.hostname or ""
    if host:
        try:
            host_ascii = host.encode("idna").decode("ascii")
        except UnicodeError:
            host_ascii = host
        netloc = f"[{host_ascii}]" if ":" in host_ascii else host_ascii  # keep IPv6 literals bracketed
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        if parts.username:
            user = urllib.parse.quote(parts.username, safe="")
            password = f":{urllib.parse.quote(parts.password, safe='')}" if parts.password else ""
            netloc = f"{user}{password}@{netloc}"
    else:
        netloc = parts.netloc
    path = urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/%")
    query = urllib.parse.quote(urllib.parse.unquote(parts.query), safe="=&%")
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, query, ""))


def check_url(url: str, resolve=None) -> None:
    """Reject non-http and addresses that are not public. Does not connect."""
    parts = urllib.parse.urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL(f"non-http:{parts.scheme or 'missing'}")
    if parts.username or parts.password:
        raise UnsafeURL("credentials-in-url")
    host = parts.hostname
    if not host:
        raise UnsafeURL("missing-host")
    name = host.lower().rstrip(".")
    if (
        name in {"localhost", "metadata.google.internal", "metadata.internal"}
        or name.endswith(".local")
        or name.endswith(".localhost")
        or name.endswith(".internal")
    ):
        raise UnsafeURL("local-host")
    try:
        literal = ipaddress.ip_address(name)
    except ValueError:
        literal = None
    if literal is not None:
        if _blocked_ip(literal):
            raise UnsafeURL("private-ip")
        return
    resolver = resolve or default_resolve
    try:
        ips = resolver(name)
    except socket.gaierror as exc:
        raise FetchError(f"dns:{exc}") from exc
    if not ips:
        raise FetchError("dns:empty")
    for raw in ips:
        addr = ipaddress.ip_address(raw)
        if _blocked_ip(addr) and not _fakeip_allowed(addr):
            raise UnsafeURL("resolves-private")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

    def http_error_301(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


def _opener():
    # No auth handler, so netrc and saved credentials are never read.
    return urllib.request.build_opener(_NoRedirect)


def safe_get(url: str, timeout: int = DEFAULT_TIMEOUT, resolve=None, opener=None) -> dict:
    """GET a public URL. Follows at most MAX_REDIRECTS hops, re-checking each."""
    current = iri_to_uri(url)
    client = opener or _opener()
    for _hop in range(MAX_REDIRECTS + 1):
        check_url(current, resolve=resolve)
        req = urllib.request.Request(
            current,
            headers={"User-Agent": UA, "Accept": "*/*"},
            method="GET",
        )
        try:
            with client.open(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200)
                headers = resp.headers
                body = resp.read(MAX_BYTES + 1)
                final = resp.geturl()
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                loc = exc.headers.get("Location") if exc.headers else None
                if not loc:
                    raise FetchError(f"redirect-without-location:{exc.code}") from exc
                current = urllib.parse.urljoin(current, loc)
                continue
            raise FetchError(f"http:{exc.code}") from exc
        except TimeoutError as exc:
            raise FetchError("timeout") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise FetchError("timeout") from exc
            raise FetchError(f"url:{reason}") from exc
        truncated = len(body) > MAX_BYTES
        if truncated:
            body = body[:MAX_BYTES]
        ctype = headers.get("Content-Type", "") if headers else ""
        return {
            "status": status,
            "body": body,
            "content_type": ctype,
            "final_url": final,
            "truncated": truncated,
        }
    raise FetchError("too-many-redirects")


def parse_time(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc)
    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return datetime.fromtimestamp(float(text), timezone.utc)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        dt = None
    if dt is None:
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def in_window(dt: datetime | None, start: datetime, end: datetime) -> bool:
    if dt is None:
        return False
    return start <= dt.astimezone(timezone.utc) <= end


def fold(text: str) -> str:
    text = (text or "").lower()
    text = text.replace("_", " ")
    text = re.sub(r"[\-./]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _needles(term: dict) -> list[str]:
    raw = [term["term"], *term.get("aliases", [])]
    out = []
    for item in raw:
        folded = fold(item)
        if folded and folded not in out:
            out.append(folded)
    return out


def _hit(haystack: str, needle: str) -> bool:
    if not needle or not haystack:
        return False
    if " " in needle or any("\u4e00" <= ch <= "\u9fff" for ch in needle):
        return needle in haystack
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None


def url_match_text(url: str) -> str:
    """Text used for URL matches. Subreddit names are not content."""
    parts = urllib.parse.urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path or ""
    if host.endswith("reddit.com"):
        path = re.sub(r"^/r/[^/]+", "", path, flags=re.I)
        host = ""
    return fold(f"{host} {path} {parts.query}")


def match_terms(title: str, body: str, url: str, terms: list[dict]) -> list[dict]:
    fields = {
        "title": fold(title),
        "body": fold(body),
        "url": url_match_text(url),
    }
    hits = []
    for term in terms:
        needles = _needles(term)
        locations = [loc for loc, value in fields.items() if any(_hit(value, n) for n in needles)]
        if not locations:
            continue
        hits.append(
            {
                "term": term["term"],
                "group": term["group"],
                "locations": locations,
                "broad": bool(term.get("broad")),
            }
        )
    return hits


_BLOCK_TAGS = re.compile(
    r"<(script|style|noscript|svg|iframe|nav|footer)\b[^>]*>.*?</\1>",
    re.I | re.S,
)
_TAGS = re.compile(r"<[^>]+>")


def html_to_text(html: str) -> str:
    if not html:
        return ""
    text = _BLOCK_TAGS.sub(" ", html)
    text = _TAGS.sub(" ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_text(raw: str, limit: int, favor_precision: bool = False, favor_recall: bool = False) -> tuple[str, str, bool]:
    extractor = "html_text"
    text = ""
    if raw and "<" in raw and ">" in raw:
        try:
            import trafilatura  # base dependency since M2-2

            extracted = trafilatura.extract(
                raw, favor_precision=favor_precision, favor_recall=favor_recall,
                include_comments=False, include_tables=True,
            ) or ""
            if len(extracted) >= 80:
                text = extracted
                extractor = "trafilatura"
        except Exception:
            text = ""
    if not text:
        text = html_to_text(raw) if raw and "<" in raw else (raw or "")
        text = re.sub(r"\s+", " ", text).strip()
        extractor = "html_text" if raw and "<" in raw else "plain"
    truncated = len(text) > limit
    return text[:limit], extractor, truncated


def normalize_url(url: str) -> str:
    parts = urllib.parse.urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https"):
        return (url or "").strip()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+$", "", parts.path or "") or "/"
    if host.endswith("reddit.com"):
        match = re.match(r"(/r/[^/]+/comments/[^/]+)", path, re.I)
        if match:
            path = match.group(1).lower()
        host = "reddit.com"
    if host == "news.ycombinator.com":
        query = urllib.parse.parse_qs(parts.query)
        if "id" in query:
            return f"https://news.ycombinator.com/item?id={query['id'][0]}"
    kept = []
    for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=False):
        if key.lower() in TRACKING_KEYS or key.lower().startswith("utm_"):
            continue
        kept.append((key, value))
    kept.sort()
    return urllib.parse.urlunsplit(("https", host, path, urllib.parse.urlencode(kept), ""))


def title_tokens(title: str) -> list[str]:
    return [tok for tok in re.findall(r"[a-z0-9]+", fold(title)) if tok not in TITLE_STOP and len(tok) > 1]


def titles_similar(left: str, right: str) -> bool:
    a = title_tokens(left)
    b = title_tokens(right)
    if len(a) < 3 or len(b) < 3:
        return False
    sa, sb = set(a), set(b)
    union = sa | sb
    if not union:
        return False
    if len(sa & sb) / len(union) >= 0.72:
        return True
    import difflib

    return difflib.SequenceMatcher(None, " ".join(a), " ".join(b)).ratio() >= 0.86


def content_hash(url: str, title: str, text: str) -> str:
    raw = f"{normalize_url(url)}\n{fold(title)}\n{(text or '')[:2000]}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hn_id(url: str) -> str | None:
    match = re.search(r"[?&]id=(\d+)", url or "")
    return match.group(1) if match else None


def _link(source: str, url: str) -> dict:
    return {"source": source, "url": url}


def dedupe(items: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for item in items:
        item_urls = {normalize_url(item["url"])}
        item_urls.update(normalize_url(link["url"]) for link in item.get("source_links", []) if link.get("url"))
        item_hn = {hn_id(url) for url in item_urls}
        item_hn.discard(None)
        found = None
        for prev in merged:
            prev_urls = {normalize_url(prev["url"])}
            prev_urls.update(normalize_url(link["url"]) for link in prev.get("source_links", []) if link.get("url"))
            prev_hn = {hn_id(url) for url in prev_urls}
            prev_hn.discard(None)
            if item_urls & prev_urls or item_hn & prev_hn or titles_similar(prev.get("title", ""), item.get("title", "")):
                found = prev
                break
        if found is None:
            copy = dict(item)
            copy["source_links"] = list(item.get("source_links") or [_link(item["source"], item["url"])])
            copy["matched_terms"] = [dict(term) for term in item.get("matched_terms", [])]
            merged.append(copy)
            continue
        _merge_into(found, item)
    return merged


def _merge_into(prev: dict, item: dict) -> None:
    links = list(prev.get("source_links") or [])
    seen = {(link.get("source"), normalize_url(link.get("url", ""))) for link in links}
    for link in item.get("source_links") or [_link(item["source"], item["url"])]:
        key = (link.get("source"), normalize_url(link.get("url", "")))
        if key not in seen and link.get("url"):
            links.append(link)
            seen.add(key)
    prev["source_links"] = links
    by_term = {term["term"]: term for term in prev.get("matched_terms", [])}
    for term in item.get("matched_terms", []):
        have = by_term.get(term["term"])
        if have is None:
            by_term[term["term"]] = dict(term)
        else:
            locs = list(dict.fromkeys([*have.get("locations", []), *term.get("locations", [])]))
            have["locations"] = locs
    prev["matched_terms"] = list(by_term.values())
    if len(item.get("text") or "") > len(prev.get("text") or ""):
        prev["text"] = item.get("text") or ""
        prev["extractor"] = item.get("extractor")
        prev["text_truncated"] = item.get("text_truncated", False)
    if str(item.get("fetch_status", "")).startswith("ok") and not str(prev.get("fetch_status", "")).startswith("ok"):
        prev["fetch_status"] = item["fetch_status"]
        prev["extractor"] = item.get("extractor")
    if (item.get("points") or 0) > (prev.get("points") or 0):
        prev["points"] = item.get("points")
        prev["comments"] = item.get("comments")
    prev_dt = parse_time(prev.get("published_at"))
    item_dt = parse_time(item.get("published_at"))
    if item_dt and (prev_dt is None or item_dt < prev_dt):
        prev["published_at"] = item.get("published_at")
    # Prefer an article URL over a discussion URL when merging the same event.
    if _discussion_url(prev.get("url", "")) and not _discussion_url(item.get("url", "")):
        prev["url"] = item["url"]
        prev["source"] = item.get("source", prev.get("source"))
        prev["title"] = item.get("title") or prev.get("title")


def _discussion_url(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host.endswith("news.ycombinator.com") or host.endswith("reddit.com")


def _local_name(tag: str) -> str:
    if tag.startswith("{") and "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _child_text(node, names: set[str]) -> str:
    for child in list(node):
        if _local_name(child.tag) in names and child.text:
            return child.text
    return ""


def _atom_link(node) -> str:
    alt = ""
    for child in list(node):
        if _local_name(child.tag) != "link":
            continue
        href = child.get("href") or ""
        rel = (child.get("rel") or "alternate").lower()
        if rel == "alternate" and href:
            return href
        if href and not alt:
            alt = href
    return alt or _child_text(node, {"link"})


def entry_media(node, body: str, link: str) -> list[dict]:
    """Images / audio / video of one feed entry (DEPTAI-216): RSS <enclosure>, Atom link rel=enclosure, Media RSS
    media:content / media:thumbnail (also inside media:group), <img>/<video> and YouTube / v.redd.it links in the body,
    and the entry link itself when it is a media URL. Only http(s); avatars, badges and icons are dropped."""
    from newsd import content as C

    found = []
    for child in node.iter():
        if child is node:
            continue
        ln = _local_name(child.tag)
        ns = child.tag.split("}", 1)[0] if child.tag.startswith("{") else ""
        if ln == "enclosure" or (ln == "link" and (child.get("rel") or "") == "enclosure"):
            found.append(C.media_entry(child.get("url") or child.get("href") or "", child.get("type") or "", "",
                                       base=link))
        elif ln in ("content", "thumbnail") and child.get("url") and ("search.yahoo.com/mrss" in ns or ln == "thumbnail"):
            found.append(C.media_entry(child.get("url"), child.get("type") or "",
                                       "image" if ln == "thumbnail" else child.get("medium") or "",
                                       child.get("width"), child.get("height"), base=link))
    found += C.media_from_html(body, link)
    found += C.link_media(link)
    return C.dedupe_media([e for e in found if e])


def parse_feed(xml_bytes: bytes) -> list[dict]:
    if not xml_bytes:
        return []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    entries = []
    for node in root.iter():
        if _local_name(node.tag) in {"item", "entry"}:
            entries.append(node)
    out = []
    for node in entries:
        title = unescape(_child_text(node, {"title"})).strip()
        link = _atom_link(node).strip()
        published = _child_text(node, {"published", "updated", "pubDate", "date"})
        content = ""
        for child in list(node):
            if _local_name(child.tag) in {"content", "encoded", "description", "summary"}:
                content = "".join(child.itertext()) or child.text or ""
                if len(content) > 40:
                    break
        if not title and not link:
            continue
        enc = None  # podcast / media enclosure (RSS <enclosure> or Atom link rel=enclosure)
        for child in list(node):
            ln = _local_name(child.tag)
            if ln == "enclosure" or (ln == "link" and child.get("rel") == "enclosure"):
                u, t = child.get("url") or child.get("href") or "", (child.get("type") or "").lower()
                if u.startswith("https://") and t.startswith(("audio/", "video/mp4")):
                    enc = {"url": u, "type": t}
                    break
        row = {
            "title": title,
            "url": link,
            "published_at": to_iso(parse_time(published)),
            "listing_text": content,
        }
        if enc:
            row["enclosure"] = enc
        media = entry_media(node, content, link)
        if media:
            row["media"] = media
        out.append(row)
    return out


def parse_hn_hits(payload: dict, query: str) -> list[dict]:
    out = []
    for hit in payload.get("hits") or []:
        object_id = str(hit.get("objectID") or "")
        story_url = hit.get("url") or ""
        discussion = f"https://news.ycombinator.com/item?id={object_id}" if object_id else ""
        primary = story_url or discussion
        if not primary:
            continue
        links = []
        if discussion:
            links.append(_link("hn", discussion))
        if story_url:
            links.append(_link("hn", story_url))
        out.append(
            {
                "source": "hn",
                "url": primary,
                "title": hit.get("title") or "",
                "published_at": to_iso(parse_time(hit.get("created_at") or hit.get("created_at_i"))),
                "points": hit.get("points"),
                "comments": hit.get("num_comments"),
                "listing_text": hit.get("story_text") or "",
                "source_links": links,
                "retrieved_by": query,
                "feed": "hn",
            }
        )
    return out


def parse_reddit_posts(posts: list[dict], transport: str) -> list[dict]:
    out = []
    for post in posts:
        title = post.get("title") or ""
        if title.strip().lower() in REMOVED_TITLES:
            continue
        permalink = post.get("permalink") or ""
        if permalink.startswith("/"):
            permalink = "https://www.reddit.com" + permalink
        url = permalink or post.get("url") or ""
        if not url:
            continue
        created = post.get("created_utc") or post.get("created")
        links = [_link("reddit", url)]
        external = post.get("url") or ""
        if external and normalize_url(external) != normalize_url(url):
            links.append(_link("reddit", external))
        out.append(
            {
                "source": "reddit",
                "url": url,
                "title": title,
                "published_at": to_iso(parse_time(created)),
                "points": post.get("score"),
                "comments": post.get("num_comments"),
                "listing_text": post.get("selftext") or post.get("listing_text") or "",
                "source_links": links,
                "transport": transport,
                "feed": post.get("_feed") or "reddit",
            }
        )
    return out


def _is_media(url: str) -> bool:
    path = urllib.parse.urlsplit(url or "").path.lower()
    return path.endswith(MEDIA_EXT)


def annotate_bodies(items: list[dict], fetch_fn, text_limit: int, min_body: int) -> None:
    pending = []
    for item in items:
        listing = item.get("listing_text") or ""
        text, extractor, truncated = extract_text(listing, text_limit)
        if len(text) >= min_body:
            item["text"] = text
            item["extractor"] = "rss" if item["source"] == "rss" else ("hn_story" if item["source"] == "hn" else "rss")
            item["fetch_status"] = "ok"
            item["text_truncated"] = truncated
            continue
        if _is_media(item.get("url") or "") and len(text) < min_body:
            item["text"] = text
            item["extractor"] = "none"
            item["fetch_status"] = "failed:media"
            item["text_truncated"] = False
            continue
        pending.append(item)

    def _one(item):
        try:
            result = fetch_fn(item["url"])
        except UnsafeURL as exc:
            return item, None, f"failed:{exc.reason}"
        except FetchError as exc:
            return item, None, f"failed:{exc.reason}"
        except Exception as exc:  # noqa: BLE001 — keep the candidate, record the reason
            return item, None, f"failed:{type(exc).__name__}"
        return item, result, None

    if not pending:
        return
    workers = min(6, len(pending))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, item) for item in pending]
        for future in as_completed(futures):
            item, result, error = future.result()
            if error:
                item["text"] = (item.get("text") or html_to_text(item.get("listing_text") or ""))[:text_limit]
                item["extractor"] = "none"
                item["fetch_status"] = error
                item["text_truncated"] = False
                continue
            ctype = (result.get("content_type") or "").lower()
            if ctype.startswith("image/") or ctype.startswith("video/"):
                item["text"] = ""
                item["extractor"] = "none"
                item["fetch_status"] = "failed:media"
                item["text_truncated"] = False
                continue
            if "pdf" in ctype:
                item["text"] = ""
                item["extractor"] = "none"
                item["fetch_status"] = "failed:unsupported_type"
                item["text_truncated"] = False
                continue
            body = result.get("body") or b""
            charset = "utf-8"
            match = re.search(r"charset=([\w.-]+)", ctype)
            if match:
                charset = match.group(1)
            try:
                raw = body.decode(charset, "replace")
            except LookupError:
                raw = body.decode("utf-8", "replace")
            text, extractor, truncated = extract_text(raw, text_limit)
            item["text"] = text
            item["extractor"] = extractor
            item["text_truncated"] = truncated
            if len(text) >= min_body:
                item["fetch_status"] = "ok"
            else:
                item["fetch_status"] = "failed:empty"


def finalize_candidate(item: dict) -> dict:
    text = item.get("text") or ""
    url = item.get("url") or ""
    title = item.get("title") or ""
    out = {
        "source": item.get("source"),
        "url": url,
        "title": title,
        "published_at": item.get("published_at"),
        "points": item.get("points"),
        "comments": item.get("comments"),
        "matched_terms": [
            {
                "term": term["term"],
                "group": term["group"],
                "locations": term.get("locations") or [],
            }
            for term in item.get("matched_terms", [])
        ],
        "text": text,
        "extractor": item.get("extractor") or "none",
        "fetch_status": item.get("fetch_status") or "failed:not_fetched",
        "content_hash": content_hash(url, title, text),
        "source_links": item.get("source_links") or [_link(item.get("source"), url)],
    }
    return out


class DropSampler:
    def __init__(self, limit: int = 120, seed: int = 181):
        self.limit = limit
        self.rng = random.Random(seed)
        self.rows: list[dict] = []
        self.seen = 0

    def add(self, row: dict) -> None:
        self.seen += 1
        if len(self.rows) < self.limit:
            self.rows.append(row)
            return
        slot = self.rng.randrange(self.seen)
        if slot < self.limit:
            self.rows[slot] = row


def filter_and_dedupe(raw_items: list[dict], terms: list[dict], sampler: DropSampler) -> tuple[list[dict], dict]:
    counts = {"fetched": len(raw_items), "matched": 0, "dropped": 0, "reasons": {}}
    matched = []
    for item in raw_items:
        title = item.get("title") or ""
        if title.strip().lower() in REMOVED_TITLES:
            _count_reason(counts, "removed")
            continue
        hits = match_terms(title, item.get("listing_text") or "", item.get("url") or "", terms)
        if not hits:
            _count_reason(counts, "no_term_match")
            sampler.add(
                {
                    "source": item.get("source"),
                    "url": item.get("url"),
                    "title": title,
                    "published_at": item.get("published_at"),
                    "reason": "no_term_match",
                    "feed": item.get("feed"),
                    "listing_excerpt": html_to_text(item.get("listing_text") or "")[:280],
                }
            )
            continue
        item = dict(item)
        item["matched_terms"] = hits
        item["source_links"] = list(item.get("source_links") or [_link(item["source"], item["url"])])
        matched.append(item)
    counts["matched"] = len(matched)
    deduped = dedupe(matched)
    counts["deduped"] = len(deduped)
    return deduped, counts


def _count_reason(counts: dict, reason: str) -> None:
    counts["dropped"] += 1
    counts["reasons"][reason] = counts["reasons"].get(reason, 0) + 1


def pipeline(raw_items, terms, fetch_fn, text_limit=4000, min_body=200, sampler=None, skip_body=False) -> dict:
    sampler = sampler or DropSampler()
    matched, counts = filter_and_dedupe(raw_items, terms, sampler)
    if skip_body:
        for item in matched:
            text, extractor, truncated = extract_text(item.get("listing_text") or "", text_limit)
            item["text"] = text
            item["extractor"] = extractor if text else "none"
            item["text_truncated"] = truncated
            item["fetch_status"] = "ok" if len(text) >= min_body else "failed:skipped"
    else:
        annotate_bodies(matched, fetch_fn, text_limit, min_body)
    candidates = [finalize_candidate(item) for item in matched]
    body_ok = sum(1 for item in candidates if str(item["fetch_status"]).startswith("ok"))
    body_failed = len(candidates) - body_ok
    return {
        "candidates": candidates,
        "counts": counts,
        "body_ok": body_ok,
        "body_failed": body_failed,
        "dropped_sample": sampler.rows,
        "dropped_seen": sampler.seen,
    }


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


class CachedFetch:
    def __init__(self, cache_dir: Path | None, timeout: int):
        self.cache_dir = cache_dir
        self.timeout = timeout
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    def __call__(self, url: str) -> dict:
        key = _cache_key(url)
        meta_path = self.cache_dir / f"{key}.meta.json" if self.cache_dir else None
        body_path = self.cache_dir / f"{key}.body" if self.cache_dir else None
        if meta_path and meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            body = body_path.read_bytes() if body_path and body_path.exists() else b""
            return {
                "status": meta.get("status", 200),
                "body": body,
                "content_type": meta.get("content_type", ""),
                "final_url": meta.get("final_url", url),
                "truncated": meta.get("truncated", False),
            }
        result = safe_get(url, timeout=self.timeout)
        if meta_path and body_path:
            body_path.write_bytes(result["body"])
            meta_path.write_text(
                json.dumps(
                    {
                        "status": result["status"],
                        "content_type": result["content_type"],
                        "final_url": result["final_url"],
                        "truncated": result["truncated"],
                    }
                ),
                encoding="utf-8",
            )
        return result


def _get_bytes(url: str, timeout: int, cache: CachedFetch | None = None) -> tuple[bytes, str]:
    fetcher = cache or (lambda u: safe_get(u, timeout=timeout))
    result = fetcher(url)
    return result["body"], result.get("content_type") or ""


def fetch_hn(sources: dict, start: datetime, end: datetime, timeout: int, cache: CachedFetch | None) -> tuple[list[dict], dict]:
    cfg = sources["hn"]
    start_i = int(start.timestamp())
    end_i = int(end.timestamp())
    stats = {"fetched": 0, "errors": [], "queries": {}}
    items = []
    seen = set()

    def one(query: str, tag: str, points: int, pages: int):
        found = []
        errors = []
        for page in range(pages):
            params = {
                "query": query,
                "tags": tag,
                "numericFilters": f"created_at_i>={start_i},created_at_i<={end_i},points>{points}",
                "hitsPerPage": str(cfg.get("hits_per_page", 10)),
                "page": str(page),
            }
            url = cfg["endpoint"] + "?" + urllib.parse.urlencode(params)
            try:
                body, _ctype = _get_bytes(url, timeout, cache)
                payload = json.loads(body.decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{query}/{tag}/p{page}:{type(exc).__name__}:{exc}")
                break
            hits = parse_hn_hits(payload, query)
            if not hits:
                break
            found.extend(hits)
            if len(payload.get("hits") or []) < int(cfg.get("hits_per_page", 10)):
                break
        return found, errors

    jobs = []
    for query in cfg["queries"]:
        jobs.append((query, "story", int(cfg.get("points_min", 5)), int(cfg.get("max_pages", 3))))
        jobs.append((query, "show_hn", int(cfg.get("show_points_min", 1)), 1))
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(one, *job): job for job in jobs}
        for future in as_completed(futures):
            found, errors = future.result()
            stats["errors"].extend(errors)
            for item in found:
                key = normalize_url(item["url"]) + "|" + (hn_id(item["url"]) or "")
                extra = hn_id(next((link["url"] for link in item["source_links"] if "item?id=" in link["url"]), ""))
                key = extra or key
                if key in seen:
                    continue
                seen.add(key)
                items.append(item)
    stats["fetched"] = len(items)
    if not items and stats["errors"]:
        # Spec fallback: unfiltered recent stories, then local window + term filter.
        url = cfg["endpoint"] + "?" + urllib.parse.urlencode({"tags": "story", "hitsPerPage": "20"})
        try:
            body, _ctype = _get_bytes(url, timeout, cache)
            payload = json.loads(body.decode("utf-8"))
            for item in parse_hn_hits(payload, ""):
                dt = parse_time(item.get("published_at"))
                if in_window(dt, start, end):
                    items.append(item)
            stats["fallback"] = "unfiltered-search_by_date"
            stats["fetched"] = len(items)
        except Exception as exc:  # noqa: BLE001
            stats["errors"].append(f"fallback:{type(exc).__name__}:{exc}")
    return items, stats


def _reddit_rss_url(feed: dict) -> str:
    sub = feed["subreddit"]
    if feed.get("mode") == "search":
        query = urllib.parse.urlencode(
            {"q": feed.get("query", ""), "restrict_sr": "1", "sort": "new"}
        )
        return f"https://www.reddit.com/r/{sub}/search.rss?{query}"
    return f"https://www.reddit.com/r/{sub}/new.rss"


def _feed_covers(entries: list[dict], start: datetime) -> bool:
    times = [parse_time(item.get("published_at")) for item in entries]
    times = [item for item in times if item]
    if not times:
        return False
    return min(times) <= start


def _pullpush(feed: dict, start: datetime, end: datetime, timeout: int) -> list[dict]:
    params = {
        "subreddit": feed["subreddit"],
        "after": str(int(start.timestamp())),
        "before": str(int(end.timestamp())),
        "size": "100",
        "sort": "desc",
        "sort_type": "created_utc",
    }
    if feed.get("mode") == "search" and feed.get("query"):
        params["q"] = feed["query"]
    url = "https://api.pullpush.io/reddit/search/submission/?" + urllib.parse.urlencode(params)
    body, _ctype = _get_bytes(url, timeout, None)
    payload = json.loads(body.decode("utf-8"))
    data = payload.get("data") or []
    if isinstance(data, dict):
        data = data.get("children") or []
    posts = []
    for row in data:
        post = row.get("data") if isinstance(row, dict) and "data" in row else row
        if isinstance(post, dict):
            post = dict(post)
            post["_feed"] = feed["subreddit"]
            posts.append(post)
    return posts


def _arctic(feed: dict, start: datetime, end: datetime, timeout: int) -> list[dict]:
    posts = []
    before = int(end.timestamp())
    after = int(start.timestamp())
    for _page in range(3):
        params = {
            "subreddit": feed["subreddit"],
            "after": str(after),
            "before": str(before),
            "limit": "100",
        }
        url = "https://arctic-shift.photon-reddit.com/api/posts/search?" + urllib.parse.urlencode(params)
        body, _ctype = _get_bytes(url, timeout, None)
        payload = json.loads(body.decode("utf-8"))
        batch = payload.get("data") or []
        if not batch:
            break
        for post in batch:
            post = dict(post)
            post["_feed"] = feed["subreddit"]
            posts.append(post)
        if len(batch) < 100:
            break
        oldest = min(int(post.get("created_utc") or before) for post in batch)
        if oldest <= after:
            break
        before = oldest - 1
        time.sleep(0.3)
    return posts


def fetch_reddit(sources: dict, start: datetime, end: datetime, timeout: int, cache: CachedFetch | None) -> tuple[list[dict], dict]:
    stats = {
        "fetched": 0,
        "errors": [],
        "transports": {},
        "rss_covers_window": {},
        "replay": {},
    }
    items = []
    for feed in sources["reddit"]["feeds"]:
        name = f"{feed['subreddit']}:{feed.get('mode', 'new')}"
        rss_url = _reddit_rss_url(feed)
        entries = []
        transport = None
        try:
            body, ctype = _get_bytes(rss_url, timeout, cache)
            if b"<feed" in body[:500] or b"<rss" in body[:500] or "xml" in ctype:
                entries = parse_feed(body)
                transport = "reddit-rss"
            else:
                stats["errors"].append(f"{name}:not_a_feed")
        except FetchError as exc:
            stats["errors"].append(f"{name}:rss:{exc.reason}")
            if exc.reason in {"http:403", "http:401"} or "403" in exc.reason:
                old = rss_url.replace("https://www.reddit.com", "https://old.reddit.com")
                try:
                    body, ctype = _get_bytes(old, timeout, None)
                    if b"<feed" in body[:800] or b"<rss" in body[:800]:
                        entries = parse_feed(body)
                        transport = "old.reddit"
                    else:
                        stats["errors"].append(f"{name}:old.reddit:not_a_feed")
                except Exception as old_exc:  # noqa: BLE001
                    stats["errors"].append(f"{name}:old.reddit:{old_exc}")
        except Exception as exc:  # noqa: BLE001
            stats["errors"].append(f"{name}:rss:{type(exc).__name__}")
        in_win = []
        for entry in entries:
            dt = parse_time(entry.get("published_at"))
            if not in_window(dt, start, end):
                continue
            in_win.append(
                {
                    "source": "reddit",
                    "url": entry["url"],
                    "title": entry["title"],
                    "published_at": entry["published_at"],
                    "points": None,
                    "comments": None,
                    "listing_text": entry.get("listing_text") or "",
                    "source_links": [_link("reddit", entry["url"])],
                    "transport": transport or "reddit-rss",
                    "feed": name,
                }
            )
        covers = _feed_covers(entries, start)
        stats["rss_covers_window"][name] = covers
        if covers and in_win:
            items.extend(in_win)
            stats["transports"][transport or "reddit-rss"] = stats["transports"].get(transport or "reddit-rss", 0) + len(in_win)
            stats["replay"][name] = "rss"
            continue
        # RSS did not cover the historical window. Spec chain: PullPush, then a public archive.
        archive_posts = None
        archive_transport = None
        try:
            archive_posts = _pullpush(feed, start, end, timeout)
            archive_transport = "pullpush"
        except Exception as exc:  # noqa: BLE001
            stats["errors"].append(f"{name}:pullpush:{type(exc).__name__}:{exc}")
            try:
                archive_posts = _arctic(feed, start, end, timeout)
                archive_transport = "arctic-shift"
            except Exception as arc_exc:  # noqa: BLE001
                stats["errors"].append(f"{name}:arctic-shift:{type(arc_exc).__name__}:{arc_exc}")
                stats["replay"][name] = "unavailable"
                items.extend(in_win)
                continue
        parsed = parse_reddit_posts(archive_posts or [], archive_transport or "archive")
        kept = []
        for item in parsed:
            dt = parse_time(item.get("published_at"))
            if in_window(dt, start, end):
                item["feed"] = name
                kept.append(item)
        items.extend(kept)
        stats["transports"][archive_transport] = stats["transports"].get(archive_transport, 0) + len(kept)
        stats["replay"][name] = archive_transport
        if in_win:
            items.extend(in_win)
    # Dedupe listing URLs before the interest filter so search+new don't double-count drops.
    uniq = []
    seen = set()
    for item in items:
        key = normalize_url(item["url"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(item)
    stats["fetched"] = len(uniq)
    return uniq, stats


def _looks_like_feed(body: bytes, ctype: str) -> bool:
    head = body[:800].lstrip().lower()
    return head.startswith(b"<?xml") or head.startswith(b"<feed") or head.startswith(b"<rss") or "xml" in (ctype or "").lower()


def fetch_rss(sources: dict, start: datetime, end: datetime, timeout: int, cache: CachedFetch | None) -> tuple[list[dict], dict]:
    stats = {"fetched": 0, "errors": [], "feeds": {}}
    items = []
    for feed in sources["rss"]:
        urls = feed.get("urls") or [feed["url"]]
        parsed = None
        used = None
        last_error = None
        for url in urls:
            try:
                body, ctype = _get_bytes(url, timeout, cache)
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}:{exc}"
                continue
            if not _looks_like_feed(body, ctype):
                last_error = "not_a_feed"
                continue
            parsed = parse_feed(body)
            used = url
            break
        if parsed is None:
            stats["errors"].append(f"{feed['id']}:{last_error or 'failed'}")
            stats["feeds"][feed["id"]] = {"status": f"failed:{last_error or 'failed'}", "in_window": 0}
            continue
        kept = 0
        oldest = None
        for entry in parsed:
            dt = parse_time(entry.get("published_at"))
            if dt and (oldest is None or dt < oldest):
                oldest = dt
            if not in_window(dt, start, end):
                continue
            items.append(
                {
                    "source": "rss",
                    "url": entry["url"],
                    "title": entry["title"],
                    "published_at": entry["published_at"],
                    "points": None,
                    "comments": None,
                    "listing_text": entry.get("listing_text") or "",
                    **({"enclosure": entry["enclosure"]} if entry.get("enclosure") else {}),
                    **({"media": entry["media"]} if entry.get("media") else {}),
                    "source_links": [_link("rss", entry["url"])],
                    "feed": feed["id"],
                    "transport": "atom" if used and "atom" in used else "rss",
                }
            )
            kept += 1
        covers = bool(oldest and oldest <= start) or kept > 0 and len(parsed) < 5
        # GitHub release atoms only carry the latest page. If the oldest entry is
        # after the window start, earlier days in a long window are not replayable.
        stats["feeds"][feed["id"]] = {
            "status": "ok",
            "url": used,
            "entries": len(parsed),
            "in_window": kept,
            "oldest": to_iso(oldest),
            "covers_window_start": bool(oldest and oldest <= start),
        }
        if not covers and kept == 0 and oldest and oldest > end:
            stats["feeds"][feed["id"]]["replay"] = "feed_has_no_items_in_window"
        elif oldest and oldest > start and kept == 0:
            stats["feeds"][feed["id"]]["replay"] = "not_replayable"
    stats["fetched"] = len(items)
    return items, stats


def collect(start: datetime, end: datetime, interests: dict, sources: dict, fetch_fn, timeout: int, cache: CachedFetch | None, enabled: set[str], skip_body: bool = False) -> dict:
    begun = time.time()
    sampler = DropSampler()
    raw = []
    source_stats = {}
    if "hn" in enabled:
        hn_items, hn_stats = fetch_hn(sources, start, end, timeout, cache)
        raw.extend(hn_items)
        source_stats["hn"] = hn_stats
    if "reddit" in enabled:
        reddit_items, reddit_stats = fetch_reddit(sources, start, end, timeout, cache)
        raw.extend(reddit_items)
        source_stats["reddit"] = reddit_stats
    if "rss" in enabled:
        rss_items, rss_stats = fetch_rss(sources, start, end, timeout, cache)
        raw.extend(rss_items)
        source_stats["rss"] = rss_stats
    result = pipeline(
        raw,
        interests["terms"],
        fetch_fn,
        text_limit=int(interests.get("text_limit", 4000)),
        min_body=int(interests.get("min_body_chars", 200)),
        sampler=sampler,
        skip_body=skip_body,
    )
    by_source = {}
    for item in raw:
        by_source.setdefault(item["source"], {"fetched": 0})
        by_source[item["source"]]["fetched"] += 1
    for item in result["candidates"]:
        bucket = by_source.setdefault(item["source"], {"fetched": 0})
        bucket["matched"] = bucket.get("matched", 0) + 1
        if str(item["fetch_status"]).startswith("ok"):
            bucket["body_ok"] = bucket.get("body_ok", 0) + 1
        else:
            bucket["body_failed"] = bucket.get("body_failed", 0) + 1
    # Drops are counted before cross-source dedupe, so attribute them from the sampler reasons globally.
    elapsed = round(time.time() - begun, 2)
    body_total = result["body_ok"] + result["body_failed"]
    stats = {
        "window": {"start": to_iso(start), "end": to_iso(end)},
        "elapsed_s": elapsed,
        "fetched": len(raw),
        "matched_before_dedupe": result["counts"]["matched"],
        "candidates": len(result["candidates"]),
        "dropped": result["counts"]["dropped"],
        "drop_reasons": result["counts"]["reasons"],
        "dropped_sampled": len(result["dropped_sample"]),
        "body_ok": result["body_ok"],
        "body_failed": result["body_failed"],
        "body_success_rate": round(result["body_ok"] / body_total, 4) if body_total else None,
        "by_source_candidates": by_source,
        "sources": source_stats,
    }
    return {"candidates": result["candidates"], "dropped": result["dropped_sample"], "stats": stats}


def write_output(out_dir: Path, payload: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "window": payload["stats"]["window"],
        "candidates": payload["candidates"],
    }
    (out_dir / "candidates.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "stats.json").write_text(json.dumps(payload["stats"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (out_dir / "dropped.jsonl").open("w", encoding="utf-8") as handle:
        for row in payload["dropped"]:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect HN, Reddit, and RSS candidates for the news-daily keyword list.")
    parser.add_argument("--start", required=True, help="Window start, ISO-8601")
    parser.add_argument("--end", required=True, help="Window end, ISO-8601")
    parser.add_argument("--out", required=True, help="Output directory")
    parser.add_argument("--interests", default="config/interests.json")
    parser.add_argument("--sources", default="config/collect-sources.json")
    parser.add_argument("--cache", default="")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--only", default="hn,reddit,rss", help="Comma-separated sources")
    parser.add_argument("--skip-body", action="store_true", help="Do not fetch article pages; listing text only")
    args = parser.parse_args(argv)
    start = parse_time(args.start)
    end = parse_time(args.end)
    if start is None or end is None or start >= end:
        print("invalid window", file=sys.stderr)
        return 2
    interests = load_json(Path(args.interests))
    sources = load_json(Path(args.sources))
    cache = CachedFetch(Path(args.cache), args.timeout) if args.cache else None
    fetch_fn = cache if cache else (lambda url: safe_get(url, timeout=args.timeout))
    enabled = {part.strip() for part in args.only.split(",") if part.strip()}
    payload = collect(start, end, interests, sources, fetch_fn, args.timeout, cache, enabled, skip_body=args.skip_body)
    write_output(Path(args.out), payload)
    print(json.dumps({
        "out": args.out,
        "candidates": payload["stats"]["candidates"],
        "dropped": payload["stats"]["dropped"],
        "elapsed_s": payload["stats"]["elapsed_s"],
        "body_success_rate": payload["stats"]["body_success_rate"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
