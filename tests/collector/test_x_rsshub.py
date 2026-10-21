import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import x_rsshub
import source_config as sc

NOW = datetime(2026, 10, 16, 9, tzinfo=timezone.utc)
def item(i, h="OpenAI", when="2026-10-16T01:00:00.000Z", body=None):
    return {"url": f"https://x.com/{h}/status/{1000 + i}", "date_published": when,
            "content_html": body or f"Post {i}: a long enough announcement about a new model release today<br><video width=\"1920\" height=\"1080\" src=\"https://video.twimg.com/v.mp4\" poster=\"https://pbs.twimg.com/media/A?format=jpg&amp;name=orig\">",
            "authors": [{"name": "OpenAI", "avatar": "https://pbs.twimg.com/profile_images/1/a_normal.jpg"}]}

class T(unittest.TestCase):
    def test_run(self):
        feeds = {"OpenAI": [item(1), item(2), item(3), item(4, when="2026-10-10T00:00:00Z")],
                 "karpathy": [item(5, h="someoneelse")], "bad": None}
        def f(base, key, h, t):
            if feeds[h] is None: raise OSError(f"503 for {base}?key={key}")
            return feeds[h]
        r = x_rsshub.run([("OpenAI", "官方·LLM", []), ("karpathy", "特别关注", []), ("bad", "x", [])], "https://r", "SECRET", per=2, pace=0, fetcher=f, now=NOW, sleep=lambda s: None)
        self.assertEqual([i["url"][-4:] for i in r["items"]], ["1001", "1002"])
        self.assertEqual(r["ok"], ["OpenAI", "karpathy"])
        self.assertNotIn("SECRET", r["failed"][0]["error"])
        m = r["items"][0]["media"][0]
        self.assertEqual((m["width"], m["url"]), (1920, "https://pbs.twimg.com/media/A?format=jpg&name=orig"))
        self.assertTrue(r["items"][0]["avatar"].endswith("_400x400.jpg"))

    def test_retry_and_breaker(self):
        import ssl
        import urllib.error
        calls, slept = {}, []
        def flaky(base, key, h, t):  # first attempt hits a TLS EOF, the retry works
            calls[h] = calls.get(h, 0) + 1
            if h == "OpenAI" and calls[h] == 1:
                raise urllib.error.URLError(ssl.SSLEOFError(8, "EOF occurred in violation of protocol"))
            if h == "gone":
                raise urllib.error.HTTPError("https://r", 404, "nf", {}, None)
            return [item(1)]
        r = x_rsshub.run([("OpenAI", "g", []), ("gone", "g", [])], "https://r", "K", pace=0, fetcher=flaky, now=NOW, sleep=slept.append)
        self.assertEqual((r["ok"], calls), (["OpenAI"], {"OpenAI": 2, "gone": 1}))  # 404 is not retried
        def dead(base, key, h, t):
            calls[h] = calls.get(h, 0) + 1
            raise urllib.error.URLError(ssl.SSLEOFError(8, "EOF occurred in violation of protocol"))
        calls.clear()
        accts = [(f"a{i}", "g", []) for i in range(43)]
        r = x_rsshub.run(accts, "https://r", "K", pace=0, fetcher=dead, now=NOW, sleep=slept.append, breaker=5)
        self.assertEqual((len(calls), sum(calls.values()), len(r["failed"]), r["ok"]), (5, 15, 43, []))
        self.assertIn("circuit_open", r)
        self.assertTrue(r["failed"][-1]["error"].startswith("skipped"))


class Apply(unittest.TestCase):
    def test_card_added_sources(self):
        src = {"rss": [{"id": "a", "url": "https://a.com/feed"}], "reddit": {"feeds": [{"subreddit": "LocalLLaMA", "mode": "new"}]}}
        cfg = {"feeds": [{"id": "gh-x-y", "url": "https://github.com/x/y/releases.atom", "label": "x/y"}, {"id": "dup", "url": "https://a.com/feed"}],
               "subreddits": ["localllama", "comfyui"], "hf_orgs": ["Qwen", "bfl"], "disabled": ["hforg:bfl", "rss:gh-x-y"]}
        s, _ = sc.apply(src, cfg)
        self.assertEqual([f["id"] for f in s["rss"]], ["a"])
        self.assertEqual([f["subreddit"] for f in s["reddit"]["feeds"]], ["LocalLLaMA", "comfyui"])
        self.assertEqual(s["hf_trending"]["orgs"], ["Qwen"])
        s, _ = sc.apply(src, {**cfg, "disabled": []})
        self.assertEqual([f["id"] for f in s["rss"]], ["a", "gh-x-y"])


class Bench(unittest.TestCase):
    def test_tables(self):
        import media
        a = "## Evaluation\n| Model | GenEval |\n|---|---|\n| **Foo-2** | 0.91 |\n| Bar | 0.82 |\n| Baz | 0.74 |\n"
        r = media.bench_table(a, "https://x", "Foo-2")
        self.assertEqual([(x["label"], x["raw"], x["self"]) for x in r["rows"]], [("Foo-2", "0.91", True), ("Bar", "0.82", False), ("Baz", "0.74", False)])
        b = "## Results\n| Benchmark | Foo-2 | Bar | Baz |\n|---|---|---|---|\n| MMLU | 80.1 | 70.2 | 65 |\n"
        r = media.bench_table(b, "https://x", "Foo-2")
        self.assertEqual((r["metric"], [x["label"] for x in r["rows"]]), ("MMLU", ["Foo-2", "Bar", "Baz"]))
        self.assertIsNone(media.bench_table("| a | b |\n|--|--|\n| x | 1 |\n| y | 2 |\n| z | 3 |\n", "https://x"))  # no eval hint
        self.assertIsNone(media.bench_table("## Eval\n| Model | Note |\n|--|--|\n| x | good |\n| y | ok |\n| z | n/a |\n", "https://x"))


if __name__ == "__main__":
    unittest.main()
