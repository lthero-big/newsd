"""DEPTAI-216 item 3b: source categories and zones (rename / reorder / switch off / 特别关注 / move sources),
their effect on the daily issue, the /sources and /models pages, and migration 0011."""
from pagekit import login, register, subscribe_all
from poolseed import Pool
from sqlalchemy import select

ZH = ("Qwen 发布 新版 推理 引擎", "DeepSeek 量化 工具 开源 社区 反响", "Gemma 长文本 评测 结果 公布")
EN = ("Llama beats baselines on coding benchmarks", "Hardware vendors ship Mistral ready accelerators")
RD = ("Running Phi locally on a single consumer gpu",)
TERMS = ["Qwen", "DeepSeek", "Gemma", "Llama", "Mistral", "Phi"]  # one each: the per-topic cap is 2


def _setup(client, name="alice"):
    from newsd.db import SessionLocal
    from newsd.models import PoolItem, Source
    register(client, name)
    db = SessionLocal()
    if db.scalar(select(PoolItem.id).limit(1)) is None:
        p = Pool(db)
        p.source("rss:zh", name="中文源")
        p.source("rss:en", name="English Feed")
        p.source("reddit:localllama", kind="reddit", name="r/LocalLLaMA")
        for key, lang in (("rss:zh", "zh"), ("rss:en", "en")):
            db.scalar(select(Source).where(Source.key == key)).language = lang
        db.commit()
        for i, t in enumerate(ZH):
            p.item(t, source="rss:zh", hours=1 + i)
        for i, t in enumerate(EN):
            p.item(t, source="rss:en", hours=1 + i)
        for t in RD:
            p.item(t, source="reddit:localllama", src_kind="reddit", hours=2, extra={"points": 50})
    subscribe_all(db, name, interest=TERMS)
    db.close()
    login(client, name)


def _cat(view, key):
    return next(c for c in view["cats"] if c["key"] == key)


def _zone(view, cat, name):
    return next(z for z in _cat(view, cat)["zones"] if z["name"] == name)


def _issue(client):
    assert client.post("/api/me/issues/build", json={"key": "2026-10-21"}).status_code == 200
    return client.get("/api/me/issues/2026-10-21").json()["items"]


def test_default_zones_rename_reorder_and_validation(client):
    _setup(client)
    v = client.get("/api/me/zones").json()
    assert [c["key"] for c in v["cats"]] == ["x", "reddit", "hn", "rss", "github", "hf", "arxiv"]
    rss = _cat(v, "rss")
    assert [z["name"] for z in rss["zones"]] == ["中文", "英文"]  # placed by source language
    assert [s["key"] for s in _zone(v, "rss", "中文")["sources"]] == ["rss:zh"]
    assert [s["key"] for s in _zone(v, "reddit", "默认")["sources"]] == ["reddit:localllama"]
    # create / rename / duplicate names / reorder
    z = client.post("/api/me/zones", json={"category": "rss", "name": "  特别   关注 ", "special": True}).json()
    assert z["name"] == "特别 关注" and z["special"] is True
    assert client.post("/api/me/zones", json={"category": "rss", "name": "中文"}).status_code == 409
    assert client.post("/api/me/zones", json={"category": "nope", "name": "x"}).status_code == 400
    assert client.post("/api/me/zones", json={"category": "rss", "name": "   "}).status_code == 400
    zh, en = _zone(v, "rss", "中文")["id"], _zone(v, "rss", "英文")["id"]
    assert client.patch(f"/api/me/zones/{zh}", json={"name": "英文"}).status_code == 409
    assert client.patch(f"/api/me/zones/{zh}", json={"name": "华语"}).json()["name"] == "华语"
    assert client.put("/api/me/zones/order", params={"category": "rss"}, json={"ids": [z["id"], en, zh]}).status_code == 200
    assert [x["name"] for x in _cat(client.get("/api/me/zones").json(), "rss")["zones"]] == ["特别 关注", "英文", "华语"]
    assert client.put("/api/me/zones/order", params={"category": "rss"}, json={"ids": [en, zh]}).status_code == 400
    # category order is saved; unknown keys are dropped and missing ones appended
    r = client.put("/api/me/source-cats", json={"order": ["rss", "bogus", "x"]}).json()
    assert r["order"][:3] == ["rss", "x", "reddit"] and len(r["order"]) == 7
    assert [c["key"] for c in client.get("/api/me/zones").json()["cats"]][:2] == ["rss", "x"]
    # another user can neither see nor touch alice's zones
    _setup(client, "bob")
    assert client.patch(f"/api/me/zones/{zh}", json={"name": "mine"}).status_code == 404
    assert client.delete(f"/api/me/zones/{zh}").status_code == 404
    assert [z["name"] for z in _cat(client.get("/api/me/zones").json(), "rss")["zones"]] == ["中文", "英文"]


def test_move_sources_between_zones_weight_and_category_checks(client):
    _setup(client)
    v = client.get("/api/me/zones").json()
    zh, en = _zone(v, "rss", "中文")["id"], _zone(v, "rss", "英文")["id"]
    reddit = _zone(v, "reddit", "默认")["id"]
    # a drag from 中文 to 英文: the target's full list, then the source zone's
    assert client.put(f"/api/me/zones/{en}/sources", json={"keys": ["rss:zh", "rss:en"]}).status_code == 200
    assert client.put(f"/api/me/zones/{zh}/sources", json={"keys": []}).status_code == 200
    v = client.get("/api/me/zones").json()
    assert [s["key"] for s in _zone(v, "rss", "英文")["sources"]] == ["rss:zh", "rss:en"] and _zone(v, "rss", "中文")["sources"] == []
    # sources only move inside their own category
    assert client.put(f"/api/me/zones/{reddit}/sources", json={"keys": ["rss:en"]}).status_code == 400
    assert client.patch("/api/me/subscriptions/rss:en", json={"zone_id": reddit}).status_code == 400
    assert client.put(f"/api/me/zones/{en}/sources", json={"keys": ["rss:en", "rss:en"]}).status_code == 400
    assert client.put(f"/api/me/zones/{en}/sources", json={"keys": ["rss:nope"]}).status_code == 404
    # weight / enabled / zone per subscription
    r = client.patch("/api/me/subscriptions/rss:en", json={"weight": 2, "enabled": False, "zone_id": zh}).json()
    assert r == {"key": "rss:en", "enabled": False, "weight": 2, "zone_id": zh}
    assert client.patch("/api/me/subscriptions/rss:en", json={"weight": 3}).status_code == 422
    # delete a zone: its sources are unsubscribed; add one back from the catalog straight into a zone
    d = client.delete(f"/api/me/zones/{zh}").json()
    assert d["unsubscribed"] == ["rss:en"]
    v = client.get("/api/me/zones").json()
    assert [s["key"] for s in _cat(v, "rss")["catalog"]] == ["rss:en"]
    assert client.post(f"/api/me/zones/{en}/sources", json={"key": "rss:en", "weight": 1, "position": 0}).json()["weight"] == 1
    v = client.get("/api/me/zones").json()
    assert [(s["key"], s["weight"]) for s in _zone(v, "rss", "英文")["sources"]] == [("rss:en", 1), ("rss:zh", 0)]
    assert client.post(f"/api/me/zones/{reddit}/sources", json={"key": "rss:en"}).status_code == 400
    # 最近 3 条 comes from the pool, no fetch
    r = client.get("/api/me/sources/recent", params={"key": "rss:zh"}).json()
    assert len(r["items"]) == 3 and r["items"][0]["title"] == ZH[0]


def test_zones_shape_the_daily_issue(client):
    _setup(client)
    v = client.get("/api/me/zones").json()
    zh, en = _zone(v, "rss", "中文"), _zone(v, "rss", "英文")
    titles = lambda: {i["title"] for i in _issue(client)}  # noqa: E731
    assert set(ZH) | set(EN) | set(RD) <= titles()
    # a disabled zone switches its sources off (their per-source switch is untouched)
    client.patch(f"/api/me/zones/{zh['id']}", json={"enabled": False})
    got = titles()
    assert not set(ZH) & got and set(EN) <= got
    assert _zone(client.get("/api/me/zones").json(), "rss", "中文")["sources"][0]["enabled"] is True
    client.patch(f"/api/me/zones/{zh['id']}", json={"enabled": True})
    # a total quota of 1: only the best item survives ...
    assert client.put("/api/me/quotas", json=[{"max_items": 1}]).status_code == 200
    assert len(_issue(client)) == 1
    # ... unless it comes from a 特别关注 zone: those skip every cap and do not use it up
    client.patch(f"/api/me/zones/{en['id']}", json={"special": True})
    got = titles()
    assert set(EN) <= got and len(got) == len(EN) + 1
    # weight 2 (x1.6) lifts a source's items
    client.patch(f"/api/me/zones/{en['id']}", json={"special": False})
    client.put("/api/me/quotas", json=[])
    base = {i["title"]: i["score"] for i in _issue(client)}
    client.patch("/api/me/subscriptions/reddit:localllama", json={"weight": 2})
    after = {i["title"]: i["score"] for i in _issue(client)}
    assert after[RD[0]] > base[RD[0]] and after[EN[0]] == base[EN[0]]


def test_special_zone_is_never_down_ranked(client):
    _setup(client)
    items = _issue(client)
    zh_ids = [i["item_id"] for i in items if i["title"] in ZH]
    for iid in zh_ids[:2]:
        client.put(f"/api/me/items/{iid}/mark", json={"state": "trash"})
    left = {i["item_id"]: i for i in client.get("/api/me/issues/2026-10-21").json()["items"]}
    assert left[zh_ids[2]]["ranked_down"]
    zh = _zone(client.get("/api/me/zones").json(), "rss", "中文")
    client.patch(f"/api/me/zones/{zh['id']}", json={"special": True})
    left = {i["item_id"]: i for i in client.get("/api/me/issues/2026-10-21").json()["items"]}
    assert not left[zh_ids[2]]["ranked_down"] and left[zh_ids[2]]["source_section"] == "特别关注"


def test_sources_and_models_pages(client):
    _setup(client)
    s = client.get("/sources")
    assert s.status_code == 200 and 'href="/sources" data-k="sources" aria-current="page"' in s.text
    assert "window.ND_ZONES=" in s.text and "rss:zh" in s.text and "/static/v2/sources.js" in s.text and "/static/v2/dnd.js" in s.text
    m = client.get("/models")
    assert m.status_code == 200 and "故障切换链" in m.text and "AI 内容类别" in m.text and 'data-k="models"' in m.text
    st = client.get("/settings").text
    assert 'data-source="' not in st and 'id="prov-list"' not in st and "关键词、配额与推送" in st and 'href="/sources"' in st
    for f in ("sources.js", "models.js", "dnd.js"):
        assert client.get(f"/static/v2/{f}").status_code == 200
    client.cookies.clear()
    assert client.get("/sources", follow_redirects=False).status_code in (302, 303, 307)
    assert client.get("/api/me/zones").status_code == 401


def test_migration_0011_places_existing_subscriptions_and_round_trips(client):
    from alembic import command
    from sqlalchemy import inspect, text

    from newsd.db import alembic_config, get_engine
    _setup(client)
    v = client.get("/api/me/zones").json()
    client.patch(f"/api/me/zones/{_zone(v, 'rss', '中文')['id']}", json={"special": True})
    command.downgrade(alembic_config(), "0010_216_feedback")
    eng = get_engine()
    assert "source_zones" not in inspect(eng).get_table_names()
    assert "zone_id" not in {c["name"] for c in inspect(eng).get_columns("subscriptions")}
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM subscriptions")).scalar() == 3  # subscriptions survive
    command.upgrade(alembic_config(), "head")
    eng = get_engine()
    with eng.connect() as c:
        rows = c.execute(text("SELECT z.category, z.name, z.special, src.key FROM subscriptions s JOIN source_zones z ON z.id = s.zone_id "
                              "JOIN sources src ON src.id = s.source_id ORDER BY src.key")).fetchall()
    assert [(r[0], r[1], bool(r[2]), r[3]) for r in rows] == [("reddit", "默认", False, "reddit:localllama"),
                                                             ("rss", "英文", False, "rss:en"), ("rss", "中文", False, "rss:zh")]
