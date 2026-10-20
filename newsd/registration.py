"""Registration policy.

Modes (set NEWSD_REGISTRATION as the default; an admin can switch it at runtime, stored in global_settings):
  invite - sign-up needs a single-use invite code (default)
  open   - anyone can sign up; an invite code is optional and still accepted (and consumed) if given
  closed - no new accounts; admins create users from the CLI
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select

from .models import GlobalSetting, Invite, User, now
from .security import hash_password, token_hash
from .settings import get_settings

REG_MODES = ("invite", "open", "closed")
_KEY = "registration_mode"


def get_registration_mode(db) -> str:
    row = db.get(GlobalSetting, _KEY)
    if row is not None and row.value in REG_MODES:
        return row.value
    mode = (get_settings().registration or "invite").strip().lower()
    return mode if mode in REG_MODES else "invite"


def set_registration_mode(db, mode: str) -> None:
    if mode not in REG_MODES:
        raise ValueError(mode)
    row = db.get(GlobalSetting, _KEY)
    if row is None:
        db.add(GlobalSetting(key=_KEY, value=mode))
    else:
        row.value = mode
    db.commit()


def _find_invite(db, code: str):
    inv = db.scalar(select(Invite).where(Invite.code_hash == token_hash(code), Invite.used_by.is_(None)))
    if not inv or (inv.expires_at and inv.expires_at < now()):
        return None
    return inv


def register_user(db, username: str, password: str, invite: str = "") -> User:
    mode = get_registration_mode(db)
    if mode == "closed":
        raise HTTPException(403, "registration closed")
    username = username.strip()
    invite = (invite or "").strip()
    inv = None
    if mode == "invite" or invite:
        # invite mode requires a code; open mode accepts one but rejects a wrong one instead of silently ignoring it
        inv = _find_invite(db, invite) if invite else None
        if inv is None:
            raise HTTPException(403, "invalid invite")
    if len(username) < 2 or len(username) > 64 or len(password) < 8:
        raise HTTPException(400, "username >= 2 chars, password >= 8 chars")
    if db.scalar(select(User).where(User.username == username)):
        raise HTTPException(409, "username taken")
    u = User(username=username, password_hash=hash_password(password))
    db.add(u)
    db.flush()
    if inv:
        inv.used_by = u.id
    db.commit()
    return u
