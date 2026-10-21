import sys
from pathlib import Path

import httpx
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "pages"))    # pagekit (login / register)
sys.path.insert(0, str(HERE.parent / "scoring"))  # poolseed
sys.path.insert(0, str(HERE))
from pushkit import FakeSMTP, Net  # noqa: E402


@pytest.fixture(autouse=True)
def push_env(monkeypatch):
    monkeypatch.setenv("NEWSD_PUSH_ITEM_DELAY", "0")
    for k in ("NEWSD_SMTP_HOST", "NEWSD_SMTP_USER", "NEWSD_SMTP_PASSWORD", "NEWSD_SMTP_FROM", "NEWSD_TELEGRAM_BOT_TOKEN",
              "NEWSD_PUBLIC_URL", "NEWSD_PUSH_ALLOW_PRIVATE", "NEWSD_WORKER_TZ", "NEWSD_PUSH_BACKOFF", "NEWSD_PUSH_MAX_ATTEMPTS"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture()
def net(monkeypatch):
    """Every outgoing push HTTP call goes here; hostnames resolve to a public address (no DNS in tests)."""
    from newsd.push import channels
    n = Net()
    monkeypatch.setattr(channels, "TRANSPORT", httpx.MockTransport(n.handle))
    monkeypatch.setattr(channels, "_resolve", lambda host: ["93.184.216.34"])
    return n


@pytest.fixture()
def smtp(monkeypatch):
    srv = FakeSMTP()
    monkeypatch.setenv("NEWSD_SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("NEWSD_SMTP_PORT", str(srv.port))
    monkeypatch.setenv("NEWSD_SMTP_SECURITY", "none")
    monkeypatch.setenv("NEWSD_SMTP_USER", srv.credentials[0])
    monkeypatch.setenv("NEWSD_SMTP_PASSWORD", srv.credentials[1])
    monkeypatch.setenv("NEWSD_SMTP_FROM", "newsd@example.org")
    from newsd.settings import reset_settings
    reset_settings()
    yield srv
    srv.stop()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWSD_DATABASE_URL", f"sqlite:///{tmp_path}/push.db")
    monkeypatch.setenv("NEWSD_SECRET_KEY", "test-secret")
    monkeypatch.setenv("NEWSD_ENV_FILE", str(tmp_path / "none.env"))
    from newsd.db import SessionLocal, init_db, reset_engine
    from newsd.settings import reset_settings
    reset_settings(); reset_engine()
    init_db()
    s = SessionLocal()
    yield s
    s.close()
    reset_engine(); reset_settings()
