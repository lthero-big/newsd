"""Alembic environment. Uses newsd's engine/settings; run via `newsd db upgrade` (or newsd.db.init_db)."""
from alembic import context

from newsd import models  # noqa: F401  (register tables)
from newsd.db import Base, get_engine

target_metadata = Base.metadata


def run_migrations_online() -> None:
    conn = context.config.attributes.get("connection")
    if conn is not None:
        context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    with get_engine().connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    context.configure(url=str(get_engine().url), target_metadata=target_metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    run_migrations_online()
