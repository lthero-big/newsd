"""M2-3 per-user rule scoring, dedup and quota cut (no model needed).

Pipeline for one user (``newsd.issues.build_issue`` drives it and layers the M2-4 model on top):

1. ``UserRules`` from the user's keywords: interest (include), boost (weighted) and block (exclude).
   A keyword that is also a global tag term inherits that term's aliases, tag group and "broad" flag.
2. ``score_candidates``: match against title, URL and the internal text (extracted full text when the
   pool has it, else the source summary). Blocked items and -- when the user has any interest/boost
   keywords -- items matching none of them are excluded with a reason. Score 0-100 =
   source weight x (relevance, engagement, base) x recency.
3. ``dedupe``: by normalized URL first, then title similarity; the best-scored copy is kept and the
   others are attached as ``also``.
4. ``cut``: walk by score, applying the per-topic cap, per-group caps (module + categories), per-module
   caps and the total cap, in that order. Every dropped item gets a reason.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from newsd.collector import collect as core
from newsd.models import GlobalSetting, Keyword, PoolItemSource, Quota, Source, TagTerm

MODULES = ("model", "opensource", "community", "news")
DEFAULT_TOTAL = 30
DEFAULT_MODULE_CAPS = {"model": 8, "opensource": 6, "community": 8, "news": 8}
DEFAULT_TOPIC_CAP = 2
SOURCE_WEIGHT = {"rss": 1.25, "hf": 0.95, "github": 1.0, "ossinsight": 1.0, "hn": 1.0, "reddit": 0.85, "x": 0.9}
ENGAGEMENT_SCALE = {"hn": 400, "reddit": 600, "github": 3000, "ossinsight": 2000, "hf": 1500, "x": 3000}
OFFICIAL_FEEDS = {"rss:openai-news", "rss:google-ai-blog", "rss:hf-blog"}
LOCATION_FACTOR = {"title": 1.0, "body": 0.5, "url": 0.3}
FULLTEXT_LIMIT = 20000


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ------------------------------------------------------------------------------------------ rules

@dataclass
class UserRules:
    include: list[dict] = field(default_factory=list)  # match_terms() term dicts + kind/weight
    block: list[dict] = field(default_factory=list)
    release_patterns: list[re.Pattern] = field(default_factory=list)

    @property
    def has_include(self) -> bool:
        return bool(self.include)


def load_rules(db, user_id: int) -> UserRules:
    tags = {}
    for t in db.scalars(select(TagTerm)):
        tags.setdefault(core.fold(t.term), t)
    rules = UserRules()
    for k in db.scalars(select(Keyword).where(Keyword.user_id == user_id).order_by(Keyword.id)):
        tag = tags.get(core.fold(k.term))
        term = {"term": k.term, "aliases": list(tag.aliases or []) if tag else [], "group": tag.tag if tag else k.term,
                "broad": bool(tag.broad) if tag else False, "kind": k.kind, "weight": float(k.weight or 1.0)}
        (rules.block if k.kind == "block" else rules.include).append(term)
    pats = db.get(GlobalSetting, "model_release_patterns")
    for p in (pats.value if pats is not None and isinstance(pats.value, list) else []):
        try:
            rules.release_patterns.append(re.compile(p, re.I))
        except re.error:
            continue
    return rules


# ------------------------------------------------------------------------------------------ candidates

@dataclass
class Candidate:
    item_id: int
    title: str
    url: str
    summary: str
    text: str  # internal text used for matching (full text when extracted)
    kind: str | None
    published_at: datetime | None
    extra: dict
    source_keys: list[str]
    source_kinds: list[str]
    canonical_url: str = ""
    module: str = "news"
    matched: list[dict] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    topic: str | None = None
    rule_score: float = 0.0
    score: float = 0.0  # final score (rule, or rule + model)
    detail: dict = field(default_factory=dict)
    also: list[dict] = field(default_factory=list)
    reason: str | None = None  # why it was dropped
    group: str = ""
    llm: dict | None = None


def item_sources(db, item_ids: list[int]) -> dict[int, list[Source]]:
    out: dict[int, list[Source]] = {}
    if not item_ids:
        return out
    rows = db.execute(select(PoolItemSource.item_id, Source).join(Source, Source.id == PoolItemSource.source_id)
                      .where(PoolItemSource.item_id.in_(item_ids)).order_by(PoolItemSource.id))
    for iid, src in rows:
        out.setdefault(iid, []).append(src)
    return out


def source_label(src_key: str, src_kind: str) -> str:
    if src_kind == "plugin":
        return {"plugin:hn": "hn", "plugin:github_search": "github", "plugin:hf_trending": "hf",
                "plugin:ossinsight": "ossinsight"}.get(src_key, "rss")
    if src_kind in ("x_account", "x_query"):
        return "x"
    if src_kind == "rsshub":
        return "x" if "twitter" in src_key else "rss"
    return src_kind  # rss | reddit


def classify(c: Candidate, rules: UserRules) -> str:
    labels = {source_label(k, kd) for k, kd in zip(c.source_keys, c.source_kinds)}
    if c.kind == "model":
        return "model"
    if c.kind in ("repo", "release") or labels & {"github", "hf", "ossinsight"}:
        return "opensource"
    if set(c.source_keys) & OFFICIAL_FEEDS:
        return "model"
    named = any(not h["broad"] and "title" in h["locations"] for h in c.matched)
    if named and any(p.search(c.title.lower()) for p in rules.release_patterns):
        return "model"
    if labels & {"hn", "reddit", "x"}:
        return "community"
    return "news"


def score_candidate(c: Candidate, rules: UserRules, now: datetime) -> None:
    body = f"{c.summary}\n{c.text}"[:FULLTEXT_LIMIT]
    blocked = core.match_terms(c.title, body, c.url, rules.block) if rules.block else []
    if blocked:
        c.reason = f"blocked:{blocked[0]['term']}"
        return
    by_term = {t["term"]: t for t in rules.include}
    hits = core.match_terms(c.title, body, c.url, rules.include) if rules.include else []
    for h in hits:
        h["kind"], h["weight"] = by_term[h["term"]]["kind"], by_term[h["term"]]["weight"]
    c.matched = hits
    if rules.has_include and not hits:
        c.reason = "no-keyword-match"
        return
    kw = 0.0
    for h in hits:
        loc = max(LOCATION_FACTOR[l] for l in h["locations"])
        kw += h["weight"] * loc * (0.5 if h["broad"] else 1.0)
    relevance = min(1.0, kw / 2.0) if rules.has_include else 0.5
    labels = [source_label(k, kd) for k, kd in zip(c.source_keys, c.source_kinds)] or ["rss"]
    label = labels[0]
    pts = max(0, int((c.extra or {}).get("points") or 0))
    com = max(0, int((c.extra or {}).get("comments") or 0))
    if label == "rss":
        engagement = 0.5
    else:
        scale = ENGAGEMENT_SCALE.get(label, 500)
        engagement = min(1.0, (math.log1p(pts) + 0.5 * math.log1p(com)) / (1.5 * math.log1p(scale)))
    age_h = max(0.0, (now - c.published_at).total_seconds() / 3600) if c.published_at else 24.0
    half_life = 24.0 if c.kind in (None, "post", "release") else 96.0
    decay = 0.5 ** (age_h / half_life)
    weight = max(SOURCE_WEIGHT.get(l, 1.0) for l in labels)
    total = 100 * weight * (0.5 * relevance + 0.35 * engagement + 0.15) * (0.4 + 0.6 * decay)
    c.rule_score = c.score = round(total, 2)
    c.detail = {"relevance": round(relevance, 3), "engagement": round(engagement, 3), "decay": round(decay, 3),
                "source_weight": weight}
    title_specific = sorted(h["term"] for h in hits if not h["broad"] and "title" in h["locations"])
    c.topic = core.fold(title_specific[0]) if title_specific else None
    c.categories = sorted({h["group"] for h in hits} | {h["term"] for h in hits})
    c.module = classify(c, rules)


def score_candidates(cands: list[Candidate], rules: UserRules, now: datetime | None = None) -> tuple[list[Candidate], list[Candidate]]:
    """Return (kept sorted by score desc, dropped with reason)."""
    now = now or _now()
    kept, dropped = [], []
    for c in cands:
        score_candidate(c, rules, now)
        (dropped if c.reason else kept).append(c)
    kept.sort(key=lambda c: (-c.score, -c.item_id))
    return kept, dropped


# ------------------------------------------------------------------------------------------ dedup

def dedupe(cands: list[Candidate]) -> tuple[list[Candidate], list[Candidate]]:
    """Normalized URL first, then title similarity. Input must be sorted best-first."""
    kept: list[Candidate] = []
    by_url: dict[str, Candidate] = {}
    dropped = []
    for c in cands:
        key = core.normalize_url(c.canonical_url or c.url)
        twin = by_url.get(key)
        how = "url"
        if twin is None:
            twin = next((k for k in kept if core.titles_similar(k.title, c.title)), None)
            how = "title"
        if twin is not None:
            twin.also.append({"item_id": c.item_id, "url": c.url, "title": c.title, "by": how})
            c.reason = f"duplicate-of:{twin.item_id}:{how}"
            dropped.append(c)
            continue
        kept.append(c)
        by_url[key] = c
    return kept, dropped


# ------------------------------------------------------------------------------------------ quotas

@dataclass
class Caps:
    total: int = DEFAULT_TOTAL
    modules: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_MODULE_CAPS))
    groups: list[dict] = field(default_factory=list)  # [{"module", "group", "max_items", "categories"}] in order
    topic: int = DEFAULT_TOPIC_CAP

    def public(self) -> dict:
        return {"total": self.total, "modules": self.modules, "groups": self.groups, "topic": self.topic}


def load_caps(db, user_id: int) -> Caps:
    """User quotas override the defaults: module="" group="" = total, group="" = module cap, both = group cap.
    An admin can change the per-topic cap with the global setting ``topic_cap``."""
    caps = Caps()
    g = db.get(GlobalSetting, "topic_cap")
    if g is not None and isinstance(g.value, int):
        caps.topic = g.value
    for q in db.scalars(select(Quota).where(Quota.user_id == user_id).order_by(Quota.id)):
        if not q.module and not q.group_name:
            caps.total = q.max_items
        elif not q.group_name:
            caps.modules[q.module] = q.max_items
        else:
            caps.groups.append({"module": q.module, "group": q.group_name, "max_items": q.max_items,
                                "categories": [core.fold(x) for x in (q.categories or [q.group_name])]})
    from .perms import group_quota
    gq = group_quota(db, user_id)  # 3c: a group's daily quota caps the member's own total
    if gq is not None:
        caps.total = min(caps.total, gq)
    return caps


def group_of(c: Candidate, caps: Caps) -> dict | None:
    cats = {core.fold(x) for x in c.categories}
    for g in caps.groups:
        if g["module"] == c.module and cats & set(g["categories"]):
            return g
    return None


def cut(cands: list[Candidate], caps: Caps) -> tuple[list[Candidate], list[Candidate]]:
    """Greedy by score: topic cap, then group cap, then module cap, then total cap. Items from a 特别关注 zone
    (``detail["special"]``, DEPTAI-216 3b) are always kept and do not count against any cap."""
    kept, dropped = [], []
    per_topic: dict[str, int] = {}
    per_group: dict[tuple, int] = {}
    per_module: dict[str, int] = {}
    n_special = 0
    for c in sorted(cands, key=lambda c: (-c.score, -c.item_id)):
        g = group_of(c, caps)
        gk = (g["module"], g["group"]) if g else None
        c.group = g["group"] if g else ""
        if (c.detail or {}).get("special"):
            kept.append(c)
            n_special += 1
            continue
        if c.topic and caps.topic and per_topic.get(c.topic, 0) >= caps.topic:
            c.reason = f"topic-cap:{c.topic}"
        elif g and per_group.get(gk, 0) >= g["max_items"]:
            c.reason = f"group-cap:{c.module}/{g['group']}"
        elif c.module in caps.modules and per_module.get(c.module, 0) >= caps.modules[c.module]:
            c.reason = f"module-cap:{c.module}"
        elif len(kept) - n_special >= caps.total:
            c.reason = "total-cap"
        if c.reason:
            dropped.append(c)
            continue
        kept.append(c)
        if c.topic:
            per_topic[c.topic] = per_topic.get(c.topic, 0) + 1
        if gk:
            per_group[gk] = per_group.get(gk, 0) + 1
        per_module[c.module] = per_module.get(c.module, 0) + 1
    return kept, dropped
