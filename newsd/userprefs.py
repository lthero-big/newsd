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
# The 14 themes of the accepted v2 demo: id -> (name, bg, accent, scheme). Default: the editorial look.
THEMES = {
    "arch-editorial": ("报刊编辑风", "#f6f2ea", "#b3261e", "light"), "arch-brutal": ("新粗野主义", "#fff8e7", "#c8340c", "light"),
    "arch-swiss": ("瑞士网格", "#f3f3ef", "#d0021b", "light"), "arch-bento": ("Bento 便当格", "#eceef2", "#3048d8", "light"),
    "arch-crt": ("终端 CRT", "#070b08", "#3dff8b", "dark"), "arch-aurora": ("极光毛玻璃", "#0c0a18", "#9d8cff", "dark"),
    "bubble-poster": ("泡泡字海报", "#fff4e0", "#ff5d8f", "light"), "infinite-canvas": ("无限画布", "#f6f6f3", "#a855f7", "light"),
    "magnetic-dock": ("磁吸 Dock", "#e9edf5", "#4f7cff", "light"), "multistate": ("多状态按钮", "#fafafa", "#22c55e", "light"),
    "now-playing": ("播客暖棕", "#2a1d17", "#f4a259", "dark"), "one-accent": ("单一强调色", "#151515", "#f97316", "dark"),
    "pricing-toggle": ("套餐切换", "#f5f7fa", "#f26207", "light"), "taste-moat": ("品味护城河", "#0b0b10", "#d4ff3a", "dark"),
}
PALETTES = {k: v[0] for k, v in THEMES.items()}
DEFAULT_PALETTE = "arch-editorial"
# v1 palettes (and the demo's short aliases) keep working; they map onto the closest v2 theme.
LEGACY = {"indigo": "arch-bento", "emerald": "multistate", "amber": "now-playing", "rose": "bubble-poster",
          "slate": "arch-swiss", "signal": "taste-moat", "paper": "pricing-toggle", "ember": "one-accent", "dock": "magnetic-dock"}
THEME_COOKIE = "newsd_theme"  # lets the login/register pages follow the last used theme
FEEDBACK_MODES = ("both", "like")  # ♥ + 👎, or ♥ only
MOTION = ("on", "off")


def normalize_theme(t: str | None) -> str | None:
    t = LEGACY.get(t or "", t)
    return t if t in THEMES else None


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
    return normalize_theme(p) or DEFAULT_PALETTE


class ThemeIn(BaseModel):
    palette: str


@router.get("/me/theme")
def get_theme(user=Depends(require_user), db=Depends(get_db)):
    return {"palette": get_palette(db, user.id), "palettes": PALETTES,
            "themes": [{"id": k, "name": v[0], "bg": v[1], "accent": v[2], "scheme": v[3]} for k, v in THEMES.items()]}


@router.put("/me/theme")
def put_theme(body: ThemeIn, response: Response, user=Depends(require_user), db=Depends(get_db)):
    t = normalize_theme(body.palette)
    if t is None:
        raise HTTPException(400, f"palette must be one of {sorted(THEMES)}")
    put_setting(db, user.id, "theme", {"palette": t})
    response.set_cookie(THEME_COOKIE, t, max_age=365 * 86400, samesite="lax")
    return {"palette": t}


def get_ui(db, user_id: int) -> dict:
    v = get_setting(db, user_id, "ui")
    v = v if isinstance(v, dict) else {}
    return {"feedback": v.get("feedback") if v.get("feedback") in FEEDBACK_MODES else "both",
            "motion": v.get("motion") if v.get("motion") in MOTION else "on"}


class UiIn(BaseModel):
    feedback: str | None = None
    motion: str | None = None


@router.get("/me/ui")
def ui_get(user=Depends(require_user), db=Depends(get_db)):
    return get_ui(db, user.id)


@router.put("/me/ui")
def ui_put(body: UiIn, user=Depends(require_user), db=Depends(get_db)):
    cur = get_ui(db, user.id)
    if body.feedback is not None:
        if body.feedback not in FEEDBACK_MODES:
            raise HTTPException(400, f"feedback must be one of {FEEDBACK_MODES}")
        cur["feedback"] = body.feedback
    if body.motion is not None:
        if body.motion not in MOTION:
            raise HTTPException(400, f"motion must be one of {MOTION}")
        cur["motion"] = body.motion
    put_setting(db, user.id, "ui", cur)
    return cur
