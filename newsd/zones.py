"""Source categories and zones (DEPTAI-216 item 3b, per the accepted v2 demo's 订阅源 page).

* Every subscription belongs to one *category* derived from its source (X, Reddit, Hacker News, RSS, GitHub,
  Hugging Face, arXiv) and sits in one of the user's *zones* of that category (e.g. 中文 / 英文 / 特别关注),
  at a position, with a weight 0 / 1 / 2 (score x1.0 / x1.3 / x1.6).
* A disabled zone switches all of its sources off (the per-source switches are kept for when it comes back).
* A ``special`` zone (特别关注) is exempt from every quota cap and from learned down-ranking.
* The category order is a per-user setting (``source_cats``).
Subscriptions created elsewhere (onboarding, templates, the plain subscriptions API) have no zone yet;
``ensure`` puts them into a default zone (中文 / 英文 by language, else 默认) the next time zones are read.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .auth import get_db, require_user
from .models import PoolItem, PoolItemSource, Source, SourceZone, Subscription, UserSetting

router = APIRouter(prefix="/api")
# key -> (name, icon, what the category covers)
CATS = {
    "x": ("X", "𝕏", "账号、列表或关键词搜索（经自建 RSSHub）"),
    "reddit": ("Reddit", "r/", "子版块 RSS（自带 429 限速与重试）"),
    "hn": ("Hacker News", "Y", "首页、Show HN、关键词"),
    "rss": ("RSS", "◉", "任意 RSS / Atom / JSON Feed"),
    "github": ("GitHub", "⌥", "仓库 Release、Trending、搜索"),
    "hf": ("Hugging Face", "🤗", "组织新模型、博客、Daily Papers"),
    "arxiv": ("arXiv", "χ", "分类 + 关键词"),
}
PLUGIN_CATS = {"plugin:hn": "hn", "plugin:github_search": "github", "plugin:hf_trending": "hf", "plugin:ossinsight": "github"}
WEIGHTS = {0: 1.0, 1: 1.3, 2: 1.6}
SPECIAL = "特别关注"
MAX_ZONES = 30
MAX_NAME = 64


def category(key: str, kind: str) -> str:
    if kind == "plugin":
        return PLUGIN_CATS.get(key, "rss")
    if kind in ("x_account", "x_query") or (kind == "rsshub" and "twitter" in key):
        return "x"
    if kind in ("reddit", "hn", "github", "hf", "arxiv"):
        return kind
    return "rss"


def default_zone_name(lang: str | None) -> str:
    lang = (lang or "").lower()
    return "中文" if lang.startswith("zh") else "英文" if lang.startswith("en") else "默认"


def describe(s: Source) -> str:
    cfg = s.config or {}
    what = {"x_account": "账号", "x_query": "搜索", "rsshub": "RSSHub", "reddit": "子版块", "rss": "Feed", "plugin": "聚合源"}.get(s.kind, s.kind)
    detail = cfg.get("query") or cfg.get("route") or cfg.get("handle") or (s.url or "").removeprefix("https://").removeprefix("http://")
    return f"{what} · {detail}"[:120] if detail else what


# ------------------------------------------------------------------------------------------- settings

def cat_order(db, user_id: int) -> list[str]:
    row = db.scalar(select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.key == "source_cats"))
    saved = [c for c in (row.value if row is not None and isinstance(row.value, list) else []) if c in CATS]
    return list(dict.fromkeys(saved + list(CATS)))


def save_cat_order(db, user_id: int, order: list[str]) -> list[str]:
    order = list(dict.fromkeys([c for c in order if c in CATS] + list(CATS)))
    row = db.scalar(select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.key == "source_cats"))
    if row is None:
        row = UserSetting(user_id=user_id, key="source_cats")
        db.add(row)
    row.value = order
    db.commit()
    return order


# ------------------------------------------------------------------------------------------- placement

def _zones(db, user_id: int, cat: str | None = None) -> list[SourceZone]:
    q = select(SourceZone).where(SourceZone.user_id == user_id)
    if cat:
        q = q.where(SourceZone.category == cat)
    return list(db.scalars(q.order_by(SourceZone.category, SourceZone.position, SourceZone.id)))


def _get_or_make_zone(db, user_id: int, cat: str, name: str) -> SourceZone:
    z = db.scalar(select(SourceZone).where(SourceZone.user_id == user_id, SourceZone.category == cat, SourceZone.name == name))
    if z is None:
        top = db.scalar(select(func.max(SourceZone.position)).where(SourceZone.user_id == user_id, SourceZone.category == cat))
        z = SourceZone(user_id=user_id, category=cat, name=name, position=0 if top is None else top + 1, enabled=True, special=False)
        db.add(z)
        db.flush()
    return z


def _next_pos(db, zone_id: int) -> int:
    top = db.scalar(select(func.max(Subscription.position)).where(Subscription.zone_id == zone_id))
    return 0 if top is None else top + 1


def ensure(db, user_id: int) -> None:
    """Give every unplaced subscription (or one whose zone is in the wrong category) a zone."""
    zones = {z.id: z for z in _zones(db, user_id)}
    changed = False
    rows = db.execute(select(Subscription, Source).join(Source, Source.id == Subscription.source_id)
                      .where(Subscription.user_id == user_id).order_by(Source.key)).all()
    rank = {"中文": 0, "英文": 1}
    rows.sort(key=lambda r: (rank.get(default_zone_name(r[1].language), 2), r[1].key))  # new zones: 中文, 英文, 默认
    for sub, src in rows:
        cat = category(src.key, src.kind)
        z = zones.get(sub.zone_id)
        if z is not None and z.category == cat:
            continue
        z = _get_or_make_zone(db, user_id, cat, default_zone_name(src.language))
        zones[z.id] = z
        sub.zone_id, sub.position = z.id, _next_pos(db, z.id)
        db.flush()
        changed = True
    if changed:
        db.commit()


def view(db, user_id: int) -> dict:
    ensure(db, user_id)
    subs = db.execute(select(Subscription, Source).join(Source, Source.id == Subscription.source_id)
                      .where(Subscription.user_id == user_id).order_by(Subscription.position, Source.key)).all()
    by_zone: dict[int, list[dict]] = {}
    subscribed = set()
    for sub, src in subs:
        subscribed.add(src.id)
        by_zone.setdefault(sub.zone_id, []).append({
            "key": src.key, "name": (sub.overrides or {}).get("label") or src.name or src.key, "kind": src.kind,
            "desc": describe(src), "url": src.url, "language": src.language, "enabled": bool(sub.enabled),
            "weight": int(sub.weight or 0), "available": bool(src.enabled)})
    zones: dict[str, list[dict]] = {}
    for z in _zones(db, user_id):
        zones.setdefault(z.category, []).append({"id": z.id, "name": z.name, "enabled": bool(z.enabled), "special": bool(z.special),
                                                 "sources": by_zone.get(z.id, [])})
    catalog: dict[str, list[dict]] = {}
    for src in db.scalars(select(Source).where(Source.enabled.is_(True)).order_by(Source.key)):
        if src.id not in subscribed:
            catalog.setdefault(category(src.key, src.kind), []).append(
                {"key": src.key, "name": src.name or src.key, "desc": describe(src), "language": src.language})
    cats = [{"key": c, "name": CATS[c][0], "icon": CATS[c][1], "desc": CATS[c][2], "zones": zones.get(c, []),
             "catalog": catalog.get(c, [])} for c in cat_order(db, user_id)]
    return {"cats": cats, "weights": {str(k): v for k, v in WEIGHTS.items()}}


# ------------------------------------------------------------------------------------------- pipeline

def active_filter():
    """SQL condition for subscriptions whose zone (if any) is enabled; used by the pool's per-user view."""
    from sqlalchemy import or_
    off = select(SourceZone.id).where(SourceZone.enabled.is_(False))
    return or_(Subscription.zone_id.is_(None), Subscription.zone_id.not_in(off))


def source_flags(db, user_id: int) -> dict[str, tuple[int, bool]]:
    """source key -> (weight, in a special zone) for the user's active subscriptions."""
    rows = db.execute(select(Source.key, Subscription.weight, SourceZone.special).join(Source, Source.id == Subscription.source_id)
                      .outerjoin(SourceZone, SourceZone.id == Subscription.zone_id).where(Subscription.user_id == user_id))
    return {k: (int(w or 0), bool(sp)) for k, w, sp in rows}


def special_keys(db, user_id: int) -> set[str]:
    return {k for k, (_w, sp) in source_flags(db, user_id).items() if sp}


def apply_to_candidates(db, user_id: int, cands) -> None:
    """Build time: weight multiplier (best of the item's sources) and the special-zone flag (skips quotas)."""
    flags = source_flags(db, user_id)
    if not any(w or sp for w, sp in flags.values()):
        return
    for c in cands:
        fs = [flags[k] for k in c.source_keys if k in flags]
        if not fs:
            continue
        mult = max(WEIGHTS.get(w, 1.0) for w, _ in fs)
        special = any(sp for _, sp in fs)
        if mult != 1.0:
            c.score = round(c.score * mult, 2)
        if mult != 1.0 or special:
            c.detail = {**(c.detail or {}), "source_weight_user": mult, **({"special": True} if special else {})}


# ------------------------------------------------------------------------------------------- API

def _zone(db, user_id: int, zid: int) -> SourceZone:
    z = db.get(SourceZone, zid)
    if z is None or z.user_id != user_id:
        raise HTTPException(404, "no such zone")
    return z


def _clean_name(name: str) -> str:
    name = " ".join((name or "").split())[:MAX_NAME]
    if not name:
        raise HTTPException(400, "name required")
    return name


def _sub(db, user_id: int, key: str) -> tuple[Subscription, Source]:
    row = db.execute(select(Subscription, Source).join(Source, Source.id == Subscription.source_id)
                     .where(Subscription.user_id == user_id, Source.key == key)).first()
    if row is None:
        raise HTTPException(404, "not subscribed")
    return row[0], row[1]


@router.get("/me/zones")
def get_zones(user=Depends(require_user), db=Depends(get_db)):
    return view(db, user.id)


class ZoneIn(BaseModel):
    category: str
    name: str = Field(max_length=200)
    special: bool = False


@router.post("/me/zones")
def create_zone(body: ZoneIn, user=Depends(require_user), db=Depends(get_db)):
    if body.category not in CATS:
        raise HTTPException(400, f"category must be one of {list(CATS)}")
    name = _clean_name(body.name)
    if len(_zones(db, user.id)) >= MAX_ZONES:
        raise HTTPException(400, f"at most {MAX_ZONES} zones")
    if db.scalar(select(SourceZone.id).where(SourceZone.user_id == user.id, SourceZone.category == body.category, SourceZone.name == name)):
        raise HTTPException(409, "a zone with this name already exists in this category")
    z = _get_or_make_zone(db, user.id, body.category, name)
    z.special = body.special
    db.commit()
    return {"id": z.id, "name": z.name, "category": z.category, "enabled": z.enabled, "special": z.special}


class ZonePatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None
    special: bool | None = None


@router.patch("/me/zones/{zid}")
def patch_zone(zid: int, body: ZonePatch, user=Depends(require_user), db=Depends(get_db)):
    z = _zone(db, user.id, zid)
    if body.name is not None:
        name = _clean_name(body.name)
        if name != z.name and db.scalar(select(SourceZone.id).where(SourceZone.user_id == user.id, SourceZone.category == z.category,
                                                                     SourceZone.name == name)):
            raise HTTPException(409, "a zone with this name already exists in this category")
        z.name = name
    if body.enabled is not None:
        z.enabled = body.enabled
    if body.special is not None:
        z.special = body.special
    db.commit()
    return {"id": z.id, "name": z.name, "category": z.category, "enabled": z.enabled, "special": z.special}


@router.delete("/me/zones/{zid}")
def delete_zone(zid: int, user=Depends(require_user), db=Depends(get_db)):
    """Deletes the zone and unsubscribes the sources in it (the page asks first when it is not empty)."""
    z = _zone(db, user.id, zid)
    subs = list(db.scalars(select(Subscription).where(Subscription.user_id == user.id, Subscription.zone_id == z.id)))
    keys = [db.get(Source, s.source_id).key for s in subs]
    for s in subs:
        db.delete(s)
    db.delete(z)
    db.commit()
    return {"deleted": zid, "unsubscribed": keys}


class OrderIn(BaseModel):
    ids: list[int] = Field(max_length=MAX_ZONES)


@router.put("/me/zones/order")
def order_zones(body: OrderIn, category: str, user=Depends(require_user), db=Depends(get_db)):
    zones = {z.id: z for z in _zones(db, user.id, category)}
    if sorted(body.ids) != sorted(zones):
        raise HTTPException(400, "ids must list every zone of this category exactly once")
    for i, zid in enumerate(body.ids):
        zones[zid].position = i
    db.commit()
    return {"category": category, "ids": body.ids}


class CatOrderIn(BaseModel):
    order: list[str] = Field(max_length=20)


@router.put("/me/source-cats")
def order_cats(body: CatOrderIn, user=Depends(require_user), db=Depends(get_db)):
    return {"order": save_cat_order(db, user.id, body.order)}


class ZoneSourcesIn(BaseModel):
    keys: list[str] = Field(max_length=1000)


@router.put("/me/zones/{zid}/sources")
def set_zone_sources(zid: int, body: ZoneSourcesIn, user=Depends(require_user), db=Depends(get_db)):
    """The zone's full, ordered list of sources after a drag: listed sources move here (from any zone of the same
    category) in this order. Sources no longer listed must be placed elsewhere by the same drag (the client sends
    the source zone's new list too)."""
    z = _zone(db, user.id, zid)
    if len(set(body.keys)) != len(body.keys):
        raise HTTPException(400, "duplicate keys")
    rows = [_sub(db, user.id, k) for k in body.keys]
    for sub, src in rows:
        if category(src.key, src.kind) != z.category:
            raise HTTPException(400, f"{src.key} is not a {CATS[z.category][0]} source")
    for i, (sub, _src) in enumerate(rows):
        sub.zone_id, sub.position = z.id, i
    db.commit()
    return {"id": z.id, "keys": body.keys}


class SourcePatch(BaseModel):
    enabled: bool | None = None
    weight: int | None = Field(default=None, ge=0, le=2)
    zone_id: int | None = None


@router.patch("/me/subscriptions/{key:path}")
def patch_subscription(key: str, body: SourcePatch, user=Depends(require_user), db=Depends(get_db)):
    sub, src = _sub(db, user.id, key)
    if body.zone_id is not None:
        z = _zone(db, user.id, body.zone_id)
        if z.category != category(src.key, src.kind):
            raise HTTPException(400, "zone is in another category")
        if sub.zone_id != z.id:
            sub.zone_id, sub.position = z.id, _next_pos(db, z.id)
    if body.enabled is not None:
        sub.enabled = body.enabled
    if body.weight is not None:
        sub.weight = body.weight
    db.commit()
    return {"key": src.key, "enabled": sub.enabled, "weight": sub.weight, "zone_id": sub.zone_id}


class AddIn(BaseModel):
    key: str
    weight: int = Field(default=0, ge=0, le=2)
    position: int | None = Field(default=None, ge=0)


@router.post("/me/zones/{zid}/sources")
def add_to_zone(zid: int, body: AddIn, user=Depends(require_user), db=Depends(get_db)):
    """Subscribe to a catalog source straight into this zone (or move it here when already subscribed)."""
    z = _zone(db, user.id, zid)
    src = db.scalar(select(Source).where(Source.key == body.key, Source.enabled.is_(True)))
    if src is None:
        raise HTTPException(404, "unknown source")
    if category(src.key, src.kind) != z.category:
        raise HTTPException(400, f"{src.key} is not a {CATS[z.category][0]} source")
    sub = db.scalar(select(Subscription).where(Subscription.user_id == user.id, Subscription.source_id == src.id))
    if sub is None:
        sub = Subscription(user_id=user.id, source_id=src.id, enabled=True)
        db.add(sub)
    sub.weight = body.weight
    members = list(db.scalars(select(Subscription).where(Subscription.zone_id == z.id, Subscription.id != (sub.id or 0))
                              .order_by(Subscription.position, Subscription.id)))
    at = len(members) if body.position is None else min(body.position, len(members))
    members.insert(at, sub)
    sub.zone_id = z.id
    for i, m in enumerate(members):
        m.position = i
    db.commit()
    return {"key": src.key, "zone_id": z.id, "weight": sub.weight, "enabled": sub.enabled}


@router.get("/me/sources/recent")
def recent_items(key: str, user=Depends(require_user), db=Depends(get_db)):
    """「最近 3 条」: the newest items the shared pool already holds for this source (no fetch is made)."""
    src = db.scalar(select(Source).where(Source.key == key, Source.enabled.is_(True)))
    if src is None:
        raise HTTPException(404, "unknown source")
    rows = db.scalars(select(PoolItem).join(PoolItemSource, PoolItemSource.item_id == PoolItem.id)
                      .where(PoolItemSource.source_id == src.id).order_by(func.coalesce(PoolItem.published_at, PoolItem.first_seen_at).desc()).limit(3))
    return {"key": key, "items": [{"title": p.title, "url": p.url,
                                   "at": (p.published_at or p.first_seen_at).isoformat() if (p.published_at or p.first_seen_at) else None}
                                  for p in rows]}
