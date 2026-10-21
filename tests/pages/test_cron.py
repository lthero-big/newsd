"""DEPTAI-216 item 4d: /api/cron/tick, the Postgres/pooler engine options and the Vercel entrypoint/build step."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from newsd import cron as C
from newsd import db as D

KEY = "cron-secret-0123456789"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("CRON_SECRET", "NEWSD_CRON_SECRET", "NEWSD_CRON_STALE_MINUTES", "VERCEL", "VERCEL_ENV", "NEWSD_DB_POOL", "NEWSD_DB_PREPARE",
              "NEWSD_MIGRATE_ON_START", "NEWSD_MIGRATE_ON_BUILD", "NEWSD_MIGRATE_DATABASE_URL", "NEWSD_WORKER_TIMES", "NEWSD_WORKER_TZ"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NEWSD_UPDATE_CHECK", "0")


@pytest.fixture()
def fake_pipeline(monkeypatch):
    """Stand-in for the real pipeline: finishes the claimed run and records the call."""
    calls = []

    def run(db, run):
        calls.append(run.id)
        run.status, run.finished_at, run.issue_key = "ok", C._now(), "2026-10-21"
        db.commit()
        return {"id": run.id, "status": "ok", "issue_key": run.issue_key, "seconds": 0.1, "error_count": 0, "users_ok": 1, "users_failed": 0}

    monkeypatch.setattr(C, "_pipeline", run)
    return calls


def _hdr(key=KEY):
    return {"Authorization": f"Bearer {key}"}


def test_tick_needs_a_configured_key(client, monkeypatch, fake_pipeline):
    r = client.get("/api/cron/tick")
    assert r.status_code == 503 and "CRON_SECRET" in r.text  # off until a key is set
    monkeypatch.setenv("CRON_SECRET", KEY)
    assert client.get("/api/cron/tick").status_code == 401
    assert client.get("/api/cron/tick", headers=_hdr("wrong")).status_code == 401
    assert client.get(f"/api/cron/tick?key={KEY}").status_code == 401  # no keys in URLs
    assert client.get("/api/cron/tick", headers={"Authorization": KEY}).status_code == 401  # must be "Bearer <key>"
    assert client.get("/api/cron/tick?run=bogus", headers=_hdr()).status_code == 400
    assert fake_pipeline == []
    r = client.get("/api/cron/tick", headers=_hdr())
    assert r.status_code == 200 and r.json()["pipeline"]["ran"] and len(fake_pipeline) == 1
    # NEWSD_CRON_SECRET wins over CRON_SECRET; the X-Newsd-Cron-Key header works too; POST is accepted
    monkeypatch.setenv("NEWSD_CRON_SECRET", "other-secret-abcdef")
    assert client.get("/api/cron/tick", headers=_hdr()).status_code == 401
    r = client.post("/api/cron/tick?run=skip", headers={"X-Newsd-Cron-Key": "other-secret-abcdef"})
    assert r.status_code == 200 and r.json()["pipeline"] == {"ran": False, "reason": "skip"} and "push" in r.json()


def test_tick_is_idempotent_per_slot(client, monkeypatch, fake_pipeline):
    monkeypatch.setenv("CRON_SECRET", KEY)
    j1 = client.get("/api/cron/tick", headers=_hdr()).json()
    j2 = client.get("/api/cron/tick", headers=_hdr()).json()  # duplicate delivery of the same cron
    assert j1["pipeline"]["ran"] and j1["pipeline"]["run"]["status"] == "ok"
    assert not j2["pipeline"]["ran"] and j2["pipeline"]["reason"] == "done" and j2["pipeline"]["run_id"] == j1["pipeline"]["run"]["id"]
    j3 = client.get("/api/cron/tick?run=force", headers=_hdr()).json()
    assert j3["pipeline"]["ran"] and len(fake_pipeline) == 2
    # the admin run list shows the tick's runs
    from pagekit import login
    login(client, "root", "rootpass123")
    runs = client.get("/api/admin/worker-runs").json()
    assert {r["trigger"] for r in runs} == {"cron"}


def test_slots_follow_the_worker_times_and_zone():
    tz = ZoneInfo("Asia/Shanghai")
    times = [(7, 0), (19, 0)]
    # 2026-10-21 00:30 UTC = 08:30 Shanghai -> the 07:00 slot (23:00 UTC the day before)
    assert C.last_slot(datetime(2026, 10, 21, 0, 30), times, tz) == datetime(2026, 10, 20, 23, 0)
    # 22:00 UTC = 06:00 next day Shanghai -> still the previous evening's 19:00 slot
    assert C.last_slot(datetime(2026, 10, 21, 22, 0), times, tz) == datetime(2026, 10, 21, 11, 0)
    assert C.last_slot(datetime(2026, 10, 21, 7, 0), [(7, 0)], ZoneInfo("UTC")) == datetime(2026, 10, 21, 7, 0)


def test_a_run_before_the_slot_does_not_count(client, monkeypatch, fake_pipeline):
    from newsd.models import WorkerRun
    monkeypatch.setenv("CRON_SECRET", KEY)
    db = D.SessionLocal()
    now = C._now()
    slot = C.last_slot(now, [(7, 0)], ZoneInfo("UTC"))
    db.add(WorkerRun(trigger="schedule", status="ok", started_at=slot - timedelta(minutes=5), finished_at=slot, cycle="c", issue_key="k"))
    db.add(WorkerRun(trigger="manual", status="ok", started_at=now, finished_at=now, cycle="c", issue_key="k"))  # manual runs never count
    db.commit(); db.close()
    j = client.get("/api/cron/tick", headers=_hdr()).json()
    assert j["pipeline"]["ran"], j


def test_busy_and_stale_runs(client, monkeypatch, fake_pipeline):
    from newsd.models import WorkerRun
    monkeypatch.setenv("CRON_SECRET", KEY)
    db = D.SessionLocal()
    now = C._now()
    fresh = WorkerRun(trigger="cron", status="running", started_at=now - timedelta(minutes=2), cycle="c", issue_key="k")
    db.add(fresh); db.commit()
    j = client.get("/api/cron/tick?run=force", headers=_hdr()).json()
    assert not j["pipeline"]["ran"] and j["pipeline"]["reason"] == "busy" and j["pipeline"]["run_id"] == fresh.id
    # killed by the platform long ago: closed out as interrupted, and the next tick may run
    fresh.started_at = now - timedelta(minutes=50); db.commit()
    j = client.get("/api/cron/tick?run=force", headers=_hdr()).json()
    assert j["interrupted"] == [fresh.id] and j["pipeline"]["ran"]
    db.expire_all()
    assert db.get(WorkerRun, fresh.id).status == "interrupted"
    db.close()


def test_claim_lets_only_the_lower_id_proceed(client):
    """Two overlapping ticks: the second one's row sees the first, gives way and removes its own row."""
    from newsd.models import WorkerRun
    db = D.SessionLocal()
    now = C._now()
    slot = now - timedelta(hours=1)
    first, why, _ = C.claim(db, slot, "auto", now)
    assert first is not None and why == ""
    second, why, other = C.claim(db, slot, "force", now)
    assert second is None and why == "busy" and other.id == first.id
    # the race itself: a later row inserted before the earlier tick re-checks
    late = WorkerRun(trigger="cron", status="running", started_at=now, cycle="", issue_key="")
    db.add(late); db.commit()
    assert C._blocking(db, slot, "auto", exclude=late.id, before=late.id)[1].id == first.id  # late gives way
    assert C._blocking(db, slot, "auto", exclude=first.id, before=first.id) is None  # first proceeds
    db.close()


def test_real_pipeline_through_the_tick(client, monkeypatch):
    """No fake: an empty install runs the real pipeline (no sources, one admin user, AI off) and records a cron run."""
    monkeypatch.setenv("CRON_SECRET", KEY)
    monkeypatch.setenv("NEWSD_WORKER_EXTRACT", "0")
    from newsd.settings import reset_settings
    reset_settings()
    j = client.get("/api/cron/tick", headers=_hdr()).json()
    run = j["pipeline"]["run"]
    assert j["pipeline"]["ran"] and run["status"] in ("ok", "partial") and run["issue_key"]


def test_engine_options_for_poolers(monkeypatch):
    from sqlalchemy.pool import NullPool
    assert D.normalize_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert D.normalize_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert D.normalize_url("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert D.normalize_url("sqlite:///x.db") == "sqlite:///x.db"
    sup = "postgresql+psycopg://postgres.ref:pw@aws-0-eu-central-1.pooler.supabase.com:6543/postgres"
    kw = D.engine_kwargs(sup)
    assert kw["connect_args"] == {"prepare_threshold": None, "sslmode": "require"} and kw["pool_pre_ping"]
    assert "sslmode" not in D.engine_kwargs(sup + "?sslmode=verify-full")["connect_args"]
    plain = D.engine_kwargs("postgresql+psycopg://u:p@db:5432/newsd")
    assert "connect_args" not in plain and "poolclass" not in plain  # docker compose Postgres: normal pool, prepared statements on
    monkeypatch.setenv("VERCEL", "1")
    kw = D.engine_kwargs("postgresql+psycopg://u:p@db:5432/newsd")
    assert kw["poolclass"] is NullPool and kw["connect_args"] == {"prepare_threshold": None}


def _import_vercel(monkeypatch):
    """newsd.vercel sets Vercel defaults in os.environ on import (secure cookies, no client pool): pin them through
    monkeypatch first so they are undone after the test instead of leaking into the rest of the suite."""
    for k, v in (("NEWSD_DEPLOY_MODE", "vercel"), ("NEWSD_DB_POOL", "null"), ("NEWSD_COOKIE_SECURE", "1")):
        monkeypatch.setenv(k, v)
    from newsd import vercel as V
    return V


def test_vercel_entry_refuses_sqlite_and_explains(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    V = _import_vercel(monkeypatch)
    from newsd.settings import reset_settings
    monkeypatch.setenv("NEWSD_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("NEWSD_DATABASE_URL", "sqlite:////tmp/x.db")
    monkeypatch.delenv("NEWSD_SECRET_KEY", raising=False)
    reset_settings()
    probs = V.problems()
    assert len(probs) == 2 and "NEWSD_SECRET_KEY" in probs[0] and "Postgres" in probs[1]
    r = TestClient(V.build()).get("/healthz")
    assert r.status_code == 503 and r.json()["problems"] == probs
    reset_settings()


def test_vercel_migrations_only_for_production(tmp_path, monkeypatch, capsys):
    from newsd import vercel_build as B
    from newsd.vercel_build import migrate_allowed
    assert migrate_allowed("NEWSD_MIGRATE_ON_START")  # not on Vercel at all: production semantics
    monkeypatch.setenv("VERCEL_ENV", "preview")
    assert not migrate_allowed("NEWSD_MIGRATE_ON_START")
    monkeypatch.setenv("NEWSD_MIGRATE_ON_START", "1")
    assert migrate_allowed("NEWSD_MIGRATE_ON_START")
    # build step: preview skips, production without a Postgres URL fails the build with instructions
    assert B.main() == 0 and "not migrating" in capsys.readouterr().out
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setenv("NEWSD_DATABASE_URL", "sqlite:////tmp/x.db")
    assert B.main() == 1 and "Transaction pooler" in capsys.readouterr().err
    monkeypatch.setenv("NEWSD_MIGRATE_ON_BUILD", "0")
    assert B.main() == 0
