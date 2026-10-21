"""M3 per-user display and push preferences, stored in ``user_settings``.

* theme: one of a fixed palette (the old weekly theme rotation is gone; the user picks a colour and it stays).
* push: when and where the daily issue should be delivered. Only stored here; M4 does the delivery
  (bot tokens / SMTP are server-side, the user only supplies the destination).
"""
from __future__ import annotations

import re
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from .auth import get_db, require_user
from .models import UserSetting

router = APIRouter(prefix="/api")
PALETTES = {"indigo": "靛蓝", "emerald": "翠绿", "amber": "琥珀", "rose": "玫红", "slate": "石墨"}
DEFAULT_PALETTE = "indigo"
THEME_COOKIE = "newsd_theme"  # lets the login/register pages follow the last used palette
CHANNELS = ("email", "telegram", "webhook")
DEFAULT_PUSH = {"enabled": False, "time": "08:00", "timezone": "Asia/Shanghai",
                "channels": {c: {"enabled": False, "target": ""} for c in CHANNELS}}


def get_setting(db, user_id: int, key: str):
    row = db.scalar(select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.key == key))
    return row.value if row is not None else None


def put_setting(db, user_id: int, key: str, value) -> None:
    row = db.scalar(select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.key == key))
    if row is None:
        row = UserSetting(user_id=user_id, key=key); db.add(row)
    row.value = value
    db.commit()


def get_palette(db, user_id: int) -> str:
    v = get_setting(db, user_id, "theme")
    p = v.get("palette") if isinstance(v, dict) else None
    return p if p in PALETTES else DEFAULT_PALETTE


class ThemeIn(BaseModel):
    palette: str


@router.get("/me/theme")
def get_theme(user=Depends(require_user), db=Depends(get_db)):
    return {"palette": get_palette(db, user.id), "palettes": PALETTES}


@router.put("/me/theme")
def put_theme(body: ThemeIn, response: Response, user=Depends(require_user), db=Depends(get_db)):
    if body.palette not in PALETTES:
        raise HTTPException(400, f"palette must be one of {sorted(PALETTES)}")
    put_setting(db, user.id, "theme", {"palette": body.palette})
    response.set_cookie(THEME_COOKIE, body.palette, max_age=365 * 86400, samesite="lax")
    return {"palette": body.palette}


# ------------------------------------------------------------------------------------------------ push

class ChannelIn(BaseModel):
    enabled: bool = False
    target: str = Field(default="", max_length=512)


class PushIn(BaseModel):
    enabled: bool = False
    time: str = "08:00"
    timezone: str = "Asia/Shanghai"
    channels: dict[str, ChannelIn] = Field(default_factory=dict)


_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_TG = re.compile(r"^(-?\d{3,20}|@[A-Za-z][\w]{3,31})$")
_MAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")


def clean_push(body: PushIn) -> dict:
    if not _TIME.match(body.time):
        raise ValueError("time must be HH:MM")
    try:
        ZoneInfo(body.timezone)
    except Exception:  # noqa: BLE001
        raise ValueError("unknown timezone")
    unknown = set(body.channels) - set(CHANNELS)
    if unknown:
        raise ValueError(f"unknown channel: {sorted(unknown)}")
    channels = {}
    for name in CHANNELS:
        ch = body.channels.get(name) or ChannelIn()
        target = ch.target.strip()
        if ch.enabled or target:
            if name == "email" and not _MAIL.match(target):
                raise ValueError("email: invalid address")
            if name == "telegram" and not _TG.match(target):
                raise ValueError("telegram: use a numeric chat id or @channel")
            if name == "webhook" and not re.match(r"^https?://[^\s/]+", target):
                raise ValueError("webhook: must be an http(s) URL")
        channels[name] = {"enabled": ch.enabled and bool(target), "target": target}
    return {"enabled": body.enabled, "time": body.time, "timezone": body.timezone, "channels": channels}


def get_push(db, user_id: int) -> dict:
    v = get_setting(db, user_id, "push")
    out = {**DEFAULT_PUSH, "channels": {c: dict(DEFAULT_PUSH["channels"][c]) for c in CHANNELS}}
    if isinstance(v, dict):
        out.update({k: v[k] for k in ("enabled", "time", "timezone") if k in v})
        for c in CHANNELS:
            if isinstance(v.get("channels", {}).get(c), dict):
                out["channels"][c].update(v["channels"][c])
    return out


@router.get("/me/push")
def my_push(user=Depends(require_user), db=Depends(get_db)):
    return get_push(db, user.id)


@router.put("/me/push")
def put_push(body: PushIn, user=Depends(require_user), db=Depends(get_db)):
    try:
        clean = clean_push(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    put_setting(db, user.id, "push", clean)
    return get_push(db, user.id)
