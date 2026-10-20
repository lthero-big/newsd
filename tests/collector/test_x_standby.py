import os, sys, unittest
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import x_standby as xs  # noqa: E402


class XStandby(unittest.TestCase):
    def test_likes_and_spam(self):
        self.assertEqual(xs.likes_of({"title": "[558] gem"}), 558)
        self.assertIsNone(xs.likes_of({"title": "no likes"}))
        self.assertEqual(xs.spam("ดีมาก #GeminiFourth #a #b #c #d OH MY BEST DAY WITH FOURTH ดีมากดีมากดีมาก"), "hashtags")
        self.assertEqual(xs.spam("I love the new season of my favourite cooking show, the finale was great fun to watch"), "off-topic")
        self.assertIsNone(xs.spam("We just released an open weights model that beats GPT-6 on coding benchmarks today"))
        self.assertIsNone(xs.spam("Back from the conference, lots of thoughts about the week and the people I met", topic=False))

    def test_keyword_row(self):
        now = datetime(2026, 10, 19, 7, tzinfo=timezone.utc)
        def fake(base, key, route, timeout=90):
            if "/keyword/" not in route:
                return []
            return [{"title": "[300] x", "url": "https://twitter.com/dev/status/2", "date_published": "2026-10-19T05:00:00.000Z",
                     "content_html": "Nano Banana portrait. Prompt: a cinematic portrait of an old fisherman at dawn, soft rim light", "authors": [{}]},
                    {"title": "[10] x", "url": "https://x.com/dev/status/3", "date_published": "2026-10-19T05:00:00.000Z",
                     "content_html": "A new open weights model that beats GPT-6 on coding benchmarks today", "authors": [{}]}]
        old = xs.get_feed; xs.get_feed = fake
        try:
            rows, st = xs.collect("http://h", "k", 26, 0, 0, now=now)
        finally:
            xs.get_feed = old
        self.assertEqual([r["url"] for r in rows], ["https://x.com/dev/status/2"])   # 10 likes is under the min_faves floor
        self.assertTrue(rows[0].get("prompt_q") and rows[0].get("prompt"))
        self.assertNotIn("transport", rows[0])


if __name__ == "__main__":
    unittest.main()
