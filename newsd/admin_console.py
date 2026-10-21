"""DEPTAI-216 3c admin console API: overview numbers, users & groups (with permissions), site-wide source
health, structured logs and the worker run timeline.

Everything under /api/admin requires the admin role, except the read-only log / timeline endpoints which
also accept the group permission ``logs`` (see newsd/perms.py).
"""
from __future__ import annotations

import re
import secrets
import threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select

from . import perms as P
from .auth import get_db, require_admin
from .eventlog import LEVELS, MODULES, add as ev
from .models import (Invite, Issue, ItemMark, LogEvent, PoolItemSource, PushDelivery, Source, SourceFetch, Subscription,
                     User, UserActivity, UserGroup, UserGroupMember, WebSession, WorkerRun, now)
from .security import hash_password, new_token, token_hash
from .zones import CATS, category

router = APIRouter(prefix="/api/admin")
COLORS = ("#b3261e", "#2563eb", "#a855f7", "#16a34a", "#d97706", "#0f766e", "#64748b", "#db2777")
MAX_GROUPS = 50
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_EMAIL = re.compile(r"^[^@\s,;]{1,64}@[^@\s,;]{1,190}\.[^@\s,;]{1,20}$")


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


# ------------------------------------------------------------------------------------------------ groups

def group_view(g: UserGroup, n: int = 0) -> dict:
    return {"id": g.id, "name": g.name, "description": g.description or "", "color": g.color, "role": g.role,
            "perms": P.clean(g.perms), "default_sources": list(g.default_sources or []), "quota": g.quota, "n": n}


def _counts(db) -> dict[int, int]:
    return dict(db.execute(select(UserGroupMember.group_id, func.count()).group_by(UserGroupMember.group_id)).all())


def _group(db, gid: int) -> UserGroup:
    g = db.get(UserGroup, gid)
    if g is None:
        raise HTTPException(404, "no such group")
    return g


def subscribe_defaults(db, user: User, g: UserGroup) -> list[str]:
    """Subscribe ``user`` to the group's default sources they do not have yet (enabled catalog sources only)."""
    keys = [k for k in (g.default_sources or []) if isinstance(k, str)]
    if not keys:
        return []
    have = set(db.scalars(select(Subscription.source_id).where(Subscription.user_id == user.id)))
    added = []
    for s in db.scalars(select(Source).where(Source.key.in_(keys), Source.enabled.is_(True))):
        if s.id not in have:
            db.add(Subscription(user_id=user.id, source_id=s.id))
            added.append(s.key)
    return added


def join(db, user: User, g: UserGroup, apply_role: bool = False) -> bool:
    """Add ``user`` to ``g`` (idempotent). ``apply_role``: an invited user starts with the group's role
    (never admin through a group; an admin keeps admin)."""
    if db.get(UserGroupMember, (g.id, user.id)) is not None:
        return False
    db.add(UserGroupMember(group_id=g.id, user_id=user.id))
    if apply_role and user.role != "admin" and g.role in ("user", "readonly"):
        user.role = g.role
    subscribe_defaults(db, user, g)
    return True


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=255)
    color: str = Field(default="#64748b", max_length=16)
    role: str = "user"
    perms: list[str] = Field(default_factory=lambda: ["read", "src"])
    default_sources: list[str] = Field(default_factory=list, max_length=200)
    quota: int | None = Field(default=None, ge=1, le=1000)


class GroupPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=255)
    color: str | None = Field(default=None, max_length=16)
    role: str | None = None
    perms: list[str] | None = None
    default_sources: list[str] | None = Field(default=None, max_length=200)
    quota: int | None = Field(default=None, ge=0, le=1000)  # 0 = no cap


def _check_group_fields(db, name, color, role, sources, gid=None):
    if name is not None:
        name = " ".join(name.split())
        if not name:
            raise HTTPException(400, "name required")
        q = select(UserGroup.id).where(UserGroup.name == name)
        if gid:
            q = q.where(UserGroup.id != gid)
        if db.scalar(q):
            raise HTTPException(409, "group name exists")
    if color is not None and not _COLOR.match(color):
        raise HTTPException(400, "color must be #rrggbb")
    if role is not None and role not in ("user", "readonly"):
        raise HTTPException(400, "a group's default role is user or readonly (admin is set per user)")
    if sources:
        known = set(db.scalars(select(Source.key).where(Source.key.in_(sources))))
        bad = [k for k in sources if k not in known]
        if bad:
            raise HTTPException(400, "unknown sources: " + ", ".join(bad[:5]))
    return name


@router.get("/groups")
def list_groups(_=Depends(require_admin), db=Depends(get_db)):
    n = _counts(db)
    grouped = db.scalar(select(func.count(func.distinct(UserGroupMember.user_id)))) or 0
    return {"perms": [{"key": k, "name": t, "admin_only": k in P.ADMIN_ONLY} for k, t in P.PERMS],
            "roles": [{"key": k, "name": t} for k, t in P.ROLE_NAMES.items()], "colors": list(COLORS),
            "groups": [group_view(g, n.get(g.id, 0)) for g in db.scalars(select(UserGroup).order_by(UserGroup.position, UserGroup.id))],
            "users": db.scalar(select(func.count()).select_from(User)) or 0, "grouped": grouped}


@router.post("/groups")
def create_group(body: GroupIn, admin=Depends(require_admin), db=Depends(get_db)):
    if (db.scalar(select(func.count()).select_from(UserGroup)) or 0) >= MAX_GROUPS:
        raise HTTPException(400, f"at most {MAX_GROUPS} groups")
    name = _check_group_fields(db, body.name, body.color, body.role, body.default_sources)
    pos = (db.scalar(select(func.max(UserGroup.position))) or 0) + 1
    g = UserGroup(name=name, description=body.description.strip(), color=body.color, role=body.role, perms=P.clean(body.perms),
                  default_sources=list(dict.fromkeys(body.default_sources)), quota=body.quota, position=pos)
    db.add(g)
    db.flush()
    ev(db, "INFO", "admin", f"新建分组「{name}」", user=admin)
    db.commit()
    return group_view(g)


@router.patch("/groups/{gid}")
def patch_group(gid: int, body: GroupPatch, admin=Depends(require_admin), db=Depends(get_db)):
    g = _group(db, gid)
    name = _check_group_fields(db, body.name, body.color, body.role, body.default_sources, gid)
    if name is not None:
        g.name = name
    if body.description is not None:
        g.description = body.description.strip()
    for f in ("color", "role"):
        if getattr(body, f) is not None:
            setattr(g, f, getattr(body, f))
    if body.perms is not None:
        g.perms = P.clean(body.perms)
    if body.default_sources is not None:
        g.default_sources = list(dict.fromkeys(body.default_sources))
    if "quota" in body.model_fields_set:
        g.quota = body.quota or None
    ev(db, "INFO", "admin", f"修改分组「{g.name}」", user=admin)
    db.commit()
    return group_view(g, _counts(db).get(g.id, 0))


@router.delete("/groups/{gid}")
def delete_group(gid: int, admin=Depends(require_admin), db=Depends(get_db)):
    g = _group(db, gid)
    name = g.name
    db.execute(delete(UserGroupMember).where(UserGroupMember.group_id == gid))
    db.delete(g)
    ev(db, "INFO", "admin", f"删除分组「{name}」（成员保留）", user=admin)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------------------------------------ users

def _memberships(db) -> dict[int, list[int]]:
    out: dict[int, list[int]] = defaultdict(list)
    for gid, uid in db.execute(select(UserGroupMember.group_id, UserGroupMember.user_id)
                               .join(UserGroup, UserGroup.id == UserGroupMember.group_id)
                               .order_by(UserGroup.position, UserGroup.id)):
        out[uid].append(gid)
    return out


def _invite_status(i: Invite) -> str:
    return "used" if i.used_by else "expired" if i.expires_at and i.expires_at < now() else "unused"


@router.get("/people")
def people(_=Depends(require_admin), db=Depends(get_db)):
    """Users (with groups, status, last seen) plus pending e-mail invites, for the 用户与分组 table."""
    subs = dict(db.execute(select(Subscription.user_id, func.count()).group_by(Subscription.user_id)).all())
    issues = dict(db.execute(select(Issue.user_id, func.count()).group_by(Issue.user_id)).all())
    mem = _memberships(db)
    users = [{"id": u.id, "username": u.username, "email": u.email, "role": u.role, "active": u.active,
              "status": "active" if u.active else "disabled", "groups": mem.get(u.id, []), "created_at": _iso(u.created_at),
              "last_seen_at": _iso(u.last_seen_at), "subscriptions": subs.get(u.id, 0), "issues": issues.get(u.id, 0)}
             for u in db.scalars(select(User).order_by(User.id))]
    invites = [{"id": i.id, "email": i.note, "group_id": i.group_id, "created_at": _iso(i.created_at), "expires_at": _iso(i.expires_at)}
               for i in db.scalars(select(Invite).where(Invite.used_by.is_(None), Invite.note.is_not(None)).order_by(Invite.id.desc()).limit(500))
               if _invite_status(i) == "unused"]
    return {"users": users, "invites": invites}


@router.get("/users/{uid}/detail")
def user_detail(uid: int, _=Depends(require_admin), db=Depends(get_db)):
    u = db.get(User, uid)
    if u is None:
        raise HTTPException(404, "no such user")
    groups = P.user_groups(db, u.id)
    marks = dict(db.execute(select(ItemMark.state, func.count()).where(ItemMark.user_id == u.id).group_by(ItemMark.state)).all())
    acts = db.scalars(select(LogEvent).where(LogEvent.user_id == u.id).order_by(LogEvent.id.desc()).limit(12))
    days = db.scalar(select(func.count()).select_from(UserActivity).where(UserActivity.user_id == u.id,
                                                                            UserActivity.day >= (now() - timedelta(days=30)).strftime("%Y-%m-%d")))
    return {"id": u.id, "username": u.username, "email": u.email, "role": u.role, "active": u.active,
            "created_at": _iso(u.created_at), "last_seen_at": _iso(u.last_seen_at), "groups": [g.id for g in groups],
            "perms": sorted(P.effective(db, u, groups), key=P.KEYS.index), "favorites": marks.get("favorite", 0),
            "trash": marks.get("trash", 0) + marks.get("purged", 0), "active_days_30": days or 0,
            "sessions": db.scalar(select(func.count()).select_from(WebSession).where(WebSession.user_id == u.id, WebSession.expires_at > now())) or 0,
            "activity": [{"at": _iso(e.at), "level": e.level, "module": e.module, "message": e.message} for e in acts]}


class BulkIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    action: str
    group_id: int | None = None
    role: str | None = None


ACTIONS = ("add_group", "remove_group", "role", "disable", "enable", "reset_password", "logout")


@router.post("/users/bulk")
def bulk(body: BulkIn, admin=Depends(require_admin), db=Depends(get_db)):
    if body.action not in ACTIONS:
        raise HTTPException(400, "action must be one of: " + ", ".join(ACTIONS))
    users = list(db.scalars(select(User).where(User.id.in_(set(body.ids)))))
    if len(users) != len(set(body.ids)):
        raise HTTPException(404, "no such user")
    me_in = any(u.id == admin.id for u in users)
    out: dict = {"ok": True, "n": 0}
    if body.action in ("add_group", "remove_group"):
        g = _group(db, body.group_id or 0)
        for u in users:
            if body.action == "add_group":
                out["n"] += join(db, u, g)
            else:
                out["n"] += db.execute(delete(UserGroupMember).where(UserGroupMember.group_id == g.id,
                                                                      UserGroupMember.user_id == u.id)).rowcount or 0
        msg = f"{'加入' if body.action == 'add_group' else '移出'}分组「{g.name}」· {out['n']} 人"
    elif body.action == "role":
        from .cli import ROLES, set_role
        if body.role not in ROLES:
            raise HTTPException(400, "role must be one of: " + ", ".join(ROLES))
        if me_in and body.role != "admin":
            raise HTTPException(400, "cannot demote yourself")
        try:
            for u in users:
                if u.role != body.role:
                    set_role(db, u.username, body.role)
                    out["n"] += 1
        except ValueError as exc:
            db.rollback()
            raise HTTPException(400, str(exc))
        msg = f"改角色为 {P.ROLE_NAMES.get(body.role, body.role)} · {out['n']} 人"
    elif body.action in ("disable", "enable"):
        if me_in and body.action == "disable":
            raise HTTPException(400, "cannot deactivate yourself")
        if body.action == "disable":
            admins_left = db.scalar(select(func.count()).select_from(User).where(
                User.role == "admin", User.active.is_(True), User.id.not_in([u.id for u in users]))) or 0
            if not admins_left:
                raise HTTPException(400, "would leave no active admin")
        for u in users:
            if u.active != (body.action == "enable"):
                u.active = body.action == "enable"
                out["n"] += 1
                if not u.active:
                    db.execute(delete(WebSession).where(WebSession.user_id == u.id))
        msg = f"{'启用' if body.action == 'enable' else '停用'} · {out['n']} 人"
    elif body.action == "logout":
        for u in users:
            out["n"] += db.execute(delete(WebSession).where(WebSession.user_id == u.id)).rowcount or 0
        msg = f"强制下线 · {len(users)} 人 · {out['n']} 个会话"
    else:  # reset_password: a random temporary password, shown to the admin once; every session is signed out
        if me_in:
            raise HTTPException(400, "change your own password in 账号 instead")
        out["passwords"] = {}
        for u in users:
            pw = secrets.token_urlsafe(9)
            u.password_hash = hash_password(pw)
            db.execute(delete(WebSession).where(WebSession.user_id == u.id))
            out["passwords"][u.username] = pw
            out["n"] += 1
        msg = f"重置密码 · {out['n']} 人"
    ev(db, "INFO", "admin", msg, user=admin, detail={"users": ", ".join(u.username for u in users[:50])})
    db.commit()
    return out


class InviteBatchIn(BaseModel):
    emails: list[str] = Field(default_factory=list, max_length=100)
    group_id: int | None = None
    days: int = Field(default=14, ge=1, le=90)
    count: int = Field(default=0, ge=0, le=50)  # codes without an e-mail ("复制邀请链接")


@router.post("/invites/batch")
def invite_batch(body: InviteBatchIn, admin=Depends(require_admin), db=Depends(get_db)):
    """One single-use code per e-mail (or ``count`` anonymous codes), optionally joining a group on sign-up.
    newsd sends no e-mail: the codes are returned once for the admin to pass on."""
    emails = list(dict.fromkeys(e.strip().lower() for e in body.emails if e.strip()))
    bad = [e for e in emails if not _EMAIL.match(e)]
    if bad:
        raise HTTPException(400, "invalid e-mail: " + ", ".join(bad[:5]))
    if not emails and not body.count:
        raise HTTPException(400, "give e-mails or a count")
    g = _group(db, body.group_id) if body.group_id else None
    out = []
    for e in emails or [None] * body.count:
        code = new_token("inv")
        db.add(Invite(code_hash=token_hash(code), created_by=admin.id, group_id=g.id if g else None, note=e,
                      expires_at=now() + timedelta(days=body.days)))
        out.append({"email": e, "code": code})
    ev(db, "INFO", "admin", f"生成邀请 {len(out)} 个" + (f" · 分组「{g.name}」" if g else ""), user=admin)
    db.commit()
    return {"invites": out, "group": g.name if g else None, "expires_days": body.days}


# ------------------------------------------------------------------------------------------------ source health

HEALTH_DAYS = 14


def _reason(status: str) -> str:
    return status.split(":", 1)[1] if ":" in status else status


def health_rows(db) -> list[dict]:
    t = now()
    since = t - timedelta(days=HEALTH_DAYS)
    fetches: dict[int, list[SourceFetch]] = defaultdict(list)
    for f in db.scalars(select(SourceFetch).where(SourceFetch.started_at >= since).order_by(SourceFetch.started_at.desc(), SourceFetch.id.desc())):
        fetches[f.source_id].append(f)
    subs = dict(db.execute(select(Subscription.source_id, func.count()).where(Subscription.enabled.is_(True))
                           .group_by(Subscription.source_id)).all())
    items = dict(db.execute(select(PoolItemSource.source_id, func.count(func.distinct(PoolItemSource.item_id)))
                            .where(PoolItemSource.fetched_at >= t - timedelta(hours=24)).group_by(PoolItemSource.source_id)).all())
    from .admin import source_quality
    qual = source_quality(db, 30)
    out = []
    for s in db.scalars(select(Source).order_by(Source.key)):
        F = [f for f in fetches.get(s.id, []) if not f.status.startswith(("running", "skipped"))]
        last = F[0] if F else None
        fails = 0
        for f in F:
            if not f.status.startswith("failed"):
                break
            fails += 1
        empty = 0
        for f in F:
            if f.status != "ok" or f.entries:
                break
            empty += 1
        recent_fail = sum(1 for f in F if f.status.startswith("failed") and f.started_at >= t - timedelta(hours=24))
        if not s.enabled:
            st, note = "off", "管理员已停用"
        elif fails >= 2:
            st, note = "err", f"{_reason(last.status)} · 连续 {fails} 次"
        elif fails == 1:
            st, note = "warn", f"{_reason(last.status)}（最近一次失败）"
        elif empty >= 3:
            st, note = "warn", f"连续 {empty} 次没有条目"
        elif recent_fail >= 3:
            st, note = "warn", f"24 小时内失败 {recent_fail} 次"
        elif last is None:
            st, note = "ok", "还没有抓取记录" if subs.get(s.id) else "无人订阅，不抓取"
        else:
            st, note = "ok", ""
        hist = []
        for d in range(HEALTH_DAYS - 1, -1, -1):
            lo = (t - timedelta(days=d)).replace(hour=0, minute=0, second=0, microsecond=0)
            day = [f for f in F if lo <= f.started_at < lo + timedelta(days=1)]
            bad = sum(1 for f in day if f.status.startswith("failed"))
            hist.append("n" if not day else "e" if bad == len(day) else "w" if bad else "")
        lat = (last.finished_at - last.started_at).total_seconds() if last and last.finished_at else None
        q = qual.get(s.id, {})
        out.append({"key": s.key, "name": s.name or s.key, "kind": s.kind, "cat": category(s.key, s.kind), "url": s.url,
                    "enabled": s.enabled, "content_extractor": s.content_extractor, "st": st, "note": note,
                    "users": subs.get(s.id, 0), "items": items.get(s.id, 0), "lat": round(lat, 1) if lat is not None else None,
                    "last_at": _iso((last.finished_at or last.started_at) if last else None), "last_status": last.status if last else None,
                    "hist": hist, "pass_rate": q.get("pass_rate"), "collected": q.get("collected", 0), "selected": q.get("selected", 0)})
    return out


@router.get("/health")
def health(_=Depends(require_admin), db=Depends(get_db)):
    last = db.scalar(select(func.max(SourceFetch.finished_at)))
    return {"checked_at": _iso(last), "cats": {k: {"name": v[0], "icon": v[1]} for k, v in CATS.items()}, "sources": health_rows(db)}


def fetch_now(keys: set[str] | None, cycle: str) -> dict:
    """Fetch the given sources (None = every subscribed one) right now, in a fresh cycle so the
    once-per-cycle claim does not skip them. Module-level so tests can replace it."""
    from .db import SessionLocal
    from .pool import run_cycle_sync
    db = SessionLocal()
    try:
        return run_cycle_sync(db, cycle, keys=keys, extract=False)
    finally:
        db.close()


class RetryIn(BaseModel):
    key: str = Field(min_length=1, max_length=191)


@router.post("/health/retry")
def health_retry(body: RetryIn, admin=Depends(require_admin), db=Depends(get_db)):
    s = db.scalar(select(Source).where(Source.key == body.key))
    if s is None:
        raise HTTPException(404, "no such source")
    if not s.enabled:
        raise HTTPException(409, "source is disabled")
    if not db.scalar(select(Subscription.id).where(Subscription.source_id == s.id, Subscription.enabled.is_(True)).limit(1)):
        raise HTTPException(409, "no subscribers: the source is not fetched")
    rep = fetch_now({s.key}, "retry-" + now().strftime("%Y%m%d%H%M%S"))
    status = str((rep.get("sources") or {}).get(s.key, "unknown"))
    ev(db, "INFO" if status == "ok" else "ERROR", "fetch", f"手动重试 · {status}", source=s.key, user=admin)
    db.commit()
    row = next((r for r in health_rows(db) if r["key"] == s.key), None)
    return {"key": s.key, "status": status, "new_items": rep.get("new_items", 0), "row": row}


_bg_lock = threading.Lock()
_bg: dict[str, threading.Thread] = {}


def _start(name: str, fn) -> bool:
    with _bg_lock:
        t = _bg.get(name)
        if t is not None and t.is_alive():
            return False
        t = threading.Thread(target=fn, name=f"newsd-admin-{name}", daemon=True)
        _bg[name] = t
        t.start()
        return True


@router.post("/health/recheck")
def health_recheck(admin=Depends(require_admin), db=Depends(get_db)):
    """Fetch every subscribed source again in the background (a fresh cycle)."""
    started = _start("recheck", lambda: fetch_now(None, "recheck-" + now().strftime("%Y%m%d%H%M%S")))
    if not started:
        raise HTTPException(409, "a re-check is already running")
    ev(db, "INFO", "fetch", "全部重新检测（后台）", user=admin)
    db.commit()
    return {"started": True}


# ------------------------------------------------------------------------------------------------ logs & runs

_QK = re.compile(r"^(run|src|user|mod|level):(.+)$", re.I)


def _parse_q(q: str) -> tuple[dict, list[str]]:
    kv, text = {}, []
    for w in (q or "").split():
        m = _QK.match(w)
        if m:
            kv[m.group(1).lower()] = m.group(2)
        else:
            text.append(w)
    return kv, text[:8]


def _levels(s: str | None) -> list[str]:
    out = [x.strip().upper() for x in (s or "").split(",") if x.strip()]
    out = ["WARN" if x == "WARNING" else x for x in out]
    return [x for x in out if x in LEVELS] or list(LEVELS)


def _like(s: str) -> str:
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _filters(level, module, source, run, user, since, until, q):
    kv, text = _parse_q(q)
    conds = [LogEvent.level.in_(_levels(kv.get("level") or level))]
    if since:
        conds.append(LogEvent.at >= since)
    if until:
        conds.append(LogEvent.at <= until)
    if module or kv.get("mod"):
        conds.append(LogEvent.module == (kv.get("mod") or module))
    if source:
        conds.append(LogEvent.source_key == source)
    if kv.get("src"):
        conds.append(LogEvent.source_key.like(_like(kv["src"]), escape="\\"))
    r = kv.get("run") or run
    if r:
        try:
            conds.append(LogEvent.run_id == int(str(r).lower().removeprefix("run-").removeprefix("#")))
        except ValueError:
            raise HTTPException(400, "run must be a run id")
    if user or kv.get("user"):
        conds.append(LogEvent.username == (kv.get("user") or user))
    for w in text:
        conds.append(or_(LogEvent.message.like(_like(w), escape="\\"), LogEvent.source_key.like(_like(w), escape="\\")))
    return conds


def event_view(e: LogEvent) -> dict:
    return {"id": e.id, "at": _iso(e.at), "level": e.level, "module": e.module, "message": e.message, "source": e.source_key,
            "run": e.run_id, "user": e.username, "detail": e.detail or {}}


def _parse_time(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "bad time, use ISO 8601")
    return t.replace(tzinfo=None) - (t.utcoffset() or timedelta(0)) if t.tzinfo else t


@router.get("/logs")
def logs(level: str | None = None, module: str | None = None, source: str | None = None, run: str | None = None,
         user: str | None = None, since: str | None = None, until: str | None = None, q: str = "", limit: int = 200,
         before: int | None = None, _=Depends(P.require("logs")), db=Depends(get_db)):
    """Filtered events, newest first (``before`` = id cursor for 加载更多), plus a time histogram over the
    whole filtered range (all levels, so the bars show where trouble clustered) and filter facets."""
    t_until = _parse_time(until) or now()
    t_since = _parse_time(since) or t_until - timedelta(hours=24)
    if t_since >= t_until:
        raise HTTPException(400, "since must be before until")
    conds = _filters(level, module, source, run, user, t_since, t_until, q)
    limit = max(1, min(limit, 500))
    page = select(LogEvent).where(*conds)
    if before:
        page = page.where(LogEvent.id < before)
    rows = list(db.scalars(page.order_by(LogEvent.id.desc()).limit(limit + 1)))
    total = db.scalar(select(func.count()).select_from(LogEvent).where(*conds)) or 0
    span = (t_until - t_since).total_seconds()
    nb = 48 if span > 3600 * 6 else 30
    step = span / nb
    hist = [{"ERROR": 0, "WARN": 0, "INFO": 0, "DEBUG": 0} for _ in range(nb)]
    hconds = _filters("DEBUG,INFO,WARN,ERROR", module, source, run, user, t_since, t_until, q)
    for at, lv in db.execute(select(LogEvent.at, LogEvent.level).where(*hconds).limit(50000)):
        i = min(nb - 1, max(0, int((at - t_since).total_seconds() // step)))
        hist[i][lv] = hist[i].get(lv, 0) + 1
    since7 = now() - timedelta(days=7)
    facets = {
        "modules": [{"key": k, "name": v} for k, v in MODULES.items()],
        "sources": sorted(x for x in db.scalars(select(LogEvent.source_key).where(LogEvent.at >= since7, LogEvent.source_key.is_not(None)).distinct()) if x),
        "users": sorted(x for x in db.scalars(select(LogEvent.username).where(LogEvent.at >= since7, LogEvent.username.is_not(None)).distinct()) if x),
        "runs": [{"id": r.id, "status": r.status, "trigger": r.trigger, "started_at": _iso(r.started_at)}
                 for r in db.scalars(select(WorkerRun).order_by(WorkerRun.id.desc()).limit(30))]}
    return {"items": [event_view(e) for e in rows[:limit]], "more": len(rows) > limit, "total": total,
            "since": _iso(t_since), "until": _iso(t_until), "hist": {"step": step, "buckets": hist}, "facets": facets,
            "levels": dict(Counter(lv for lv, in db.execute(select(LogEvent.level).where(*_filters("DEBUG,INFO,WARN,ERROR", module, source, run, user, t_since, t_until, q)).limit(50000))))}


@router.get("/logs/{eid}")
def log_event(eid: int, _=Depends(P.require("logs")), db=Depends(get_db)):
    e = db.get(LogEvent, eid)
    if e is None:
        raise HTTPException(404, "no such event")
    near = []
    if e.run_id:
        near = [event_view(x) for x in db.scalars(select(LogEvent).where(LogEvent.run_id == e.run_id, LogEvent.id.between(e.id - 200, e.id + 200))
                                                  .order_by(LogEvent.id).limit(400))]
        i = next((k for k, x in enumerate(near) if x["id"] == e.id), 0)
        near = near[max(0, i - 5): i + 6]
    return {**event_view(e), "context": near}


def _stage_view(st: dict) -> dict:
    return {k: st.get(k) for k in ("stage", "status", "started_at", "finished_at", "counts", "error")}


@router.get("/timeline")
def timeline(limit: int = 12, _=Depends(P.require("logs")), db=Depends(get_db)):
    """Recent worker runs with their stage timings and per-user durations (for the 任务运行时间线 gantt)."""
    from .worker import run_view
    rows = []
    for r in db.scalars(select(WorkerRun).order_by(WorkerRun.id.desc()).limit(max(1, min(limit, 50)))):
        v = run_view(r, detail=False)
        v["stages"] = [_stage_view(s) for s in v["stages"]]
        v["users"] = [{"user": u.get("user"), "status": u.get("status"), "seconds": u.get("seconds"), "items_out": u.get("items_out")}
                      for u in (r.users or [])]
        v["events"] = dict(Counter(lv for lv, in db.execute(select(LogEvent.level).where(LogEvent.run_id == r.id))))
        rows.append(v)
    running = db.scalar(select(WorkerRun.id).where(WorkerRun.status == "running").limit(1))
    return {"runs": rows, "running": running}


def run_now() -> None:
    """A full manual pipeline run in this process (what `newsd worker run-once` does). Module-level for tests."""
    from .worker import tick
    tick("manual")


@router.post("/runs/trigger")
def trigger_run(admin=Depends(require_admin), db=Depends(get_db)):
    if db.scalar(select(WorkerRun.id).where(WorkerRun.status == "running", WorkerRun.started_at >= now() - timedelta(hours=3)).limit(1)):
        raise HTTPException(409, "a run is already in progress")
    if not _start("run", run_now):
        raise HTTPException(409, "a run is already in progress")
    ev(db, "INFO", "scheduler", "管理员手动触发运行", user=admin)
    db.commit()
    return {"started": True}


# ------------------------------------------------------------------------------------------------ overview

@router.get("/overview")
def overview(_=Depends(require_admin), db=Depends(get_db)):
    t = now()
    day = t.replace(hour=0, minute=0, second=0, microsecond=0)
    users = list(db.scalars(select(User)))
    mem = _memberships(db)
    n = _counts(db)
    groups = [group_view(g, n.get(g.id, 0)) for g in db.scalars(select(UserGroup).order_by(UserGroup.position, UserGroup.id))]
    days = [(t - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(13, -1, -1)]
    per_day = dict(db.execute(select(UserActivity.day, func.count()).where(UserActivity.day >= days[0]).group_by(UserActivity.day)).all())
    act7 = db.scalar(select(func.count(func.distinct(UserActivity.user_id))).where(UserActivity.day >= days[-7])) or 0
    new7 = sum(1 for u in users if u.created_at >= t - timedelta(days=7))
    inv = sum(1 for i in db.scalars(select(Invite).where(Invite.used_by.is_(None))) if _invite_status(i) == "unused")
    H = health_rows(db)
    hc = Counter(h["st"] for h in H)
    today_items = db.scalar(select(func.count(func.distinct(PoolItemSource.item_id))).where(PoolItemSource.fetched_at >= day)) or 0
    yest_items = db.scalar(select(func.count(func.distinct(PoolItemSource.item_id))).where(PoolItemSource.fetched_at >= day - timedelta(days=1),
                                                                                         PoolItemSource.fetched_at < day)) or 0
    runs = list(db.scalars(select(WorkerRun).where(WorkerRun.started_at >= day)))
    model_calls = sum(int(u.get("model_requests") or 0) for r in runs for u in (r.users or []))
    failovers = db.scalar(select(func.count()).select_from(LogEvent).where(LogEvent.at >= day, LogEvent.module == "llm",
                                                                           LogEvent.message.like("模型切换%"))) or 0
    pushes = Counter(s for s, in db.execute(select(PushDelivery.status).where(PushDelivery.created_at >= day, PushDelivery.kind == "scheduled")))
    done = pushes.get("ok", 0) + pushes.get("failed", 0)
    alerts = []
    for h in sorted((h for h in H if h["st"] in ("err", "warn")), key=lambda h: h["st"] != "err")[:6]:
        alerts.append({"kind": h["st"], "tag": "失败" if h["st"] == "err" else "警告", "text": f"{CATS.get(h['cat'], ('?',))[0]} · {h['name']}：{h['note']}",
                       "href": f"/admin/health?cat={h['cat']}"})
    for r in runs:
        if r.status in ("failed", "partial"):
            alerts.append({"kind": "err" if r.status == "failed" else "warn", "tag": "任务",
                           "text": f"运行 #{r.id} {r.status} · {len(r.errors or [])} 个错误", "href": f"/admin/logs?run={r.id}&level=WARN,ERROR"})
    if pushes.get("failed"):
        alerts.append({"kind": "err", "tag": "推送", "text": f"今天 {pushes['failed']} 次推送失败", "href": "/admin/logs?mod=push&level=WARN,ERROR"})
    if inv:
        alerts.append({"kind": "plain", "tag": "邀请", "text": f"{inv} 个邀请未使用", "href": "/admin/users?status=invited"})
    ung = sum(1 for u in users if not mem.get(u.id))
    if ung and groups:
        alerts.append({"kind": "plain", "tag": "分组", "text": f"{ung} 人未分组", "href": "/admin/users?g=none"})
    return {
        "users": len(users), "new_7d": new7, "active_7d": act7, "disabled": sum(1 for u in users if not u.active), "invites": inv,
        "groups": groups, "ungrouped": ung, "trend": [{"day": d, "n": per_day.get(d, 0)} for d in days],
        "sources_enabled": sum(1 for h in H if h["st"] != "off"), "health": {k: hc.get(k, 0) for k in ("ok", "warn", "err", "off")},
        "cats": len({h["cat"] for h in H if h["st"] != "off"}), "items_today": today_items, "items_yesterday": yest_items,
        "model_calls": model_calls, "failovers": failovers, "runs_today": len(runs),
        "runs_failed": sum(1 for r in runs if r.status == "failed"),
        "push_rate": round(pushes.get("ok", 0) / done * 100, 1) if done else None, "alerts": alerts[:10]}
