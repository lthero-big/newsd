"""Test database: SQLite in tmp_path by default. With NEWSD_TEST_PG_URL=postgresql://user:pw@host:port/db every fixture gets
an empty Postgres schema instead, so the whole suite can be run against Postgres (DEPTAI-216 4d):

    docker run -d --name newsd-test-pg -e POSTGRES_PASSWORD=pw -p 127.0.0.1:55432:5432 postgres:16-alpine
    NEWSD_TEST_PG_URL=postgresql://postgres:pw@127.0.0.1:55432/postgres pytest -q
"""
import os


def db_url(tmp_path, name: str) -> str:
    pg = os.environ.get("NEWSD_TEST_PG_URL", "")
    if not pg:
        return f"sqlite:///{tmp_path}/{name}.db"
    from sqlalchemy import create_engine, text

    from newsd.db import normalize_url
    eng = create_engine(normalize_url(pg))
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    eng.dispose()
    return pg
