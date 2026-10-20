from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .settings import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_Session = None


def get_engine():
    global _engine, _Session
    if _engine is None:
        url = get_settings().database_url
        kw = {}
        if url.startswith("sqlite"):
            kw["connect_args"] = {"check_same_thread": False}
            path = url.split("///", 1)[-1]
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(url, **kw)
        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _pragmas(conn, _rec):
                cur = conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        _Session = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def SessionLocal():
    get_engine()
    return _Session()


def alembic_config():
    from alembic.config import Config
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    return cfg


BASELINE_REVISION = "0001_m1_baseline"


def upgrade_db(revision: str = "head") -> None:
    """Bring the schema to `revision` with Alembic. Databases created by the M1 create_all()
    (tables present, no alembic_version) are stamped at the M1 baseline first."""
    from alembic import command
    from sqlalchemy import inspect

    from . import models  # noqa: F401
    cfg = alembic_config()
    tables = set(inspect(get_engine()).get_table_names())
    if "users" in tables and "alembic_version" not in tables:
        command.stamp(cfg, BASELINE_REVISION)
    command.upgrade(cfg, revision)


def init_db() -> None:
    upgrade_db("head")


def reset_engine() -> None:
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None
