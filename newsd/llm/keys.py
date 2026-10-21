"""Per-user model settings (DEPTAI-216: several providers, priority order, automatic failover).

Each provider is one ``llm_providers`` row: a preset (:mod:`newsd.llm.presets`), an optional custom
base URL, a model, the API key (encrypted with the server master key via ``newsd.security``; only
the last 4 chars are ever shown), an enabled flag and a priority (0 = primary). Per-user options
(``translate_to``, ``retries`` per provider before failing over, request ``timeout``) live in
``user_settings`` key ``llm``.

The v1 single-model API (``public_settings`` / ``save_settings`` / ``model_config``) still works and
acts on the primary provider (the first by priority)."""
from __future__ import annotations

from sqlalchemy import func, select

from newsd.models import LLMProvider, User, UserSetting, now
from newsd.security import decrypt_secret, encrypt_secret

from . import netguard, presets
from .client import KEYLESS, FailoverClient, LLMClient, LLMError, ModelConfig, base_url

SETTING_KEY = "llm"
LANGS = ("zh", "en", "ja")
RETRY_CHOICES = (0, 1, 2, 3)
DEFAULT_RETRIES = 2
DEFAULT_TIMEOUT = 60
TIMEOUT_RANGE = (10, 300)
MAX_PROVIDERS = 12
# Tests (and embedders) may set this to an httpx transport; every per-user client uses it.
TRANSPORT = None
_KEEP = object()


# ------------------------------------------------------------------------------------------- options

def _setting(db, user_id: int) -> UserSetting | None:
    return db.scalar(select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.key == SETTING_KEY))


def options(db, user_id: int) -> dict:
    row = _setting(db, user_id)
    v = row.value if row is not None and isinstance(row.value, dict) else {}
    return {"translate_to": v.get("translate_to", "zh"), "retries": v.get("retries", DEFAULT_RETRIES),
            "timeout": v.get("timeout", DEFAULT_TIMEOUT)}


def set_options(db, user_id: int, *, translate_to: str | None = None, retries: int | None = None,
                timeout: int | None = None, commit: bool = True) -> dict:
    if translate_to is not None and translate_to not in LANGS:
        raise LLMError("config", f"translate_to must be one of {LANGS}")
    if retries is not None and retries not in RETRY_CHOICES:
        raise LLMError("config", f"retries must be one of {RETRY_CHOICES}")
    if timeout is not None and not (TIMEOUT_RANGE[0] <= timeout <= TIMEOUT_RANGE[1]):
        raise LLMError("config", f"timeout must be {TIMEOUT_RANGE[0]}-{TIMEOUT_RANGE[1]} seconds")
    row = _setting(db, user_id)
    if row is None:
        row = UserSetting(user_id=user_id, key=SETTING_KEY, value={})
        db.add(row)
    v = dict(row.value) if isinstance(row.value, dict) else {}
    for k, val in (("translate_to", translate_to), ("retries", retries), ("timeout", timeout)):
        if val is not None:
            v[k] = val
    row.value = v
    if commit:
        db.commit()
    return options(db, user_id)


def translate_to(db, user_id: int) -> str:
    return options(db, user_id)["translate_to"]


# ------------------------------------------------------------------------------------------- providers

def _rows(db, user_id: int) -> list[LLMProvider]:
    return list(db.scalars(select(LLMProvider).where(LLMProvider.user_id == user_id)
                           .order_by(LLMProvider.priority, LLMProvider.id)))


def _get(db, user_id: int, pid: int) -> LLMProvider:
    row = db.get(LLMProvider, pid)
    if row is None or row.user_id != user_id:
        raise LookupError("no such provider")
    return row


def _key(row: LLMProvider | None) -> tuple[str, bool]:
    """(plaintext key, readable). Unreadable = the master key changed or the ciphertext is corrupt."""
    if row is None or not row.secret:
        return "", True
    try:
        return decrypt_secret(row.secret), True
    except Exception:
        return "", False


def _is_admin(db, user_id: int) -> bool:
    u = db.get(User, user_id)
    return bool(u is not None and u.role == "admin")


def _preset(preset_id: str) -> presets.Preset:
    p = presets.get(preset_id or "")
    if p is None:
        raise LLMError("config", f"unknown provider {preset_id!r}")
    return p


def _clean_base(preset_id: str, base: str | None, admin: bool) -> str | None:
    """None = use the preset default. A URL equal to the default is stored as None."""
    if base is None or not base.strip():
        return None
    b = netguard.normalize(base)
    if b == base_url(preset_id):
        return None
    return netguard.check(b, allow_private=admin)


def build_config(preset_id: str, model: str, api_key: str, base: str | None, *, admin: bool) -> ModelConfig:
    """Validated config (raises LLMError). Admin-owned custom URLs skip the private-address check."""
    _preset(preset_id)
    cfg = ModelConfig(preset_id, (model or "").strip(), (api_key or "").strip(), base or "", trusted=admin)
    cfg.validate()
    return cfg


def _row_config(db, row: LLMProvider, admin: bool | None = None) -> ModelConfig | None:
    key, _ = _key(row)
    if not key and row.preset not in KEYLESS:
        return None
    try:
        return build_config(row.preset, row.model, key, row.base_url,
                            admin=_is_admin(db, row.user_id) if admin is None else admin)
    except LLMError:
        return None


def public_provider(db, row: LLMProvider) -> dict:
    key, readable = _key(row)
    has_key = bool(row.secret and readable)
    p = presets.get(row.preset)
    usable = _row_config(db, row) is not None
    return {"id": row.id, "name": row.name, "preset": row.preset, "preset_name": p.name if p else row.preset,
            "short": p.short if p else "", "color": p.color if p else "#334155", "wire": p.wire if p else None,
            "model": row.model, "base_url": row.base_url or base_url(row.preset), "custom_base_url": row.base_url,
            "enabled": bool(row.enabled), "priority": row.priority, "has_key": has_key,
            "key_last4": row.key_last4 if has_key else None, "key_unreadable": not readable,
            "needs_key": row.preset not in KEYLESS, "usable": usable,
            "last_test": None if row.last_test_at is None else {
                "at": row.last_test_at.isoformat() + "Z", "ok": row.last_test_ok, "latency_ms": row.last_test_ms,
                "error": row.last_test_error}}


def overview(db, user_id: int) -> dict:
    provs = [public_provider(db, r) for r in _rows(db, user_id)]
    chain = [p for p in provs if p["enabled"] and p["usable"]]
    return {"providers": provs, "chain": [{"id": p["id"], "name": p["name"], "model": p["model"]} for p in chain],
            "options": options(db, user_id), "ai_enabled": bool(chain)}


def _last4(key: str) -> str | None:
    return key[-4:] if len(key) >= 12 else None


def add_provider(db, user_id: int, *, preset: str, model: str, api_key: str | None = None, base_url: str | None = None,
                 name: str | None = None, enabled: bool = True) -> dict:
    admin = _is_admin(db, user_id)
    if len(_rows(db, user_id)) >= MAX_PROVIDERS:
        raise LLMError("config", f"at most {MAX_PROVIDERS} providers")
    p = _preset(preset)
    base = _clean_base(preset, base_url, admin)
    key = (api_key or "").strip()
    build_config(preset, model, key, base, admin=admin)
    top = db.scalar(select(func.max(LLMProvider.priority)).where(LLMProvider.user_id == user_id))
    row = LLMProvider(user_id=user_id, name=(name or "").strip()[:64] or p.name, preset=preset, base_url=base,
                      model=model.strip(), secret=encrypt_secret(key) if key else None, key_last4=_last4(key) if key else None,
                      enabled=bool(enabled), priority=0 if top is None else top + 1)
    db.add(row)
    db.commit()
    return public_provider(db, row)


def update_provider(db, user_id: int, pid: int, *, preset=_KEEP, model=_KEEP, api_key=_KEEP, base_url=_KEEP,
                    name=_KEEP, enabled=_KEEP) -> dict:
    """api_key: omit/None keeps the stored key, "" removes it. base_url: "" or None resets to the preset default.
    A changed preset without a new base_url also resets it. Everything is validated before writing."""
    row = _get(db, user_id, pid)
    admin = _is_admin(db, user_id)
    new_preset = row.preset if preset is _KEEP or preset is None else preset
    _preset(new_preset)
    new_model = row.model if model is _KEEP or model is None else model.strip()
    if base_url is _KEEP:
        new_base = row.base_url if new_preset == row.preset else None
    else:
        new_base = _clean_base(new_preset, base_url, admin)
    old_key, _ = _key(row)
    new_key = old_key if api_key is _KEEP or api_key is None else api_key.strip()
    build_config(new_preset, new_model, new_key, new_base, admin=admin)
    if not (api_key is _KEEP or api_key is None):
        row.secret = encrypt_secret(new_key) if new_key else None
        row.key_last4 = _last4(new_key) if new_key else None
    if not (name is _KEEP or name is None):
        row.name = name.strip()[:64] or _preset(new_preset).name
    if not (enabled is _KEEP or enabled is None):
        row.enabled = bool(enabled)
    if (new_preset, new_model, new_base) != (row.preset, row.model, row.base_url) or api_key not in (_KEEP, None):
        row.last_test_at = row.last_test_ok = row.last_test_ms = row.last_test_error = None
    row.preset, row.model, row.base_url = new_preset, new_model, new_base
    row.updated_at = now()
    db.commit()
    return public_provider(db, row)


def delete_provider(db, user_id: int, pid: int) -> None:
    db.delete(_get(db, user_id, pid))
    db.commit()
    _renumber(db, user_id)


def _renumber(db, user_id: int) -> None:
    for i, r in enumerate(_rows(db, user_id)):
        r.priority = i
    db.commit()


def reorder(db, user_id: int, ids: list[int]) -> dict:
    rows = {r.id: r for r in _rows(db, user_id)}
    if sorted(ids) != sorted(rows) or len(set(ids)) != len(ids):
        raise LLMError("config", "order must list each of your providers exactly once")
    for i, pid in enumerate(ids):
        rows[pid].priority = i
    db.commit()
    return overview(db, user_id)


def stored_config(db, user_id: int, pid: int) -> ModelConfig:
    """The saved provider's config for a connection test; raises LookupError / LLMError."""
    row = _get(db, user_id, pid)
    key, readable = _key(row)
    if not readable:
        raise LLMError("config", "the stored key cannot be decrypted (server key changed); enter it again")
    return build_config(row.preset, row.model, key, row.base_url, admin=_is_admin(db, user_id))


def record_test(db, user_id: int, pid: int, result: dict) -> None:
    row = _get(db, user_id, pid)
    row.last_test_at = now()
    row.last_test_ok = bool(result.get("ok"))
    row.last_test_ms = result.get("latency_ms")
    row.last_test_error = None if result.get("ok") else f"{result.get('kind')}: {result.get('error')}"[:300]
    db.commit()


def configs_for_user(db, user_id: int) -> list[tuple[LLMProvider, ModelConfig]]:
    admin = _is_admin(db, user_id)
    out = []
    for r in _rows(db, user_id):
        if r.enabled:
            cfg = _row_config(db, r, admin)
            if cfg is not None:
                out.append((r, cfg))
    return out


def client_for_user(db, user_id: int, **kw) -> FailoverClient | None:
    """A failover client over the user's enabled, usable providers in priority order (None = AI off)."""
    cfgs = configs_for_user(db, user_id)
    if not cfgs:
        return None
    opts = options(db, user_id)
    kw.setdefault("transport", TRANSPORT)
    kw.setdefault("retries", opts["retries"])
    kw.setdefault("timeout", float(opts["timeout"]))
    return FailoverClient([LLMClient(cfg, **kw) for _, cfg in cfgs])


# ------------------------------------------------------------------------------------------- v1 API (primary)

def _primary(db, user_id: int) -> LLMProvider | None:
    rows = _rows(db, user_id)
    return rows[0] if rows else None


def public_settings(db, user_id: int) -> dict:
    row = _primary(db, user_id)
    key, readable = _key(row)
    has_key = bool(row is not None and row.secret and readable)
    provider = row.preset if row is not None else None
    model = row.model if row is not None else None
    return {"configured": bool(provider and model), "provider": provider, "model": model,
            "has_key": has_key, "key_last4": row.key_last4 if has_key else None, "key_unreadable": not readable,
            "translate_to": translate_to(db, user_id),
            "ai_enabled": bool(row is not None and row.enabled and (has_key or provider in KEYLESS))}


def save_settings(db, user_id: int, provider: str, model: str, api_key: str | None = None,
                  translate_to: str | None = None) -> dict:
    """Set the primary provider. api_key None = keep the stored key; "" = remove it."""
    if translate_to is not None and translate_to not in LANGS:
        raise LLMError("config", f"translate_to must be one of {LANGS}")
    row = _primary(db, user_id)
    if row is None:
        add_provider(db, user_id, preset=provider, model=model, api_key=api_key)
    else:
        update_provider(db, user_id, row.id, preset=provider, model=model, api_key=api_key)
    if translate_to is not None:
        set_options(db, user_id, translate_to=translate_to)
    return public_settings(db, user_id)


def delete_settings(db, user_id: int) -> None:
    for r in _rows(db, user_id):
        db.delete(r)
    row = _setting(db, user_id)
    if row is not None:
        db.delete(row)
    db.commit()


def model_config(db, user_id: int) -> ModelConfig | None:
    """The primary provider's config, or None when it is missing, disabled or unusable."""
    row = _primary(db, user_id)
    if row is None or not row.enabled:
        return None
    return _row_config(db, row)
