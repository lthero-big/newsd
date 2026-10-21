from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .settings import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_Session = None


def normalize_url(url: str) -> str:
    """Supabase / Heroku style ``postgres://`` and ``postgresql://`` URLs use the psycopg 3 driver newsd ships with
    (SQLAlchemy would otherwise pick psycopg2, which is not installed)."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def serverless() -> bool:
    """Short-lived processes (Vercel functions): no client-side pool, the platform pooler (Supabase/PgBouncer) does it."""
    return os.environ.get("NEWSD_DB_POOL", "").lower() == "null" or bool(os.environ.get("VERCEL"))


def engine_kwargs(url: str) -> dict:
    kw: dict = {}
    if url.startswith("sqlite"):
        kw["connect_args"] = {"check_same_thread": False, "timeout": 30}  # web + worker share the file
        return kw
    from sqlalchemy.engine import make_url
    u = make_url(url)
    ca: dict = {}
    if u.drivername == "postgresql+psycopg":
        # transaction-mode poolers (Supabase :6543, PgBouncer pool_mode=transaction) hand every transaction a different
        # server connection, so psycopg's automatic prepared statements break ("prepared statement ... does not exist")
        if u.port == 6543 or serverless() or os.environ.get("NEWSD_DB_PREPARE", "").lower() in ("0", "off", "false", "no"):
            ca["prepare_threshold"] = None
        if (u.host or "").endswith((".supabase.com", ".supabase.co")) and "sslmode" not in u.query:
            ca["sslmode"] = "require"
    if serverless():
        kw["poolclass"] = NullPool  # a frozen function must not keep sockets that go stale between invocations
    else:
        kw["pool_pre_ping"] = True
    if ca:
        kw["connect_args"] = ca
    return kw


def get_engine():
    global _engine, _Session
    if _engine is None:
        url = normalize_url(get_settings().database_url)
        if url.startswith("sqlite"):
            path = url.split("///", 1)[-1]
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(url, **engine_kwargs(url))
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


def schema_status() -> tuple[str | None, str]:
    """(current revision, head revision) without changing anything."""
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    with get_engine().connect() as conn:
        cur = MigrationContext.configure(conn).get_current_revision()
    return cur, head


def init_db() -> None:
    upgrade_db("head")


def reset_engine() -> None:
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None
