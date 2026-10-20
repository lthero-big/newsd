"""Real media for cards (deterministic, no AI): README image/gif/video for HF + GitHub repos,
Reddit post media, og:image for articles unless it is a generic site default.

    python3 scripts/collect/media.py data/<date>.json      # enrich in place (keeps titles etc.)

Card image schema: {src, width, height, alt, type: image|gif|video, srcset?, poster?}. Images must be
>= MIN_W px wide (dimensions read from the file header, never guessed); videos are kept when the URL is a
direct mp4/webm (box ratio 16:9 unless known). Hotlinks direct URLs; failures leave the card unchanged.
"""
from __future__ import annotations

import json
import re
import struct
import sys
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MIN_W = 600
UA = {"User-Agent": "news-daily-media/1.0 (+https://github.com/newsd)"}
GENERIC_THUMBS = ("cdn-thumbnails.huggingface.co/social-thumbnails", "opengraph.githubassets.com")
BADGE = re.compile(r"shields\.io|badge|badgen|/workflows/|travis-ci|codecov|img\.shields|star-history|contrib\.rocks|"
                   r"discord|twitter\.com/intent|hits\.|visitor|komarev|buymeacoffee|ko-fi|paypal|logo|icon|avatar|emoji|"
                   r"huggingface\.co/datasets/huggingface/badges|\.svg(\?|$)", re.I)
VIDEO = re.compile(r"\.(mp4|webm|mov)(\?|$)", re.I)


def get(url: str, n: int | None = None, timeout: int = 12) -> tuple[bytes, str]:
    h = dict(UA)
    if n:
        h["Range"] = f"bytes=0-{n - 1}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return r.read(n or 3_000_000), (r.headers.get("content-type") or "").lower()


def dims(b: bytes) -> tuple[int, int] | None:
    if b[:8] == b"\x89PNG\r\n\x1a\n" and len(b) >= 24:
        return struct.unpack(">II", b[16:24])
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return struct.unpack("<HH", b[6:10])
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        c = b[12:16]
        if c == b"VP8 ":
            w, h = struct.unpack("<HH", b[26:30]); return w & 0x3FFF, h & 0x3FFF
        if c == b"VP8L":
            v = int.from_bytes(b[21:25], "little"); return (v & 0x3FFF) + 1, ((v >> 14) & 0x3FFF) + 1
        if c == b"VP8X":
            return int.from_bytes(b[24:27], "little") + 1, int.from_bytes(b[27:30], "little") + 1
    if b[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(b):
            if b[i] != 0xFF:
                i += 1; continue
            m = b[i + 1]
            if m in (0xC0, 0xC1, 0xC2, 0xC3):
                h, w = struct.unpack(">HH", b[i + 5:i + 9]); return w, h
            i += 2 + struct.unpack(">H", b[i + 2:i + 4])[0]
    return None


def probe(url: str) -> dict | None:
    """Image dims from header bytes (≥MIN_W) or a direct video; github user-attachments may be either."""
    if VIDEO.search(url):
        return {"src": url, "width": 1280, "height": 720, "alt": "", "type": "video"}
    try:
        b, ct = get(url, 262144)
    except Exception:  # noqa: BLE001
        return None
    if ct.startswith("video/"):
        return {"src": url, "width": 1280, "height": 720, "alt": "", "type": "video"}
    d = dims(b)
    if not d or d[0] < MIN_W or d[1] < 120 or d[0] / max(d[1], 1) > 6:
        return None
    return {"src": url, "width": d[0], "height": d[1], "alt": "", "type": "gif" if b[:3] == b"GIF" else "image"}


def readme_candidates(md: str, base: str) -> list[str]:
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    md = re.sub(r"^---\n.*?\n---\n", "", md, flags=re.S)  # HF yaml front matter
    out = []
    for m in re.finditer(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?[^)]*\)|<(?:img|source|video)\b[^>]*?\bsrc=[\"']([^\"']+)[\"'][^>]*>|"
                         r"(https://github\.com/user-attachments/assets/[0-9a-f-]{36})", md, re.I):
        tag = m.group(0)
        u = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        if not u or BADGE.search(u) or BADGE.search(tag[:200]):
            continue
        wm = re.search(r'width=["\']?(\d+)', tag)
        if wm and int(wm.group(1)) < 200:
            continue
        if not u.startswith(("http://", "https://")):
            u = urllib.parse.urljoin(base, u.lstrip("./"))
        if u.startswith("http://"):
            continue
        out.append(u.replace("/blob/", "/raw/") if "github.com/" in u and "/blob/" in u else u)
    return list(dict.fromkeys(out))[:8]


BENCH_HINT = re.compile(r"bench|eval|score|accuracy|mmlu|gsm|humaneval|math|arena|elo|pass@|geneval|dpg|results?|leaderboard|评测|结果|得分", re.I)
NUM = re.compile(r"^[^\d\-]*(-?\d{1,4}(?:\.\d+)?)\s*%?(?:\s*(?:±|\+-)\s*[\d.]+)?\**$")


def _cell(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    return re.sub(r"[*_`]+", "", s).strip()


def bench_table(md: str, source: str, name: str = "") -> dict | None:
    """First README markdown table that looks like an eval table (hinted by heading/header, >=3 numeric rows).
    Values are copied verbatim from the source; nothing is computed or guessed. None when unparseable."""
    lines, heading = md.splitlines(), ""
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        if ln.startswith("#"):
            heading = _cell(ln.lstrip("#"))
        if ln.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{2,}", lines[i + 1].strip()):
            head = [_cell(c) for c in ln.strip("|").split("|")]
            rows, j = [], i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append([_cell(c) for c in lines[j].strip().strip("|").split("|")]); j += 1
            i = j
            if len(head) < 2 or not rows or not (BENCH_HINT.search(heading) or BENCH_HINT.search(" ".join(head))):
                continue
            toks = [t for t in re.split(r"[-_./ ]+", name.lower()) if len(t) > 2 and not t.isdigit()][:2]
            hit = lambda x: bool(toks) and any(t in x.lower() for t in toks)
            num = lambda c: NUM.match(c) if c else None
            nz = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())
            me = lambda x: bool(nz(name)) and len(nz(x)) > 3 and (nz(name) in nz(x) or nz(x) in nz(name))
            # A) rows are models, columns are benchmarks -> one benchmark column across models
            if re.search(r"model|method|name|模型|方法", head[0], re.I) or any(hit(r[0]) for r in rows):
                for col in range(1, min(len(head), 8)):
                    vals = [{"label": r[0][:40], "value": float(m.group(1)), "raw": r[col][:16], "self": me(r[0])}
                            for r in rows if col < len(r) and r[0] and (m := num(r[col]))]
                    if len(vals) >= 3:
                        return {"title": (heading or "评测")[:40], "metric": head[col][:30] or "score", "rows": vals[:6], "source": source}
            # B) columns are models, rows are benchmarks -> one benchmark row across models
            elif re.search(r"bench|task|dataset|metric|评测|任务", head[0], re.I) or any(hit(h) for h in head[1:]):
                for r in rows:
                    vals = [{"label": head[k][:40], "value": float(m.group(1)), "raw": r[k][:16], "self": me(head[k])}
                            for k in range(1, min(len(head), len(r))) if head[k] and (m := num(r[k]))]
                    if len(vals) >= 3 and r[0]:
                        return {"title": (heading or "评测")[:40], "metric": " ".join(x for x in r[:2] if x and not num(x))[:30] or r[0][:30], "rows": vals[:6], "source": source}
            continue
        i += 1
    return None


def readme_bench(card: dict) -> dict | None:
    url = card["url"].rstrip("/")
    m = re.match(r"https://huggingface\.co/([\w.-]+/[\w.-]+)$", url)
    if m:
        raw, page = f"https://huggingface.co/{m.group(1)}/raw/main/README.md", url
    else:
        m = re.match(r"https://github\.com/([\w.-]+)/([\w.-]+)$", url)
        if not m:
            return None
        raw, page = f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/HEAD/README.md", url + "#readme"
    try:
        md = get(raw, 400000)[0].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    return bench_table(md, page, m.group(m.lastindex))


def readme_media(card: dict) -> dict | None:
    url = card["url"].rstrip("/")
    m = re.match(r"https://huggingface\.co/((?:datasets/|spaces/)?[\w.-]+/[\w.-]+)$", url)
    if m:
        repo = m.group(1)
        raw, base = f"https://huggingface.co/{repo}/raw/main/README.md", f"https://huggingface.co/{repo}/resolve/main/"
    else:
        m = re.match(r"https://github\.com/([\w.-]+)/([\w.-]+)$", url)
        if not m:
            return None
        raw, base = f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/HEAD/README.md", f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/HEAD/"
    try:
        md = get(raw, 400000)[0].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    for u in readme_candidates(md, base):
        p = probe(u)
        if p:
            return p
    return None


def reddit_media(card: dict) -> dict | None:
    if "reddit.com/r/" not in card["url"]:
        return None
    try:
        d = json.loads(get(card["url"].rstrip("/") + "/.json?raw_json=1", None)[0])[0]["data"]["children"][0]["data"]
    except Exception:  # noqa: BLE001
        return None
    v = ((d.get("secure_media") or {}).get("reddit_video") or {})
    if v.get("fallback_url"):
        return {"src": v["fallback_url"].split("?")[0], "width": v.get("width") or 1280, "height": v.get("height") or 720, "alt": "", "type": "video",
                **({"poster": d["preview"]["images"][0]["source"]["url"]} if d.get("preview") else {})}
    src = ((d.get("preview") or {}).get("images") or [{}])[0].get("source") or {}
    if src.get("url") and (src.get("width") or 0) >= MIN_W:
        dest = d.get("url_overridden_by_dest") or ""
        u = dest if re.match(r"https://i\.redd\.it/\S+\.(png|jpe?g|gif|webp)$", dest) else src["url"]
        return {"src": u, "width": src["width"], "height": src["height"], "alt": "", "type": "gif" if u.endswith(".gif") else "image"}
    return None


def _og_candidate_ok(u: str) -> bool:
    base_name = urllib.parse.urlparse(u).path.rsplit("/", 1)[-1].lower()
    generic = any(g in u for g in GENERIC_THUMBS) or re.fullmatch(r"(og|og[-_]image|default|share|social|social[-_]card|card|banner|preview|cover)\.(png|jpe?g|webp)", base_name)
    return u.startswith("https://") and not generic and not BADGE.search(urllib.parse.urlparse(u).path)


def og_image_from_html(html: str, base_url: str = "") -> str | None:
    """Regex og:image / twitter:image parser (the pre-trafilatura logic), used as fallback."""
    for prop in ("og:image", "twitter:image", "og:image:url"):
        m = re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]*content=["\']([^"\']+)' % re.escape(prop), html, re.I) \
            or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']%s["\']' % re.escape(prop), html, re.I)
        if m:
            u = urllib.parse.urljoin(base_url, m.group(1).replace("&amp;", "&"))
            if _og_candidate_ok(u):
                return u
    return None


def og_candidates(html: str, base_url: str = "") -> list[str]:
    """Lead-image candidates: trafilatura metadata (og:image/twitter:image/... in one pass) first, regex fallback."""
    out = []
    try:
        from trafilatura import extract_metadata

        doc = extract_metadata(html, default_url=base_url or None)
        if doc is not None and doc.image:
            u = urllib.parse.urljoin(base_url, doc.image.replace("&amp;", "&"))
            if _og_candidate_ok(u):
                out.append(u)
    except Exception:  # noqa: BLE001
        pass
    fb = og_image_from_html(html, base_url)
    if fb and fb not in out:
        out.append(fb)
    return out


def og_media(card: dict) -> dict | None:
    if card["source"] not in ("rss", "hn"):
        return None
    try:
        b, ct = get(card["url"], 300000)
    except Exception:  # noqa: BLE001
        return None
    if "html" not in ct:
        return None
    for u in og_candidates(b.decode("utf-8", "replace"), card["url"]):
        p = probe(u)
        if p:
            p["og"] = True
            return p
    return None


LOGO_URL_RE = re.compile(r"(^|[/_.-])(logo|brand|avatar|icon|favicon|profile_images)([/_.-]|$)", re.I)


def logo_like(b: bytes, src: str = "") -> bool:
    """Brand logos / flat marks shouldn't get big-image treatment: ~square with very few colours, or almost flat."""
    if LOGO_URL_RE.search(src.split("?")[0]):
        return True
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(b)).convert("RGB")
    except Exception:
        return False
    w, h = im.size
    px = list(im.resize((64, 64)).getdata())
    c = Counter((r >> 4, g >> 4, bb >> 4) for r, g, bb in px)
    colours, dom = len(c), c.most_common(1)[0][1] / len(px)
    return (0.8 <= w / max(h, 1) <= 1.25 and colours <= 24) or (colours <= 20 and dom >= 0.7)


def mark_logo(card: dict) -> None:
    m = card.get("image")
    if not m or m.get("type") == "video" or "logo" in m:
        return
    try:
        b, _ = get(m["src"])
        m["logo"] = logo_like(b, m["src"])
    except Exception:
        pass


def find(card: dict) -> dict | None:
    if card["source"] == "x":
        return None  # X media comes with the RSSHub item
    return readme_media(card) or reddit_media(card) or og_media(card)


def enrich(cards: list[dict], workers: int = 8) -> dict:
    todo = [c for c in cards if c.get("source") != "x" and (not c.get("image") or any(g in c["image"].get("src", "") for g in GENERIC_THUMBS))]
    with ThreadPoolExecutor(workers) as pool:
        found = dict(zip([c["id"] for c in todo], pool.map(find, todo)))
        bt = [c for c in cards if c.get("source") in ("hf", "github", "ossinsight") and "bench" not in c]
        for c, b in zip(bt, pool.map(readme_bench, bt)):
            if b:
                c["bench"] = b
    # generic site defaults: the same og image on 2+ cards (or the domain's root og) is dropped
    rep = Counter(m["src"] for m in found.values() if m and m.get("og"))
    stats = {"checked": len(todo), "readme_or_reddit": 0, "og": 0, "generic_dropped": 0, "kept_old": 0}
    for c in todo:
        m = found.get(c["id"])
        if m and m.get("og") and rep[m["src"]] > 1:
            m = None; stats["generic_dropped"] += 1
        if m:
            m.pop("og", None) and stats.__setitem__("og", stats["og"] + 1) or stats.__setitem__("readme_or_reddit", stats["readme_or_reddit"] + 1)
            c["image"] = m
        elif c.get("image") and any(g in c["image"].get("src", "") for g in GENERIC_THUMBS):
            c.pop("image"); stats["kept_old"] += 0
    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(mark_logo, cards))
    stats["logo"] = sum(1 for c in cards if (c.get("image") or {}).get("logo"))
    return stats


if __name__ == "__main__":
    p = Path(sys.argv[1])
    d = json.loads(p.read_text(encoding="utf-8"))
    st = enrich(d["items"])
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("media:", st, "with media:", sum(1 for c in d["items"] if c.get("image")))
