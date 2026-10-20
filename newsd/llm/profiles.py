"""Processing profiles: one Markdown prompt per content category (does the item belong, 0-10 rubric,
which section to file it under) plus a default threshold. Built-ins live in ``newsd/llm/profiles/*.md``;
an admin can add more with ``NEWSD_PROFILES_DIR``. Users pick profiles and adjust thresholds
(user setting ``profiles`` = [{"name", "threshold"}])."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select

from newsd.models import UserSetting

BUILTIN_DIR = Path(__file__).parent / "profiles"
DEFAULT_PICKS = ("ai-news",)
SETTING_KEY = "profiles"


@dataclass(frozen=True)
class Profile:
    name: str
    title: str
    threshold: float
    sections: tuple[str, ...]
    prompt: str  # Markdown body: belongs / rubric / sections

    def public(self) -> dict:
        return {"name": self.name, "title": self.title, "threshold": self.threshold, "sections": list(self.sections),
                "prompt": self.prompt}


def parse_profile(text: str, fallback_name: str = "") -> Profile:
    meta, body = {}, text
    if text.startswith("---"):
        head, _, body = text[3:].partition("\n---")
        for line in head.strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        body = body.lstrip("\n")
    name = meta.get("name") or fallback_name
    if not name:
        raise ValueError("profile needs a name")
    sections = tuple(s.strip() for s in meta.get("sections", "").split(",") if s.strip()) or ("其他",)
    threshold = float(meta.get("threshold", 6))
    if not 0 <= threshold <= 10:
        raise ValueError(f"profile {name}: threshold must be 0-10")
    return Profile(name=name, title=meta.get("title", name), threshold=threshold, sections=sections, prompt=body.strip())


@lru_cache(maxsize=4)
def _load(dirs: tuple[str, ...]) -> dict[str, Profile]:
    out: dict[str, Profile] = {}
    for d in dirs:
        p = Path(d)
        if not p.is_dir():
            continue
        for f in sorted(p.glob("*.md")):
            prof = parse_profile(f.read_text(encoding="utf-8"), f.stem)
            out[prof.name] = prof
    return out


def all_profiles() -> dict[str, Profile]:
    dirs = [str(BUILTIN_DIR)]
    extra = os.environ.get("NEWSD_PROFILES_DIR", "").strip()
    if extra:
        dirs.append(extra)
    return _load(tuple(dirs))


def get_picks(db, user_id: int) -> list[dict]:
    """The user's picks as [{"name", "title", "threshold", "default_threshold"}]; defaults when unset."""
    profs = all_profiles()
    row = db.scalar(select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.key == SETTING_KEY))
    raw = row.value if row is not None and isinstance(row.value, list) else [{"name": n} for n in DEFAULT_PICKS]
    out = []
    for pick in raw:
        prof = profs.get(pick.get("name")) if isinstance(pick, dict) else None
        if prof is None:
            continue  # profile removed by the admin
        thr = pick.get("threshold")
        out.append({"name": prof.name, "title": prof.title, "default_threshold": prof.threshold,
                    "threshold": float(thr) if thr is not None else prof.threshold})
    return out


def set_picks(db, user_id: int, picks: list[dict]) -> list[dict]:
    profs = all_profiles()
    clean, seen = [], set()
    for p in picks:
        name = p.get("name")
        if name not in profs:
            raise ValueError(f"unknown profile {name!r}")
        if name in seen:
            raise ValueError(f"duplicate profile {name!r}")
        seen.add(name)
        thr = p.get("threshold")
        if thr is not None and not (0 <= float(thr) <= 10):
            raise ValueError("threshold must be between 0 and 10")
        clean.append({"name": name, "threshold": None if thr is None else float(thr)})
    row = db.scalar(select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.key == SETTING_KEY))
    if row is None:
        row = UserSetting(user_id=user_id, key=SETTING_KEY)
        db.add(row)
    row.value = clean
    db.commit()
    return get_picks(db, user_id)


def system_prompt(prof: Profile) -> str:
    sections = "、".join(prof.sections)
    return (
        f"你是新闻日报的编辑，负责「{prof.title}」这一类内容的筛选和打分。\n\n{prof.prompt}\n\n"
        "## 输出格式\n\n"
        "对输入里的每一条都给出判断，只输出 JSON，不要任何解释或代码块：\n"
        '{"items": [{"id": <输入里的 id>, "belongs": true/false, "score": 0-10 的数字, '
        f'"section": "{sections} 之一", "reason": "一句话中文理由"}}]}}\n'
        "不属于本类的条目 belongs 为 false，score 给 0。"
    )
