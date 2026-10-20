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
    registration: str = field(default_factory=lambda: os.environ.get("NEWSD_REGISTRATION", "invite"))  # invite | open | closed
    cookie_secure: bool = field(default_factory=lambda: os.environ.get("NEWSD_COOKIE_SECURE", "0") == "1")
    session_days: int = field(default_factory=lambda: int(os.environ.get("NEWSD_SESSION_DAYS", "30")))
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
