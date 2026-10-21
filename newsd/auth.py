from __future__ import annotations

from datetime import timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy import delete, func, select

from .db import SessionLocal
from .models import ApiToken, LoginFailure, User, UserActivity, WebSession, now
from .security import new_token, token_hash, verify_password
from .settings import get_settings

COOKIE = "newsd_session"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def is_locked(db, ip: str) -> bool:
    s = get_settings()
    since = now() - timedelta(minutes=s.login_lock_minutes)
    n = db.scalar(select(func.count()).select_from(LoginFailure).where(LoginFailure.ip == ip, LoginFailure.at >= since))
    return (n or 0) >= s.login_max_failures


def authenticate(db, ip: str, username: str, password: str) -> User | None:
    if is_locked(db, ip):
        raise HTTPException(429, "too many failed logins; try again later")
    user = db.scalar(select(User).where(User.username == username))
    if not user or not user.active or not verify_password(password, user.password_hash):
        db.add(LoginFailure(ip=ip))
        db.commit()
        return None
    db.execute(delete(LoginFailure).where(LoginFailure.ip == ip))
    db.commit()
    return user


def create_session(db, user: User) -> str:
    tok = new_token("ns")
    db.add(WebSession(token_hash=token_hash(tok), user_id=user.id,
                      expires_at=now() + timedelta(days=get_settings().session_days)))
    db.commit()
    return tok


_seen: dict[tuple, tuple[str, object]] = {}
TOUCH_SECONDS = 300


def touch(db, u: User) -> None:
    """3c: keep users.last_seen_at roughly current (at most one write per user per 5 minutes per process) and
    record one user_activity row per UTC day, for the admin overview's active-user numbers."""
    t = now()
    day = t.strftime("%Y-%m-%d")
    k = (id(db.get_bind()), u.id)
    prev = _seen.get(k)
    if prev and prev[0] == day and (t - prev[1]).total_seconds() < TOUCH_SECONDS:
        return
    _seen[k] = (day, t)
    try:
        u.last_seen_at = t
        if (not prev or prev[0] != day) and db.get(UserActivity, (u.id, day)) is None:
            db.add(UserActivity(user_id=u.id, day=day))
        db.commit()
    except Exception:  # noqa: BLE001  (a concurrent insert of the same day row; activity is best effort)
        db.rollback()


def current_user(request: Request, db=Depends(get_db)) -> User | None:
    auth = request.headers.get("authorization", "")
    raw = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-news-api-key", "")
    if raw:
        t = db.scalar(select(ApiToken).where(ApiToken.token_hash == token_hash(raw)))
        if t:
            t.last_used_at = now()
            db.commit()
            u = db.get(User, t.user_id)
            if u and u.active:
                touch(db, u)
                return u
            return None
        return None  # unknown keys are anonymous, never a new namespace
    cookie = request.cookies.get(COOKIE)
    if cookie:
        s = db.scalar(select(WebSession).where(WebSession.token_hash == token_hash(cookie)))
        if s and s.expires_at > now():
            u = db.get(User, s.user_id)
            if u and u.active:
                touch(db, u)
                return u
            return None
    return None


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(401, "login required")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    return user
