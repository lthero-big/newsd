import json
from pathlib import Path

from sqlalchemy import inspect

ROOT = Path(__file__).resolve().parents[1]


def login(c, u, p):
    return c.post("/login", data={"username": u, "password": p}, follow_redirects=False)


def _import():
    from newsd.catalog import import_config
    from newsd.db import SessionLocal
    db = SessionLocal()
    try:
        return import_config(db)
    finally:
        db.close()


def _user(client, name):
    from newsd.cli import main
    main(["admin", "registration", "open"])
    client.cookies.clear()
    assert client.post("/api/register", data={"username": name, "password": name + "pass123"}).status_code == 200
    assert login(client, name, name + "pass123").status_code == 303


def test_migrations_build_empty_db(client):
    from newsd.db import get_engine
    names = set(inspect(get_engine()).get_table_names())
    assert {"alembic_version", "users", "sources", "subscriptions", "keywords", "quotas", "tag_terms",
            "subscription_templates", "subscription_template_items"} <= names


def test_import_counts_match_json(client):
    cfg = json.loads((ROOT / "config/collect-sources.json").read_text())
    tags = json.loads((ROOT / "config/tags.json").read_text())
    c = _import()
    assert c["rss"] == len(cfg["rss"])
    assert c["reddit"] == len(cfg["reddit"]["feeds"])
    assert c["rsshub"] == len(cfg["rsshub"]["routes"])
    assert c["x_query"] == len(cfg["x"]["queries"])
    assert c.get("x_account", 0) == len(cfg["x"].get("accounts") or [])
    assert c["plugin"] == len({"hn", "github_search", "hf_trending", "ossinsight"} & set(cfg))
    assert c["tag_terms"] == sum(len(v) for v in tags["tags"].values())
    assert _import() == c  # idempotent
    from newsd.db import SessionLocal
    from newsd.models import Source, TagTerm
    db = SessionLocal()
    assert db.query(Source).count() == c["sources"] and db.query(TagTerm).count() == c["tag_terms"]
    db.close()


def test_user_prefs_and_isolation(client):
    _import()
    _user(client, "alice")
    assert client.post("/api/me/subscriptions/apply-template").json()["added_sources"] > 0
    assert client.put("/api/me/subscriptions", json={"source": "rss:hf-blog", "enabled": False, "overrides": {"max_items": 3}}).status_code == 200
    assert client.put("/api/me/subscriptions", json={"source": "rss:hf-blog", "overrides": {"url": "http://evil"}}).status_code == 400
    assert client.put("/api/me/subscriptions", json={"source": "rss:nope"}).status_code == 404
    kid = client.post("/api/me/keywords", json={"kind": "block", "term": "crypto"}).json()["id"]
    assert client.post("/api/me/keywords", json={"kind": "bogus", "term": "x"}).status_code == 400
    q = [{"max_items": 40}, {"module": "news", "max_items": 30}, {"module": "news", "group_name": "LLM", "max_items": 10, "categories": ["LLM"]}]
    assert len(client.put("/api/me/quotas", json=q).json()) == 3
    assert client.put("/api/me/quotas", json=[{"group_name": "x", "max_items": 1}]).status_code == 400
    alice_subs = client.get("/api/me/subscriptions").json()
    assert {"source": "rss:hf-blog", "enabled": False, "overrides": {"max_items": 3}} in alice_subs

    _user(client, "bob")
    assert client.get("/api/me/subscriptions").json() == []
    assert client.get("/api/me/quotas").json() == []
    assert all(k["term"] != "crypto" for k in client.get("/api/me/keywords").json())
    assert client.delete(f"/api/me/keywords/{kid}").status_code == 404  # not bob's
    assert client.delete("/api/me/subscriptions/rss:hf-blog").status_code == 404

    client.cookies.clear()
    assert client.get("/api/me/subscriptions").status_code == 401
    login(client, "alice", "alicepass123")
    assert any(k["id"] == kid for k in client.get("/api/me/keywords").json())
    assert len(client.get("/api/me/quotas").json()) == 3


def test_legacy_create_all_db_is_stamped(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWSD_DATABASE_URL", f"sqlite:///{tmp_path}/old.db")
    monkeypatch.setenv("NEWSD_SECRET_KEY", "x"); monkeypatch.setenv("NEWSD_ENV_FILE", str(tmp_path / "none.env"))
    from newsd.db import Base, get_engine, init_db, reset_engine
    from newsd.settings import reset_settings
    reset_settings(); reset_engine()
    Base.metadata.create_all(get_engine(), tables=[t for n, t in Base.metadata.tables.items() if n in (
        "users", "invites", "sessions", "api_tokens", "login_failures", "user_settings", "global_settings")])
    init_db()
    assert "sources" in inspect(get_engine()).get_table_names()
    reset_engine(); reset_settings()


def test_default_x_accounts_are_recommended_by_group(client):
    """The followed X accounts from config/collect-sources.json join the onboarding interests by catalog group."""
    _import()
    from newsd.db import SessionLocal
    from newsd.onboarding import preset_groups
    cfg = json.loads((ROOT / "config/collect-sources.json").read_text())
    by_group = {}
    for a in cfg["x"]["accounts"]:
        by_group.setdefault(a["group"], []).append("xa:" + a["handle"])
    db = SessionLocal()
    try:
        groups = {g["name"]: g["sources"] for g in preset_groups(db)}
    finally:
        db.close()
    assert set(by_group["官方·LLM"]) | set(by_group["中文区"]) <= set(groups["ai-news"])
    assert set(by_group["官方·图像/工作流"]) <= set(groups["creative-ai"])
    assert not set(by_group.get("未分组", [])) & {k for v in groups.values() for k in v}
    assert all(len(v) == len(set(v)) for v in groups.values())
