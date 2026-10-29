"""DEPTAI-267: X as an optional source fed by Grok Bot (it reads public X with its own read-only access).

newsd never calls X here. Grok Bot (or any admin API-token holder) does a round like:

    GET  /api/admin/x-import/wanted    -> the deduped X accounts / searches the allowed users subscribe to + today's budget
    POST /api/admin/x-import/items    -> posts it read for one source; items go into the shared pool like any fetch
                                    (url-key / content-hash dedupe, so re-posting the same post never adds a row)

Scoring, filtering and per-user distribution then run unchanged: an item reaches the users subscribed to its source.

Admin switch (GlobalSetting ``x_import``), off by default, so a deployment without Grok Bot never shows any of this:
  enabled       master switch
  scope         "all" (every user) or "users" (only ``users``); admins always pass when enabled
  users         user ids for scope "users"
  max_per_user  how many X accounts / keywords one user may add through 「X 关注」
  daily_cap     X reads per UTC day across everyone (Grok Bot's X quota is shared: 30/min, 1000/day)
Usage is counted in GlobalSetting ``x_import_usage`` ({"day", "calls", "items", "exhausted_at"}).
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .auth import get_db, require_admin, require_user
from .eventlog import add as log_event
from .models import GlobalSetting, Source, SourceFetch, Subscription, User, UserSetting
from .perms import has

router = APIRouter(prefix="/api")
KEY, USAGE_KEY, FOLLOW_KEY = "x_import", "x_import_usage", "x_follow"
DEFAULTS = {"enabled": False, "scope": "all", "users": [], "max_per_user": 10, "daily_cap": 200}
X_KINDS = ("x_account", "x_query")
HANDLE = re.compile(r"^@?([A-Za-z0-9_]{1,15})$")
STATUS = re.compile(r"^https://(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d+)")
MAX_ITEMS_PER_POST = 100


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _get(db, key: str):
    row = db.get(GlobalSetting, key)
    return row.value if row is not None else None


def _put(db, key: str, value) -> None:
    row = db.get(GlobalSetting, key)
    if row is None:
        db.add(GlobalSetting(key=key, value=value))
    else:
        row.value = value  # JSON columns track reassignment only


def config(db) -> dict:
    raw = _get(db, KEY) or {}
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    cfg["users"] = [int(u) for u in cfg["users"] or []]
    return cfg


def usage(db) -> dict:
    day = _utcnow().strftime("%Y-%m-%d")
    u = _get(db, USAGE_KEY) or {}
    if u.get("day") != day:
        u = {"day": day, "calls": 0, "items": 0, "exhausted_at": None}
    return u


def allowed(db, user: User | None, cfg: dict | None = None) -> bool:
    """Whether this user sees / can use X (the switch is on and the user is in scope)."""
    cfg = cfg or config(db)
    if user is None or not cfg["enabled"]:
        return False
    return user.role == "admin" or cfg["scope"] == "all" or user.id in cfg["users"]


# ------------------------------------------------------------------------------------------------ admin switch

class XImportCfg(BaseModel):
    enabled: bool = False
    scope: str = Field(default="all", pattern="^(all|users)$")
    users: list[int] = Field(default_factory=list)
    max_per_user: int = Field(default=10, ge=0, le=200)
    daily_cap: int = Field(default=200, ge=0, le=1000)


def admin_view(db) -> dict:
    cfg, u = config(db), usage(db)
    return {**cfg, "usage": u, "remaining": max(0, cfg["daily_cap"] - u["calls"]),
            "exhausted": bool(cfg["enabled"] and u["calls"] >= cfg["daily_cap"])}


@router.get("/admin/x-import")
def get_cfg(_=Depends(require_admin), db=Depends(get_db)):
    return admin_view(db)


@router.put("/admin/x-import")
def put_cfg(body: XImportCfg, admin=Depends(require_admin), db=Depends(get_db)):
    known = set(db.scalars(select(User.id).where(User.id.in_(body.users or [0]))))
    bad = sorted(set(body.users) - known)
    if bad:
        raise HTTPException(400, f"unknown user ids: {bad}")
    _put(db, KEY, body.model_dump())
    log_event(db, "INFO", "x", f"X 信息源设置已更新：{'启用' if body.enabled else '关闭'}，范围 {body.scope}",
              user=admin, detail=body.model_dump())
    db.commit()
    return admin_view(db)


# ------------------------------------------------------------------------------------------- user: X 关注

class FollowIn(BaseModel):
    value: str = Field(min_length=1, max_length=120)


def _follows(db, user_id: int) -> list[str]:
    row = db.scalar(select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.key == FOLLOW_KEY))
    return list(row.value or []) if row is not None else []


def _set_follows(db, user_id: int, keys: list[str]) -> None:
    row = db.scalar(select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.key == FOLLOW_KEY))
    if row is None:
        db.add(UserSetting(user_id=user_id, key=FOLLOW_KEY, value=keys))
    else:
        row.value = keys


def source_for(db, value: str) -> Source:
    """``@handle`` -> x_account ``xa:<handle>``; anything else -> x_query ``xq:u-<hash>`` (one shared source per term)."""
    v = " ".join(value.split())
    m = HANDLE.match(v) if v.startswith("@") else None
    if m:
        handle = m.group(1)
        src = db.scalar(select(Source).where(func.lower(Source.key) == f"xa:{handle.lower()}"))
        if src is None:
            src = Source(key=f"xa:{handle}", kind="x_account", name=f"@{handle}", url=f"https://x.com/{handle}",
                         config={"via": "grokbot"}, enabled=True)
            db.add(src); db.flush()
        return src
    if len(v) < 2:
        raise HTTPException(400, "keyword too short")
    key = "xq:u-" + hashlib.sha256(v.lower().encode("utf-8")).hexdigest()[:12]
    src = db.scalar(select(Source).where(Source.key == key))
    if src is None:
        src = Source(key=key, kind="x_query", name=f"X 搜索：{v}", url=None, config={"terms": [v], "via": "grokbot"},
                     enabled=True)
        db.add(src); db.flush()
    return src


def _require_x(db, user: User) -> dict:
    cfg = config(db)
    if not allowed(db, user, cfg):
        raise HTTPException(404, "not found")  # switched off / out of scope: the feature does not exist for this user
    return cfg


def _follow_rows(db, user_id: int) -> list[dict]:
    keys = _follows(db, user_id)
    srcs = {s.key: s for s in db.scalars(select(Source).where(Source.key.in_(keys or [""])))}
    return [{"key": k, "kind": srcs[k].kind, "name": srcs[k].name} for k in keys if k in srcs]


@router.get("/me/x")
def my_x(user=Depends(require_user), db=Depends(get_db)):
    cfg = _require_x(db, user)
    return {"follows": _follow_rows(db, user.id), "limit": cfg["max_per_user"]}


@router.post("/me/x")
def add_x(body: FollowIn, user=Depends(require_user), db=Depends(get_db)):
    cfg = _require_x(db, user)
    if not has(db, user, "src"):
        raise HTTPException(403, "permission required: src")
    keys = _follows(db, user.id)
    src = source_for(db, body.value)
    if src.key not in keys:
        if len(keys) >= cfg["max_per_user"]:
            db.rollback()
            raise HTTPException(400, f"每人最多 {cfg['max_per_user']} 个 X 账号 / 关键词")
        keys.append(src.key)
        _set_follows(db, user.id, keys)
    if not src.enabled:
        db.rollback()
        raise HTTPException(400, "管理员已停用这个源")
    sub = db.scalar(select(Subscription).where(Subscription.user_id == user.id, Subscription.source_id == src.id))
    if sub is None:
        db.add(Subscription(user_id=user.id, source_id=src.id, enabled=True))
    db.commit()
    from .zones import ensure
    ensure(db, user.id)  # places it in the user's X zone
    return {"follows": _follow_rows(db, user.id), "limit": cfg["max_per_user"], "added": src.key}


@router.delete("/me/x/{key:path}")
def del_x(key: str, user=Depends(require_user), db=Depends(get_db)):
    cfg = _require_x(db, user)
    keys = _follows(db, user.id)
    if key not in keys:
        raise HTTPException(404, "not followed")
    keys.remove(key)
    _set_follows(db, user.id, keys)
    src = db.scalar(select(Source).where(Source.key == key))
    if src is not None:
        sub = db.scalar(select(Subscription).where(Subscription.user_id == user.id, Subscription.source_id == src.id))
        if sub is not None:
            db.delete(sub)
    db.commit()
    return {"follows": _follow_rows(db, user.id), "limit": cfg["max_per_user"]}


# --------------------------------------------------------------------------------------- Grok Bot import API

def wanted(db) -> dict:
    """What to read this round: every enabled X source with at least one enabled subscription by an active,
    allowed user, deduped, most-subscribed first, cut to the remaining daily budget (one read per source)."""
    cfg, u = config(db), usage(db)
    if not cfg["enabled"]:
        return {"enabled": False}
    users = list(db.scalars(select(User).where(User.active.is_(True))))
    ok_ids = [x.id for x in users if allowed(db, x, cfg)]
    counts = (select(Subscription.source_id, func.count().label("n"))
              .where(Subscription.enabled.is_(True), Subscription.user_id.in_(ok_ids or [0]))
              .group_by(Subscription.source_id).subquery())
    rows = db.execute(select(Source, counts.c.n).join(counts, counts.c.source_id == Source.id)
                      .where(Source.enabled.is_(True), Source.kind.in_(X_KINDS))
                      .order_by(counts.c.n.desc(), Source.key)).all()
    remaining = max(0, cfg["daily_cap"] - u["calls"])
    out = []
    for src, n in rows:
        cfgs = src.config or {}
        if src.kind == "x_account":
            out.append({"source": src.key, "kind": "account", "handle": src.key.split(":", 1)[1], "subscribers": n})
        elif cfgs.get("terms"):
            q = " OR ".join(cfgs["terms"]) + (f" ({' OR '.join(cfgs['and'])})" if cfgs.get("and") else "")
            out.append({"source": src.key, "kind": "search", "query": q, "subscribers": n})
    return {"enabled": True, "remaining": remaining, "daily_cap": cfg["daily_cap"], "total": len(out),
            "sources": out[:remaining], "skipped_for_budget": max(0, len(out) - remaining)}


@router.get("/admin/x-import/wanted")
def get_wanted(_=Depends(require_admin), db=Depends(get_db)):
    return wanted(db)


class XMedia(BaseModel):
    url: str
    type: str = "photo"
    width: int | None = None
    height: int | None = None
    video: str | None = None


class XPost(BaseModel):
    url: str
    text: str = Field(min_length=1, max_length=8000)
    author: str | None = None
    author_name: str | None = None
    created_at: str | None = None
    likes: int | None = None
    reposts: int | None = None
    replies: int | None = None
    media: list[XMedia] = Field(default_factory=list)


class XBatch(BaseModel):
    source: str
    calls: int = Field(default=1, ge=0, le=50)  # X reads this batch cost (counts against daily_cap)
    items: list[XPost] = Field(default_factory=list, max_length=MAX_ITEMS_PER_POST)


def row_of(p: XPost) -> dict | None:
    m = STATUS.match(p.url.strip())
    if not m:
        return None
    url = f"https://x.com/{m.group(1)}/status/{m.group(2)}"
    text = " ".join(p.text.split())
    title = text if len(text) <= 120 else text[:117].rstrip() + "…"
    media = []  # pool media rows (see content.lead_image / safe_media): images, or videos with their poster
    for x in p.media:
        if not x.url.startswith("https://pbs.twimg.com/"):
            continue
        if x.type == "video" and (x.video or "").startswith("https://video.twimg.com/"):
            media.append({"type": "video", "url": x.video, "poster": x.url})
        else:
            media.append({"type": "image", "url": x.url, **({"width": x.width, "height": x.height} if x.width else {})})
    # points / comments are what scoring reads as engagement (same keys HN / Reddit rows use)
    extra = {"author": p.author or m.group(1), "author_name": p.author_name, "likes": p.likes, "reposts": p.reposts,
             "replies": p.replies, "points": p.likes, "comments": p.replies, "media": media, "transport": "grokbot"}
    return {"url": url, "title": title, "listing_text": p.text, "published_at": p.created_at, "kind": "post",
            "extra": {k: v for k, v in extra.items() if v not in (None, [])}}


def import_batch(db, body: XBatch, actor: User | None = None) -> dict:
    from .pool.collector import upsert_item
    cfg = config(db)
    if not cfg["enabled"]:
        raise HTTPException(409, "X 信息源未启用")
    src = db.scalar(select(Source).where(Source.key == body.source))
    if src is None or src.kind not in X_KINDS:
        raise HTTPException(404, "unknown X source")
    u = usage(db)
    if u["calls"] + body.calls > cfg["daily_cap"]:
        if not u.get("exhausted_at"):
            u["exhausted_at"] = _utcnow().isoformat(timespec="seconds")
            _put(db, USAGE_KEY, u)
            log_event(db, "WARN", "x", f"X 今日额度已用完（{u['calls']}/{cfg['daily_cap']}），后续抓取跳过",
                      source=src.key)
            db.commit()
        raise HTTPException(429, f"X daily cap reached ({u['calls']}/{cfg['daily_cap']})")
    now = _utcnow()
    cycle = "xi:" + now.strftime("%Y-%m-%dT%H:%M:%S.%f")
    new = seen = bad = 0
    for p in body.items:
        row = row_of(p)
        if row is None:
            bad += 1
            continue
        _, created = upsert_item(db, src, row, cycle, now)
        new += int(created); seen += int(not created)
    db.add(SourceFetch(source_id=src.id, cycle=cycle, status="ok", url="grokbot", entries=len(body.items),
                       new_items=new, started_at=now, finished_at=_utcnow()))
    u = {**u, "calls": u["calls"] + body.calls, "items": u["items"] + new}
    _put(db, USAGE_KEY, u)
    db.commit()
    return {"source": src.key, "new": new, "seen": seen, "rejected": bad,
            "remaining": max(0, cfg["daily_cap"] - u["calls"])}


@router.post("/admin/x-import/items")
def post_items(body: XBatch, admin=Depends(require_admin), db=Depends(get_db)):
    return import_batch(db, body, admin)
