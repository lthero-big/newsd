import os; os.environ.setdefault("NEWS_NO_MEDIA", "1")
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate  # noqa: E402
import plugins  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 16, 5, tzinfo=timezone.utc)
TAGS = json.loads((ROOT / "config/tags.json").read_text(encoding="utf-8"))
TERMS = generate.tag_terms(TAGS)


def raw(source, title, url, points=0, comments=0, hours=2, kind="post", feed=None, text=""):
    return {"source": source, "title": title, "url": url, "points": points, "comments": comments, "kind": kind,
            "published_at": (NOW - timedelta(hours=hours)).isoformat(), "listing_text": text,
            "source_links": [{"source": source, "url": url}], "feed": feed or source}


class GenerateTests(unittest.TestCase):
    def build(self, items):
        return generate.build("2026-10-16", items, {}, TERMS, TAGS, NOW - timedelta(hours=24), NOW, [], 5, False)

    def test_tags_include_video_audio_terms(self):
        out = self.build([raw("hn", "Sora 2 and Veo 4 compared with Kling", "https://example.com/a", 300, 50),
                          raw("hn", "ElevenLabs ships new voice model; Suno answers", "https://example.com/b", 200, 20)])
        for card in out["items"]:
            self.assertIn("图片视频音频", card["tags"])

    def test_community_needs_ai_term(self):
        flock = raw("hn", "Ousted Savannah police officer gave ICE agent husband access to Flock system",
                    "https://www.wjcl.com/article/savannah-police-officer-ice-agent-flock-access/74135816", 566, 302,
                    text="Savannah police officer fired over ICE access to Flock - WJCL")
        kept = [raw("hn", "Open ChatGPT", "https://example.com/open-chatgpt", 300, 120),
                raw("hn", "Running a 30B open-weights model locally on a 4090", "https://example.com/local", 900, 200),
                raw("hn", "Show HN: A coding agent that runs entirely in the terminal", "https://example.com/coding-agent", 250, 80),
                raw("hn", "Claude Code now supports background tasks", "https://example.com/cc", 400, 150)]
        out = self.build([flock, *kept])
        titles = [c["title"] for c in out["items"]]
        self.assertNotIn(flock["title"], titles)
        for k in kept:
            self.assertIn(k["title"], titles)
        self.assertEqual(out["stats"]["dropped"]["off_topic"], 1)

    def test_community_ambiguous_words_need_context(self):
        off = [raw("hn", "The new Ford model lineup and its agents", "https://example.com/ford", 500, 100),
               raw("hn", "Gemini season horoscope thread", "https://example.com/astro", 500, 100)]
        on = raw("hn", "Why language models hallucinate", "https://example.com/lm", 500, 100)
        titles = [c["title"] for c in self.build([*off, on])["items"]]
        self.assertEqual(titles, [on["title"]])

    def test_no_keyword_posts_dropped_and_repos_go_to_opensource(self):
        out = self.build([raw("hn", "Gardening tips for autumn", "https://example.com/g", 900, 300),
                          raw("github", "acme/agent-kit", "https://github.com/acme/agent-kit", 800, 20, kind="repo", text="An AI agent toolkit")])
        self.assertEqual([c["module"] for c in out["items"]], ["opensource"])
        self.assertEqual(out["stats"]["dropped"]["no_keyword"], 1)

    def test_release_with_model_name_is_model_news(self):
        out = self.build([raw("reddit", "DeepSeek released V4 open weights", "https://www.reddit.com/r/LocalLLaMA/comments/x1/ds/", 500, 80, feed="LocalLLaMA:new")])
        self.assertEqual(out["items"][0]["module"], "model")
        self.assertEqual(out["items"][0]["source_label"], "r/LocalLLaMA")

    def test_url_and_title_dedupe(self):
        out = self.build([raw("hn", "Qwen 3.5 released with open weights today", "https://qwen.ai/blog?utm_source=hn", 300),
                          raw("reddit", "Qwen 3.5 released with open weights today!", "https://qwen.ai/blog", 100)])
        self.assertEqual(len(out["items"]), 1)
        self.assertEqual(out["items"][0]["source"], "hn")

    def test_score_prefers_engagement_and_recency(self):
        fresh = generate.score({**raw("hn", "x", "https://e.com/1", 400, 100, hours=1), "matched": []}, NOW)
        stale = generate.score({**raw("hn", "x", "https://e.com/2", 400, 100, hours=60), "matched": []}, NOW)
        quiet = generate.score({**raw("hn", "x", "https://e.com/3", 2, 0, hours=1), "matched": []}, NOW)
        self.assertGreater(fresh["total"], stale["total"])
        self.assertGreater(fresh["total"], quiet["total"])

    def test_quota_and_release_feed_cap(self):
        items = [raw("rss", f"v1.{i} llama.cpp release", f"https://github.com/ggml-org/llama.cpp/releases/tag/b{i}", kind="release", feed="llamacpp") for i in range(8)]
        out = self.build(items)
        self.assertLessEqual(len(out["items"]), 2)

    def test_stable_id_matches_identity_mjs_rules(self):
        self.assertEqual(generate.stable_id("https://Example.com/a?b=2&utm_source=x&a=1"), generate.stable_id("https://example.com/a?a=1&b=2"))

    def test_x_plugin_merges_file_and_skips_when_absent(self):
        ctx = plugins.Context(start=NOW - timedelta(hours=24), end=NOW, sources={}, x_items=None)
        items, stats = plugins.x_items(ctx)
        self.assertEqual(items, [])
        self.assertIn("skipped", stats)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            p.write_text(json.dumps({"items": [{"url": "https://x.com/a/status/1", "text": "Kling 3 video model is out", "author": "a", "created_at": NOW.isoformat(), "likes": 10, "reposts": 2}]}))
            ctx.x_items = p
            items, _ = plugins.x_items(ctx)
            self.assertEqual(items[0]["points"], 14)
            self.assertEqual(items[0]["source"], "x")

    def test_plugin_failure_is_isolated(self):
        plugins.REGISTRY["boom"] = lambda ctx: 1 / 0
        try:
            items, stats = plugins.run_all(plugins.Context(start=NOW, end=NOW, sources={}), ["boom"])
        finally:
            del plugins.REGISTRY["boom"]
        self.assertEqual(items, [])
        self.assertTrue(stats["boom"]["errors"])


if __name__ == "__main__":
    unittest.main()
