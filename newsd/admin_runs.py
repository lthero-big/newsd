"""Admin API for worker runs (M2-5): /api/admin/worker-runs. The /admin page renders the same data in its "Worker 运行" section.
(/api/admin/runs from M3 stays the per-source-fetch / per-user-issue view.)"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from .auth import get_db, require_admin
from .models import WorkerRun
from .worker import run_view

router = APIRouter(prefix="/api/admin")


def recent_runs(db, limit: int = 50) -> list[WorkerRun]:
    return list(db.scalars(select(WorkerRun).order_by(WorkerRun.id.desc()).limit(max(1, min(limit, 200)))))


@router.get("/worker-runs")
def list_worker_runs(limit: int = 50, _=Depends(require_admin), db=Depends(get_db)):
    return [run_view(r, detail=False) for r in recent_runs(db, limit)]


@router.get("/worker-runs/{rid}")
def get_worker_run(rid: int, _=Depends(require_admin), db=Depends(get_db)):
    run = db.get(WorkerRun, rid)
    if run is None:
        raise HTTPException(404, "no such run")
    return run_view(run)
