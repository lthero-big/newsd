"""Per-user model settings. provider/model/translate_to are stored in ``user_settings`` (key ``llm``);
the API key goes in ``user_settings.secret``, encrypted with the server master key (M1 Fernet helper,
``newsd.security.encrypt_secret``). The key is never returned by the API -- only its last 4 chars."""
from __future__ import annotations

from sqlalchemy import select

from newsd.models import UserSetting
from newsd.security import decrypt_secret, encrypt_secret

from .client import KEYLESS, LLMClient, LLMError, ModelConfig

SETTING_KEY = "llm"
LANGS = ("zh", "en", "ja")
# Tests (and embedders) may set this to an httpx transport; every per-user client uses it.
TRANSPORT = None


def _row(db, user_id: int) -> UserSetting | None:
    return db.scalar(select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.key == SETTING_KEY))


def public_settings(db, user_id: int) -> dict:
    row = _row(db, user_id)
    v = row.value if row is not None and isinstance(row.value, dict) else {}
    provider = v.get("provider")
    readable = True
    if row is not None and row.secret:
        try:
            decrypt_secret(row.secret)
        except Exception:  # master key changed / corrupt
            readable = False
    has_key = bool(row is not None and row.secret and readable)
    return {"configured": bool(provider and v.get("model")), "provider": provider, "model": v.get("model"),
            "has_key": has_key, "key_last4": v.get("last4") if has_key else None, "key_unreadable": not readable,
            "translate_to": v.get("translate_to", "zh"),
            "ai_enabled": bool(provider and v.get("model") and (has_key or provider in KEYLESS))}


def save_settings(db, user_id: int, provider: str, model: str, api_key: str | None = None,
                  translate_to: str | None = None) -> dict:
    """api_key None = keep the stored key; "" = remove it. Validates everything before writing."""
    row = _row(db, user_id)
    model = (model or "").strip()
    new_key = _decrypt(row) if api_key is None else api_key.strip()
    if translate_to is not None and translate_to not in LANGS:
        raise LLMError("config", f"translate_to must be one of {LANGS}")
    ModelConfig(provider, model, new_key).validate()
    if row is None:
        row = UserSetting(user_id=user_id, key=SETTING_KEY, value={})
        db.add(row)
    v = dict(row.value) if isinstance(row.value, dict) else {}
    if api_key is not None:
        row.secret = encrypt_secret(new_key) if new_key else None
        v["last4"] = new_key[-4:] if len(new_key) >= 12 else None
    if translate_to is not None:
        v["translate_to"] = translate_to
    v.update(provider=provider, model=model)
    row.value = v
    db.commit()
    return public_settings(db, user_id)


def delete_settings(db, user_id: int) -> None:
    row = _row(db, user_id)
    if row is not None:
        db.delete(row)
        db.commit()


def _decrypt(row: UserSetting | None) -> str:
    if row is None or not row.secret:
        return ""
    try:
        return decrypt_secret(row.secret)
    except Exception:
        return ""


def model_config(db, user_id: int) -> ModelConfig | None:
    """The user's ModelConfig, or None when AI is not enabled for them (no provider/model, or no key)."""
    row = _row(db, user_id)
    v = row.value if row is not None and isinstance(row.value, dict) else {}
    if not v.get("provider") or not v.get("model"):
        return None
    key = _decrypt(row)
    if not key and v["provider"] not in KEYLESS:
        return None
    cfg = ModelConfig(v["provider"], v["model"], key)
    try:
        cfg.validate()
    except LLMError:
        return None
    return cfg


def translate_to(db, user_id: int) -> str:
    row = _row(db, user_id)
    v = row.value if row is not None and isinstance(row.value, dict) else {}
    return v.get("translate_to", "zh")


def client_for_user(db, user_id: int, **kw) -> LLMClient | None:
    cfg = model_config(db, user_id)
    if cfg is None:
        return None
    kw.setdefault("transport", TRANSPORT)
    return LLMClient(cfg, **kw)
