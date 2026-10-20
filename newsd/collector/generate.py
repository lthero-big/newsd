#!/usr/bin/env python3
"""Deterministic news-daily generator: plugins -> rule scoring -> dedupe -> quotas -> daily JSON (schema 2.0).

No LLM is involved: cards carry the original title, an excerpt taken from the
source listing (or the fetched page), source, metrics and link.

    python3 scripts/collect/generate.py --date 2026-10-16 --out data/2026-10-16.json \
        [--x-items var/x-items.json] [--arena var/arena.json] [--raw-dir var/runs/2026-10-16]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as core  # noqa: E402
import plugins  # noqa: E402
import learning  # noqa: E402
import linkdates  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
TZ = "Asia/Shanghai"
MODULES = [
    ("model", "模型新闻", "新模型、版本发布与榜单"),
    ("opensource", "开源项目发现", "新仓库、热门模型权重与工具发布"),
    ("community", "社区新闻", "HN、Reddit 与 X 上的讨论"),
]
QUOTAS = {"model": 8, "opensource": 6, "community": 8}
SOURCE_WEIGHT = {"rss": 1.25, "hf": 0.95, "github": 1.0, "ossinsight": 1.0, "hn": 1.0, "reddit": 0.85, "x": 0.9}
# Typical "very popular" engagement per source, used to normalise log engagement to ~[0,1].
ENGAGEMENT_SCALE = {"hn": 400, "reddit": 600, "github": 3000, "ossinsight": 2000, "hf": 1500, "x": 3000, "rss": 1}
SOURCE_LABEL = {"hn": "Hacker News", "reddit": "Reddit", "rss": "RSS", "github": "GitHub", "hf": "Hugging Face", "ossinsight": "OSSInsight", "x": "X"}
TAG_ORDER = ["AI", "LLM", "图片视频音频", "Agent"]
OFFICIAL_FEEDS = {"openai-news", "google-ai-blog", "hf-blog"}


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def tag_terms(tags_cfg: dict) -> list[dict]:
    out = []
    for tag, terms in tags_cfg["tags"].items():
        for term in terms:
            out.append({**term, "group": tag})
    return out


def stable_id(url: str) -> str:
    """Mirror scripts/identity.mjs (drop utm_*/fbclid/gclid, sort query, WHATWG href)."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")]
    query.sort(key=lambda kv: kv[0])
    path = parts.path or "/"
    href = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), parts.fragment))
    return "sha256:" + hashlib.sha256(href.encode("utf-8")).hexdigest()


def clean(text: str) -> str:
    text = core.html_to_text(text) if text and "<" in text else (text or "")
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[<>]", "", text)
    text = re.sub(r"\\([-*_\[\]()#.!])", r"\1", text)  # markdown escapes like "\-"
    text = re.sub(r"\*\*|__|`|\[\s*\[|\]\s*\]|^\s*[\[\]]\s*|\s[\[\]]\s", " ", text)
    return re.sub(r"\s+", " ", text).strip()


CHROME_MARKERS = ("Skip to content", "Navigation Menu", "You signed in with another tab", "Appearance settings", "Sign in Sign up", "Toggle navigation", "Log In Sign Up", "Back to Articles")


def strip_chrome(text: str) -> str:
    """Drop GitHub/site page chrome: everything from the first nav marker on, plus '… · GitHub' page-title prefixes."""
    cut = min([i for i in (text.find(m) for m in CHROME_MARKERS) if i >= 0], default=-1)
    if cut >= 0:
        text = text[:cut]
    text = re.sub(r"^(?:Release \S+ · )?[\w.-]+/[\w.-]+ · GitHub\s*", "", text.strip())
    text = re.sub(r"^GitHub - [\w.-]+/[\w.-]+:\s*", "", text)
    text = re.sub(r"\s*· GitHub\s*$", "", text)
    text = re.sub(r"\s*(?:Hugging Face|GitHub)\s*$", "", text)
    return text.strip()


def excerpt(text: str, limit: int = 220) -> str:
    text = strip_chrome(clean(text))
    if len(text) < 25 or text.lower() in ("reddit", "github", "x", "hacker news"):
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind("。"), cut.rfind(". "), cut.rfind("！"), cut.rfind("? "))
    return (cut[: stop + 1] if stop > limit * 0.5 else cut.rstrip() + "…").strip()


def classify(item: dict, release_re: list[re.Pattern]) -> str:
    kind, source = item.get("kind"), item["source"]
    if kind == "model":
        return "model"
    if kind == "repo" or kind == "release":
        return "opensource"
    if item.get("feed") in OFFICIAL_FEEDS:
        return "model"
    title = item["title"]
    model_named = any(t["group"] in ("LLM", "图片视频音频") and not t["broad"] and "title" in t["locations"] for t in item["matched"])
    if model_named and any(p.search(title.lower()) for p in release_re):
        return "model"
    if source == "hn" and re.search(r"github\.com/[^/]+/[^/]+/?$", item["url"]) and title.lower().startswith("show hn"):
        return "opensource"
    return "community"


# ── Community relevance gate (rule only, no LLM) ──────────────────────────────
# HN/RSS (and RSSHub-searched X) cards in 社区新闻 must name AI explicitly.
# The tag list's broad words ("agent", "model", "workflow", "Gemini", "Cursor", …) also
# mean police agents, car models, people's names, so they only count with AI context.
# This is a relevance filter only; it never touches learning / feedback weights.
_W = r"(?<![a-z0-9])"
_E = r"(?![a-z0-9])"
COMMUNITY_STRONG = re.compile(
    _W + r"(?:ai|a\.i\.|genai|llms?|gpt[\w.-]*|chatgpt|openai|anthropic|deepmind|hugging ?face|neural|"
    r"machine learning|deep learning|(?<!\d )(?<!\d)ml|inference|diffusion|open[- ]weights?|prompts?|prompting|"
    r"transformers?|embeddings?|fine[- ]?tun\w*|lora|gguf|rag|mcp|multimodal|chatbots?|copilot|agentic|"
    r"vlms?|text[- ]to[- ](?:image|video|speech|3d)|tts|asr|claude|qwen[\w.-]*|comfyui|deepseek|llama\.cpp|vllm|ollama)" + _E
    + r"|大模型|智能体|人工智能|提示词|生成式|模型|神经网络|机器学习|深度学习|多模态", re.I)
COMMUNITY_CONTEXT = re.compile(  # ambiguous words that become AI-specific in these phrases
    _W + r"(?:(?:language|foundation|frontier|reasoning|open|base|world|video|image|vision|embedding|weights?)[- ]models?|"
    r"models? (?:weights|training|release|card|context)|(?:coding|browser|autonomous|computer[- ]use|research) agents?|"
    r"multi[- ]agents?|sub[- ]?agents?|agent[- ](?:orchestration|framework|skills|sdk|harness|memory|runtime|loop|tooling)|gemini (?:\d|pro|flash|nano|ultra|cli|api)|cursor (?:ide|editor|agent))" + _E, re.I)
# Non-broad tag names that are still ordinary words / names outside AI.
COMMUNITY_AMBIGUOUS_NAMES = {"Cursor", "Runway", "Pika", "Llama", "Mistral", "Devin", "Windsurf", "Codex", "Aider", "Cline",
                             "Imagen", "Veo", "upscaler", "Suno", "Udio", "Recraft", "Kling", "Phi-4", "RAG", "Grok"}


def community_ai_ok(item: dict) -> bool:
    """True when a community card is clearly about AI: a strong AI term, a specific (non-broad, unambiguous)
    tag name, or an ambiguous word in AI context — in the title or listing/source text."""
    text = clean(item.get("title") or "") + " \n " + clean(item.get("listing_text") or "")[:1500]
    if COMMUNITY_STRONG.search(text) or COMMUNITY_CONTEXT.search(text):
        return True
    return any(not t.get("broad") and t["term"] not in COMMUNITY_AMBIGUOUS_NAMES
               for t in item.get("matched") or [])


def community_gated(item: dict) -> bool:
    if item.get("prompt_q") or item.get("module") != "community":
        return False
    # Reddit is exempt: the configured subs (LocalLLaMA, StableDiffusion, comfyui) are AI-only communities.
    return item["source"] in ("hn", "rss") or (item["source"] == "x" and item.get("transport") == "rsshub")


def score(item: dict, now: datetime) -> dict:
    source = item["source"]
    pts = max(0, int(item.get("points") or 0))
    com = max(0, int(item.get("comments") or 0))
    scale = ENGAGEMENT_SCALE.get(source, 500)
    engagement = 0.0 if source == "rss" else min(1.0, (math.log1p(pts) + 0.5 * math.log1p(com)) / (1.5 * math.log1p(scale)))
    if source == "rss":
        engagement = 0.5  # official/release feeds have no public counters; neutral prior
    title_hits = [t for t in item["matched"] if "title" in t["locations"]]
    specific = [t for t in title_hits if not t["broad"]]
    specificity = min(1.0, 0.45 * len(specific) + 0.15 * len(title_hits) + (0.1 if item["matched"] else 0))
    dt = core.parse_time(item.get("published_at"))
    age_h = max(0.0, (now - dt).total_seconds() / 3600) if dt else 24.0
    half_life = 24.0 if item.get("kind") in ("post", "release") else 96.0
    decay = 0.5 ** (age_h / half_life)
    weight = SOURCE_WEIGHT.get(source, 1.0)
    total = weight * (0.55 * engagement + 0.35 * specificity + 0.10) * (0.4 + 0.6 * decay)
    return {"total": round(total * 100, 2), "engagement": round(engagement, 3), "specificity": round(specificity, 3),
            "decay": round(decay, 3), "source_weight": weight}


TOPIC_CAP = 2


def topic_key(item: dict) -> str | None:
    specific = sorted(t["term"] for t in item["matched"] if not t["broad"] and "title" in t["locations"])
    return specific[0].lower() if specific else None


def dedupe(items: list[dict]) -> list[dict]:
    items = sorted(items, key=lambda i: -i["score"]["total"])
    kept: list[dict] = []
    by_url: dict[str, dict] = {}
    for item in items:
        keys = {core.normalize_url(item["url"])} | {core.normalize_url(l["url"]) for l in item.get("source_links", [])}
        twin = next((by_url[k] for k in keys if k in by_url), None)
        if twin is None:
            twin = next((k for k in kept if core.titles_similar(k["title"], item["title"])), None)
        if twin is not None:
            twin.setdefault("also", []).append({"source": item["source"], "url": item["url"]})
            continue
        kept.append(item)
        for k in keys:
            by_url[k] = item
    return kept


STALE_DAYS, STALE_FACTOR, STALE_CHECK_N = 3, 0.6, 40


def linked_target(i: dict) -> str | None:
    """Primary content a post points at: X post -> first outbound link; HN/Reddit -> the story URL."""
    if i["source"] == "x":
        return (i.get("links_out") or [None])[0]
    if i["source"] in ("hn", "reddit") and not re.match(r"https://(news\.ycombinator\.com|(www\.|old\.)?reddit\.com)/", i["url"]):
        return i["url"]
    return None


def mark_stale(items: list[dict]) -> None:
    """旧闻重提: linked content published >3 days before the post -> flag + downrank. Unknown dates are skipped."""
    todo = [(i, linked_target(i)) for i in items]
    todo = [(i, u) for i, u in todo if u]
    with ThreadPoolExecutor(8) as pool:
        dates = list(pool.map(lambda t: linkdates.published(t[1]), todo))
    for (i, u), d in zip(todo, dates):
        pub, post = core.parse_time(d), core.parse_time(i.get("published_at"))
        if pub and post and (post - pub).days > STALE_DAYS:
            i["stale_link"] = {"url": u, "published_at": core.to_iso(pub)}
            i["score"] = {**i["score"], "total": round(i["score"]["total"] * STALE_FACTOR, 2), "stale": STALE_FACTOR}
    linkdates.save_cache()


MIN_IMAGE_WIDTH = 600
PROMPT_QUOTA = 2
IMAGE_HOSTS = ("https://pbs.twimg.com/", "https://opengraph.githubassets.com/", "https://cdn-thumbnails.huggingface.co/")


def x_image(m: dict, min_w: int | None = None) -> dict | None:
    """pbs.twimg.com: serve name=large (≤2048) with a srcset so the browser picks a sharp size; never upscale."""
    mt = re.match(r"(https://pbs\.twimg\.com/[^?]+?)\.(jpg|jpeg|png|webp)(?:\?.*)?$", m.get("url") or "")
    if not mt:  # RSSHub style: .../media/ID?format=jpg&name=orig
        mt = re.match(r"(https://pbs\.twimg\.com/[^?]+)\?format=(jpg|jpeg|png|webp)\b", m.get("url") or "")
    w, h = int(m.get("width") or 0), int(m.get("height") or 0)
    if not mt or w < (min_w or MIN_IMAGE_WIDTH) or h <= 0:
        return None
    base, fmt = mt.group(1), mt.group(2).replace("jpeg", "jpg")
    sizes = [(n, px) for n, px in (("small", 680), ("medium", 1200), ("large", 2048)) if px < w] + [("orig", w)]
    srcset = ", ".join(f"{base}?format={fmt}&name={n} {min(px, w)}w" for n, px in sizes if px <= 2048 or n == "orig")
    return {"src": f"{base}?format={fmt}&name={'large' if w > 1200 else 'orig'}", "srcset": srcset,
            "width": w, "height": h, "alt": ""}


def pick_image(i: dict) -> dict | None:
    if i["source"] == "x":
        for m in i.get("media") or []:
            img = x_image(m)
            if img and m.get("video"):
                return {"src": m["video"], "poster": img["src"], "width": img["width"], "height": img["height"], "alt": "", "type": "video"}
            if img:
                return img
        return None
    if i["source"] in ("github", "ossinsight"):
        mt = re.match(r"https://github\.com/([\w.-]+)/([\w.-]+)/?$", i["url"])
        if mt:  # GitHub social card, 1200x600 PNG
            return {"src": f"https://opengraph.githubassets.com/1/{mt.group(1)}/{mt.group(2)}", "width": 1200, "height": 600, "alt": ""}
    if i["source"] == "hf":
        mt = re.match(r"https://huggingface\.co/([\w.-]+/[\w.-]+)/?$", i["url"])
        if mt:  # HF model-card social thumbnail, 1200x648 PNG
            return {"src": f"https://cdn-thumbnails.huggingface.co/social-thumbnails/models/{mt.group(1)}.png", "width": 1200, "height": 648, "alt": ""}
    og = i.get("og_image")
    if og and og["width"] >= MIN_IMAGE_WIDTH:
        return {"src": og["src"], "width": og["width"], "height": og["height"], "alt": ""}
    return None


def og_image(html_text: str) -> dict | None:
    """og:image only when the page declares its size (≥600px wide), so nothing gets upscaled."""
    def meta(prop):
        m = re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]*content=["\']([^"\']+)' % re.escape(prop), html_text, re.I) \
            or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']%s["\']' % re.escape(prop), html_text, re.I)
        return m.group(1).strip() if m else None
    src, w, h = meta("og:image"), meta("og:image:width"), meta("og:image:height")
    try:
        w, h = int(w), int(h)
    except (TypeError, ValueError):
        return None
    if not src or not src.startswith("https://") or w < MIN_IMAGE_WIDTH or h <= 0:
        return None
    return {"src": src.replace("&amp;", "&"), "width": w, "height": h}


def fetch_excerpts(items: list[dict], timeout: int) -> None:
    def one(item):
        if len(clean(item.get("listing_text") or "")) >= 60 or item["source"] in ("github", "hf"):
            return
        try:
            res = core.safe_get(item["url"], timeout=timeout)
            if "html" not in (res["content_type"] or "") and "text" not in (res["content_type"] or ""):
                return
            body = res["body"].decode("utf-8", "replace")
            item["og_image"] = og_image(body[:200000])
            text, _, _ = core.extract_text(body, 2000)
            item["page_text"] = text
        except Exception:  # noqa: BLE001 - excerpt is best effort
            return
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(one, items))


def metrics(item: dict) -> dict:
    src = item["source"]
    out = {}
    if src in ("github", "ossinsight"):
        out = {"stars": item.get("points"), "forks": item.get("comments")}
    elif src == "hf":
        out = {"likes": item.get("points"), "downloads": item.get("downloads")}
    elif src == "x":
        out = {"engagement": item.get("points"), "replies": item.get("comments")}
    else:
        out = {"points": item.get("points"), "comments": item.get("comments")}
    return {k: int(v) for k, v in out.items() if v is not None}


def source_label(item: dict) -> str:
    feed = item.get("feed") or ""
    if item["source"] == "reddit":
        return f"r/{feed.split(':')[0]}" if feed and feed != "reddit" else "Reddit"
    if item["source"] == "rss":
        return item.get("feed_label") or feed
    if item["source"] == "x" and item.get("author"):
        return f"X · @{str(item['author']).lstrip('@')}"
    return SOURCE_LABEL.get(item["source"], item["source"])


def arena_view(path: Path | None) -> list[dict]:
    if not path or not path.is_file():
        return []
    boards = []
    for b in load(path).get("boards", []):
        boards.append({
            "name": b["name"], "snapshot_date": b.get("snapshot_date"), "fetch_status": b.get("fetch_status", "failed"),
            "rows": [{"rank": r.get("rank"), "model": str(r.get("model")), "score": r.get("score"),
                      "delta": r.get("delta_vs_previous")} for r in (b.get("rows") or [])[:10]],
        })
    return boards


PM_QUOTAS = {"model": 3, "opensource": 2, "community": 3}


def item_keys(items: list[dict]) -> tuple[set, list]:
    urls, titles = set(), []
    for c in items:
        urls.add(core.normalize_url(c["url"]))
        urls.update(core.normalize_url(l["url"]) for l in c.get("links") or [])
        if c.get("title"):
            titles.append(c["title"])
    return urls, titles


def is_known(i: dict, urls: set, titles: list) -> bool:
    if core.normalize_url(i["url"]) in urls or any(core.normalize_url(l["url"]) in urls for l in i.get("source_links") or []):
        return True
    return any(core.titles_similar(t, i.get("title") or "") for t in titles)


RELEASE_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/releases/tag/([^/?#]+)")
BARE_TAG = re.compile(r"^(?:b\d{3,}|v?\d+(?:\.\d+)+(?:[-.]?(?:rc|beta|alpha|dev|post)\d*)?)$", re.I)
PRERELEASE = re.compile(r"(?:rc|beta|alpha|dev|nightly)\d*$", re.I)


def not_title(ex: str, title: str) -> str:
    """An excerpt that merely repeats the title adds nothing."""
    t = clean(title)
    return "" if t and ex.startswith(t[:60]) and len(ex) - len(t) < 25 else ex


def collapse_releases(items: list[dict]) -> list[dict]:
    """One card per repo for GitHub release feeds: build tags (b11681), bare versions and pre-releases are folded
    into the most substantive release of that repo; the card is titled by repo with the version list."""
    groups, out = {}, []
    for i in items:
        m = RELEASE_URL.match(i.get("url") or "") if i.get("kind") == "release" else None
        if m:
            groups.setdefault(m.group(1), []).append((m.group(2), i))
        else:
            out.append(i)
    for repo, rel in groups.items():
        def notes(i):
            return strip_chrome(clean(i.get("listing_text") or ""))
        def rank(t):
            tag, i = t
            return (not PRERELEASE.search(tag), not tag.lower().startswith("b"), len(notes(i)) >= 80, len(notes(i)), i.get("published_at") or "")
        rel.sort(key=rank, reverse=True)
        tag, rep = rel[0]
        body = notes(rep)
        title_rest = re.sub(r"^\S+:\s*", "", clean(rep.get("title") or "")) if ":" in (rep.get("title") or "") else ""
        if len(body) < 40 and not title_rest and all(BARE_TAG.match(t) for t, _ in rel):
            continue  # nothing but version numbers / boilerplate → drop
        tags = [t for t, _ in rel]
        name = repo.split("/")[1]
        card = dict(rep)
        card["title"] = f"{name} {tags[0]}" + (f"（另有 {len(tags) - 1} 个版本：{'、'.join(tags[1:4])}{'…' if len(tags) > 4 else ''}）" if len(tags) > 1 else "") + (f"：{title_rest}" if title_rest else "")
        card["listing_text"] = body
        card["points"] = max((i.get("points") or 0) for _, i in rel)
        card["also"] = [{"source": "rss", "url": i["url"]} for _, i in rel[1:4]]
        out.append(card)
    return out


def build(date: str, raw: list[dict], stats: dict, terms: list[dict], tags_cfg: dict, start, end, arena, timeout: int, fetch_bodies: bool,
          exclude: list[dict] | None = None, quotas: dict | None = None, batch: str = "am", fresh_after=None, min_score: dict | None = None,
          feedback: "learning.Feedback | None" = None, check_links: bool | None = None):
    quotas = quotas or QUOTAS
    ex_urls, ex_titles = item_keys(exclude or [])
    fetched_at = core.to_iso(end)
    release_re = [re.compile(p) for p in tags_cfg["model_release_patterns"]]
    now = end
    seen_reasons = {"no_keyword": 0, "removed": 0}
    pool, prompt_pool = [], []
    check_links = (not os.environ.get("NEWS_NO_MEDIA")) if check_links is None else check_links
    seen_links = linkdates.load_seen() if check_links else {}
    raw = collapse_releases(raw)
    for item in raw:
        if not item.get("url") or not item.get("title"):
            continue
        text = item.get("listing_text") or ""
        if item["source"] in ("github", "hf"):
            text += " " + " ".join(item.get("topics") or [])
        item["matched"] = core.match_terms(item["title"], clean(text)[:1500], item["url"], terms)
        if item.get("prompt_q") and not item["matched"]:  # prompt-sharing posts are image-gen by definition
            item["matched"] = [{"term": "prompt", "group": "图片视频音频", "locations": ["body"], "broad": False}]
        if not item["matched"] and item["source"] in ("hn", "reddit", "x"):
            seen_reasons["no_keyword"] += 1
            continue
        if any(seen_links.get(u, date) < date for u in (linkdates.canon(item["url"]), *(linkdates.canon(x) for x in item.get("links_out") or []))):
            seen_reasons["seen_before"] = seen_reasons.get("seen_before", 0) + 1  # cross-edition: link already ran in an earlier edition
            continue
        if exclude and is_known(item, ex_urls, ex_titles):
            seen_reasons["known"] = seen_reasons.get("known", 0) + 1
            continue
        if fresh_after and item.get("kind", "post") in ("post", "release"):
            dt = core.parse_time(item.get("published_at"))
            if dt and dt < fresh_after:  # 15:00 run: stories must be newer than the morning fetch
                seen_reasons["stale"] = seen_reasons.get("stale", 0) + 1
                continue
        item["module"] = "community" if item.get("prompt_q") else classify(item, release_re)
        if community_gated(item) and not community_ai_ok(item):
            seen_reasons["off_topic"] = seen_reasons.get("off_topic", 0) + 1
            continue
        item["score"] = score(item, now)
        if feedback:
            if stable_id(item["url"]) in feedback.trashed_ids:
                seen_reasons["trashed"] = seen_reasons.get("trashed", 0) + 1
                continue
            keys = learning.item_keys({"source": item["source"], "url": item["url"], "source_label": source_label(item),
                                       "keywords": sorted({t["term"] for t in item["matched"]})[:6]})
            f = feedback.factor(keys)
            if f != 1:  # owner feedback on specific keys (repo / account / feed / keyword), never broad tags
                item["score"] = {**item["score"], "total": round(item["score"]["total"] * f, 2), "feedback": f, "feedback_why": feedback.why(keys)}
        if item.get("prompt_q"):
            prompt_pool.append(item)
            continue
        if min_score and item["score"]["total"] < min_score.get(item["module"], 0):
            # 15:00 bar: a new item must beat the weakest morning card of its module
            seen_reasons["below_bar"] = seen_reasons.get("below_bar", 0) + 1
            continue
        pool.append(item)
    pool = dedupe(pool)
    if check_links:
        mark_stale(sorted(pool, key=lambda i: -i["score"]["total"])[:STALE_CHECK_N] + prompt_pool[:10])
    selected = []
    for module, _, _ in MODULES:
        ranked = [i for i in pool if i["module"] == module]
        # per-source cap inside a module keeps one noisy feed from filling the quota
        cap, counts, topics, picked = max(3, quotas[module] // 2), {}, {}, []
        for i in ranked:
            rsshub = i.get("transport") == "rsshub"
            key = "rsshub" if rsshub else (i.get("feed") if i["source"] == "rss" else i["source"])
            limit = 2 if rsshub or i["source"] == "x" or (i["source"] == "rss" and i.get("kind") == "release") else cap  # X: max 2 per module
            if counts.get(key, 0) >= limit:
                continue
            # Rule-based topic dedupe: at most TOPIC_CAP cards per specific title keyword
            # (e.g. "FLUX"); the rest are folded into the first card as extra links.
            topic = topic_key(i)
            if topic and topics.get(topic):
                if len(topics[topic]) >= TOPIC_CAP:
                    topics[topic][0].setdefault("also", []).append({"source": i["source"], "url": i["url"]})
                    continue
            counts[key] = counts.get(key, 0) + 1
            if topic:
                topics.setdefault(topic, []).append(i)
            picked.append(i)
            if len(picked) >= quotas[module]:
                break
        selected.extend(picked)
    # 今日提示词: guaranteed 1–2 prompt-sharing posts (morning 2, incremental 1), outside module quotas and the X cap
    sel_urls = {i["url"] for i in selected}
    want = PROMPT_QUOTA if batch == "am" else 1
    ranked_p = sorted((i for i in dedupe(prompt_pool) if i["url"] not in sel_urls),
                      key=lambda i: (bool(i.get("prompt")), bool(i.get("media")), i.get("likes") or 0), reverse=True)
    selected.extend(ranked_p[:want])
    if fetch_bodies:
        fetch_excerpts(selected, timeout)
    cards = []
    for i in selected:
        tags = sorted({t["group"] for t in i["matched"]}, key=TAG_ORDER.index) or ["AI"]
        if "AI" not in tags and tags:
            tags = ["AI", *tags] if i["source"] not in ("github", "hf") else tags
        links = [{"label": (RELEASE_URL.match(a["url"]).group(2) if RELEASE_URL.match(a["url"]) else SOURCE_LABEL.get(a["source"], a["source"])), "url": a["url"]} for a in i.get("also", [])][:4]
        for l in i.get("source_links", []):
            if core.normalize_url(l["url"]) != core.normalize_url(i["url"]) and len(links) < 4:
                links.append({"label": "讨论" if "ycombinator" in l["url"] or "reddit.com" in l["url"] else "原文", "url": l["url"]})
        cards.append({
            "id": stable_id(i["url"]), "module": i["module"], "kind": i.get("kind", "post"),
            "title": clean(i["title"])[:200] or i["url"],
            "excerpt": not_title(excerpt(i.get("listing_text") or "") or excerpt(i.get("page_text") or ""), i["title"]),
            "url": i["url"], "source": i["source"], "source_label": source_label(i),
            "published_at": i.get("published_at"), "metrics": metrics(i), "tags": tags,
            "keywords": sorted({t["term"] for t in i["matched"]})[:6],
            "score": i["score"], "links": links, "visibility": "public",
            "batch": batch, "fetched_at": fetched_at,
        })
        img = pick_image(i)
        if img:
            cards[-1]["image"] = img
        if i["source"] == "x":
            gal = [g for g in (x_image(m, 300) for m in (i.get("media") or []) if m.get("type") == "photo") if g]
            if len(gal) >= 2 or (gal and not img):
                cards[-1]["gallery"] = [{k: g[k] for k in ("src", "width", "height")} for g in gal[:4]]
                if img and len(gal) >= 2:
                    cards[-1].pop("image")
        if linked_target(i) and i["source"] == "x":
            cards[-1]["linked"] = linked_target(i)
        if i.get("stale_link"):
            cards[-1]["stale_link"] = i["stale_link"]
        if i.get("prompt_q"):
            cards[-1]["prompt_pick"] = True
            if i.get("prompt"):
                cards[-1]["prompt"] = i["prompt"]
        enc = i.get("enclosure") or {}
        if enc.get("type", "").startswith("audio/"):
            cards[-1]["audio"] = {"src": enc["url"], "type": enc["type"]}
        elif enc.get("type") == "video/mp4" and not img:
            cards[-1]["image"] = {"src": enc["url"], "width": 1280, "height": 720, "alt": "", "type": "video"}
        if i["source"] == "x" and i.get("author"):
            av = i.get("avatar") or ""
            cards[-1]["author"] = {"handle": str(i["author"]).lstrip("@"), "name": i.get("author_name") or "",
                                   **({"avatar": av} if av.startswith("https://pbs.twimg.com/") else {}), "verified": bool(i.get("verified"))}
            if i.get("likes") is not None:
                cards[-1]["metrics"] = {**cards[-1]["metrics"], "likes_x": i.get("likes") or 0, "reposts": i.get("reposts") or 0}
    if not os.environ.get("NEWS_NO_MEDIA"):
        import media  # README / Reddit / og media; replaces generic HF/GitHub social cards
        media.enrich(cards)
    counts = {m: sum(1 for c in cards if c["module"] == m) for m, _, _ in MODULES}
    # Headline: best-scoring model-news card that is a story (not a bare repo/model id).
    top = next((c for c in cards if c["module"] == "model" and c["kind"] == "post"), None) \
        or next((c for c in cards if c["module"] == "model"), cards[0] if cards else None)
    return {
        "schema_version": "2.0",
        "date": date,
        "window": {"start": core.to_iso(start), "end": core.to_iso(end), "timezone": TZ},
        "headline": top["title"] if top else "今日暂无条目",
        "modules": [{"id": m, "index": f"0{n + 1}", "title": t, "desc": d, "count": counts[m]} for n, (m, t, d) in enumerate(MODULES)],
        "items": cards,
        "arena": {"boards": arena},
        "stats": {
            "fetched": len(raw), "kept_after_filter": len(pool), "selected": len(cards), "dropped": seen_reasons,
            "sources": {name: {k: v for k, v in st.items() if k in ("fetched", "skipped") or (k == "errors" and v)} for name, st in stats.items()},
        },
        "meta": {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "generator": "news-daily/rules", "visibility": "public"},
    }


def pm_bar(base: dict) -> dict:
    out = {}
    for c in base["items"]:
        if c.get("batch", "am") == "am":
            out[c["module"]] = min(out.get(c["module"], 1e9), c["score"]["total"])
    return out


def merge_incremental(base: dict, new: dict, keep_window: bool = False) -> dict:
    """Morning items stay byte-identical (only a missing batch defaults to 'am'); new items are appended."""
    am_at = (base.get("window") or {}).get("end")
    items = [{**c, "batch": c.get("batch") or "am", "fetched_at": c.get("fetched_at") or am_at} for c in base["items"]]
    seen = {c["id"] for c in items}
    items += [c for c in new["items"] if c["id"] not in seen]
    out = dict(base)
    out["items"] = items
    for m in out["modules"]:
        m["count"] = sum(1 for c in items if c["module"] == m["id"])
    out["modules"] = [dict(m) for m in out["modules"]]
    if not keep_window and new.get("window"):
        out["window"] = {**base["window"], "end": new["window"]["end"]}
    out.setdefault("stats", {})
    out["stats"] = {**base.get("stats", {}), "selected": len(items)}
    if not keep_window:
        out["stats"]["incremental"] = {"fetched": new["stats"]["fetched"], "added": len(new["items"]), "dropped": new["stats"]["dropped"]}
        out["meta"] = {**base.get("meta", {}), "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sources", default=str(ROOT / "config/collect-sources.json"))
    ap.add_argument("--tags", default=str(ROOT / "config/tags.json"))
    ap.add_argument("--only", default="hn,reddit,rss,rsshub,github,hf,ossinsight,x")
    ap.add_argument("--x-items", default="")
    ap.add_argument("--arena", default="")
    ap.add_argument("--raw-dir", default="")
    ap.add_argument("--from-raw", default="", help="replay a saved run dir (raw.json + stats.json) instead of fetching")
    ap.add_argument("--end", default="", help="window end (ISO); with --from-raw use the original fetch time")
    ap.add_argument("--feedback", default=str(ROOT / "var/state/feedback.json"), help="owner feedback (sync-state.mjs); missing = no learning")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--timeout", type=int, default=core.DEFAULT_TIMEOUT)
    ap.add_argument("--base", default="", help="existing data/<date>.json (merged; required with --incremental)")
    ap.add_argument("--seen-raw", action="append", default=[], help="earlier raw.json of the same day; with --incremental, anything already seen there is not 'new'")
    ap.add_argument("--run", default="15:00", help="incremental run label (15:00 / 20:00); stored on new items as 'run'")
    ap.add_argument("--incremental", action="store_true", help="15:00 run: keep morning items, add only new ones as batch 'pm'")
    ap.add_argument("--no-bodies", action="store_true", help="never fetch article pages for excerpts")
    args = ap.parse_args(argv)
    tz = ZoneInfo(TZ)
    seen_raw = []
    for f in args.seen_raw:  # read before this run's raw.json overwrites anything
        if Path(f).is_file():
            seen_raw += [{"url": r["url"], "title": "", "links": []} for r in load(Path(f)) if r.get("url")]
    day_end = datetime.fromisoformat(args.date).replace(hour=23, minute=59, second=59, tzinfo=tz)
    end = core.parse_time(args.end) if args.end else min(datetime.now(timezone.utc), day_end.astimezone(timezone.utc))
    start = end - timedelta(hours=args.hours)
    tags_cfg = load(Path(args.tags))
    terms = tag_terms(tags_cfg)
    import source_config  # owner 信息源 settings (var/state/sources.json) merged over defaults
    eff_sources, plugins_off = source_config.apply(load(Path(args.sources)), source_config.load_settings())
    ctx = plugins.Context(start=start, end=end, sources=eff_sources, timeout=args.timeout,
                          x_items=Path(args.x_items) if args.x_items else None)
    if args.from_raw:
        raw, stats = load(Path(args.from_raw) / "raw.json"), load(Path(args.from_raw) / "stats.json")
    else:
        raw, stats = plugins.run_all(ctx, [p.strip() for p in args.only.split(",") if p.strip() and p.strip() not in plugins_off])
    if args.raw_dir:
        rd = Path(args.raw_dir)
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        (rd / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    arena = arena_view(Path(args.arena) if args.arena else None)
    fb = learning.Feedback.load(Path(args.feedback) if args.feedback else None, end.timestamp() * 1000)
    base_path = Path(args.base) if args.base else None
    base = load(base_path) if base_path and base_path.is_file() else None
    if args.incremental:
        if not base:
            print(f"--incremental 需要已有的当天数据 --base（{args.base or '未给出'}）", file=sys.stderr)
            return 2
        daily = merge_incremental(base, build(args.date, raw, stats, terms, tags_cfg, start, end, arena, args.timeout,
                                              not args.no_bodies, feedback=fb, exclude=base["items"] + seen_raw, quotas=PM_QUOTAS, batch="pm",
                                              fresh_after=core.parse_time(base["window"]["end"]) - timedelta(hours=1),
                                              min_score=pm_bar(base)))
        old = {c["id"] for c in base["items"]}
        for c in daily["items"]:
            if c["id"] not in old and c.get("batch") == "pm":
                c["run"] = args.run
    else:
        daily = build(args.date, raw, stats, terms, tags_cfg, start, end, arena, args.timeout, not args.no_bodies, feedback=fb)
        zh = {c["id"]: c for c in (base or {}).get("items", []) if c.get("title_zh")}
        for c in daily["items"]:  # re-running keeps Chinese titles the agent already wrote
            if c["id"] in zh:
                c["title_zh"] = zh[c["id"]]["title_zh"]
                for k in ("summary_zh", "comment_zh"):
                    if zh[c["id"]].get(k):
                        c[k] = zh[c["id"]][k]
        keep = [c for c in (base or {}).get("items", []) if c.get("batch") == "pm"]
        if keep:  # re-running the morning job must not drop or duplicate already published 15:00 items
            urls, titles = item_keys(daily["items"])
            keep = [c for c in keep if core.normalize_url(c["url"]) not in urls and not any(core.titles_similar(t, c["title"]) for t in titles)]
            daily = merge_incremental(daily, {**daily, "items": keep, "window": None}, keep_window=True)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(daily, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(out), "fetched": daily["stats"]["fetched"], "selected": daily["stats"]["selected"],
                      "modules": {m["id"]: m["count"] for m in daily["modules"]}}, ensure_ascii=False))
    return 0 if daily["items"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
