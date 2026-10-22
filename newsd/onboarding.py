"""M3 optional onboarding: after registration the user picks interests and subscribes to a preset group of sources
in one click. An interest = a processing profile (M2-4) + a list of catalog source keys that usually feed it.
Source keys that are not in the catalog (or disabled) are skipped, so presets keep working on trimmed catalogs."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from .auth import get_db, require_user
from .llm.profiles import all_profiles, get_picks, set_picks
from .models import Source, Subscription
from .userprefs import get_setting, put_setting

PRESETS = {
    "ai-news": ["rss:openai-news", "rss:google-ai-blog", "rss:hf-blog", "rsshub:rsshub-qbitai", "rsshub:rsshub-qbitai-llm",
                "rsshub:rsshub-infoq-ai", "rsshub:rsshub-anthropic", "plugin:hn", "xq:models"],
    "research": ["rsshub:rsshub-hf-papers", "plugin:hf_trending", "rss:hf-blog"],
    "open-source": ["rss:comfyui-releases", "rss:llamacpp-releases", "rss:ollama-releases", "rss:vllm-releases",
                    "rss:transformers-releases", "rss:diffusers-releases", "plugin:github_search", "plugin:ossinsight",
                    "reddit:LocalLLaMA"],
    "creative-ai": ["reddit:StableDiffusion", "reddit:comfyui", "rss:comfyui-releases", "rss:diffusers-releases"],
    "agents": ["xq:agents", "xq:prompts", "plugin:github_search"],
}
# Followed X accounts join an interest by their catalog group (config/collect-sources.json x.accounts[].group), so the
# default recommendations follow the catalog. 未分组 accounts stay in the catalog / default template only.
X_GROUPS = {
    "ai-news": ("官方·LLM", "特别关注", "中文区", "英文区"),
    "agents": ("特别关注",),
    "creative-ai": ("官方·图像/工作流",),
}
router = APIRouter(prefix="/api")


def preset_groups(db) -> list[dict]:
    rows = db.execute(select(Source.key, Source.kind, Source.config).where(Source.enabled.is_(True)).order_by(Source.id)).all()
    live = {k for k, _, _ in rows}
    x_by_group: dict[str, list[str]] = {}
    for k, kind, cfg in rows:  # catalog order
        if kind == "x_account":
            x_by_group.setdefault((cfg or {}).get("group") or "未分组", []).append(k)
    out = []
    for name, prof in all_profiles().items():
        keys = [k for k in PRESETS.get(name, []) if k in live]
        keys += [k for g in X_GROUPS.get(name, ()) for k in x_by_group.get(g, []) if k not in keys]
        out.append({"name": name, "title": prof.title, "sections": prof.sections, "sources": keys})
    return out


def is_onboarded(db, user_id: int) -> bool:
    if get_setting(db, user_id, "onboarded"):
        return True
    return db.scalar(select(Subscription.id).where(Subscription.user_id == user_id).limit(1)) is not None


def finish(db, user_id: int) -> None:
    put_setting(db, user_id, "onboarded", True)


def apply_interests(db, user_id: int, names: list[str]) -> dict:
    groups = {g["name"]: g for g in preset_groups(db)}
    bad = [n for n in names if n not in groups]
    if bad:
        raise ValueError(f"unknown interest: {bad}")
    keys = list(dict.fromkeys(k for n in names for k in groups[n]["sources"]))
    have = set(db.scalars(select(Subscription.source_id).where(Subscription.user_id == user_id)))
    added = 0
    for src in db.scalars(select(Source).where(Source.key.in_(keys))) if keys else []:
        if src.id not in have:
            db.add(Subscription(user_id=user_id, source_id=src.id)); added += 1
    db.commit()
    if names:
        old = {p["name"]: p["threshold"] for p in get_picks(db, user_id)}
        set_picks(db, user_id, [{"name": n, "threshold": old.get(n)} for n in names])
    finish(db, user_id)
    return {"added_sources": added, "interests": names}


class InterestsIn(BaseModel):
    interests: list[str] = []


@router.get("/onboarding")
def onboarding_info(user=Depends(require_user), db=Depends(get_db)):
    return {"done": is_onboarded(db, user.id), "groups": preset_groups(db)}


@router.post("/onboarding")
def onboarding_apply(body: InterestsIn, user=Depends(require_user), db=Depends(get_db)):
    """Subscribe to the preset sources of the chosen interests and select their processing profiles. An empty list
    just marks onboarding done (skip)."""
    try:
        return apply_interests(db, user.id, body.interests)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
