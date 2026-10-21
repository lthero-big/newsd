from tests.dburl import db_url
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "pool"))  # poolkit (fake internet)
sys.path.insert(0, str(HERE.parent / "llm"))  # llmkit (fake model endpoints)
from poolkit import Net  # noqa: E402


@pytest.fixture()
def net():
    return Net()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWSD_DATABASE_URL", db_url(tmp_path, "worker"))
    monkeypatch.setenv("NEWSD_SECRET_KEY", "test-secret")
    monkeypatch.setenv("NEWSD_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("NEWSD_LLM_RPM", "0")  # no real sleeping between fake model calls
    for k in ("NEWSD_WORKER_TIMES", "NEWSD_WORKER_TZ", "NEWSD_WORKER_RUN_ON_START"):
        monkeypatch.delenv(k, raising=False)
    from newsd.db import SessionLocal, init_db, reset_engine
    from newsd.settings import reset_settings

    reset_settings(); reset_engine()
    init_db()
    s = SessionLocal()
    yield s
    s.close()
    reset_engine(); reset_settings()


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Model calls fail loudly unless a test installs a fake transport."""
    import httpx

    from newsd.llm import keys

    def refuse(request):
        raise AssertionError(f"unexpected real model call to {request.url}")

    monkeypatch.setattr(keys, "TRANSPORT", httpx.MockTransport(refuse))
