"""Model settings API: presets, per-user providers (key write-only, priority, enable, failover options),
test connection, processing profiles. The v1 single-model endpoints (/api/me/llm) act on the primary provider."""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from newsd.auth import get_db, require_user

from . import keys as llm_keys
from . import presets
from .client import KEYLESS, PROVIDER_LABELS, PROVIDERS, LLMClient, LLMError, base_url
from .profiles import all_profiles, get_picks, set_picks

router = APIRouter(prefix="/api")
TEST_LIMIT = 6  # test-connection calls per user per minute
TEST_TIMEOUT = 20.0
_test_calls: dict[int, deque] = defaultdict(deque)


def _rate(user_id: int) -> None:
    q = _test_calls[user_id]
    now = time.monotonic()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= TEST_LIMIT:
        raise HTTPException(429, "too many tests; wait a minute")
    q.append(now)


async def _run_test(cfg) -> dict:
    async with LLMClient(cfg, retries=0, timeout=TEST_TIMEOUT, rpm=0, transport=llm_keys.TRANSPORT) as client:
        return await client.test_connection()


def _fail(exc: LLMError, **kw) -> dict:
    return {"ok": False, "kind": exc.kind, "error": exc.args[0], **kw}


@router.get("/llm/providers")
def providers(user=Depends(require_user)):
    """v1 list (every provider id, legacy ones included)."""
    return [{"provider": p, "label": PROVIDER_LABELS[p], "needs_key": p not in KEYLESS, "available": bool(base_url(p)),
             "hidden": presets.PRESETS[p].hidden} for p in PROVIDERS]


@router.get("/llm/presets")
def list_presets(user=Depends(require_user)):
    return {"regions": [{"id": r, "name": n} for r, n in presets.REGIONS],
            "presets": [{**p.public(), "base_url": base_url(p.id)} for p in presets.VISIBLE]}


# ------------------------------------------------------------------------------------------- providers

class ProviderIn(BaseModel):
    preset: str = Field(max_length=32)
    model: str = Field(min_length=1, max_length=128)
    name: str | None = Field(default=None, max_length=64)
    api_key: str | None = Field(default=None, max_length=512)
    base_url: str | None = Field(default=None, max_length=512)  # omit / "" = preset default
    enabled: bool = True


class ProviderPatch(BaseModel):
    preset: str | None = Field(default=None, max_length=32)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    name: str | None = Field(default=None, max_length=64)
    api_key: str | None = Field(default=None, max_length=512)  # omit to keep, "" to remove
    base_url: str | None = Field(default=None, max_length=512)  # omit to keep, "" to reset to the preset default
    enabled: bool | None = None


class OrderIn(BaseModel):
    ids: list[int] = Field(max_length=50)


class OptionsIn(BaseModel):
    translate_to: str | None = None
    retries: int | None = None
    timeout: int | None = None


class ProviderTestIn(BaseModel):
    preset: str = Field(max_length=32)
    model: str = Field(min_length=1, max_length=128)
    api_key: str | None = Field(default=None, max_length=512)
    base_url: str | None = Field(default=None, max_length=512)
    provider_id: int | None = None  # editing a saved provider: reuse its key if none is posted


@router.get("/me/llm/providers")
def my_providers(user=Depends(require_user), db=Depends(get_db)):
    return llm_keys.overview(db, user.id)


@router.post("/me/llm/providers")
def add_provider(body: ProviderIn, user=Depends(require_user), db=Depends(get_db)):
    try:
        return llm_keys.add_provider(db, user.id, preset=body.preset, model=body.model, api_key=body.api_key,
                                     base_url=body.base_url, name=body.name, enabled=body.enabled)
    except LLMError as exc:
        db.rollback()
        raise HTTPException(400, exc.args[0])


@router.patch("/me/llm/providers/{pid}")
def patch_provider(pid: int, body: ProviderPatch, user=Depends(require_user), db=Depends(get_db)):
    fields = body.model_dump(exclude_unset=True)
    if "base_url" in fields and fields["base_url"] is None:
        fields["base_url"] = ""
    try:
        return llm_keys.update_provider(db, user.id, pid, **fields)
    except LookupError:
        raise HTTPException(404, "no such provider")
    except LLMError as exc:
        db.rollback()
        raise HTTPException(400, exc.args[0])


@router.delete("/me/llm/providers/{pid}")
def delete_provider(pid: int, user=Depends(require_user), db=Depends(get_db)):
    try:
        llm_keys.delete_provider(db, user.id, pid)
    except LookupError:
        raise HTTPException(404, "no such provider")
    return {"ok": True}


@router.put("/me/llm/order")
def put_order(body: OrderIn, user=Depends(require_user), db=Depends(get_db)):
    try:
        return llm_keys.reorder(db, user.id, body.ids)
    except LLMError as exc:
        db.rollback()
        raise HTTPException(400, exc.args[0])


@router.put("/me/llm/options")
def put_options(body: OptionsIn, user=Depends(require_user), db=Depends(get_db)):
    try:
        return llm_keys.set_options(db, user.id, translate_to=body.translate_to, retries=body.retries,
                                    timeout=body.timeout)
    except LLMError as exc:
        db.rollback()
        raise HTTPException(400, exc.args[0])


@router.post("/me/llm/providers/test")
async def test_unsaved(body: ProviderTestIn, user=Depends(require_user), db=Depends(get_db)):
    """Test a provider from the add/edit form without saving it."""
    _rate(user.id)
    key = (body.api_key or "").strip()
    if not key and body.provider_id is not None:
        try:
            row = llm_keys._get(db, user.id, body.provider_id)
        except LookupError:
            raise HTTPException(404, "no such provider")
        if row.preset == body.preset:  # a stored key is never sent to a different provider
            key, _ = llm_keys._key(row)
    admin = user.role == "admin"
    try:
        base = llm_keys._clean_base(body.preset, body.base_url, admin)
        cfg = llm_keys.build_config(body.preset, body.model, key, base, admin=admin)
    except LLMError as exc:
        return _fail(exc, provider=body.preset, model=body.model)
    return await _run_test(cfg)


@router.post("/me/llm/providers/{pid}/test")
async def test_saved(pid: int, user=Depends(require_user), db=Depends(get_db)):
    """Test a saved provider and remember the result (shown on its card)."""
    _rate(user.id)
    try:
        cfg = llm_keys.stored_config(db, user.id, pid)
    except LookupError:
        raise HTTPException(404, "no such provider")
    except LLMError as exc:
        result = _fail(exc)
    else:
        result = await _run_test(cfg)
    llm_keys.record_test(db, user.id, pid, result)
    return {**result, "id": pid}


# ------------------------------------------------------------------------------------------- v1 (primary provider)

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
    """Test the posted provider/model/key (not saved), falling back to the primary provider for missing fields."""
    _rate(user.id)
    body = body or TestIn()
    primary = llm_keys._primary(db, user.id)
    provider = body.provider or (primary.preset if primary else None)
    model = body.model or (primary.model if primary else None)
    if not provider or not model:
        raise HTTPException(400, "provider and model are required")
    if body.api_key:
        key = body.api_key.strip()
    else:  # reuse the stored key only for the provider it was saved for
        key = llm_keys._key(primary)[0] if primary is not None and primary.preset == provider else ""
    base = primary.base_url if primary is not None and primary.preset == provider else None
    try:
        cfg = llm_keys.build_config(provider, model, key, base, admin=user.role == "admin")
    except LLMError as exc:
        return _fail(exc, provider=provider, model=model)
    return await _run_test(cfg)


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
