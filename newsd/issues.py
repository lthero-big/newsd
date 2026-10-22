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
from .content import safe_media
from .llm import keys as llm_keys
from .llm.client import LLMClient
from .llm.process import _dedupe as ai_dedupe, _rerank as ai_rerank, _score_profile
from .llm.profiles import all_profiles, get_picks
from .llm.translate import translate_texts
from .feedback import adjust_candidates, annotate
from .zones import SPECIAL, apply_to_candidates as apply_zones, special_keys
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


USAGE_FIELDS = ("requests", "retries", "repairs", "invalid_json", "failovers", "input_tokens", "output_tokens")
STAGES = {"queued": "排队中", "collect": "整理候选", "score": "模型打分", "dedupe": "语义去重", "select": "筛选排版",
          "rerank": "AI 重排", "translate": "翻译", "save": "保存", "done": "已完成", "failed": "失败"}


def _usage_snapshot(client) -> dict:
    u = client.usage
    return {f: int(getattr(u, f, 0) or 0) for f in USAGE_FIELDS}


def usage_delta(client, before: dict | None) -> dict:
    """Usage of ``client`` since ``before`` (a shared client may already have served other users)."""
    now_ = _usage_snapshot(client)
    before = before or {}
    out = {f: now_[f] - before.get(f, 0) for f in USAGE_FIELDS}
    out["errors"] = dict(client.usage.errors)
    out["chain"] = getattr(client, "labels", [client.label])
    return out


def _log_usage(db, user_id: int, key: str, trigger: str, client, usage: dict, seconds: float, items_out: int | None,
               status: str = "ok", error: str | None = None) -> None:
    """One 模型用量 log row per build that used a model (the 模型 page lists the latest ones)."""
    from . import eventlog
    user = db.get(User, user_id)
    tin, tout = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    eventlog.add(db, "INFO" if status == "ok" else "WARN", "llm",
                 f"模型用量 · 日报 {key} · {fmt_tokens(tin)} 入 / {fmt_tokens(tout)} 出 · {usage.get('requests', 0)} 次请求",
                 source="usage", user=user,
                 detail={"key": key, "trigger": trigger, "status": status, "model": client.label,
                         "chain": ", ".join(usage.get("chain") or []), "requests": usage.get("requests", 0),
                         "input_tokens": tin, "output_tokens": tout, "retries": usage.get("retries", 0),
                         "failovers": usage.get("failovers", 0), "seconds": round(seconds, 1), "items_out": items_out,
                         "error": (error or "")[:300] or None})


def fmt_tokens(n: int) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


async def build_issue(db, user_id: int, key: str | None = None, *, client: LLMClient | None | str = "auto",
                      since: datetime | None = None, since_hours: float = 26, now: datetime | None = None,
                      ai_dedupe_on: bool = True, ai_rerank_on: bool = True, translate: bool = True,
                      trigger: str = "api", progress=None) -> dict:
    """``progress(stage, client)`` is called as the build moves through STAGES (for the live status line)."""
    def step(stage):
        if progress is not None:
            progress(stage, client)
    t_start = _now()
    now = now or t_start
    key = key or default_key(now)
    since = since or now - timedelta(hours=since_hours)
    own = False
    if client == "auto":
        client = llm_keys.client_for_user(db, user_id)
        own = client is not None
    report = {"user_id": user_id, "key": key, "ai": "on" if client else "skipped: no model key",
              "model": client.label if client else None, "scored": 0, "reused": 0, "empty": 0, "errors": 0}
    before = _usage_snapshot(client) if client is not None else None
    try:
        step("collect")
        cands, pool = candidates(db, user_id, since)
        rules = load_rules(db, user_id)
        caps = load_caps(db, user_id)
        kept, dropped = score_candidates(cands, rules, now)
        kept, d = dedupe(kept)
        dropped += d
        if client is not None:
            step("score")
            kept, d = await _model_layer(db, client, user_id, kept, pool, report)
            dropped += d
            if ai_dedupe_on and not report.get("stopped") and len(kept) > 1:
                step("dedupe")
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
        step("select")
        apply_zones(db, user_id, kept)
        adjust_candidates(db, user_id, kept)
        selected, d = cut(kept, caps)
        dropped += d
        if client is not None and ai_rerank_on and not report.get("stopped") and len(selected) > 1:
            step("rerank")
            cards = await ai_rerank(client, [_card(c) for c in selected], report)
            by_id = {c.item_id: c for c in selected}
            selected = [by_id[x["id"]] for x in cards]
        tr = {}
        if translate:
            step("translate")
            lang = llm_keys.translate_to(db, user_id)
            texts = [c.title for c in selected] + [c.summary[:400] for c in selected if c.summary]
            tr, stats = await translate_texts(db, None if client is None or report.get("stopped") else client, texts, lang)
            report["translation"] = {"lang": lang, **stats}
        step("save")
        if client is not None:
            report["usage"] = usage_delta(client, before)
            _log_usage(db, user_id, key, trigger, client, report["usage"], (_now() - t_start).total_seconds(), len(selected))
        issue = _store(db, user_id, key, selected, dropped, tr, report, caps, since, now, len(cands))
        report.update(issue_id=issue.id, items_in=len(cands), items_out=len(selected), stopped=bool(report.get("stopped")),
                      dropped=_reason_counts(dropped))
        return report
    except Exception as exc:
        if client is not None:
            try:
                db.rollback()
                _log_usage(db, user_id, key, trigger, client, usage_delta(client, before),
                           (_now() - t_start).total_seconds(), None, status="failed", error=f"{type(exc).__name__}: {exc}")
                db.commit()
            except Exception:  # noqa: BLE001  (the usage log must never hide the real error)
                db.rollback()
        raise
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

def _item_dict(ii: IssueItem, p: PoolItem, srcs: dict, marks: dict, special: set | None = None) -> dict:
    keys = [s.key for s in srcs.get(p.id, [])]
    return {"source_section": SPECIAL if special and special & set(keys) else None,
            "rank": ii.rank, "item_id": p.id, "title": p.title, "title_translated": ii.title_translated, "url": p.url,
              "summary": p.summary, "summary_translated": ii.summary_translated, "image": p.image, "author": p.author,
              "published_at": p.published_at.isoformat() if p.published_at else None, "module": ii.module,
              "group": ii.group_name, "score": ii.score, "rule_score": ii.rule_score, "llm_score": ii.llm_score,
              "profile": ii.profile, "section": ii.section, "matched": [m["term"] for m in ii.matched or []],
              "categories": ii.categories or [], "also": ii.also or [], "sources": [s.name for s in srcs.get(p.id, [])],
              "source_keys": [s.key for s in srcs.get(p.id, [])], "state": marks.get(p.id),
              "source_cat": source_label(srcs[p.id][0].key, srcs[p.id][0].kind) if srcs.get(p.id) else "rss",
              "media": safe_media((p.extra or {}).get("media"), p.image, (p.extra or {}).get("enclosure"), p.url),
              "points": (p.extra or {}).get("points"), "comments": (p.extra or {}).get("comments")}


def issue_view(db, issue: Issue) -> dict:
    rows = db.execute(select(IssueItem, PoolItem).join(PoolItem, PoolItem.id == IssueItem.item_id)
                      .where(IssueItem.issue_id == issue.id).order_by(IssueItem.rank)).all()
    srcs = item_sources(db, [p.id for _, p in rows])
    marks = user_marks(db, issue.user_id)
    sp = special_keys(db, issue.user_id)
    items = annotate(db, issue.user_id, [_item_dict(ii, p, srcs, marks, sp) for ii, p in rows])
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
    sp = special_keys(db, user_id)
    out = [_item_dict(ii, p, srcs, marks, sp) | {"key": key} for ii, p, key in (latest[i] for i in item_ids if i in latest)]
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
    background: bool = False  # start it and return at once; poll GET /api/me/issues/build/status


# Live state of the latest manual build per user (this web process only; the scheduler logs to 日志与任务).
_builds: dict[int, dict] = {}
_tasks: set = set()


def _iso(dt: datetime | None) -> str | None:
    return dt.replace(tzinfo=timezone.utc).isoformat() if dt else None


def build_status(user_id: int) -> dict:
    j = _builds.get(user_id)
    if not j:
        return {"state": "idle"}
    out = {k: v for k, v in j.items() if not k.startswith("_")}
    cl = j.get("_client")
    if cl is not None and j["state"] == "running":
        out["usage"] = {k: v for k, v in usage_delta(cl, j.get("_before")).items() if k in USAGE_FIELDS}
    end = j.get("_finished") or _now()
    out["elapsed"] = round((end - j["_started"]).total_seconds(), 1)
    out["stage_label"] = STAGES.get(out.get("stage"), out.get("stage"))
    return out


async def _run_build(user_id: int, body: BuildIn) -> None:
    from .db import SessionLocal
    j = _builds[user_id]

    def progress(stage, client):
        j["stage"] = stage
        if client is not None and j.get("_client") is not client:
            j["_client"], j["_before"] = client, _usage_snapshot(client)
            j["model"] = client.label
    db = SessionLocal()
    try:
        rep = await build_issue(db, user_id, body.key, client="auto" if body.ai else None, since_hours=body.since_hours,
                                trigger="manual", progress=progress)
        j.update(state="done", stage="done", key=rep["key"], items_in=rep.get("items_in"), items_out=rep.get("items_out"),
                 ai=rep.get("ai"), usage={k: v for k, v in (rep.get("usage") or {}).items() if k in USAGE_FIELDS} or None)
    except Exception as exc:  # noqa: BLE001
        if j.get("_client") is not None:
            j["usage"] = {k: v for k, v in usage_delta(j["_client"], j.get("_before")).items() if k in USAGE_FIELDS}
        j.update(state="failed", stage="failed", error=f"{type(exc).__name__}: {exc}"[:300])
    finally:
        j["_finished"] = _now()
        j["finished_at"] = _iso(j["_finished"])
        j.pop("_client", None)
        db.close()


@router.get("/me/issues/build/status")
def build_my_issue_status(user=Depends(require_user)):
    return build_status(user.id)


@router.post("/me/issues/build")
async def build_my_issue(body: BuildIn | None = None, user=Depends(require_user), db=Depends(get_db)):
    body = body or BuildIn()
    if body.background:
        cur = _builds.get(user.id)
        if cur and cur["state"] == "running":
            return {"started": False, **build_status(user.id)}
        started = _now()
        _builds[user.id] = {"state": "running", "stage": "queued", "_started": started, "started_at": _iso(started)}
        task = asyncio.create_task(_run_build(user.id, body))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return {"started": True, **build_status(user.id)}
    rep = await build_issue(db, user.id, body.key, client="auto" if body.ai else None, since_hours=body.since_hours,
                            trigger="manual")
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
