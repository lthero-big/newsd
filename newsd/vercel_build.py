"""DEPTAI-216 4d: Vercel build step (``[tool.vercel.scripts] build = "python -m newsd.vercel_build"``).

Migrates the database before the new deployment goes live, so a failed migration fails the build and the previous
deployment keeps serving. Only production builds migrate by default (VERCEL_ENV=production); set
NEWSD_MIGRATE_ON_BUILD=1 to migrate for other environments too, or 0 to never migrate here.

NEWSD_MIGRATE_DATABASE_URL, when set, is used instead of NEWSD_DATABASE_URL: migrations are DDL in one long session, so the
Supabase *session* pooler (port 5432) or a direct connection is the better fit than the transaction pooler (6543).
"""
from __future__ import annotations

import os
import sys


def migrate_allowed(var: str) -> bool:
    """NEWSD_MIGRATE_ON_BUILD / NEWSD_MIGRATE_ON_START: 1 or 0 decide; otherwise only production deployments migrate.
    (Lives here, not in newsd.vercel, because importing newsd.vercel builds the app.)"""
    flag = os.environ.get(var, "").strip()
    if flag in ("0", "1"):
        return flag == "1"
    return os.environ.get("VERCEL_ENV", "production") == "production"


def main() -> int:
    env = os.environ.get("VERCEL_ENV", "")
    if not migrate_allowed("NEWSD_MIGRATE_ON_BUILD") or (not env and os.environ.get("NEWSD_MIGRATE_ON_BUILD") != "1"):
        print(f"newsd: VERCEL_ENV={env or '-'}: not migrating in this build (NEWSD_MIGRATE_ON_BUILD=1 forces it)")
        return 0
    url = os.environ.get("NEWSD_MIGRATE_DATABASE_URL") or os.environ.get("NEWSD_DATABASE_URL") or ""
    if not url or url.startswith("sqlite"):
        print("newsd: NEWSD_DATABASE_URL is not set to a Postgres URL for this environment. Add it under Project Settings → "
              "Environment Variables (Supabase → Connect → Transaction pooler) and redeploy.", file=sys.stderr)
        return 1
    os.environ["NEWSD_DATABASE_URL"] = url
    from .db import schema_status, upgrade_db
    before, head = schema_status()
    upgrade_db("head")
    after, _ = schema_status()
    print(f"newsd: database schema {before or '(empty)'} -> {after} (head {head})")
    return 0 if after == head else 1


if __name__ == "__main__":
    raise SystemExit(main())
