"""M3 per-user display and push preferences, stored in ``user_settings``.

* theme: one of a fixed palette (the old weekly theme rotation is gone; the user picks a colour and it stays).
* push: moved to newsd.push (M4: channel config, delivery, test push); ``get_push`` is re-exported for the pages.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select

from .auth import get_db, require_user
from .models import UserSetting
from .push.prefs import CHANNELS, get_push  # noqa: F401  (re-exported for pages.py)

router = APIRouter(prefix="/api")
PALETTES = {"indigo": "靛蓝", "emerald": "翠绿", "amber": "琥珀", "rose": "玫红", "slate": "石墨"}
DEFAULT_PALETTE = "indigo"
THEME_COOKIE = "newsd_theme"  # lets the login/register pages follow the last used palette


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
