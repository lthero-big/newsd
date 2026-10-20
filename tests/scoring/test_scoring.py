import asyncio
import json

import httpx
from sqlalchemy import func, select

from llmkit import FakeLLM
from newsd.issues import build_issue_sync
from newsd.llm import keys
from newsd.llm.client import LLMClient, ModelConfig
from newsd.models import Issue, IssueItem, ItemScore
from poolseed import Pool

KEY = "sk-scoring-0123456789abcdef"


def ids(db, user, key="d1"):
    issue = db.scalar(select(Issue).where(Issue.user_id == user.id, Issue.key == key))
    return [ii.item_id for ii in db.scalars(select(IssueItem).where(IssueItem.issue_id == issue.id).order_by(IssueItem.rank))]


def build(db, user, key="d1", **kw):
    kw.setdefault("client", None)
    return build_issue_sync(db, user.id, key, **kw)


def test_two_users_same_pool_different_results(db):
    p = Pool(db)
    qwen = p.item("Qwen 3.5 open weights released", summary="Alibaba's Qwen team ships a new model.", hours=0.5)
    sora = p.item("Sora 2 video model gets audio", summary="OpenAI updates Sora.")
    both = p.item("OpenAI and Qwen benchmark showdown", summary="Comparing Qwen and GPT.")
    misc = p.item("A new database engine", summary="Nothing about AI here.")
    alice = p.user("alice", interest=["Qwen"])
    bob = p.user("bob", interest=["Sora", "OpenAI"], block=["benchmark"])
    ra, rb = build(db, alice), build(db, bob)
    a, b = ids(db, alice), ids(db, bob)
    assert set(a) == {qwen.id, both.id} and a[0] == qwen.id  # title hit + recency
    assert set(b) == {sora.id}  # "both" is blocked for bob, misc matches nobody
    assert misc.id not in a + b
    assert ra["dropped"] == {"no-keyword-match": 2} and rb["dropped"] == {"blocked": 1, "no-keyword-match": 2}
    assert ra["ai"] == "skipped: no model key" and "usage" not in ra
    # scores really are per user: the shared item scores differently for different keyword sets
    ii = db.scalar(select(IssueItem).where(IssueItem.item_id == both.id))
    assert ii.matched[0]["term"] == "Qwen"


def test_full_text_is_matched_when_available(db):
    p = Pool(db)
    deep = p.item("Weekly roundup", summary="short", text="... and deep in the article: DeepSeek V4 drops ...")
    shallow = p.item("Weekly roundup two", summary="short")
    blocked = p.item("Another roundup", summary="short", text="sponsored content about crypto tokens")
    u = p.user("carol", interest=["DeepSeek", "roundup"], block=["crypto"])
    build(db, u)
    got = ids(db, u)
    assert got[0] == deep.id and shallow.id in got and blocked.id not in got
    m = db.scalar(select(IssueItem).where(IssueItem.item_id == deep.id)).matched
    assert {"term": "DeepSeek", "locations": ["body"]}.items() <= {k: v for k, v in next(x for x in m if x["term"] == "DeepSeek").items()}.items()


def test_tag_aliases_and_weights(db):
    p = Pool(db)
    p.tag("LLM", "GPT-5", aliases=["gpt 5"])
    a = p.item("Hands-on with GPT 5", summary="")
    b = p.item("Claude Code tips", summary="")
    u = p.user("dave", interest=["GPT-5"], boost=[("Claude Code", 5.0)])
    build(db, u)
    got = ids(db, u)
    assert got == [b.id, a.id]  # alias matched; boost weight 5 ranks Claude first
    assert db.scalar(select(IssueItem).where(IssueItem.item_id == a.id)).categories == ["GPT-5", "LLM"]


def test_dedup_by_url_and_title_first(db):
    p = Pool(db)
    a = p.item("OpenAI ships GPT-6 with million token context", canonical="https://openai.com/blog/gpt-6", hours=1)
    b = p.item("GPT-6 news", url="https://www.openai.com/blog/gpt-6/?utm_source=x", canonical="https://www.openai.com/blog/gpt-6/", hours=2)
    c = p.item("OpenAI ships GPT-6 with a million token context", hours=3)
    d = p.item("OpenAI pricing update for GPT-6 API", hours=4)
    u = p.user("erin", interest=["GPT-6"], quotas=[{"max_items": 30}])
    rep = build(db, u)
    assert ids(db, u) == [a.id, d.id]
    also = db.scalar(select(IssueItem).where(IssueItem.item_id == a.id)).also
    assert {(x["item_id"], x["by"]) for x in also} == {(b.id, "url"), (c.id, "title")}
    assert rep["dropped"] == {"duplicate-of": 2}


def test_module_group_total_and_topic_caps(db):
    p = Pool(db)
    p.tag("LLM", "Qwen"); p.tag("LLM", "Llama"); p.tag("LLM", "Mistral"); p.tag("图片视频音频", "Sora")
    words = [("ships faster inference kernels", "adds vision support", "tops coding leaderboard"),
             ("license changes explained", "runs on phones now", "gets tool calling"),
             ("raises new funding round", "releases tiny variant", "beats rivals on math")]
    llm = [p.item(f"{name} {words[j][i]}", hours=i + 0.1 * j)
           for j, name in enumerate(["Qwen", "Llama", "Mistral"]) for i in range(3)]
    video = [p.item(t, hours=5 + i) for i, t in enumerate(["Sora makes music videos", "Sora pricing drops",
                                                           "Sora hits Android"])]
    hn = [p.item(t, source="plugin:hn", src_kind="plugin", extra={"points": 300})
          for t in ("Show HN: Qwen in the terminal", "Ask HN: Llama for local search?")]
    # group cap: LLM group in module "news" max 3; module cap: news max 5; total 6; topic cap 2 (default)
    u = p.user("frank", interest=["Qwen", "Llama", "Mistral", "Sora"],
               quotas=[{"max_items": 6}, {"module": "news", "max_items": 5},
                       {"module": "news", "group_name": "LLM", "max_items": 3, "categories": ["LLM"]}])
    rep = build(db, u)
    rows = db.scalars(select(IssueItem).join(Issue).where(Issue.user_id == u.id).order_by(IssueItem.rank)).all()
    by_mod = {}
    for r in rows:
        by_mod.setdefault(r.module, []).append(r)
    assert len(rows) == 6  # total cap
    assert len(by_mod["news"]) == 5  # module cap
    assert sum(1 for r in by_mod["news"] if r.group_name == "LLM") == 3  # group cap
    assert len(by_mod["community"]) == 1  # HN items are "community"; topic cap/total cut the rest
    news_topics = [json.dumps(sorted(m["term"] for m in r.matched)) for r in by_mod["news"]]
    assert max(news_topics.count(t) for t in news_topics) <= 2  # topic cap: at most 2 per hot topic
    d = rep["dropped"]
    assert d.get("group-cap", 0) >= 1 and d.get("topic-cap", 0) >= 1 and d.get("total-cap", 0) >= 1
    issue = db.scalar(select(Issue).where(Issue.user_id == u.id))
    assert issue.stats["caps"]["total"] == 6 and issue.stats["caps"]["modules"]["news"] == 5
    assert all(x["reason"] for x in issue.stats["dropped_items"])

    # without quotas the same pool fills more slots (defaults: total 30, news 8, community 8)
    g = p.user("gina", interest=["Qwen", "Llama", "Mistral", "Sora"])
    build(db, g)
    assert len(ids(db, g)) > 6


def test_no_interest_keywords_keeps_everything_and_rebuild_replaces(db):
    p = Pool(db)
    a = p.item("Alpha story")
    u = p.user("hank")
    build(db, u)
    assert ids(db, u) == [a.id]
    b = p.item("Beta story", hours=0.1)
    build(db, u)  # same key: replaced, not duplicated
    assert ids(db, u) == [b.id, a.id]
    assert db.scalar(select(func.count()).select_from(Issue).where(Issue.user_id == u.id)) == 1


def _scoring_llm(scores):
    def reply(system, user, body, request):
        if "输出格式" in system:
            items = json.loads(user)
            out = []
            for i in items:
                s = scores.get(i["id"])
                if s is None:
                    continue  # missing verdict -> recorded as empty
                out.append({"id": i["id"], "belongs": s > 0, "score": s, "section": "模型发布", "reason": "r"})
            return json.dumps({"items": out})
        if "分组" in system:
            return '{"groups": []}'
        if "主编" in system:
            return json.dumps({"order": [i["id"] for i in json.loads(user)]})
        if "翻译成" in system:
            return json.dumps({"translations": [{"i": r["i"], "text": "译" + str(r["i"])} for r in json.loads(user)]},
                              ensure_ascii=False)
        raise AssertionError(system[:60])
    return reply


def test_model_layer_on_top_of_rules(db, monkeypatch):
    p = Pool(db)
    hi = p.item("Qwen model one", hours=5)
    lo = p.item("Qwen model two", hours=1)
    unk = p.item("Qwen model three", hours=2)
    off = p.item("Unrelated thing", hours=1)
    u = p.user("ivy", interest=["Qwen", "model"])
    keys.save_settings(db, u.id, "openai", "gpt-4o-mini", KEY)
    fake = FakeLLM("openai", _scoring_llm({hi.id: 9, lo.id: 3}))  # unk gets no verdict
    monkeypatch.setattr(keys, "TRANSPORT", fake.transport)
    rep = build(db, u, client="auto")
    got = ids(db, u)
    assert rep["ai"] == "on" and rep["model"] == "openai/gpt-4o-mini"
    assert got[0] == hi.id and unk.id in got and lo.id not in got and off.id not in got
    rows = {r.item_id: r for r in db.scalars(select(IssueItem))}
    assert rows[hi.id].llm_score == 9 and rows[hi.id].score == round(0.4 * rows[hi.id].rule_score + 54, 2)
    assert rows[hi.id].rule_score < rows[unk.id].rule_score  # older, but the model verdict lifts it to the top
    assert rows[unk.id].llm_score is None and rows[unk.id].score == round(0.4 * rows[unk.id].rule_score + 30, 2)
    assert rep["dropped"].get("model-below-threshold") == 1
    assert rows[hi.id].title_translated and rows[hi.id].title_translated.startswith("译")
    st = {s.item_id: s.status for s in db.scalars(select(ItemScore))}
    assert st[hi.id] == "ok" and st[unk.id] == "empty"
    assert all(c["headers"]["authorization"] == f"Bearer {KEY}" for c in fake.calls)


def test_model_auth_error_falls_back_to_rules(db):
    p = Pool(db)
    a = p.item("Qwen thing")
    u = p.user("jack", interest=["Qwen"])
    fake = FakeLLM("anthropic", lambda *a: httpx.Response(401, json={"error": {"message": "bad"}}))

    async def go():
        async with LLMClient(ModelConfig("anthropic", "claude-x", KEY), rpm=0, transport=fake.transport) as c:
            from newsd.issues import build_issue
            return await build_issue(db, u.id, "d1", client=c)
    rep = asyncio.run(go())
    assert rep["stopped"] is True and ids(db, u) == [a.id] and len(fake.calls) == 1
