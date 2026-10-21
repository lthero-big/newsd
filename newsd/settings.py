"""Runtime settings, read from environment / .env. No cloud services required."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Settings:
    secret_key: str = field(default_factory=lambda: os.environ.get("NEWSD_SECRET_KEY", ""))
    database_url: str = field(default_factory=lambda: os.environ.get("NEWSD_DATABASE_URL", "sqlite:///./var/newsd.db"))
    registration: str = field(default_factory=lambda: os.environ.get("NEWSD_REGISTRATION", "invite"))  # invite | open | closed (default; admin can override at runtime)
    cookie_secure: bool = field(default_factory=lambda: os.environ.get("NEWSD_COOKIE_SECURE", "0") == "1")
    session_days: int = field(default_factory=lambda: int(os.environ.get("NEWSD_SESSION_DAYS", "30")))
    # worker (M2-5): daily pipeline times "HH:MM[,HH:MM...]" in worker_tz; issue key = the date in that zone
    worker_times: str = field(default_factory=lambda: os.environ.get("NEWSD_WORKER_TIMES", "07:00"))
    worker_tz: str = field(default_factory=lambda: os.environ.get("NEWSD_WORKER_TZ", "UTC"))
    worker_run_on_start: bool = field(default_factory=lambda: os.environ.get("NEWSD_WORKER_RUN_ON_START", "0") == "1")
    worker_extract: bool = field(default_factory=lambda: os.environ.get("NEWSD_WORKER_EXTRACT", "1") != "0")
    worker_since_hours: float = field(default_factory=lambda: float(os.environ.get("NEWSD_WORKER_SINCE_HOURS", "26")))
    # push (M4): SMTP is server-side; Telegram bot token may also be per user (see docs/m4-223-push.md)
    public_url: str = field(default_factory=lambda: os.environ.get("NEWSD_PUBLIC_URL", "").rstrip("/"))
    smtp_host: str = field(default_factory=lambda: os.environ.get("NEWSD_SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.environ.get("NEWSD_SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: os.environ.get("NEWSD_SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.environ.get("NEWSD_SMTP_PASSWORD", ""))
    smtp_from: str = field(default_factory=lambda: os.environ.get("NEWSD_SMTP_FROM", ""))
    smtp_security: str = field(default_factory=lambda: os.environ.get("NEWSD_SMTP_SECURITY", "starttls"))  # starttls | ssl | none
    telegram_bot_token: str = field(default_factory=lambda: os.environ.get("NEWSD_TELEGRAM_BOT_TOKEN", ""))  # default bot for users without their own
    telegram_api_base: str = field(default_factory=lambda: os.environ.get("NEWSD_TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"))
    push_max_attempts: int = field(default_factory=lambda: int(os.environ.get("NEWSD_PUSH_MAX_ATTEMPTS", "4")))
    push_backoff: str = field(default_factory=lambda: os.environ.get("NEWSD_PUSH_BACKOFF", "60,300,900"))  # seconds before retry 1, 2, ...
    push_grace_hours: float = field(default_factory=lambda: float(os.environ.get("NEWSD_PUSH_GRACE_HOURS", "12")))
    push_max_items: int = field(default_factory=lambda: int(os.environ.get("NEWSD_PUSH_MAX_ITEMS", "20")))  # per-item mode cap
    push_item_delay: float = field(default_factory=lambda: float(os.environ.get("NEWSD_PUSH_ITEM_DELAY", "0.5")))
    push_timeout: float = field(default_factory=lambda: float(os.environ.get("NEWSD_PUSH_TIMEOUT", "15")))
    push_allow_private: bool = field(default_factory=lambda: os.environ.get("NEWSD_PUSH_ALLOW_PRIVATE", "0") == "1")
    login_max_failures: int = 3
    login_lock_minutes: int = 15


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _load_dotenv(Path(os.environ.get("NEWSD_ENV_FILE", ".env")))
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    global _settings
    _settings = None
