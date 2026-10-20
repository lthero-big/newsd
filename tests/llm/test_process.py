import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select

from llmkit import FakeLLM
from newsd.llm import keys
from newsd.llm.client import LLMClient, ModelConfig
from newsd.llm.process import process_user
from newsd.models import ItemScore, Keyword, PoolItem, PoolItemSource, Source, Subscription, TranslationCache, User

KEY = "sk-proc-0123456789abcdef"
TITLES = [
    "OpenAI releases GPT-6 with longer context",     # 1 high
    "GPT-6 launched by OpenAI today",                # 2 same event as 1, lower
    "New crypto token pumps 300%",                   # 3 does not belong
    "Qwen3.5 open weights now on Hugging Face",      # 4 medium
    "通义千问发布新模型",                               # 5 Chinese: no translation needed
]
SCORES = {1: 9, 2: 7, 3: 0, 4: 6.5, 5: 5}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _seed(db, users=("alice",)):
    src = Source(key="rss:t", kind="rss", name="t", url="https://t.example/rss", config={})
    db.add(src); db.commit()
    us = []
    for name in users:
        u = User(username=name, password_hash="x"); db.add(u); db.commit()
        db.add(Subscription(user_id=u.id, source_id=src.id)); us.append(u)
    for i, t in enumerate(TITLES, 1):
        it = PoolItem(url_key=f"k{i}", canonical_url=f"https://t.example/{i}", url=f"https://t.example/{i}", title=t,
                      summary=f"summary {i}" if i != 5 else "中文摘要", published_at=_now() - timedelta(hours=i))
        db.add(it); db.commit()
        db.add(PoolItemSource(item_id=it.id, source_id=src.id, url=it.url, cycle="c1"))
    db.commit()
    return us


def responder(bad_scoring=False):
    def reply(system, user, body, request):
        if "fix malformed JSON" in system:
            return "still not json" if bad_scoring else '{"items": []}'
        if "输出格式" in system:  # profile scoring
            if bad_scoring:
                return "I think they are all great!"
            items = json.loads(user)
            return json.dumps({"items": [{"id": i["id"], "belongs": SCORES[i["id"]] > 0, "score": SCORES[i["id"]],
                                          "section": "模型发布", "reason": "r"} for i in items]}, ensure_ascii=False)
        if "分组" in system:
            return '{"groups": [[1, 2]]}'
        if "主编" in system:
            ids = [i["id"] for i in json.loads(user)]
            return json.dumps({"order": list(reversed(ids))})
        if "翻译成" in system:
            rows = json.loads(user)
            return json.dumps({"translations": [{"i": r["i"], "text": "译:" + r["text"]} for r in rows]}, ensure_ascii=False)
        raise AssertionError("unexpected prompt: " + system[:80])
    return reply


def _client(fake, provider="openai"):
    return LLMClient(ModelConfig(provider, "m-1", KEY), rpm=0, transport=fake.transport)


async def _run(db, uid, client, **kw):
    if client is None or client == "auto":
        return await process_user(db, uid, client=client, **kw)
    async with client:
        return await process_user(db, uid, client=client, **kw)


def test_no_key_pipeline_runs_without_model_calls(db):
    (u,) = _seed(db)
    db.add(Keyword(user_id=u.id, kind="block", term="crypto"))
    db.add(Keyword(user_id=u.id, kind="boost", term="qwen", weight=5)); db.commit()
    rep = asyncio.run(_run(db, u.id, "auto"))  # no key stored -> autouse transport would fail on any call
    assert rep["ai"] == "skipped: no model key" and rep["model"] is None and "usage" not in rep
    ids = [c["id"] for c in rep["items"]]
    assert 3 not in ids and ids[0] == 4 and len(ids) == 4  # block + boost + recency
    assert rep["translation"]["model_calls"] is False and rep["translation"]["translated"] == 0
    assert all(c["title_translated"] is None for c in rep["items"])
    assert db.scalar(select(func.count()).select_from(ItemScore)) == 0


def test_full_ai_path_openai(db):
    (u,) = _seed(db)
    fake = FakeLLM("openai", responder())
    rep = asyncio.run(_run(db, u.id, _client(fake)))
    assert rep["ai"] == "on" and rep["scored"] == 5 and rep["empty"] == 0
    ids = [c["id"] for c in rep["items"]]
    # threshold 6 (ai-news): 1, 2, 4 pass; 3 does not belong; 5 scores 5. Dedupe merges 2 into 1, re-rank reverses.
    assert ids == [4, 1]
    assert rep["dedupe"] == "merged 1" and rep["rerank"] == "ok"
    one = next(c for c in rep["items"] if c["id"] == 1)
    assert one["duplicates"] == [2] and one["score"] == 9 and one["section"] == "模型发布"
    assert one["title_translated"] == "译:" + TITLES[0]
    assert all(c["url"].startswith("https://t.example/") for c in rep["items"])
    assert db.scalar(select(func.count()).select_from(ItemScore).where(ItemScore.status == "ok")) == 5

    # second run reuses ok verdicts and cached translations: no scoring / translation calls
    n = len(fake.calls)
    rep2 = asyncio.run(_run(db, u.id, _client(fake)))
    systems = [c["system"] for c in fake.calls[n:]]
    assert rep2["reused"] == 5 and not any("输出格式" in s or "翻译成" in s for s in systems)
    assert rep2["translation"]["cache_hits"] >= 2


def test_thresholds_are_per_user(db):
    (u,) = _seed(db)
    from newsd.llm.profiles import set_picks
    set_picks(db, u.id, [{"name": "ai-news", "threshold": 9}])
    fake = FakeLLM("openai", responder())
    rep = asyncio.run(_run(db, u.id, _client(fake), dedupe=False, rerank=False))
    assert [c["id"] for c in rep["items"]] == [1]


def test_invalid_json_recorded_empty_run_continues_anthropic(db):
    (u,) = _seed(db)
    fake = FakeLLM("anthropic", responder(bad_scoring=True))
    rep = asyncio.run(_run(db, u.id, _client(fake, "anthropic")))
    assert rep["empty"] == 5 and rep["scored"] == 0
    assert rep["ai"].startswith("fallback")  # nothing usable -> rule ranking kept, run still produces a list
    assert len(rep["items"]) == 5 and rep["usage"]["invalid_json"] >= 1 and rep["usage"]["repairs"] >= 1
    rows = db.scalars(select(ItemScore)).all()
    assert len(rows) == 5 and all(r.status == "empty" and r.score is None for r in rows)
    # anthropic requests carried the key in x-api-key, never in Authorization
    assert all(c["headers"]["x-api-key"] == KEY and "authorization" not in c["headers"] for c in fake.calls)


def test_auth_error_stops_model_calls_and_falls_back(db):
    (u,) = _seed(db)
    fake = FakeLLM("openai", lambda *a: httpx.Response(401, json={"error": {"message": "bad key"}}))
    rep = asyncio.run(_run(db, u.id, _client(fake)))
    assert rep["stopped"] is True and rep["ai"].startswith("fallback") and len(rep["items"]) == 5
    assert rep["translation"]["model_calls"] is True and rep["translation"]["translated"] == 0
    assert len(fake.calls) == 1  # first batch failed with auth; nothing else was attempted


def test_translation_cache_shared_between_users(db):
    a, b = _seed(db, ("alice", "bob"))
    fa = FakeLLM("openai", responder())
    asyncio.run(_run(db, a.id, _client(fa), dedupe=False, rerank=False))
    cached = db.scalar(select(func.count()).select_from(TranslationCache))
    assert cached >= 3
    # bob has no key at all: still gets the shared cached translations, with zero model calls
    rep = asyncio.run(_run(db, b.id, None))
    assert rep["ai"] == "skipped: no model key"
    tr = {c["id"]: c["title_translated"] for c in rep["items"]}
    assert tr[1] == "译:" + TITLES[0] and tr[5] is None  # Chinese title needs no translation
    # bob with his own key: translation misses only, no repeat for cached texts
    fb = FakeLLM("openai", responder())
    asyncio.run(_run(db, b.id, _client(fb), dedupe=False, rerank=False))
    tcalls = [c for c in fb.calls if "翻译成" in c["system"]]
    for c in tcalls:
        assert TITLES[0] not in c["user"]
    row = db.scalar(select(TranslationCache).where(TranslationCache.source_text == TITLES[0]))
    assert row.hits >= 2 and row.model == "openai/m-1"


def test_auto_client_uses_stored_encrypted_key(db, monkeypatch):
    (u,) = _seed(db)
    keys.save_settings(db, u.id, "openai", "gpt-4o-mini", KEY)
    fake = FakeLLM("openai", responder())
    monkeypatch.setattr(keys, "TRANSPORT", fake.transport)
    rep = asyncio.run(_run(db, u.id, "auto"))
    assert rep["model"] == "openai/gpt-4o-mini" and rep["ai"] == "on"
    assert all(c["headers"]["authorization"] == f"Bearer {KEY}" for c in fake.calls)
