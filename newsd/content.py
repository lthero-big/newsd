"""Display-quality helpers shared by the pool, the issue builder and the pages (DEPTAI-216).

Deterministic, no network, no model:

* ``clean_text``      -- feed / changelog text -> readable plain text: markdown links, emphasis, code ticks and
                         backslash escapes (``test\\_kpool``) removed, PR refs / co-author trailers / release-asset
                         boilerplate dropped, a commit title repeated as its body's first line collapsed.
* ``clean_summary``   -- ``clean_text`` truncated at a sentence boundary (fallback summary when no model runs).
* ``display_title``   -- bare GitHub release titles (``b11801``, ``v0.42.3``, ``[**b11801**](...)``) become
                         ``llama.cpp b11801: <first line of the notes>``; markdown in any title is stripped.
* ``feed_media`` / ``media_from_html`` / ``link_media`` -- images, audio, video and YouTube links found in a feed entry.
* ``safe_media``      -- the media list as rendered: http(s) only, known types, capped.

The release-title shape ("<repo> <tag>") follows Horizon's GitHub release items ("owner/repo released tag"); see
THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

import re
import urllib.parse
from html import unescape

# ------------------------------------------------------------------------------------------------ text cleaning

_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^()\s]|\([^)]*\))+\)")
_MD_EMPH = re.compile(r"(\*\*|__|~~)(.+?)\1")
_MD_STAR = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_MD_CODE = re.compile(r"`{1,3}([^`]*)`{1,3}")
_MD_ESC = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|<>~])")
_MD_HEAD = re.compile(r"(^|\n)\s{0,3}#{1,6}\s+")
_TAGS = re.compile(r"<[^>]+>")
_PR_REF = re.compile(r"\(\s*#\d+\s*\)|\(\s*(?:https?://github\.com/\S+/pull/\d+)\s*\)")
_TRAILERS = re.compile(r"\b(?:Co-authored-by|Signed-off-by|Reviewed-by|Approved-by)\s*:\s*[^\n]*?(?=$|\n|\s(?:Co-authored-by|Signed-off-by)\b"
                       r"|\s[A-Z][\w/ ]{1,20}:\s)", re.I)
# Where release notes switch from "what changed" to download / build boilerplate (llama.cpp, GitHub generated notes).
_BOILER = re.compile(r"\s(?:Website|Attestations|macOS/iOS|Windows|Linux|openEuler|Docker|Downloads?|Assets|Full Changelog|"
                     r"New Contributors|Checksums?|SHA256|Installation|Binaries)\s*:", re.I)
_BY_USER = re.compile(r"\s+by @[\w-]+(?:\s+in\s+(?:https?://\S+|#\d+))?", re.I)
_URL_ONLY = re.compile(r"^\s*https?://\S+\s*$")
_WS = re.compile(r"\s+")


def strip_markdown(text: str) -> str:
    s = text or ""
    s = _MD_IMAGE.sub(" ", s)
    s = _MD_LINK.sub(r"\1", s)
    s = _MD_CODE.sub(r"\1", s)
    for _ in range(2):
        s = _MD_EMPH.sub(r"\2", s)
    s = _MD_STAR.sub(r"\1", s)
    s = _MD_ESC.sub(r"\1", s)
    s = _MD_HEAD.sub(r"\1", s)
    return s


def clean_text(text: str | None) -> str:
    """Readable single-line plain text from feed HTML / markdown / changelog text."""
    s = text or ""
    if "<" in s and ">" in s:
        s = _TAGS.sub(" ", s)
    s = unescape(unescape(s))
    s = strip_markdown(s)
    s = _TRAILERS.sub(" ", s)
    m = _BOILER.search(" " + s)
    if m and m.start() > 20:
        s = s[: m.start() - 1]
    s = _PR_REF.sub(" ", s)
    s = _BY_USER.sub("", s)
    s = _WS.sub(" ", s).strip()
    return _collapse_repeat(s)


def _collapse_repeat(s: str) -> str:
    """'llama: fused op for QSA mask llama: fused op for QSA mask more...' -> keep the line once."""
    words = s.split(" ")
    for n in range(min(24, len(words) // 2), 2, -1):
        if words[:n] == words[n:2 * n]:
            return " ".join(words[:n] + words[2 * n:])
    return s


_SENT_END = re.compile(r"(?<=[。！？!?；;])|(?<=\.)\s")


def truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    cut = s[:limit]
    best = max((m.end() for m in _SENT_END.finditer(cut)), default=0)
    if best >= limit * 0.5:
        return cut[:best].rstrip()
    sp = cut.rfind(" ")
    if sp >= limit * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,;:，、") + "…"


def clean_summary(text: str | None, limit: int = 240) -> str:
    s = clean_text(text)
    if _URL_ONLY.match(s):
        return ""  # a link-only body says nothing; the card already links out
    return truncate(s, limit)


# ------------------------------------------------------------------------------------------------ titles

_GH_RELEASE = re.compile(r"^https?://github\.com/([\w.-]+)/([\w.-]+)/releases/tag/([^/?#]+)", re.I)
_BARE_TAG = re.compile(r"^(?:v?\d[\w.+-]*|b\d+|r\d+[\w.-]*|release[-_ ]?v?\d[\w.+-]*|[\w.-]+[-@]v?\d+(?:\.\d+)+[\w.+-]*|"
                       r"(?:nightly|latest|stable)[\w.-]*)$", re.I)


def release_parts(url: str) -> tuple[str, str, str] | None:
    """(owner, repo, tag) for a GitHub release URL."""
    m = _GH_RELEASE.match(url or "")
    if not m:
        return None
    return m.group(1), m.group(2), urllib.parse.unquote(m.group(3))


def is_bare_tag(title: str, tag: str = "") -> bool:
    t = clean_text(title).strip()
    if not t:
        return True
    if tag and t.lower() in {tag.lower(), tag.lower().lstrip("v")}:
        return True
    return bool(_BARE_TAG.match(t)) and len(t) <= 40


def gist(summary: str | None, limit: int = 60, drop: tuple[str, ...] = ()) -> str:
    """First meaningful clause of release notes / a summary, for a title."""
    s = clean_text(summary)
    for d in drop:
        if d and s.lower().startswith(d.lower()):
            s = s[len(d):].lstrip(" :-–—")
    s = re.sub(r"^(?:What'?s Changed|Changes|Changelog|Release notes|Highlights|Summary)\s*[:：]?\s*", "", s, flags=re.I)
    if not s or _URL_ONLY.match(s):
        return ""
    first = re.split(r"(?<=[.!?。！？])\s|\s(?=[\w./-]+\s?:\s)(?<!^)", s, maxsplit=1)[0]  # sentence or next 'area: ' prefix
    return truncate(first.strip(" -–—:"), limit)


def display_title(title: str | None, url: str | None = None, summary: str | None = None) -> str:
    """The title shown on cards / sent to the model. Raw titles stay untouched in the pool."""
    raw = (title or "").strip()
    t = _WS.sub(" ", strip_markdown(unescape(raw))).strip() or raw
    rel = release_parts(url or "")
    if rel:
        owner, repo, tag = rel
        if is_bare_tag(t, tag) or t.lower() in {f"{repo} {tag}".lower(), f"{owner}/{repo} {tag}".lower()}:
            name = repo if repo.lower() not in t.lower() else ""
            head = f"{repo} {tag}" if not name else f"{name} {t if is_bare_tag(t, tag) else tag}"
            g = gist(summary, drop=(t, f"{repo} {tag}", repo, tag))
            return f"{head}: {g}" if g and g.lower() not in head.lower() else head
    return t


# ------------------------------------------------------------------------------------------------ media

MAX_MEDIA = 4
_IMG_EXT = re.compile(r"\.(?:png|jpe?g|gif|webp|avif)(?:[?#]|$)", re.I)
_VID_EXT = re.compile(r"\.(?:mp4|webm|mov|m4v)(?:[?#]|$)", re.I)
_AUD_EXT = re.compile(r"\.(?:mp3|m4a|aac|ogg|oga|opus|wav|flac)(?:[?#]|$)", re.I)
_NOISE = re.compile(r"avatars\.githubusercontent\.com|gravatar\.com|/emoji/|shields\.io|badge|/favicon|pixel|spacer|"
                    r"feeds\.feedburner\.com/~|/stats?\.|tracking|\.svg(?:[?#]|$)|b\.thumbs\.redditmedia\.com|"
                    r"redditstatic\.com|/static/images/|profile_images", re.I)
_YT = re.compile(r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|embed/|live/)|youtu\.be/)([\w-]{11})")
_IMG_TAG = re.compile(r"<img\b[^>]*?\bsrc=[\"']([^\"']+)[\"'][^>]*>", re.I)
_VIDEO_TAG = re.compile(r"<(?:video|source)\b[^>]*?\bsrc=[\"']([^\"']+)[\"'][^>]*>", re.I)
_HREF = re.compile(r"\bhref=[\"']([^\"']+)[\"']", re.I)
_WH = re.compile(r"\b(width|height)=[\"']?(\d+)", re.I)


def http_url(u: str | None, base: str = "") -> str:
    """http(s) URL or ''. Relative URLs are resolved against ``base``; anything else (data:, javascript:...) is dropped."""
    u = unescape((u or "").strip())
    if not u:
        return ""
    if base and not re.match(r"^[a-z][a-z0-9+.-]*:", u, re.I):
        u = urllib.parse.urljoin(base, u)
    if u.startswith("//"):
        u = "https:" + u
    p = urllib.parse.urlsplit(u)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or any(c in u for c in "\"'<> \n\r\t"):
        return ""
    return u


def youtube_id(url: str | None) -> str | None:
    m = _YT.search(url or "")
    return m.group(1) if m else None


def _kind(url: str, mime: str = "", medium: str = "") -> str | None:
    mime, medium = (mime or "").lower(), (medium or "").lower()
    if youtube_id(url):
        return "youtube"
    if mime.startswith("image/") or medium == "image" or (not mime and not medium and _IMG_EXT.search(url)):
        return "image"
    if "v.redd.it/" in url and not _VID_EXT.search(url):
        return "videolink"  # Reddit-hosted video is DASH/HLS behind a page: link out with the poster, no inline player
    if mime.startswith("video/") or medium == "video" or _VID_EXT.search(url):
        return "video"
    if mime.startswith("audio/") or medium == "audio" or _AUD_EXT.search(url):
        return "audio"
    if mime.startswith("image/") or _IMG_EXT.search(url):
        return "image"
    return None


def media_entry(url: str, mime: str = "", medium: str = "", width=None, height=None, base: str = "") -> dict | None:
    u = http_url(url, base)
    if not u or _NOISE.search(u):
        return None
    kind = _kind(u, mime, medium)
    if not kind:
        return None
    try:
        w, h = int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        w = h = 0
    if kind == "image" and ((w and w < 120) or (h and h < 80)):
        return None  # avatars, icons, tracking pixels
    out = {"type": kind, "url": u}
    if mime and "/" in mime and not mime.endswith("/"):
        out["mime"] = mime.lower()[:60]
    if w and h:
        out["width"], out["height"] = w, h
    if kind == "youtube":
        vid = youtube_id(u)
        out["url"] = f"https://www.youtube.com/watch?v={vid}"
        out["poster"] = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    return out


def media_from_html(html: str | None, base: str = "") -> list[dict]:
    """<img>/<video> in an entry's HTML body, plus links to YouTube / v.redd.it / i.redd.it / direct media files."""
    out = []
    s = html or ""
    for m in _IMG_TAG.finditer(s):
        wh = dict((k.lower(), v) for k, v in _WH.findall(m.group(0)))
        e = media_entry(m.group(1), "image/", "image", wh.get("width"), wh.get("height"), base)
        if e:
            out.append(e)
    for m in _VIDEO_TAG.finditer(s):
        e = media_entry(m.group(1), "", "", base=base)
        if e and e["type"] in ("video", "audio"):
            out.append(e)
    for m in _HREF.finditer(s):
        u = unescape(m.group(1))
        if youtube_id(u) or "v.redd.it/" in u or "i.redd.it/" in u or _VID_EXT.search(u) or _AUD_EXT.search(u):
            e = media_entry(u, "", "", base=base)
            if e:
                out.append(e)
    return out


def link_media(url: str | None) -> list[dict]:
    """The item URL itself points at media (YouTube video, i.redd.it image, an mp3...)."""
    u = url or ""
    if youtube_id(u) or "i.redd.it/" in u or _VID_EXT.search(u) or _AUD_EXT.search(u) or _IMG_EXT.search(u):
        e = media_entry(u)
        return [e] if e else []
    return []


def dedupe_media(items: list[dict], limit: int = MAX_MEDIA) -> list[dict]:
    seen, out = set(), []
    for e in items:
        if not e:
            continue
        k = e["url"].split("?")[0] if e["type"] == "image" and "redd.it" not in e["url"] else e["url"]
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    # players first (they are the point of the item), then images
    order = {"video": 0, "audio": 1, "youtube": 2, "videolink": 3, "image": 4}
    out.sort(key=lambda e: order.get(e["type"], 9))
    return out[:limit]


def lead_image(media: list[dict]) -> str | None:
    for e in media:
        if e["type"] == "image":
            return e["url"]
    for e in media:
        if e.get("poster"):
            return e["poster"]
    return None


def safe_media(media, image: str | None = None, enclosure: dict | None = None, url: str | None = None) -> dict:
    """Render-ready media for a card: {"image": url|None, "players": [{type, url, mime?, poster?}]}. Only http(s) URLs
    and known types survive; feed data is untrusted. Audio/video use preload=none so nothing large loads until played."""
    rows = [e for e in (media or []) if isinstance(e, dict)]
    if isinstance(enclosure, dict) and enclosure.get("url"):
        rows.append({"url": enclosure.get("url"), "type": None, "mime": enclosure.get("type") or ""})
    rows += link_media(url)
    clean = []
    for e in rows:
        x = media_entry(str(e.get("url") or ""), str(e.get("mime") or ""), str(e.get("type") or ""), e.get("width"), e.get("height"))
        if x:
            p = http_url(e.get("poster"))
            if p and x["type"] in ("video", "audio", "videolink"):
                x["poster"] = p
            clean.append(x)
    clean = dedupe_media(clean)
    img = http_url(image) if image and not _NOISE.search(image or "") else ""
    img = img or lead_image(clean) or ""
    players = [e for e in clean if e["type"] in ("video", "audio", "youtube", "videolink")][:2]
    return {"image": img or None, "players": players}
