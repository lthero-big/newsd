"""Rule-based learning from owner feedback (no AI). Mirror of scripts/keys.mjs for keys.

var/state/feedback.json (written by scripts/sync-state.mjs):
  {"trashed":  [{"id", "at", "keys"}],                 # 不感兴趣 → full negative (weight 1)
   "disliked": [{"id", "at", "keys", "weight": 0.5}],  # 少推这类 (vote -1), unless the item is 收藏
   "liked":    [{"id", "at", "keys", "weight": 1}],    # 点赞 (vote 1) or 收藏 — one entry per item
v3.6: 已收起 is UI-only (never a signal); ★ ratings are retired (old rows mapped ≥4★ → like, 1–2★ → 少推, 3★ → none).
   "overrides": {key: cleared_at_ms}}
All negative lists are merged into one penalty pool and share the same gating.
Brand names (OpenAI, Claude, Nvidia, Qwen…) never become kw: keys. Penalties are gated: a kw: key, or a feed:/site: key
of a major lab's first-party channel (openai-news, hf-blog, openai.com…), only counts once it appears in >=2 distinct
deleted items within the half-life — so a single delete only hits that item's specific keys (repo, x account, hf org,
niche site). Gating runs at compute time, so old feedback is re-evaluated automatically.
Per specific key: strength = Σ weight · 0.5^(age_days / 30), capped at 2 (stacks up to the cap).
Item factor = clamp(1 − 0.25·Σ penalty, 0.4, 1) × (1 + min(0.30, 0.08·Σ boost)). Restored items are not in "trashed".
Feedback.why(keys) names the keys that moved an item (boost first), for the owner-only 「合你口味」 chip tooltip.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

BROAD = {"ai", "llm", "agent", "agents", "model", "models", "gpt", "benchmark", "diffusion", "fine-tune", "open weights", "open source",
         "quantization", "text-to-image", "text-to-video", "tokenizer", "transformer", "workflow", "coding agent", "computer use", "gguf",
         "hugging face", "inference", "图片视频音频"}
BRANDS = {"gpt", "openai", "anthropic", "claude", "gemini", "google", "deepmind", "nvidia", "deepseek", "qwen", "meta", "llama", "mistral", "xai", "grok", "kimi", "moonshot", "minimax", "chatgpt", "codex", "cursor", "apple", "microsoft", "copilot", "amazon", "aws", "huggingface", "hugging_face", "flux", "black_forest_labs", "bfl", "sora", "veo", "midjourney", "zhipu", "glm", "baidu", "ernie", "alibaba", "tencent", "hunyuan", "bytedance", "seedance", "doubao", "stepfun", "nemotron", "gemma", "phi", "ollama", "vllm", "comfyui", "llama.cpp", "perplexity", "stability", "runway", "kling", "elevenlabs"}
BRAND_RE = re.compile(r"^(?:%s)(?:$|[-_.\d])" % "|".join(re.escape(b) for b in sorted(BRANDS, key=len, reverse=True)))
FIRST_PARTY_RE = re.compile(r"openai|anthropic|deepmind|google|gemini|hf-blog|huggingface|nvidia|meta\.com|ai\.meta|mistral|x\.ai|qwen|deepseek|microsoft|apple")
GENERIC_HOSTS = {"github.com", "x.com", "twitter.com", "reddit.com", "old.reddit.com", "huggingface.co", "news.ycombinator.com", "youtube.com",
                 "youtu.be", "medium.com", "substack.com", "i.redd.it", "v.redd.it"}
HALF_LIFE_DAYS, KEY_CAP, PENALTY_PER, PENALTY_FLOOR, BOOST_PER, BOOST_CAP = 30, 2.0, 0.25, 0.4, 0.08, 0.30
DISLIKE_WEIGHT = 0.5


def item_keys(card: dict) -> list[str]:
    keys, url, label = set(), str(card.get("url") or ""), str(card.get("source_label") or "")
    m = re.match(r"^https://github\.com/([\w.-]+)/([\w.-]+)", url, re.I)
    if m:
        keys.add(f"repo:{m.group(1)}/{m.group(2)}".lower())
    m = re.match(r"^https://huggingface\.co/([\w.-]+)/([\w.-]+)", url, re.I)
    if m and card.get("source") == "hf":
        keys.add(f"hf:{m.group(1)}".lower())
        keys.add(f"hffam:{m.group(1)}/{m.group(2).split('-')[0]}".lower())
    m = re.match(r"^X · @(\w+)", label)
    if m:
        keys.add(f"x:@{m.group(1)}".lower())
    m = re.match(r"^r/(\w+)", label)
    if m:
        keys.add(f"sub:{m.group(1)}".lower())
    if card.get("source") == "rss" and label:
        keys.add(f"feed:{label}".lower())
    if card.get("source") in ("hn", "rss"):
        try:
            h = (urlparse(url).hostname or "").lower()
            h = h[4:] if h.startswith("www.") else h
            if h and h not in GENERIC_HOSTS:
                keys.add(f"site:{h}")
        except ValueError:
            pass
    for k in card.get("keywords") or []:
        t = str(k).lower().strip()
        if len(t) >= 3 and t not in BROAD and not is_brand(t):
            keys.add("kw:" + re.sub(r"\s+", "_", t))
    return sorted(keys)


def is_brand(term: str) -> bool:
    return bool(BRAND_RE.match(re.sub(r"\s+", "_", term.lower().strip())))


def gated(k: str) -> bool:
    """Keys that need >=2 distinct deleted items before they penalize."""
    return k.startswith("kw:") or (k.startswith(("feed:", "site:")) and bool(FIRST_PARTY_RE.search(k)))


def strengths(events: list[dict], overrides: dict, now_ms: float, gate: bool = False) -> dict:
    out: dict = {}
    seen: dict = {}
    if gate:  # distinct items per key inside the half-life window
        for n, e in enumerate(events):
            if (now_ms - e["at"]) / 864e5 <= HALF_LIFE_DAYS:
                for k in e.get("keys") or []:
                    if not (overrides.get(k) and e["at"] <= overrides[k]):
                        seen.setdefault(k, set()).add(e.get("id") or n)
    for e in events:
        for k in e.get("keys") or []:
            if overrides.get(k) and e["at"] <= overrides[k]:
                continue
            if gate and (k.startswith("kw:") and is_brand(k[3:]) or gated(k) and len(seen.get(k, ())) < 2):
                continue
            out[k] = out.get(k, 0.0) + e.get("weight", 1) * 0.5 ** ((now_ms - e["at"]) / 864e5 / HALF_LIFE_DAYS)
    return {k: min(KEY_CAP, v) for k, v in out.items()}


class Feedback:
    def __init__(self, data: dict | None, now_ms: float):
        data = data or {}
        ov = data.get("overrides") or {}
        self.trashed_ids = {e["id"] for e in data.get("trashed") or []}
        neg = (data.get("trashed") or []) + (data.get("disliked") or [])  # a legacy "tucked" list is ignored
        self.penalty = strengths(neg, ov, now_ms, gate=True)
        self.boost = strengths(data.get("liked") or [], {}, now_ms)

    @classmethod
    def load(cls, path: Path | None, now_ms: float) -> "Feedback":
        data = json.loads(path.read_text(encoding="utf-8")) if path and path.is_file() else None
        return cls(data, now_ms)

    def factor(self, keys: list[str]) -> float:
        p = sum(self.penalty.get(k, 0) for k in keys)
        b = sum(self.boost.get(k, 0) for k in keys)
        return round(max(PENALTY_FLOOR, 1 - PENALTY_PER * p) * (1 + min(BOOST_CAP, BOOST_PER * b)), 4)

    def why(self, keys: list[str], n: int = 2) -> list[str]:
        """Keys behind a non-1 factor (for the owner-only 「合你口味」 tooltip): the strongest boosts, or penalties if none."""
        b = sorted((k for k in keys if self.boost.get(k)), key=lambda k: -self.boost[k])
        p = sorted((k for k in keys if self.penalty.get(k)), key=lambda k: -self.penalty[k])
        return (b or p)[:n]
