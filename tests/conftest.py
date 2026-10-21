from tests.dburl import db_url
import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWSD_DATABASE_URL", db_url(tmp_path, "t"))
    monkeypatch.setenv("NEWSD_SECRET_KEY", "test-secret")
    monkeypatch.setenv("NEWSD_ENV_FILE", str(tmp_path / "none.env"))
    from newsd.db import reset_engine
    from newsd.settings import reset_settings
    reset_settings(); reset_engine()
    from fastapi.testclient import TestClient
    from newsd.app import create_app
    from newsd.cli import main
    main(["admin", "create", "root", "--password", "rootpass123"])
    with TestClient(create_app()) as c:
        yield c
    reset_engine(); reset_settings()
