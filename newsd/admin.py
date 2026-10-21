"""M3 admin backend API: users, invites, registration mode, source catalog (with quality stats), run records.

Source quality (last ``days`` days, default 30), a reference for deciding which sources to keep:
  collected  - distinct pool items the source carried
  selected   - distinct items from that source that made it into at least one user's issue
  pass_rate  - selected / collected
  avg_score  - mean final score of the issue entries of those items (all users)
"""
from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import distinct, func, select

from .auth import get_db, require_admin
from .models import (Invite, Issue, IssueItem, PoolItemSource, Source, SourceFetch, Subscription, User, now)
from .registration import REG_MODES, get_registration_mode, set_registration_mode

router = APIRouter(prefix="/api/admin")


def source_quality(db, days: int = 30) -> dict[int, dict]:
    since = now() - timedelta(days=days)
    collected = dict(db.execute(select(PoolItemSource.source_id, func.count(distinct(PoolItemSource.item_id)))
                                .where(PoolItemSource.fetched_at >= since).group_by(PoolItemSource.source_id)).all())
    picked = {sid: (n, avg) for sid, n, avg in db.execute(
        select(PoolItemSource.source_id, func.count(distinct(IssueItem.item_id)), func.avg(IssueItem.score))
        .join(IssueItem, IssueItem.item_id == PoolItemSource.item_id)
        .join(Issue, Issue.id == IssueItem.issue_id)
        .where(Issue.created_at >= since, PoolItemSource.fetched_at >= since).group_by(PoolItemSource.source_id))}
    out = {}
    for sid in set(collected) | set(picked):
        c = collected.get(sid, 0)
        n, avg = picked.get(sid, (0, None))
        out[sid] = {"collected": c, "selected": n, "pass_rate": round(min(n / c, 1.0), 3) if c else None,
                    "avg_score": round(float(avg), 1) if avg is not None else None}
    return out


def source_rows(db, days: int = 30) -> list[dict]:
    q = source_quality(db, days)
    subs = dict(db.execute(select(Subscription.source_id, func.count()).where(Subscription.enabled.is_(True))
                           .group_by(Subscription.source_id)).all())
    last: dict[int, SourceFetch] = {}
    for f in db.scalars(select(SourceFetch).order_by(SourceFetch.id)):
        last[f.source_id] = f
    out = []
    for s in db.scalars(select(Source).order_by(Source.kind, Source.key)):
        lf = last.get(s.id)
        out.append({"key": s.key, "kind": s.kind, "name": s.name, "url": s.url, "enabled": s.enabled,
                    "content_extractor": s.content_extractor, "subscribers": subs.get(s.id, 0),
                    "last_fetch": {"cycle": lf.cycle, "status": lf.status, "entries": lf.entries,
                                   "at": (lf.finished_at or lf.started_at).isoformat()} if lf else None,
                    **q.get(s.id, {"collected": 0, "selected": 0, "pass_rate": None, "avg_score": None})})
    return out


@router.get("/sources")
def admin_sources(days: int = 30, _=Depends(require_admin), db=Depends(get_db)):
    return source_rows(db, max(1, min(days, 365)))


class SourcePatch(BaseModel):
    enabled: bool | None = None
    content_extractor: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=191)


@router.patch("/sources/{key:path}")
def patch_source(key: str, body: SourcePatch, _=Depends(require_admin), db=Depends(get_db)):
    s = db.scalar(select(Source).where(Source.key == key))
    if s is None:
        raise HTTPException(404, "no such source")
    for f in ("enabled", "content_extractor", "name"):
        v = getattr(body, f)
        if v is not None:
            setattr(s, f, v)
    db.commit()
    return {"key": s.key, "enabled": s.enabled, "content_extractor": s.content_extractor, "name": s.name}


class SourceIn(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][\w.\-]{0,62}$")
    name: str = Field(min_length=1, max_length=191)
    url: str = Field(max_length=2000)


@router.post("/sources")
def add_source(body: SourceIn, _=Depends(require_admin), db=Depends(get_db)):
    """Add an RSS/Atom feed to the catalog (other kinds come from config/collect-sources.json)."""
    if not re.match(r"^https?://[^\s/]+", body.url):
        raise HTTPException(400, "url must be http(s)")
    key = f"rss:{body.id}"
    if db.scalar(select(Source.id).where(Source.key == key)):
        raise HTTPException(409, "source exists")
    db.add(Source(key=key, kind="rss", name=body.name, url=body.url, config={}))
    db.commit()
    return {"key": key}


@router.get("/users")
def admin_users(_=Depends(require_admin), db=Depends(get_db)):
    subs = dict(db.execute(select(Subscription.user_id, func.count()).group_by(Subscription.user_id)).all())
    issues = dict(db.execute(select(Issue.user_id, func.count()).group_by(Issue.user_id)).all())
    last = dict(db.execute(select(Issue.user_id, func.max(Issue.updated_at)).group_by(Issue.user_id)).all())
    return [{"id": u.id, "username": u.username, "role": u.role, "active": u.active,
             "created_at": u.created_at.isoformat(), "subscriptions": subs.get(u.id, 0), "issues": issues.get(u.id, 0),
             "last_issue_at": last[u.id].isoformat() if last.get(u.id) else None}
            for u in db.scalars(select(User).order_by(User.id))]


@router.get("/invites")
def admin_invites(_=Depends(require_admin), db=Depends(get_db)):
    """Codes are stored hashed, so only status is listed; the plain code is shown once when it is created."""
    names = {u.id: u.username for u in db.scalars(select(User))}
    out = []
    for i in db.scalars(select(Invite).order_by(Invite.id.desc()).limit(200)):
        status = "used" if i.used_by else "expired" if i.expires_at and i.expires_at < now() else "unused"
        out.append({"id": i.id, "status": status, "created_by": names.get(i.created_by), "used_by": names.get(i.used_by),
                    "created_at": i.created_at.isoformat(), "expires_at": i.expires_at.isoformat() if i.expires_at else None})
    return out


@router.delete("/invites/{iid}")
def delete_invite(iid: int, _=Depends(require_admin), db=Depends(get_db)):
    i = db.get(Invite, iid)
    if i is None:
        raise HTTPException(404, "no such invite")
    if i.used_by:
        raise HTTPException(409, "invite already used")
    db.delete(i); db.commit()
    return {"ok": True}


@router.get("/registration")
def admin_registration_get(_=Depends(require_admin), db=Depends(get_db)):
    return {"mode": get_registration_mode(db), "modes": list(REG_MODES)}


@router.get("/runs")
def admin_runs(limit: int = 50, _=Depends(require_admin), db=Depends(get_db)):
    """Recent run records: source fetches (shared pool) and per-user issue builds."""
    limit = max(1, min(limit, 200))
    fetches = db.execute(select(SourceFetch, Source.key).join(Source, Source.id == SourceFetch.source_id)
                         .order_by(SourceFetch.id.desc()).limit(limit)).all()
    issues = db.execute(select(Issue, User.username).join(User, User.id == Issue.user_id)
                        .order_by(Issue.updated_at.desc(), Issue.id.desc()).limit(limit)).all()
    return {
        "fetches": [{"source": k, "cycle": f.cycle, "status": f.status, "entries": f.entries, "new_items": f.new_items,
                     "started_at": f.started_at.isoformat(),
                     "finished_at": f.finished_at.isoformat() if f.finished_at else None} for f, k in fetches],
        "issues": [{"user": name, "key": i.key, "status": i.status, "ai": i.ai, "model": i.model,
                    "items_in": (i.stats or {}).get("items_in"), "items_out": (i.stats or {}).get("items_out"),
                    "updated_at": i.updated_at.isoformat()} for i, name in issues],
    }
