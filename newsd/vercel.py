"""DEPTAI-216 4d: Vercel entrypoint (``[tool.vercel] entrypoint = "newsd.vercel:app"`` in pyproject.toml).

The whole web app runs as one Vercel Function. There is no worker process: the schedule is driven by ``/api/cron/tick``
(Vercel Cron or GitHub Actions, see docs/vercel.md). Data lives in Postgres (Supabase, transaction pooler); the function's
filesystem is read-only and not shared, so SQLite is refused.

Schema: the build step (``newsd.vercel_build``) migrates the production database before the deployment goes live. As a
fallback a cold start of the production deployment migrates too when the schema is behind. Preview deployments never
migrate unless NEWSD_MIGRATE_ON_START=1 (they would otherwise change the production schema from an unmerged branch).
"""
from __future__ import annotations

import logging
import os

os.environ.setdefault("NEWSD_DEPLOY_MODE", "vercel")
os.environ.setdefault("NEWSD_DB_POOL", "null")
os.environ.setdefault("NEWSD_COOKIE_SECURE", "1")  # Vercel only serves HTTPS

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from . import __version__  # noqa: E402
from .settings import get_settings  # noqa: E402
from .vercel_build import migrate_allowed  # noqa: E402

log = logging.getLogger("newsd.vercel")


def problems() -> list[str]:
    s = get_settings()
    out = []
    if not s.secret_key:
        out.append("NEWSD_SECRET_KEY is not set (Project Settings → Environment Variables)")
    if s.database_url.startswith("sqlite"):
        out.append("NEWSD_DATABASE_URL must point at Postgres, e.g. the Supabase transaction pooler "
                   "(postgresql://postgres.<ref>:<password>@<pooler-host>:6543/postgres); SQLite cannot work on Vercel")
    return out


def ensure_schema() -> str:
    from .db import schema_status, upgrade_db
    cur, head = schema_status()
    if cur == head:
        return "ok"
    if not migrate_allowed("NEWSD_MIGRATE_ON_START"):
        log.warning("database schema %s is behind %s and this deployment does not migrate", cur, head)
        return "behind"
    try:
        upgrade_db("head")
        return "migrated"
    except Exception:  # noqa: BLE001  (another cold start may be migrating at the same moment; serve and let it finish)
        log.exception("migration at cold start failed")
        return "error"


def misconfigured(errors: list[str]) -> FastAPI:
    """Every request explains what is missing instead of a bare 500 from a crashed import."""
    bad = FastAPI(title="newsd (misconfigured)")

    @bad.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def explain(path: str):
        return JSONResponse({"ok": False, "version": __version__, "error": "newsd is not configured", "problems": errors},
                            status_code=503)

    return bad


def build() -> FastAPI:
    errors = problems()
    if errors:
        for e in errors:
            log.error("newsd: %s", e)
        return misconfigured(errors)
    state = ensure_schema()
    from .app import create_app
    a = create_app(migrate=False)
    a.state.schema = state
    return a


app = build()
