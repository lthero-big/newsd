"""Model settings API: providers, per-user model config (key write-only), test connection, processing profiles."""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from newsd.auth import get_db, require_user

from . import keys as llm_keys
from .client import KEYLESS, PROVIDER_LABELS, PROVIDERS, LLMClient, LLMError, ModelConfig, base_url
from .profiles import all_profiles, get_picks, set_picks

router = APIRouter(prefix="/api")
TEST_LIMIT = 6  # test-connection calls per user per minute
_test_calls: dict[int, deque] = defaultdict(deque)


@router.get("/llm/providers")
def providers(user=Depends(require_user)):
    return [{"provider": p, "label": PROVIDER_LABELS[p], "needs_key": p not in KEYLESS, "available": bool(base_url(p))}
            for p in PROVIDERS]


class ModelIn(BaseModel):
    provider: str
    model: str = Field(min_length=1, max_length=128)
    api_key: str | None = Field(default=None, max_length=512)  # omit to keep the stored key, "" to remove it
    translate_to: str | None = None


@router.get("/me/llm")
def get_model(user=Depends(require_user), db=Depends(get_db)):
    return llm_keys.public_settings(db, user.id)


@router.put("/me/llm")
def put_model(body: ModelIn, user=Depends(require_user), db=Depends(get_db)):
    try:
        return llm_keys.save_settings(db, user.id, body.provider, body.model, body.api_key, body.translate_to)
    except LLMError as exc:
        db.rollback()
        raise HTTPException(400, exc.args[0])


@router.delete("/me/llm")
def delete_model(user=Depends(require_user), db=Depends(get_db)):
    llm_keys.delete_settings(db, user.id)
    return {"ok": True}


class TestIn(BaseModel):
    provider: str | None = None
    model: str | None = Field(default=None, max_length=128)
    api_key: str | None = Field(default=None, max_length=512)


@router.post("/me/llm/test")
async def test_model(body: TestIn | None = None, user=Depends(require_user), db=Depends(get_db)):
    """Test the posted provider/model/key (not saved), falling back to the stored values for missing fields."""
    q = _test_calls[user.id]
    now = time.monotonic()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= TEST_LIMIT:
        raise HTTPException(429, "too many tests; wait a minute")
    q.append(now)
    body = body or TestIn()
    stored = llm_keys.model_config(db, user.id)
    pub = llm_keys.public_settings(db, user.id)
    provider = body.provider or pub["provider"]
    model = body.model or pub["model"]
    if body.api_key:
        key = body.api_key.strip()
    else:  # reuse the stored key only for the provider it was saved for
        key = stored.api_key if stored is not None and stored.provider == provider else ""
    if not provider or not model:
        raise HTTPException(400, "provider and model are required")
    try:
        cfg = ModelConfig(provider, model.strip(), key)
        cfg.validate()
    except LLMError as exc:
        return {"ok": False, "kind": exc.kind, "error": exc.args[0], "provider": provider, "model": model}
    async with LLMClient(cfg, retries=0, timeout=20.0, rpm=0, transport=llm_keys.TRANSPORT) as client:
        return await client.test_connection()


@router.get("/profiles")
def list_profiles(user=Depends(require_user)):
    return [p.public() for p in all_profiles().values()]


class PickIn(BaseModel):
    name: str
    threshold: float | None = Field(default=None, ge=0, le=10)


@router.get("/me/profiles")
def my_profiles(user=Depends(require_user), db=Depends(get_db)):
    return get_picks(db, user.id)


@router.put("/me/profiles")
def put_profiles(body: list[PickIn], user=Depends(require_user), db=Depends(get_db)):
    try:
        return set_picks(db, user.id, [p.model_dump() for p in body])
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
