"""M2-3 per-user issues: rule scoring -> dedup -> (model layer when the user has a key) -> quota cut ->
stored in ``issues`` / ``issue_items`` for the web page and push.

Rules always run. When the user has a model key (M2-4), the picked processing profiles score the
rule-kept candidates (verdicts cached in ``item_scores``): an ok verdict below the user's threshold
drops the item, a passing one blends into the score (0.4 x rule + 0.6 x model x 10); items whose
model verdict is empty/error are blended with a neutral model score of 5. Model semantic dedup runs before the quota cut,
AI re-ranking orders the final list, and titles/summaries go through the shared translation cache.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from .auth import get_db, require_user
from .llm import keys as llm_keys
from .llm.client import LLMClient
from .llm.process import _dedupe as ai_dedupe, _rerank as ai_rerank, _score_profile
from .llm.profiles import all_profiles, get_picks
from .llm.translate import translate_texts
from .feedback import adjust_candidates, annotate
from .marks import user_marks
from .models import Issue, IssueItem, ItemScore, PoolItem, User
from .pool.collector import item_text, user_items
from .scoring import Candidate, cut, dedupe, item_sources, load_caps, load_rules, score_candidates, source_label

LLM_WEIGHT = 0.6
NEUTRAL_MODEL_SCORE = 5.0  # used for items the model gave no usable verdict on, so scales stay comparable
DROPPED_KEEP = 200  # dropped items listed in issue.stats (for "why isn't X here?")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def default_key(now: datetime | None = None) -> str:
    return (now or _now()).strftime("%Y-%m-%d")


def candidates(db, user_id: int, since: datetime | None, limit: int = 1000) -> tuple[list[Candidate], dict[int, PoolItem]]:
    items = user_items(db, user_id, since=since, limit=limit)
    srcs = item_sources(db, [i.id for i in items])
    out = []
    for it in items:
        ss = srcs.get(it.id, [])
        out.append(Candidate(item_id=it.id, title=it.title or "", url=it.url, summary=it.summary or "",
                             text=item_text(db, it) if it.extraction_id else "", kind=it.kind, published_at=it.published_at,
                             extra=it.extra or {}, source_keys=[s.key for s in ss], source_kinds=[s.kind for s in ss],
                             canonical_url=it.canonical_url))
    return out, {i.id: i for i in items}


async def _model_layer(db, client: LLMClient, user_id: int, kept: list[Candidate], pool: dict[int, PoolItem],
                       report: dict) -> tuple[list[Candidate], list[Candidate]]:
    picks = get_picks(db, user_id)
    profs = all_profiles()
    items = [pool[c.item_id] for c in kept]
    for pick in picks:
        await _score_profile(db, client, user_id, profs[pick["name"]], items, report)
    thresholds = {p["name"]: p["threshold"] for p in picks}
    verdicts: dict[int, list[ItemScore]] = {}
    if thresholds and kept:
        for s in db.scalars(select(ItemScore).where(ItemScore.user_id == user_id, ItemScore.profile.in_(list(thresholds)),
                                                     ItemScore.item_id.in_([c.item_id for c in kept]), ItemScore.status == "ok")):
            verdicts.setdefault(s.item_id, []).append(s)
    out, dropped = [], []
    for c in kept:
        vs = verdicts.get(c.item_id)
        if not vs:  # no usable verdict (empty/error): rules decide, model part is neutral
            c.score = round((1 - LLM_WEIGHT) * c.rule_score + LLM_WEIGHT * NEUTRAL_MODEL_SCORE * 10, 2)
            out.append(c)
            continue
        passing = [s for s in vs if s.belongs and s.score is not None and s.score >= thresholds[s.profile]]
        if not passing:
            best = max(vs, key=lambda s: s.score or 0)
            c.reason = f"model-below-threshold:{best.profile}:{best.score}"
            dropped.append(c)
            continue
        best = max(passing, key=lambda s: s.score)
        c.llm = {"score": best.score, "profile": best.profile, "section": best.section, "reason": best.reason}
        c.score = round((1 - LLM_WEIGHT) * c.rule_score + LLM_WEIGHT * best.score * 10, 2)
        out.append(c)
    out.sort(key=lambda c: (-c.score, -c.item_id))
    report["model_passed"] = sum(1 for c in out if c.llm)
    report["model_dropped"] = len(dropped)
    return out, dropped


def _card(c: Candidate) -> dict:
    return {"id": c.item_id, "title": c.title, "summary": c.summary, "score": c.score,
            "section": (c.llm or {}).get("section")}


async def build_issue(db, user_id: int, key: str | None = None, *, client: LLMClient | None | str = "auto",
                      since: datetime | None = None, since_hours: float = 26, now: datetime | None = None,
                      ai_dedupe_on: bool = True, ai_rerank_on: bool = True, translate: bool = True) -> dict:
    now = now or _now()
    key = key or default_key(now)
    since = since or now - timedelta(hours=since_hours)
    own = False
    if client == "auto":
        client = llm_keys.client_for_user(db, user_id)
        own = client is not None
    report = {"user_id": user_id, "key": key, "ai": "on" if client else "skipped: no model key",
              "model": client.label if client else None, "scored": 0, "reused": 0, "empty": 0, "errors": 0}
    try:
        cands, pool = candidates(db, user_id, since)
        rules = load_rules(db, user_id)
        caps = load_caps(db, user_id)
        kept, dropped = score_candidates(cands, rules, now)
        kept, d = dedupe(kept)
        dropped += d
        if client is not None:
            kept, d = await _model_layer(db, client, user_id, kept, pool, report)
            dropped += d
            if ai_dedupe_on and not report.get("stopped") and len(kept) > 1:
                cards = await ai_dedupe(client, [_card(c) for c in kept], report)
                keep_ids = {x["id"] for x in cards}
                by_id = {c.item_id: c for c in kept}
                for x in cards:
                    for dup in x.get("duplicates", []):
                        by_id[x["id"]].also.append({"item_id": dup, "url": by_id[dup].url, "title": by_id[dup].title,
                                                    "by": "semantic"})
                        by_id[dup].reason = f"duplicate-of:{x['id']}:semantic"
                dropped += [c for c in kept if c.item_id not in keep_ids]
                kept = [c for c in kept if c.item_id in keep_ids]
        adjust_candidates(db, user_id, kept)
        selected, d = cut(kept, caps)
        dropped += d
        if client is not None and ai_rerank_on and not report.get("stopped") and len(selected) > 1:
            cards = await ai_rerank(client, [_card(c) for c in selected], report)
            by_id = {c.item_id: c for c in selected}
            selected = [by_id[x["id"]] for x in cards]
        tr = {}
        if translate:
            lang = llm_keys.translate_to(db, user_id)
            texts = [c.title for c in selected] + [c.summary[:400] for c in selected if c.summary]
            tr, stats = await translate_texts(db, None if client is None or report.get("stopped") else client, texts, lang)
            report["translation"] = {"lang": lang, **stats}
        issue = _store(db, user_id, key, selected, dropped, tr, report, caps, since, now, len(cands))
        report.update(issue_id=issue.id, items_in=len(cands), items_out=len(selected), stopped=bool(report.get("stopped")),
                      dropped=_reason_counts(dropped))
        if client is not None:
            u = client.usage
            report["usage"] = {"requests": u.requests, "retries": u.retries, "repairs": u.repairs,
                               "invalid_json": u.invalid_json, "errors": dict(u.errors),
                               "failovers": getattr(u, "failovers", 0), "chain": getattr(client, "labels", [client.label])}
        return report
    finally:
        if own:
            await client.aclose()


def build_issue_sync(db, user_id: int, key: str | None = None, **kw) -> dict:
    return asyncio.run(build_issue(db, user_id, key, **kw))


def _reason_counts(dropped: list[Candidate]) -> dict:
    out: dict[str, int] = {}
    for c in dropped:
        r = (c.reason or "?").split(":")[0]
        out[r] = out.get(r, 0) + 1
    return out


def _store(db, user_id, key, selected, dropped, tr, report, caps, since, now, n_in) -> Issue:
    issue = db.scalar(select(Issue).where(Issue.user_id == user_id, Issue.key == key))
    if issue is None:
        issue = Issue(user_id=user_id, key=key, created_at=now)
        db.add(issue); db.flush()
    else:
        db.execute(delete(IssueItem).where(IssueItem.issue_id == issue.id))
    issue.status, issue.ai, issue.model = "ok", report["ai"][:191], report["model"]
    issue.window_start, issue.window_end, issue.updated_at = since, now, now
    issue.stats = {
        "items_in": n_in, "items_out": len(selected), "dropped": _reason_counts(dropped), "caps": caps.public(),
        "dropped_items": [{"item_id": c.item_id, "title": c.title[:120], "reason": c.reason} for c in dropped[:DROPPED_KEEP]],
        "model": {k: report.get(k) for k in ("scored", "reused", "empty", "errors", "model_passed", "model_dropped",
                                             "dedupe", "rerank")},
    }
    for rank, c in enumerate(selected, 1):
        db.add(IssueItem(issue_id=issue.id, item_id=c.item_id, rank=rank, module=c.module, group_name=c.group,
                         score=c.score, rule_score=c.rule_score, llm_score=(c.llm or {}).get("score"),
                         profile=(c.llm or {}).get("profile"), section=(c.llm or {}).get("section"),
                         matched=[{k: h[k] for k in ("term", "group", "locations", "kind", "weight")} for h in c.matched],
                         categories=c.categories, also=c.also or None, title_translated=tr.get(c.title),
                         summary_translated=tr.get(c.summary[:400]) if c.summary else None))
    db.commit()
    return issue


# ------------------------------------------------------------------------------------------ read side

def _item_dict(ii: IssueItem, p: PoolItem, srcs: dict, marks: dict) -> dict:
    return {"rank": ii.rank, "item_id": p.id, "title": p.title, "title_translated": ii.title_translated, "url": p.url,
              "summary": p.summary, "summary_translated": ii.summary_translated, "image": p.image, "author": p.author,
              "published_at": p.published_at.isoformat() if p.published_at else None, "module": ii.module,
              "group": ii.group_name, "score": ii.score, "rule_score": ii.rule_score, "llm_score": ii.llm_score,
              "profile": ii.profile, "section": ii.section, "matched": [m["term"] for m in ii.matched or []],
              "categories": ii.categories or [], "also": ii.also or [], "sources": [s.name for s in srcs.get(p.id, [])],
              "source_keys": [s.key for s in srcs.get(p.id, [])], "state": marks.get(p.id),
              "source_cat": source_label(srcs[p.id][0].key, srcs[p.id][0].kind) if srcs.get(p.id) else "rss",
              "points": (p.extra or {}).get("points"), "comments": (p.extra or {}).get("comments")}


def issue_view(db, issue: Issue) -> dict:
    rows = db.execute(select(IssueItem, PoolItem).join(PoolItem, PoolItem.id == IssueItem.item_id)
                      .where(IssueItem.issue_id == issue.id).order_by(IssueItem.rank)).all()
    srcs = item_sources(db, [p.id for _, p in rows])
    marks = user_marks(db, issue.user_id)
    items = annotate(db, issue.user_id, [_item_dict(ii, p, srcs, marks) for ii, p in rows])
    return {"key": issue.key, "ai": issue.ai, "model": issue.model, "created_at": issue.created_at.isoformat(),
            "updated_at": issue.updated_at.isoformat(), "stats": {k: v for k, v in (issue.stats or {}).items() if k != "dropped_items"},
            "items": items}


def item_cards(db, user_id: int, item_ids: list[int]) -> list[dict]:
    """Cards for items outside one issue (收藏 / 点赞): each as it appeared in the user's latest issue that carried it,
    plus ``key`` (that issue). Order follows ``item_ids``; items never in an issue of this user are skipped."""
    if not item_ids:
        return []
    latest: dict[int, tuple[IssueItem, PoolItem, str]] = {}
    for ii, p, key in db.execute(select(IssueItem, PoolItem, Issue.key).join(Issue, Issue.id == IssueItem.issue_id)
                                 .join(PoolItem, PoolItem.id == IssueItem.item_id)
                                 .where(Issue.user_id == user_id, IssueItem.item_id.in_(item_ids)).order_by(Issue.key)):
        latest[p.id] = (ii, p, key)
    srcs = item_sources(db, list(latest))
    marks = user_marks(db, user_id)
    out = [_item_dict(ii, p, srcs, marks) | {"key": key} for ii, p, key in (latest[i] for i in item_ids if i in latest)]
    return annotate(db, user_id, out) if out else out


def search_items(db, user_id: int, q: str, module: str = "", limit: int = 60) -> dict:
    """Archive search over the user's own issues (titles, translations, summaries, source names), newest issue first.
    Every whitespace-separated word must match; trash / purged items are left out."""
    from sqlalchemy import func, or_
    from .models import ItemMark, PoolItemSource, Source
    words = [w for w in q.lower().split() if w][:8]
    stmt = (select(IssueItem, PoolItem, Issue.key).join(Issue, Issue.id == IssueItem.issue_id)
            .join(PoolItem, PoolItem.id == IssueItem.item_id).where(Issue.user_id == user_id))
    if module:
        stmt = stmt.where(IssueItem.module == module)
    hidden = select(ItemMark.item_id).where(ItemMark.user_id == user_id, ItemMark.state.in_(("trash", "purged")))
    stmt = stmt.where(IssueItem.item_id.not_in(hidden))
    src_names = (select(func.lower(func.coalesce(Source.name, Source.key))).join(PoolItemSource, PoolItemSource.source_id == Source.id)
                 .where(PoolItemSource.item_id == PoolItem.id).correlate(PoolItem))
    for w in words:
        pat = "%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(or_(func.lower(PoolItem.title).like(pat, escape="\\"), func.lower(PoolItem.summary).like(pat, escape="\\"),
                              func.lower(func.coalesce(IssueItem.title_translated, "")).like(pat, escape="\\"),
                              func.lower(func.coalesce(IssueItem.summary_translated, "")).like(pat, escape="\\"),
                              src_names.where(func.lower(func.coalesce(Source.name, Source.key)).like(pat, escape="\\")).exists()))
    seen, items = set(), []
    rows = db.execute(stmt.order_by(Issue.key.desc(), IssueItem.rank).limit(2000)).all()
    srcs = item_sources(db, list({p.id for _, p, _k in rows[:400]}))
    for ii, p, key in rows:
        if p.id in seen:
            continue
        seen.add(p.id)
        if len(items) < limit:
            s0 = (srcs.get(p.id) or [None])[0]
            items.append({"item_id": p.id, "key": key, "module": ii.module, "title": ii.title_translated or p.title,
                          "summary": (ii.summary_translated or p.summary or "")[:400],
                          "source": (s0.name or s0.key) if s0 else None})
    return {"total": len(seen), "items": items}


router = APIRouter(prefix="/api")


@router.get("/me/search")
def my_search(q: str = "", module: str = "", user=Depends(require_user), db=Depends(get_db)):
    if len(q) > 200 or len(module) > 32:
        raise HTTPException(400, "query too long")
    if not q.strip() and not module:
        return {"total": 0, "items": []}
    return search_items(db, user.id, q, module)


@router.get("/me/issues")
def my_issues(user=Depends(require_user), db=Depends(get_db)):
    rows = db.scalars(select(Issue).where(Issue.user_id == user.id).order_by(Issue.key.desc()).limit(100))
    return [{"key": i.key, "ai": i.ai, "items": (i.stats or {}).get("items_out", 0), "updated_at": i.updated_at.isoformat()}
            for i in rows]


@router.get("/me/issues/{key}")
def my_issue(key: str, explain: bool = False, user=Depends(require_user), db=Depends(get_db)):
    q = select(Issue).where(Issue.user_id == user.id)
    q = q.order_by(Issue.key.desc()).limit(1) if key == "latest" else q.where(Issue.key == key)
    issue = db.scalar(q)
    if issue is None:
        raise HTTPException(404, "no such issue")
    view = issue_view(db, issue)
    if explain:
        view["dropped_items"] = (issue.stats or {}).get("dropped_items", [])
    return view


class BuildIn(BaseModel):
    key: str | None = Field(default=None, max_length=64, pattern=r"^[\w:.\-]+$")
    since_hours: float = Field(default=26, gt=0, le=24 * 14)
    ai: bool = True


@router.post("/me/issues/build")
async def build_my_issue(body: BuildIn | None = None, user=Depends(require_user), db=Depends(get_db)):
    body = body or BuildIn()
    rep = await build_issue(db, user.id, body.key, client="auto" if body.ai else None, since_hours=body.since_hours)
    return {k: v for k, v in rep.items() if k != "usage"} | {"usage": rep.get("usage")}


def build_all(db, key: str | None = None, **kw) -> list[dict]:
    """Build the issue for every active user (used by the scheduler / CLI --all). One failure never stops the rest."""
    out = []
    for u in db.scalars(select(User).where(User.active.is_(True)).order_by(User.id)):
        try:
            rep = build_issue_sync(db, u.id, key, **kw)
            out.append({"user": u.username, "items_out": rep["items_out"], "ai": rep["ai"]})
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            out.append({"user": u.username, "error": f"{type(exc).__name__}: {exc}"[:300]})
    return out
