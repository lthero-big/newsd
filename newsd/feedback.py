"""Reading feedback and what newsd learns from it (DEPTAI-216 item 3, per the accepted v2 demo).

* ♥ like (vote 1) = more like this; 👎 dislike (vote -1) = fewer like this. Neither hides the card.
* 🗑 delete = the item goes to the trash (``item_marks`` state ``trash``) and is a *strong* signal.
  Emptying the trash or "delete forever" keeps the row as ``purged`` (still a signal, no longer listed);
  restoring an item removes the row, so the signal disappears. Trash older than 30 days is purged.
* Learned down-ranking: every signal is split into features of the item -- source (x1), first matched
  keyword (x0.8), topic / tag group (x0.3) -- with weight 1.0 for a deletion and 0.4 for a dislike,
  halving every 30 days. Features scoring >= 0.3 are "learned"; an item whose features add up to
  >= 0.8 is ranked down: it goes to the end of its module and is labelled, never hidden. Items from a
  special-watch section are exempt. Likes add a small boost when an issue is built.
* Settings -> learned preferences lists the features; each (or all) can be forgotten, which ignores
  the signals given so far for that feature. Learning can be switched off.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update

from .auth import get_db, require_user
from .collector import collect as core
from .marks import in_users_issues
from .models import Issue, IssueItem, ItemFeedback, ItemMark, PoolItem, UserSetting
from .scoring import item_sources

router = APIRouter(prefix="/api")
W_DELETE, W_DISLIKE, W_LIKE = 1.0, 0.4, 0.5
FEATURE_W = {"src": 1.0, "kw": 0.8, "tag": 0.3}
FEATURE_NAMES = {"src": "来源", "kw": "关键词", "tag": "话题"}
HALF_LIFE_DAYS = 30
LEARNED_MIN = 0.3
RANK_DOWN_AT = 0.8
TRASH_DAYS = 30
SPECIAL = "特别关注"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ------------------------------------------------------------------------------------------- settings

def get_setting(db, user_id: int, key: str):
    row = db.scalar(select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.key == key))
    return row.value if row is not None else None


def put_setting(db, user_id: int, key: str, value) -> None:
    row = db.scalar(select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.key == key))
    if row is None:
        row = UserSetting(user_id=user_id, key=key)
        db.add(row)
    row.value = value
    db.commit()


def learn_settings(db, user_id: int) -> dict:
    v = get_setting(db, user_id, "learn")
    v = v if isinstance(v, dict) else {}
    return {"enabled": v.get("enabled", True), "forgot": dict(v.get("forgot") or {})}


def _save_learn(db, user_id: int, v: dict) -> None:
    put_setting(db, user_id, "learn", {"enabled": bool(v["enabled"]), "forgot": v["forgot"]})


# ------------------------------------------------------------------------------------------- features

def features_from(source_key: str | None, source_name: str | None, matched: list | None) -> list[tuple[str, str, str]]:
    """[(kind, key, label)] -- key is the stable identity, label what the user sees."""
    out = []
    if source_key:
        out.append(("src", source_key, source_name or source_key))
    m = (matched or [None])[0]
    if isinstance(m, dict) and m.get("term"):
        out.append(("kw", core.fold(m["term"]), m["term"]))
        if m.get("group"):
            out.append(("tag", core.fold(m["group"]), m["group"]))
    return out


def item_features(db, user_id: int, item_ids: list[int]) -> dict[int, list[tuple[str, str, str]]]:
    """Features of items as they appeared in the user's latest issue that carried them."""
    if not item_ids:
        return {}
    latest: dict[int, IssueItem] = {}
    for ii, _key in db.execute(select(IssueItem, Issue.key).join(Issue, Issue.id == IssueItem.issue_id)
                               .where(Issue.user_id == user_id, IssueItem.item_id.in_(item_ids)).order_by(Issue.key)):
        latest[ii.item_id] = ii
    srcs = item_sources(db, item_ids)
    out = {}
    for iid in item_ids:
        s = (srcs.get(iid) or [None])[0]
        ii = latest.get(iid)
        out[iid] = features_from(s.key if s else None, (s.name or s.key) if s else None, ii.matched if ii else None)
    return out


# ------------------------------------------------------------------------------------------- learning

def _decay(at: datetime, now: datetime) -> float:
    age = max(0.0, (now - at).total_seconds() / 86400)
    return 0.5 ** (age / HALF_LIFE_DAYS)


def _signals(db, user_id: int) -> list[tuple[int, float, str, datetime]]:
    """(item_id, weight, why, at) for deletions, dislikes and likes."""
    out = []
    for m in db.scalars(select(ItemMark).where(ItemMark.user_id == user_id, ItemMark.state.in_(("trash", "purged")))):
        out.append((m.item_id, W_DELETE, "del", m.created_at))
    for f in db.scalars(select(ItemFeedback).where(ItemFeedback.user_id == user_id, ItemFeedback.vote != 0)):
        out.append((f.item_id, W_DISLIKE if f.vote < 0 else -W_LIKE, "down" if f.vote < 0 else "like", f.updated_at))
    return out


def learned(db, user_id: int, now: datetime | None = None, *, include_forgotten: bool = False) -> list[dict]:
    """Learned negative features, strongest first."""
    now = now or _now()
    st = learn_settings(db, user_id)
    sig = [s for s in _signals(db, user_id) if s[2] != "like"]
    feats = item_features(db, user_id, sorted({s[0] for s in sig}))
    titles = dict(db.execute(select(PoolItem.id, PoolItem.title).where(PoolItem.id.in_(list(feats) or [0]))).all()) if feats else {}
    m: dict[str, dict] = {}
    for iid, w, why, at in sig:
        for kind, key, label in feats.get(iid, []):
            k = f"{kind}:{key}"
            forgot = st["forgot"].get(k)
            if forgot and not include_forgotten and at <= datetime.fromisoformat(forgot):
                continue
            e = m.setdefault(k, {"key": k, "kind": kind, "kind_name": FEATURE_NAMES[kind], "value": label, "score": 0.0,
                                 "deletes": 0, "dislikes": 0, "examples": []})
            e["score"] += w * FEATURE_W[kind] * _decay(at, now)
            e["deletes" if why == "del" else "dislikes"] += 1
            if len(e["examples"]) < 3 and titles.get(iid):
                e["examples"].append(titles[iid][:80])
    out = [dict(e, score=round(e["score"], 3)) for e in m.values() if e["score"] >= LEARNED_MIN]
    return sorted(out, key=lambda e: -e["score"])


def penalty_map(db, user_id: int, now: datetime | None = None) -> dict[str, float]:
    if not learn_settings(db, user_id)["enabled"]:
        return {}
    return {e["key"]: e["score"] for e in learned(db, user_id, now)}


def boost_map(db, user_id: int, now: datetime | None = None) -> dict[str, float]:
    """Positive weights from likes (same feature split, not shown as "learned")."""
    if not learn_settings(db, user_id)["enabled"]:
        return {}
    now = now or _now()
    likes = [s for s in _signals(db, user_id) if s[2] == "like"]
    feats = item_features(db, user_id, sorted({s[0] for s in likes}))
    out: dict[str, float] = {}
    for iid, w, _why, at in likes:
        for kind, key, _label in feats.get(iid, []):
            out[f"{kind}:{key}"] = out.get(f"{kind}:{key}", 0.0) + (-w) * FEATURE_W[kind] * _decay(at, now)
    return out


def penalty(feats: list[tuple[str, str, str]], pmap: dict[str, float], section: str | None = None) -> tuple[float, list[str]]:
    if section == SPECIAL or not pmap:
        return 0.0, []
    p, why = 0.0, []
    for kind, key, label in feats:
        s = pmap.get(f"{kind}:{key}")
        if s:
            p += s
            why.append(f"{FEATURE_NAMES[kind]}「{label}」")
    return round(p, 3), why


def adjust_candidates(db, user_id: int, cands) -> None:
    """Build-time: learned down-ranking and like boosts shift candidate scores before the quota cut."""
    pmap, bmap = penalty_map(db, user_id), boost_map(db, user_id)
    if not pmap and not bmap:
        return
    names = {s.key: (s.name or s.key) for ss in item_sources(db, [c.item_id for c in cands]).values() for s in ss}
    for c in cands:
        k0 = c.source_keys[0] if c.source_keys else None
        feats = features_from(k0, names.get(k0), c.matched)
        p, _ = penalty(feats, pmap)
        b = sum(bmap.get(f"{kind}:{key}", 0.0) for kind, key, _ in feats)
        if p or b:
            c.score = round(max(0.0, c.score - 12.0 * min(p, 3.0) + 6.0 * min(b, 2.0)), 2)
            c.detail = {**(c.detail or {}), "learned_penalty": p, "like_boost": round(b, 3)}


def annotate(db, user_id: int, items: list[dict]) -> list[dict]:
    """Read side: add vote / folded / ranked_down to issue items (dicts with item_id) and move ranked-down
    items to the end of their module, keeping order otherwise."""
    ids = [i["item_id"] for i in items]
    fb = {f.item_id: f for f in db.scalars(select(ItemFeedback).where(ItemFeedback.user_id == user_id,
                                                                       ItemFeedback.item_id.in_(ids or [0])))}
    pmap = penalty_map(db, user_id)
    feats = item_features(db, user_id, ids)
    for i in items:
        f = fb.get(i["item_id"])
        i["vote"] = f.vote if f else 0
        i["folded"] = bool(f and f.folded)
        p, why = penalty(feats.get(i["item_id"], []), pmap, i.get("source_section"))
        i["penalty"], i["ranked_down"], i["ranked_down_why"] = p, p >= RANK_DOWN_AT, why
        i["what"] = "、".join(f"{FEATURE_NAMES[k]}「{label}」" for k, _key, label in feats.get(i["item_id"], []))
    order = {id(i): n for n, i in enumerate(items)}
    return sorted(items, key=lambda i: (i["module"], i["ranked_down"], order[id(i)])) if pmap else items


# ------------------------------------------------------------------------------------------- trash

def auto_purge(db, user_id: int, now: datetime | None = None) -> int:
    cutoff = (now or _now()) - timedelta(days=TRASH_DAYS)
    n = db.execute(update(ItemMark).where(ItemMark.user_id == user_id, ItemMark.state == "trash",
                                          ItemMark.created_at < cutoff).values(state="purged")).rowcount
    if n:
        db.commit()
    return n


def trash_items(db, user_id: int) -> list[dict]:
    from .marks import marked_items
    auto_purge(db, user_id)
    rows = marked_items(db, user_id, "trash")
    feats = item_features(db, user_id, [r["item_id"] for r in rows])
    for r in rows:
        r["features"] = [f"{FEATURE_NAMES[k]}「{label}」" for k, _key, label in feats.get(r["item_id"], [])]
        r["expires_at"] = (datetime.fromisoformat(r["marked_at"]) + timedelta(days=TRASH_DAYS)).isoformat()
    return rows


def _feedback_row(db, user_id: int, item_id: int) -> ItemFeedback:
    row = db.scalar(select(ItemFeedback).where(ItemFeedback.user_id == user_id, ItemFeedback.item_id == item_id))
    if row is None:
        if not in_users_issues(db, user_id, item_id):
            raise LookupError(item_id)
        row = ItemFeedback(user_id=user_id, item_id=item_id, vote=0, folded=False)
        db.add(row)
    return row


# ------------------------------------------------------------------------------------------- API

class VoteIn(BaseModel):
    vote: int = Field(ge=-1, le=1)


class FoldIn(BaseModel):
    folded: bool


@router.put("/me/items/{item_id}/vote")
def put_vote(item_id: int, body: VoteIn, user=Depends(require_user), db=Depends(get_db)):
    try:
        row = _feedback_row(db, user.id, item_id)
    except LookupError:
        raise HTTPException(404, "no such item")
    row.vote, row.updated_at = body.vote, _now()
    db.commit()
    return {"item_id": item_id, "vote": row.vote, "folded": row.folded}


@router.put("/me/items/{item_id}/fold")
def put_fold(item_id: int, body: FoldIn, user=Depends(require_user), db=Depends(get_db)):
    try:
        row = _feedback_row(db, user.id, item_id)
    except LookupError:
        raise HTTPException(404, "no such item")
    row.folded = body.folded
    db.commit()
    return {"item_id": item_id, "vote": row.vote, "folded": row.folded}


@router.get("/me/trash")
def get_trash(user=Depends(require_user), db=Depends(get_db)):
    return trash_items(db, user.id)


class TrashIds(BaseModel):
    ids: list[int] | None = Field(default=None, max_length=1000)  # None = every item in the trash
    forget: bool = False  # purge: also drop the learning signal of these deletions


def _trash_rows(db, user_id: int, ids: list[int] | None):
    q = select(ItemMark).where(ItemMark.user_id == user_id, ItemMark.state == "trash")
    if ids is not None:
        q = q.where(ItemMark.item_id.in_(ids or [0]))
    return list(db.scalars(q))


@router.post("/me/trash/restore")
def restore(body: TrashIds, user=Depends(require_user), db=Depends(get_db)):
    """Put items back (signal removed). Returns the restored ids so the UI can undo by re-deleting them."""
    rows = _trash_rows(db, user.id, body.ids)
    ids = [r.item_id for r in rows]
    for r in rows:
        db.delete(r)
    db.commit()
    return {"restored": ids}


@router.post("/me/trash/purge")
def purge(body: TrashIds, user=Depends(require_user), db=Depends(get_db)):
    """Delete forever: gone from the trash; the deletion keeps down-ranking unless ``forget``."""
    rows = _trash_rows(db, user.id, body.ids)
    for r in rows:
        if body.forget:
            db.delete(r)
        else:
            r.state = "purged"
    db.commit()
    return {"purged": len(rows), "forgot": body.forget}


@router.post("/me/trash/redelete")
def redelete(body: TrashIds, user=Depends(require_user), db=Depends(get_db)):
    """Undo for "restore all": put these items back into the trash."""
    n = 0
    for iid in body.ids or []:
        if in_users_issues(db, user.id, iid):
            row = db.scalar(select(ItemMark).where(ItemMark.user_id == user.id, ItemMark.item_id == iid))
            if row is None:
                db.add(ItemMark(user_id=user.id, item_id=iid, state="trash", created_at=_now()))
            else:
                row.state, row.created_at = "trash", _now()
            n += 1
    db.commit()
    return {"trashed": n}


@router.get("/me/learned")
def get_learned(user=Depends(require_user), db=Depends(get_db)):
    st = learn_settings(db, user.id)
    return {"enabled": st["enabled"], "items": _learned_view(db, user.id), "rank_down_at": RANK_DOWN_AT,
            "weights": {"delete": W_DELETE, "dislike": W_DISLIKE, **{f"feature_{k}": v for k, v in FEATURE_W.items()}},
            "half_life_days": HALF_LIFE_DAYS}


def _learned_view(db, user_id: int) -> list[dict]:
    """Active entries plus the ones the user undid (``forgotten``), so the settings page can offer 「恢复降权」."""
    active = {e["key"] for e in learned(db, user_id)}
    forgot = learn_settings(db, user_id)["forgot"]
    return [dict(e, forgotten=e["key"] not in active and e["key"] in forgot)
            for e in learned(db, user_id, include_forgotten=True)]


class ForgetIn(BaseModel):
    keys: list[str] | None = Field(default=None, max_length=500)  # None = everything learned so far


@router.post("/me/learned/forget")
def forget(body: ForgetIn, user=Depends(require_user), db=Depends(get_db)):
    st = learn_settings(db, user.id)
    keys = body.keys if body.keys is not None else [e["key"] for e in learned(db, user.id)]
    stamp = _now().isoformat()
    for k in keys:
        if ":" in k and k.split(":", 1)[0] in FEATURE_W:
            st["forgot"][k[:300]] = stamp
    _save_learn(db, user.id, st)
    return {"forgot": len(keys), "items": _learned_view(db, user.id)}


@router.post("/me/learned/unforget")
def unforget(body: ForgetIn, user=Depends(require_user), db=Depends(get_db)):
    """Undo a 「撤销」: the feature counts again (all of its signals, including those before the undo)."""
    st = learn_settings(db, user.id)
    keys = body.keys if body.keys is not None else list(st["forgot"])
    n = sum(1 for k in keys if st["forgot"].pop(k, None) is not None)
    _save_learn(db, user.id, st)
    return {"restored": n, "items": _learned_view(db, user.id)}


class LearnIn(BaseModel):
    enabled: bool


@router.put("/me/learned")
def set_learning(body: LearnIn, user=Depends(require_user), db=Depends(get_db)):
    st = learn_settings(db, user.id)
    st["enabled"] = body.enabled
    _save_learn(db, user.id, st)
    return {"enabled": body.enabled}


def clear_signals(db, user_id: int) -> None:  # used by account reset helpers / tests
    db.execute(delete(ItemFeedback).where(ItemFeedback.user_id == user_id))
    db.commit()
