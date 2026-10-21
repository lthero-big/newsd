"""Per-user subscriptions, keywords and quotas API (M2-1). Every query is scoped to the logged-in user."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from .auth import get_db, require_user
from .models import Keyword, Quota, Source, Subscription, SubscriptionTemplate, SubscriptionTemplateItem
from .perms import guard

router = APIRouter(prefix="/api")
OVERRIDE_KEYS = {"label", "max_items", "min_likes", "min_score", "content_extractor", "language"}
KW_KINDS = ("interest", "block", "boost")


def _src(s: Source) -> dict:
    return {"key": s.key, "kind": s.kind, "name": s.name, "url": s.url, "language": s.language, "default_tags": s.default_tags or [],
            "enabled": s.enabled, "content_extractor": s.content_extractor}


@router.get("/sources")
def list_sources(user=Depends(require_user), db=Depends(get_db)):
    q = select(Source).order_by(Source.kind, Source.key)
    if user.role != "admin":
        q = q.where(Source.enabled.is_(True))
    return [_src(s) for s in db.scalars(q)]


class SubIn(BaseModel):
    source: str
    enabled: bool = True
    overrides: dict = Field(default_factory=dict)


@router.get("/me/subscriptions")
def my_subs(user=Depends(require_user), db=Depends(get_db)):
    rows = db.execute(select(Subscription, Source).join(Source).where(Subscription.user_id == user.id).order_by(Source.key))
    return [{"source": s.key, "enabled": sub.enabled, "overrides": sub.overrides or {}} for sub, s in rows]


@router.put("/me/subscriptions", dependencies=[Depends(guard("src"))])
def put_sub(body: SubIn, user=Depends(require_user), db=Depends(get_db)):
    src = db.scalar(select(Source).where(Source.key == body.source, Source.enabled.is_(True)))
    if src is None:
        raise HTTPException(404, "unknown source")
    bad = set(body.overrides) - OVERRIDE_KEYS
    if bad:
        raise HTTPException(400, f"overrides not allowed: {sorted(bad)}")
    sub = db.scalar(select(Subscription).where(Subscription.user_id == user.id, Subscription.source_id == src.id))
    if sub is None:
        sub = Subscription(user_id=user.id, source_id=src.id); db.add(sub)
    sub.enabled, sub.overrides = body.enabled, body.overrides or None
    db.commit()
    return {"source": src.key, "enabled": sub.enabled, "overrides": sub.overrides or {}}


@router.delete("/me/subscriptions/{key:path}", dependencies=[Depends(guard("src"))])
def del_sub(key: str, user=Depends(require_user), db=Depends(get_db)):
    src = db.scalar(select(Source).where(Source.key == key))
    n = 0
    if src:
        n = db.execute(delete(Subscription).where(Subscription.user_id == user.id, Subscription.source_id == src.id)).rowcount
        db.commit()
    if not n:
        raise HTTPException(404, "not subscribed")
    return {"ok": True}


@router.post("/me/subscriptions/apply-template", dependencies=[Depends(guard("src"))])
def apply_template(name: str = "default", user=Depends(require_user), db=Depends(get_db)):
    return apply_template_to_user(db, user.id, name)


def apply_template_to_user(db, user_id: int, name: str = "default") -> dict:
    tpl = db.scalar(select(SubscriptionTemplate).where(SubscriptionTemplate.name == name))
    if tpl is None:
        raise HTTPException(404, "unknown template")
    have = set(db.scalars(select(Subscription.source_id).where(Subscription.user_id == user_id)))
    added = 0
    for sid in db.scalars(select(SubscriptionTemplateItem.source_id).where(SubscriptionTemplateItem.template_id == tpl.id)):
        if sid not in have:
            db.add(Subscription(user_id=user_id, source_id=sid)); added += 1
    kw_have = {(k.kind, k.term) for k in db.scalars(select(Keyword).where(Keyword.user_id == user_id))}
    for k in tpl.keywords or []:
        if (k["kind"], k["term"]) not in kw_have:
            db.add(Keyword(user_id=user_id, kind=k["kind"], term=k["term"], weight=k.get("weight", 1.0)))
    db.commit()
    return {"added_sources": added}


class KwIn(BaseModel):
    kind: str
    term: str = Field(min_length=1, max_length=191)
    weight: float = 1.0


@router.get("/me/keywords")
def my_keywords(user=Depends(require_user), db=Depends(get_db)):
    return [{"id": k.id, "kind": k.kind, "term": k.term, "weight": k.weight}
            for k in db.scalars(select(Keyword).where(Keyword.user_id == user.id).order_by(Keyword.kind, Keyword.term))]


@router.post("/me/keywords")
def add_keyword(body: KwIn, user=Depends(require_user), db=Depends(get_db)):
    if body.kind not in KW_KINDS:
        raise HTTPException(400, f"kind must be one of {KW_KINDS}")
    term = body.term.strip()
    k = db.scalar(select(Keyword).where(Keyword.user_id == user.id, Keyword.kind == body.kind, Keyword.term == term))
    if k is None:
        k = Keyword(user_id=user.id, kind=body.kind, term=term); db.add(k)
    k.weight = body.weight
    db.commit()
    return {"id": k.id, "kind": k.kind, "term": k.term, "weight": k.weight}


@router.delete("/me/keywords/{kid}")
def del_keyword(kid: int, user=Depends(require_user), db=Depends(get_db)):
    n = db.execute(delete(Keyword).where(Keyword.id == kid, Keyword.user_id == user.id)).rowcount
    db.commit()
    if not n:
        raise HTTPException(404, "not found")
    return {"ok": True}


class QuotaIn(BaseModel):
    module: str = ""
    group_name: str = ""
    max_items: int = Field(ge=0, le=1000)
    categories: list[str] | None = None


@router.get("/me/quotas")
def my_quotas(user=Depends(require_user), db=Depends(get_db)):
    return [{"module": q.module, "group_name": q.group_name, "max_items": q.max_items, "categories": q.categories or []}
            for q in db.scalars(select(Quota).where(Quota.user_id == user.id).order_by(Quota.module, Quota.group_name))]


@router.put("/me/quotas")
def put_quotas(body: list[QuotaIn], user=Depends(require_user), db=Depends(get_db)):
    """Replace the user's whole quota set (overall cap: module="" group_name=""; module cap: group_name="")."""
    keys = [(q.module, q.group_name) for q in body]
    if len(keys) != len(set(keys)):
        raise HTTPException(400, "duplicate module/group")
    if any(g and not m for m, g in keys):
        raise HTTPException(400, "group quota needs a module")
    db.execute(delete(Quota).where(Quota.user_id == user.id))
    for q in body:
        db.add(Quota(user_id=user.id, module=q.module, group_name=q.group_name, max_items=q.max_items, categories=q.categories))
    db.commit()
    return my_quotas(user, db)
