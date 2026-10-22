"""Per-user model processing over the shared pool.

``process_user`` always produces a ranked list. Without a model key (or with AI turned off) it is the
rule ranking only -- keywords (block/interest/boost) and recency -- with no model calls; translation
is limited to what is already in the shared cache. With a key:

1. each picked processing profile scores the items in batches (belongs? 0-10 score, section);
   verdicts are stored in ``item_scores`` (empty/error verdicts are retried next run, ok ones reused);
2. items pass when some profile says belongs and score >= that profile's (user-adjusted) threshold;
3. optional semantic dedup: the model groups items about the same event, the best one is kept;
4. optional AI re-ranking of the top candidates;
5. titles and summaries are translated through the shared cache.

Invalid JSON is repaired once by the client; if still invalid the batch is recorded as ``empty`` and
the run continues. Model errors never fail the run; an auth error stops further model calls.
"""
from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from newsd.content import clean_summary, display_title
from newsd.models import ItemScore, Keyword, PoolItem
from newsd.pool.collector import item_text, user_items

from . import keys as llm_keys
from .client import LLMClient, LLMError
from .profiles import all_profiles, get_picks, system_prompt
from .translate import translate_texts

SCORE_BATCH = 8
EXCERPT = 1200
RERANK_TOP = 30
DEDUPE_MAX = 60


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ------------------------------------------------------------------------------------------ rule ranking

def rule_rank(db, user_id: int, items: list[PoolItem], now: datetime | None = None) -> list[tuple[PoolItem, float]]:
    """Keyword + recency ranking (no model). Blocked items are dropped."""
    now = now or _now()
    kws = list(db.scalars(select(Keyword).where(Keyword.user_id == user_id)))
    out = []
    for it in items:
        hay = f"{it.title}\n{it.summary}".lower()
        if any(k.kind == "block" and k.term.lower() in hay for k in kws):
            continue
        s = sum((k.weight or 1.0) for k in kws if k.kind in ("interest", "boost") and k.term.lower() in hay)
        when = it.published_at or it.first_seen_at or now
        age_h = max(0.0, (now - when).total_seconds() / 3600)
        s += 2.0 * math.exp(-age_h / 24)
        out.append((it, round(s, 3)))
    out.sort(key=lambda p: (-p[1], -(p[0].id or 0)))
    return out


# ------------------------------------------------------------------------------------------ AI steps

def _clamp(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return max(0.0, min(10.0, f))


async def _score_profile(db, client: LLMClient, user_id: int, prof, items: list[PoolItem], report: dict) -> None:
    have = {s.item_id: s for s in db.scalars(select(ItemScore).where(
        ItemScore.user_id == user_id, ItemScore.profile == prof.name, ItemScore.item_id.in_([i.id for i in items])))}
    todo = [i for i in items if i.id not in have or have[i.id].status != "ok"]
    report["reused"] += len(items) - len(todo)
    if not todo:
        return
    batches = [todo[i:i + SCORE_BATCH] for i in range(0, len(todo), SCORE_BATCH)]
    texts = {i.id: item_text(db, i)[:EXCERPT] for i in todo}
    sysmsg = system_prompt(prof)

    async def one(batch):
        if report.get("stopped"):
            return LLMError("auth", "stopped after an auth error")
        payload = json.dumps([{"id": i.id, "title": i.title, "text": texts[i.id]} for i in batch], ensure_ascii=False)
        try:
            return await client.complete_json(sysmsg, payload, max_tokens=300 * len(batch) + 200)
        except LLMError as exc:
            if exc.kind == "auth":
                report["stopped"] = True
            return exc

    replies = await asyncio.gather(*(one(b) for b in batches))
    for batch, data in zip(batches, replies):
        verdicts = {}
        if isinstance(data, LLMError):
            status = f"error:{data.kind}"
        else:
            rows = data.get("items") if isinstance(data, dict) else data if isinstance(data, list) else None
            for r in rows or []:
                if isinstance(r, dict) and "id" in r:
                    try:
                        verdicts[int(r["id"])] = r
                    except (TypeError, ValueError):
                        continue
            status = "empty"
        for it in batch:
            r = verdicts.get(it.id)
            row = have.get(it.id) or ItemScore(user_id=user_id, item_id=it.id, profile=prof.name)
            if row.id is None:
                db.add(row)
            row.model, row.created_at = client.label, _now()
            score = _clamp(r.get("score")) if r else None
            if r is not None and score is not None:
                section = r.get("section") if r.get("section") in prof.sections else prof.sections[-1]
                row.status, row.belongs, row.score = "ok", bool(r.get("belongs")), score
                row.section, row.reason = section, str(r.get("reason") or "")[:500]
                report["scored"] += 1
            else:
                row.status, row.belongs, row.score, row.section, row.reason = status, None, None, None, None
                report["empty" if status == "empty" else "errors"] += 1
    db.commit()


async def _dedupe(client: LLMClient, cands: list[dict], report: dict) -> list[dict]:
    if len(cands) < 2:
        return cands
    head, tail = cands[:DEDUPE_MAX], cands[DEDUPE_MAX:]
    payload = json.dumps([{"id": c["id"], "title": c["title"], "summary": (c["summary"] or "")[:200]} for c in head], ensure_ascii=False)
    sysmsg = ("下面是新闻条目。找出报道同一事件的条目（同一次发布、同一篇论文、同一件事的不同报道），分组输出。"
              '只输出 JSON：{"groups": [[id, id, ...], ...]}，只列出包含 2 条及以上的组。')
    try:
        data = await client.complete_json(sysmsg, payload, max_tokens=1024)
    except LLMError as exc:
        report["dedupe"] = f"skipped: {exc.kind}"
        return cands
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        report["dedupe"] = "skipped: invalid reply"
        return cands
    by_id = {c["id"]: c for c in head}
    dropped = set()
    for g in groups:
        ids = [int(x) for x in g if isinstance(x, (int, str)) and str(x).isdigit() and int(x) in by_id] if isinstance(g, list) else []
        ids = [i for i in dict.fromkeys(ids) if i not in dropped]
        if len(ids) < 2:
            continue
        keep = max(ids, key=lambda i: (by_id[i]["score"] or 0, -head.index(by_id[i])))
        for i in ids:
            if i != keep:
                dropped.add(i)
                by_id[keep].setdefault("duplicates", []).append(i)
    report["dedupe"] = f"merged {len(dropped)}"
    return [c for c in head if c["id"] not in dropped] + tail


async def _rerank(client: LLMClient, cands: list[dict], report: dict) -> list[dict]:
    if len(cands) < 2:
        return cands
    head, tail = cands[:RERANK_TOP], cands[RERANK_TOP:]
    payload = json.dumps([{"id": c["id"], "title": c["title"], "section": c.get("section"), "score": c["score"]} for c in head],
                         ensure_ascii=False)
    sysmsg = ("你是日报主编。按读者最该先看到的顺序重新排列这些条目：重要性、新颖性、可信度优先，同类内容不要扎堆。"
              '只输出 JSON：{"order": [id, ...]}，包含全部 id。')
    try:
        data = await client.complete_json(sysmsg, payload, max_tokens=512)
    except LLMError as exc:
        report["rerank"] = f"skipped: {exc.kind}"
        return cands
    order = data.get("order") if isinstance(data, dict) else data if isinstance(data, list) else None
    by_id = {c["id"]: c for c in head}
    seen = []
    for x in order or []:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i in by_id and i not in seen:
            seen.append(i)
    if not seen:
        report["rerank"] = "skipped: invalid reply"
        return cands
    report["rerank"] = "ok"
    ranked = [by_id[i] for i in seen] + [c for c in head if c["id"] not in seen]
    for n, c in enumerate(ranked):
        c["rank_source"] = "ai" if c["id"] in seen else "rule"
    return ranked + tail


# ------------------------------------------------------------------------------------------ entry point

def _card(it: PoolItem, rule: float) -> dict:
    return {"id": it.id, "title": display_title(it.title, it.url, it.summary), "url": it.url,
            "summary": clean_summary(it.summary, 400), "published_at":
            it.published_at.isoformat() if it.published_at else None, "rule_score": rule, "score": None,
            "profile": None, "section": None, "reason": None}


async def process_user(db, user_id: int, *, client: LLMClient | None | str = "auto", since: datetime | None = None,
                       since_hours: float | None = 26, limit: int = 300, rerank: bool = True, dedupe: bool = True,
                       translate: bool = True, max_items: int | None = None) -> dict:
    """Rank the user's pool items. client="auto" builds one from the user's stored key (None if no key)."""
    own = False
    if client == "auto":
        client = llm_keys.client_for_user(db, user_id)
        own = client is not None
    try:
        if since is None and since_hours is not None:
            since = _now() - timedelta(hours=since_hours)
        items = user_items(db, user_id, since=since, limit=limit)
        ranked = rule_rank(db, user_id, items)
        report = {"user_id": user_id, "items_in": len(items), "ai": "on" if client else "skipped: no model key",
                  "model": client.label if client else None, "scored": 0, "reused": 0, "empty": 0, "errors": 0,
                  "dedupe": "off", "rerank": "off", "translation": None}
        cards = [_card(it, r) for it, r in ranked]
        if client is not None:
            picks = get_picks(db, user_id)
            profs = all_profiles()
            pool = [it for it, _ in ranked]
            for pick in picks:
                await _score_profile(db, client, user_id, profs[pick["name"]], pool, report)
            thresholds = {p["name"]: p["threshold"] for p in picks}
            scores = {}
            for s in db.scalars(select(ItemScore).where(ItemScore.user_id == user_id, ItemScore.profile.in_(list(thresholds)),
                                                         ItemScore.item_id.in_([c["id"] for c in cards]))):
                scores.setdefault(s.item_id, []).append(s)
            any_ok = any(s.status == "ok" for v in scores.values() for s in v)
            if any_ok:
                kept = []
                for c in cards:
                    best = None
                    for s in scores.get(c["id"], []):
                        if s.status == "ok" and s.belongs and s.score is not None and s.score >= thresholds[s.profile]:
                            if best is None or s.score > best.score:
                                best = s
                    if best is not None:
                        c.update(score=best.score, profile=best.profile, section=best.section, reason=best.reason)
                        kept.append(c)
                kept.sort(key=lambda c: (-c["score"], -c["rule_score"]))
                cards = kept
                if dedupe and not report.get("stopped"):
                    cards = await _dedupe(client, cards, report)
                if rerank and not report.get("stopped"):
                    cards = await _rerank(client, cards, report)
            else:
                report["ai"] = "fallback: no usable model verdicts, rule ranking kept"
        if max_items is not None:
            cards = cards[:max_items]
        if translate:
            lang = llm_keys.translate_to(db, user_id)
            texts = [c["title"] for c in cards] + [c["summary"] for c in cards if c["summary"]]
            tr, stats = await translate_texts(db, client if not report.get("stopped") else None, texts, lang)
            for c in cards:
                c["title_translated"] = tr.get(c["title"])
                c["summary_translated"] = tr.get(c["summary"]) if c["summary"] else None
            report["translation"] = {"lang": lang, **stats, "model_calls": client is not None}
        report["stopped"] = bool(report.get("stopped"))
        report["items_out"] = len(cards)
        report["items"] = cards
        if client is not None:
            u = client.usage
            report["usage"] = {"requests": u.requests, "retries": u.retries, "repairs": u.repairs,
                               "invalid_json": u.invalid_json, "errors": dict(u.errors),
                               "failovers": getattr(u, "failovers", 0), "chain": getattr(client, "labels", [client.label])}
        return report
    finally:
        if own:
            await client.aclose()


def process_user_sync(db, user_id: int, **kw) -> dict:
    return asyncio.run(process_user(db, user_id, **kw))
