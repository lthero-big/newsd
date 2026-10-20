import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from poolkit import Net  # noqa: E402


@pytest.fixture()
def net():
    return Net()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWSD_DATABASE_URL", f"sqlite:///{tmp_path}/pool.db")
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
