"""Admin API for version discovery and web-triggered updates (see newsd/updates.py). Admin role only."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import updates as U
from .auth import get_db, require_admin
from .eventlog import add as ev

router = APIRouter(prefix="/api/admin/update")


@router.get("")
def status(_=Depends(require_admin), db=Depends(get_db)):
    return U.overview(db)


@router.post("/check")
def check_now(_=Depends(require_admin), db=Depends(get_db)):
    U.check(db, force=True)
    return U.overview(db, refresh=False)


class ApplyIn(BaseModel):
    version: str | None = Field(default=None, max_length=64)


@router.post("/apply", status_code=202)
def apply(body: ApplyIn, admin=Depends(require_admin), db=Depends(get_db)):
    try:
        job = U.start(db, admin, body.version)
    except U.UpdateError as exc:
        raise HTTPException(409, str(exc))
    ev(db, "WARN", "admin", f"开始更新 {job['from']} → {job['target']}", user=admin, detail={"mode": job["mode"]})
    db.commit()
    return job
