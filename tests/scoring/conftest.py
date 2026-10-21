from tests.dburl import db_url
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "llm"))  # llmkit (fake model endpoints)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWSD_DATABASE_URL", db_url(tmp_path, "scoring"))
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


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    import httpx

    from newsd.llm import keys

    def refuse(request):
        raise AssertionError(f"unexpected real model call to {request.url}")

    monkeypatch.setattr(keys, "TRANSPORT", httpx.MockTransport(refuse))
