"""Full-text + metadata extraction with trafilatura (Apache-2.0), with deterministic fallbacks.

``extract_page(html, url, favor_precision=False, favor_recall=False)`` returns an
``Extraction`` with the main text and the metadata trafilatura finds in one pass
(title, author, publish date, site name, lead image). Anything it misses falls back
to the pre-existing regex helpers (``linkdates`` for dates, ``media.og_image_from_html``
for og:image, ``collect.html_to_text`` for text). It never raises.

HTML is always fetched by our own client (``newsd.pool.fetch``); trafilatura's own
downloader is never used. The full text is for internal use only (scoring, summaries,
translation, semantic dedupe, search); pages show the summary and the source link.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field

MIN_TEXT = 80  # shorter "articles" are usually cookie walls / JS shells
DATE_CONFIG = {"original_date": True, "extensive_search": False, "outputformat": "%Y-%m-%dT%H:%M:%S%z"}


@dataclass
class Extraction:
    ok: bool
    status: str  # ok | failed:<reason>
    text: str = ""
    title: str | None = None
    author: str | None = None
    published_at: str | None = None
    sitename: str | None = None
    image: str | None = None
    extractor: str = "none"  # trafilatura | html_text | none
    meta_from: dict = field(default_factory=dict)  # field -> trafilatura | fallback

    def as_dict(self) -> dict:
        return asdict(self)


def html_hash(html: str) -> str:
    """Hash of the page with whitespace folded, so trivially re-serialised copies collide."""
    return hashlib.sha256(re.sub(r"\s+", " ", html or "").strip().encode("utf-8")).hexdigest()


def _iso(value: str | None) -> str | None:
    from newsd.collector import linkdates

    return linkdates._iso(value) if value else None


def trafilatura_metadata(html: str, url: str | None = None) -> dict:
    """title/author/date/sitename/image via trafilatura.extract_metadata; {} on any failure."""
    try:
        from trafilatura import extract_metadata

        doc = extract_metadata(html, default_url=url, date_config=DATE_CONFIG)
    except Exception:  # noqa: BLE001
        return {}
    if doc is None:
        return {}
    return {
        "title": (doc.title or None),
        "author": (doc.author or None),
        "published_at": _iso(doc.date),
        "sitename": (doc.sitename or None),
        "image": (doc.image or None),
    }


def _js_shell(html: str) -> bool:
    low = html.lower()
    return ("enable javascript" in low or "<noscript" in low and "javascript" in low) and len(re.sub(r"<[^>]+>", "", html)) < 2000


def extract_page(html: str, url: str | None = None, *, favor_precision: bool = False, favor_recall: bool = False) -> Extraction:
    from newsd.collector import linkdates
    from newsd.collector.collect import html_to_text
    from newsd.collector.media import _og_candidate_ok, og_image_from_html

    if not html or "<" not in html:
        return Extraction(ok=False, status="failed:empty")
    text, extractor = "", "none"
    try:
        import trafilatura

        text = trafilatura.extract(
            html,
            url=url,
            favor_precision=bool(favor_precision),
            favor_recall=bool(favor_recall),
            include_comments=False,
            include_tables=True,
        ) or ""
        if len(text) >= MIN_TEXT:
            extractor = "trafilatura"
        else:
            text = ""
    except Exception:  # noqa: BLE001
        text = ""
    meta = trafilatura_metadata(html, url)
    meta_from = {k: "trafilatura" for k, v in meta.items() if v}
    if not meta.get("published_at"):
        fallback = linkdates.from_html_regex(html)
        if fallback:
            meta["published_at"], meta_from["published_at"] = fallback, "fallback"
    image = meta.get("image")
    if image and url:
        import urllib.parse

        image = urllib.parse.urljoin(url, image.replace("&amp;", "&"))
    if not image or not _og_candidate_ok(image):  # https only; generic site cards, badges and logos are dropped
        fallback = og_image_from_html(html, url or "")
        image = fallback
        if fallback:
            meta_from["image"] = "fallback"
        else:
            meta_from.pop("image", None)
    meta["image"] = image
    if not text:
        if _js_shell(html):
            return Extraction(ok=False, status="failed:js_shell", meta_from=meta_from, **meta)
        plain = html_to_text(html)
        if len(plain) >= MIN_TEXT * 3:  # tag-stripped body is noisy; only accept a substantial one
            text, extractor = plain, "html_text"
    if not text:
        return Extraction(ok=False, status="failed:no_text", meta_from=meta_from, **meta)
    return Extraction(ok=True, status="ok", text=text, extractor=extractor, meta_from=meta_from, **meta)
