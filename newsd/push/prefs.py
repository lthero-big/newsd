"""Per-user push configuration.

Stored in ``user_settings`` key ``push``: everything that is safe to show (schedule, e-mail address, Telegram chat id,
webhook type / mode / templates, and masked hints for the secrets). The secrets themselves -- Telegram bot token,
webhook URL (Slack/Feishu/... URLs *are* credentials) and webhook signing secret -- are one JSON blob encrypted with the
server key in ``user_settings.secret``. The API only ever returns hints (last 4 characters)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..models import UserSetting
from ..security import decrypt_secret, encrypt_secret
from ..settings import get_settings
from . import template as tpl
from .digest import MODES

CHANNELS = ("email", "telegram", "webhook")
WEBHOOK_TYPES = {"generic": "通用 JSON", "feishu": "飞书", "dingtalk": "钉钉", "slack": "Slack", "discord": "Discord"}
SETTING_KEY = "push"
DEFAULT = {"enabled": False, "time": "08:00", "timezone": "Asia/Shanghai",
           "channels": {"email": {"enabled": False, "target": ""},
                        "telegram": {"enabled": False, "target": ""},
                        "webhook": {"enabled": False, "type": "generic", "mode": "summary", "template": "", "item_template": ""}}}
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_TG = re.compile(r"^(-?\d{3,20}|@[A-Za-z][\w]{3,31})$")
_MAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
_URL = re.compile(r"^https?://[^\s/]+\S*$")


class PrefError(ValueError):
    pass


def _row(db, user_id: int) -> UserSetting | None:
    return db.scalar(select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.key == SETTING_KEY))


def _secrets(row: UserSetting | None) -> dict:
    if row is None or not row.secret:
        return {}
    try:
        v = json.loads(decrypt_secret(row.secret))
        return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001  (master key changed / corrupt blob: behave as "no secrets")
        return {}


def hint(secret: str, kind: str = "token") -> str:
    if not secret:
        return ""
    if kind == "url":
        u = urlparse(secret)
        return f"{u.scheme}://{u.netloc}/…{secret[-4:]}"
    return "…" + secret[-4:]


def get_push(db, user_id: int) -> dict:
    """Public view (what the API and settings page get): no secret ever leaves here, only hints."""
    row = _row(db, user_id)
    v = row.value if row is not None and isinstance(row.value, dict) else {}
    sec = _secrets(row)
    out = {"enabled": bool(v.get("enabled", False)), "time": v.get("time", DEFAULT["time"]),
           "timezone": v.get("timezone", DEFAULT["timezone"]), "channels": {}}
    for c in CHANNELS:
        ch = {**DEFAULT["channels"][c], **(v.get("channels", {}).get(c) or {})}
        if c == "telegram":
            ch.update(has_token=bool(sec.get("telegram_token")), token_hint=hint(sec.get("telegram_token", "")),
                      server_bot=bool(get_settings().telegram_bot_token))
        elif c == "webhook":
            ch.update(has_url=bool(sec.get("webhook_url")), url_hint=hint(sec.get("webhook_url", ""), "url"),
                      has_secret=bool(sec.get("webhook_secret")), secret_hint=hint(sec.get("webhook_secret", "")))
        out["channels"][c] = ch
    return out


@dataclass
class ChannelConfig:
    """Everything needed to send on one channel, secrets decrypted. Never log or serialize this."""
    channel: str
    enabled: bool
    target: str = ""
    token: str = ""          # telegram
    url: str = ""            # webhook
    secret: str = ""         # webhook signing secret
    wtype: str = "generic"
    mode: str = "summary"
    template: str = ""
    item_template: str = ""

    def secrets(self) -> list[str]:
        return [s for s in (self.token, self.url, self.secret) if s]


def load_channel(db, user_id: int, channel: str) -> ChannelConfig:
    row = _row(db, user_id)
    v = row.value if row is not None and isinstance(row.value, dict) else {}
    ch = {**DEFAULT["channels"][channel], **(v.get("channels", {}).get(channel) or {})}
    sec = _secrets(row)
    cfg = ChannelConfig(channel, bool(ch.get("enabled")), ch.get("target", ""))
    if channel == "telegram":
        cfg.token = sec.get("telegram_token") or get_settings().telegram_bot_token
    elif channel == "webhook":
        cfg.url, cfg.secret = sec.get("webhook_url", ""), sec.get("webhook_secret", "")
        cfg.wtype, cfg.mode = ch.get("type", "generic"), ch.get("mode", "summary")
        cfg.template, cfg.item_template = ch.get("template", ""), ch.get("item_template", "")
    return cfg


def user_schedule(db, user_id: int) -> dict:
    row = _row(db, user_id)
    v = row.value if row is not None and isinstance(row.value, dict) else {}
    return {"enabled": bool(v.get("enabled")), "time": v.get("time", DEFAULT["time"]), "timezone": v.get("timezone", DEFAULT["timezone"])}


def enabled_channels(db, user_id: int) -> list[str]:
    row = _row(db, user_id)
    v = row.value if row is not None and isinstance(row.value, dict) else {}
    return [c for c in CHANNELS if (v.get("channels", {}).get(c) or {}).get("enabled")]


def _opt(value, keep):
    """None = keep the stored secret, "" = clear it, anything else = replace."""
    return keep if value is None else value.strip()


def save_push(db, user_id: int, body: dict) -> dict:
    """Validate and store. ``body`` is the parsed PushIn (plain dict). Raises PrefError with a user-facing message."""
    from .channels import assert_public_url  # local: channels imports prefs types

    if not _TIME.match(body["time"]):
        raise PrefError("time must be HH:MM")
    try:
        ZoneInfo(body["timezone"])
    except Exception:  # noqa: BLE001
        raise PrefError("unknown timezone")
    given = body.get("channels") or {}
    unknown = set(given) - set(CHANNELS)
    if unknown:
        raise PrefError(f"unknown channel: {sorted(unknown)}")
    row = _row(db, user_id)
    old = row.value if row is not None and isinstance(row.value, dict) else {}
    old_ch = old.get("channels", {})
    sec = _secrets(row)
    channels: dict = {}
    for c in CHANNELS:
        g = given.get(c)
        if g is None:  # channel not mentioned: keep it as stored
            channels[c] = {**DEFAULT["channels"][c], **(old_ch.get(c) or {})}
            continue
        enabled = bool(g.get("enabled"))
        if c == "email":
            target = (g.get("target") or "").strip()
            if (enabled or target) and not _MAIL.match(target):
                raise PrefError("email: invalid address")
            channels[c] = {"enabled": enabled and bool(target), "target": target}
        elif c == "telegram":
            target = (g.get("target") or "").strip()
            if (enabled or target) and not _TG.match(target):
                raise PrefError("telegram: use a numeric chat id or @channel")
            token = _opt(g.get("token"), sec.get("telegram_token", ""))
            if token and not re.fullmatch(r"\d{3,12}:[\w-]{20,}", token):
                raise PrefError("telegram: bot token looks wrong (expected 123456:ABC-...)")
            if enabled and target and not (token or get_settings().telegram_bot_token):
                raise PrefError("telegram: a bot token is required (the server has no default bot)")
            sec["telegram_token"] = token
            channels[c] = {"enabled": enabled and bool(target), "target": target}
        else:
            url = _opt(g.get("target"), sec.get("webhook_url", ""))
            wtype, mode = g.get("type") or "generic", g.get("mode") or "summary"
            template, item_template = (g.get("template") or "").strip(), (g.get("item_template") or "").strip()
            if wtype not in WEBHOOK_TYPES:
                raise PrefError(f"webhook: type must be one of {sorted(WEBHOOK_TYPES)}")
            if mode not in MODES:
                raise PrefError(f"webhook: mode must be one of {list(MODES)}")
            if (enabled or url) and not _URL.match(url):
                raise PrefError("webhook: must be an http(s) URL")
            if url:
                try:
                    assert_public_url(url, resolve=False)
                except Exception as exc:  # noqa: BLE001
                    raise PrefError(f"webhook: {exc}")
            for name, t in (("template", template), ("item_template", item_template)):
                try:
                    tpl.validate(t)
                except ValueError as exc:
                    raise PrefError(f"webhook {name}: {exc}")
            sec["webhook_url"] = url
            sec["webhook_secret"] = _opt(g.get("secret"), sec.get("webhook_secret", ""))
            channels[c] = {"enabled": enabled and bool(url), "type": wtype, "mode": mode, "template": template, "item_template": item_template}
    value = {"enabled": bool(body.get("enabled")), "time": body["time"], "timezone": body["timezone"], "channels": channels}
    sec = {k: v for k, v in sec.items() if v}
    if row is None:
        row = UserSetting(user_id=user_id, key=SETTING_KEY, value=value)
        db.add(row)
    row.value = value
    row.secret = encrypt_secret(json.dumps(sec)) if sec else None
    db.commit()
    return get_push(db, user_id)
