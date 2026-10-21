"""DEPTAI-216 item 3a: card feedback (♥ / 👎 / fold / delete), learned down-ranking, the trash lifecycle, UI prefs,
archive search and the v2 reading pages."""
from datetime import timedelta

from pagekit import login, register, subscribe_all
from poolseed import Pool, now
from sqlalchemy import select


NOISY = ("Qwen rumour about a secret seven trillion model", "Leaked roadmap claims Qwen beats everyone by spring",
         "Anonymous thread: Qwen team quits en masse")
GOOD = ("Qwen paper reports results on long-context retrieval", "Benchmarks: Qwen coder results against open baselines")


def _setup(client, name="alice"):
    """Three items from a noisy source, two from a good one; all match the interest "Qwen". A second call only adds
    another user reading the same pool."""
    from newsd.db import SessionLocal
    from newsd.models import GlobalSetting, PoolItem
    register(client, name)
    db = SessionLocal()
    if db.scalar(select(PoolItem.id).limit(1)) is None:
        db.merge(GlobalSetting(key="topic_cap", value=10)); db.commit()  # all five share the topic "qwen"
        p = Pool(db)
        p.source("rss:noisy", name="Noisy Feed")
        p.source("rss:good", name="Good Feed")
        for i, t in enumerate(NOISY):
            p.item(t, source="rss:noisy", hours=1 + i, url=f"https://noisy.example/{i}")
        for i, t in enumerate(GOOD):
            p.item(t, source="rss:good", summary="benchmarks and numbers", hours=2 + i, url=f"https://good.example/{i}")
    ids = {t: i for i, t in db.execute(select(PoolItem.id, PoolItem.title))}
    subscribe_all(db, name, interest=["Qwen"])
    db.close()
    login(client, name)
    assert client.post("/api/me/issues/build", json={"key": "2026-10-20"}).status_code == 200
    return [ids[t] for t in NOISY], [ids[t] for t in GOOD]


def _items(client, key="latest"):
    return {i["item_id"]: i for i in client.get(f"/api/me/issues/{key}").json()["items"]}


def test_vote_and_fold_are_per_user_and_validated(client):
    noisy, good = _setup(client)
    a = good[0]
    assert client.put(f"/api/me/items/{a}/vote", json={"vote": 1}).json() == {"item_id": a, "vote": 1, "folded": False}
    assert client.put(f"/api/me/items/{a}/fold", json={"folded": True}).json()["folded"] is True
    assert client.put(f"/api/me/items/{a}/vote", json={"vote": 2}).status_code in (400, 422)
    assert client.put("/api/me/items/999999/vote", json={"vote": 1}).status_code == 404
    it = _items(client)[a]
    assert it["vote"] == 1 and it["folded"] is True and it["source_cat"] == "rss"
    home = client.get("/").text
    assert f'data-id="{a}" data-vote="1"' in home and 'data-folded="1"' in home and "已收起 <b>1</b> 条" in home
    # another user never sees (or can set) alice's feedback
    _setup(client, "bob")
    assert all(i["vote"] == 0 and not i["folded"] for i in _items(client).values())
    # bob reads the same pool, so he votes on his own copy; alice's vote is untouched
    assert client.put(f"/api/me/items/{a}/vote", json={"vote": -1}).json()["vote"] == -1
    login(client, "alice")
    assert _items(client)[a]["vote"] == 1


def test_delete_learns_ranks_down_and_can_be_undone(client):
    noisy, good = _setup(client)
    assert client.put(f"/api/me/items/{noisy[0]}/mark", json={"state": "trash"}).json()["state"] == "trash"
    learned = client.get("/api/me/learned").json()
    by_key = {e["key"]: e for e in learned["items"]}
    assert by_key["src:rss:noisy"]["score"] == 1.0 and by_key["src:rss:noisy"]["deletes"] == 1
    assert by_key["kw:qwen"]["score"] == 0.8 and not by_key["kw:qwen"]["forgotten"]
    its = _items(client)
    # the other noisy items are ranked down (source 1.0 + keyword 0.8); the good ones only carry the keyword (0.8 too)
    assert its[noisy[1]]["ranked_down"] and "来源「Noisy Feed」" in its[noisy[1]]["ranked_down_why"]
    assert its[noisy[1]]["penalty"] > its[good[0]]["penalty"]
    home = client.get("/").text
    assert "↓ 已降权" in home and "因你的反馈降权" in home and 'data-what="来源「Noisy Feed」、关键词「Qwen」' in home

    # undo the source only: the keyword still counts
    r = client.post("/api/me/learned/forget", json={"keys": ["src:rss:noisy"]}).json()
    assert {e["key"]: e["forgotten"] for e in r["items"]}["src:rss:noisy"] is True
    assert not _items(client)[noisy[1]]["penalty"] > _items(client)[good[0]]["penalty"]
    r = client.post("/api/me/learned/unforget", json={"keys": ["src:rss:noisy"]}).json()
    assert r["restored"] == 1 and _items(client)[noisy[1]]["penalty"] >= 1.8
    # pausing learning turns all of it off
    assert client.put("/api/me/learned", json={"enabled": False}).json() == {"enabled": False}
    assert not any(i["ranked_down"] for i in _items(client).values())
    client.put("/api/me/learned", json={"enabled": True})
    # rebuilding applies the penalty before the quota cut: noisy items score lower than in a clean build
    before = {i: x["score"] for i, x in _items(client).items()}
    client.post("/api/me/issues/build", json={"key": "2026-10-21"})
    after = _items(client, "2026-10-21")
    assert after[noisy[1]]["score"] < before[noisy[1]]
    # restoring from the trash removes the signal
    assert client.post("/api/me/trash/restore", json={"ids": [noisy[0]]}).json() == {"restored": [noisy[0]]}
    assert client.get("/api/me/learned").json()["items"] == []
    assert not any(i["ranked_down"] for i in _items(client).values())


def test_trash_purge_keeps_signal_unless_forget(client):
    noisy, good = _setup(client)
    for i in noisy[:2]:
        client.put(f"/api/me/items/{i}/mark", json={"state": "trash"})
    trash = client.get("/api/me/trash").json()
    assert {t["item_id"] for t in trash} == set(noisy[:2]) and "来源「Noisy Feed」" in trash[0]["features"]
    assert client.get("/api/me/marks/count").json()["trash"] == 2
    page = client.get("/trash").text
    assert "Qwen rumour about a secret" in page and "已据此减少" in page and "30 天后自动清空" in page and 'data-trash="2"' in page
    # purge one: gone from trash and from the issue page, still down-ranking
    assert client.post("/api/me/trash/purge", json={"ids": [noisy[0]], "forget": False}).json() == {"purged": 1, "forgot": False}
    assert [t["item_id"] for t in client.get("/api/me/trash").json()] == [noisy[1]]
    assert "Qwen rumour about a secret" not in client.get("/").text
    assert client.get("/api/me/learned").json()["items"][0]["deletes"] == 2
    # undo the purge (redelete) and then purge with forget: the signal goes too
    assert client.post("/api/me/trash/redelete", json={"ids": [noisy[0]]}).json() == {"trashed": 1}
    assert len(client.get("/api/me/trash").json()) == 2
    assert client.post("/api/me/trash/purge", json={"ids": None, "forget": True}).json()["purged"] == 2
    assert client.get("/api/me/trash").json() == [] and client.get("/api/me/learned").json()["items"] == []
    # emptying the trash via DELETE /api/me/marks keeps the signal as "purged"
    client.put(f"/api/me/items/{noisy[2]}/mark", json={"state": "trash"})
    assert client.delete("/api/me/marks", params={"state": "trash"}).json() == {"cleared": 1}
    assert client.get("/api/me/learned").json()["items"][0]["deletes"] == 1
    assert "Qwen team quits" not in client.get("/").text


def test_trash_auto_clears_after_30_days(client):
    from newsd.db import SessionLocal
    from newsd.models import ItemMark
    noisy, good = _setup(client)
    client.put(f"/api/me/items/{noisy[0]}/mark", json={"state": "trash"})
    db = SessionLocal()
    m = db.scalar(select(ItemMark).where(ItemMark.item_id == noisy[0]))
    m.created_at = now() - timedelta(days=31)
    db.commit(); db.close()
    assert client.get("/api/me/trash").json() == [] and client.get("/api/me/marks/count").json()["trash"] == 0
    db = SessionLocal()
    assert db.scalar(select(ItemMark.state).where(ItemMark.item_id == noisy[0])) == "purged"
    db.close()
    # a 31-day-old deletion has decayed to about half: still listed, no longer enough to rank down on its own
    src = {e["key"]: e for e in client.get("/api/me/learned").json()["items"]}["src:rss:noisy"]
    assert 0.45 < src["score"] < 0.55


def test_ui_prefs_and_theme_render(client):
    _setup(client)
    assert client.get("/api/me/ui").json() == {"feedback": "both", "motion": "on"}
    assert client.put("/api/me/ui", json={"feedback": "like", "motion": "off"}).json() == {"feedback": "like", "motion": "off"}
    assert client.put("/api/me/ui", json={"feedback": "everything"}).status_code == 400
    assert client.put("/api/me/ui", json={"motion": "fast"}).status_code == 400
    home = client.get("/").text
    assert 'data-fb="like"' in home and 'data-motion="off"' in home and "反馈：♥ 赞" in home
    assert home.count('role="menuitemradio"') == 14 and 'data-theme="arch-editorial"' in home
    assert 'class="tabbar"' in home and 'href="/favorites"' in home and 'href="/calendar"' in home
    s = client.get("/settings").text
    assert 'id="learn"' in s and 'class="cside"' in s and "删除 = 1.0 · 踩 = 0.4" in s and 'data-k="learn"' in s
    themes = client.get("/api/me/theme").json()["themes"]
    assert len(themes) == 14 and {"id": "taste-moat", "name": "品味护城河", "bg": "#0b0b10", "accent": "#d4ff3a", "scheme": "dark"} in themes
    for f in ("nd.js", "app.css", "theme.css", "nd-calendar.js", "issue.js", "archive.js", "favorites.js", "trash.js", "settings.js", "nd.css"):
        assert client.get(f"/static/v2/{f}").status_code == 200, f


def test_favorites_page_and_calendar_marks(client):
    noisy, good = _setup(client)
    client.put(f"/api/me/items/{good[0]}/mark", json={"state": "favorite"})
    client.put(f"/api/me/items/{good[1]}/vote", json={"vote": 1})
    fav = client.get("/favorites").text
    assert GOOD[0] in fav and GOOD[1] in fav  # liked one is listed (hidden by default)
    assert "liked-only" in fav and 'data-fday="20' in fav
    cal = client.get("/calendar", params={"month": "2026-10"}).text
    assert 'href="/issue/2026-10-20" data-d="2026-10-20"><b>20</b><i><s></s></i>' in cal
    # a trashed favourite leaves the favourites page
    client.put(f"/api/me/items/{good[0]}/mark", json={"state": "trash"})
    assert GOOD[0] not in client.get("/favorites").text


def test_archive_search_is_scoped(client):
    noisy, good = _setup(client)
    r = client.get("/api/me/search", params={"q": "qwen results"}).json()
    assert r["total"] == 2 and {i["item_id"] for i in r["items"]} == set(good) and r["items"][0]["key"] == "2026-10-20"
    assert client.get("/api/me/search", params={"q": "noisy feed"}).json()["total"] == 3  # title + source name
    assert client.get("/api/me/search", params={"q": "100%_"}).json()["total"] == 0  # LIKE wildcards are literal
    assert client.get("/api/me/search", params={"q": ""}).json() == {"total": 0, "items": []}
    assert client.get("/api/me/search", params={"q": "x" * 201}).status_code == 400
    client.put(f"/api/me/items/{good[0]}/mark", json={"state": "trash"})
    assert {i["item_id"] for i in client.get("/api/me/search", params={"q": "results"}).json()["items"]} == {good[1]}
    mods = {i["module"] for i in client.get("/api/me/search", params={"q": "qwen"}).json()["items"]}
    m = mods.pop()
    assert all(i["module"] == m for i in client.get("/api/me/search", params={"q": "qwen", "module": m}).json()["items"])
    _setup(client, "bob")
    login(client, "bob")
    assert client.get("/api/me/search", params={"q": "results"}).json()["total"] == 2  # bob's own issue only


def test_migration_0010_round_trip(client):
    from alembic import command
    from sqlalchemy import inspect, text

    from newsd.db import alembic_config, get_engine
    noisy, good = _setup(client)
    client.put(f"/api/me/items/{noisy[0]}/mark", json={"state": "trash"})
    client.post("/api/me/trash/purge", json={"ids": [noisy[0]]})
    client.put(f"/api/me/items/{good[0]}/mark", json={"state": "favorite"})
    command.downgrade(alembic_config(), "0009_216_providers")
    eng = get_engine()
    assert "item_feedback" not in inspect(eng).get_table_names()
    with eng.connect() as c:
        assert [r[0] for r in c.execute(text("SELECT state FROM item_marks"))] == ["favorite"]  # purged rows are v2-only
    command.upgrade(alembic_config(), "head")
    assert "item_feedback" in inspect(get_engine()).get_table_names()
