import os; os.environ.setdefault("NEWS_NO_MEDIA", "1")
import unittest
import linkdates
import generate
from unittest import mock


class LinkTests(unittest.TestCase):
    def test_canon(self):
        c = linkdates.canon
        self.assertEqual(c("https://youtu.be/abcDEF123?si=x"), "https://youtube.com/watch?v=abcDEF123")
        self.assertEqual(c("https://m.youtube.com/watch?v=abcDEF123&feature=share&t=10"), "https://youtube.com/watch?v=abcDEF123")
        self.assertEqual(c("https://www.youtube.com/shorts/abcDEF123"), "https://youtube.com/watch?v=abcDEF123")
        self.assertEqual(c("http://WWW.Example.com/a/b/?utm_source=x&fbclid=1&id=2#top"), "https://example.com/a/b?id=2")
        self.assertEqual(c("https://twitter.com/foo/status/123?s=20"), "https://x.com/i/status/123")

    def test_html_dates(self):
        self.assertEqual(linkdates.from_html('<meta property="article:published_time" content="2026-10-01T08:00:00Z">'), "2026-10-01T08:00:00+00:00")
        self.assertEqual(linkdates.from_html('<script>{"datePublished":"2026-09-30"}</script>'), "2026-09-30T00:00:00+00:00")
        self.assertIsNone(linkdates.from_html("<p>no date</p>"))

    def test_stale_marks_and_downranks(self):
        item = {"source": "x", "url": "https://x.com/a/status/1", "links_out": ["https://youtu.be/abcDEF123"], "published_at": "2026-10-16T09:00:00+00:00", "score": {"total": 50}}
        fresh = {"source": "x", "url": "https://x.com/a/status/2", "links_out": ["https://example.com/new"], "published_at": "2026-10-16T09:00:00+00:00", "score": {"total": 50}}
        unknown = {"source": "x", "url": "https://x.com/a/status/3", "links_out": ["https://example.com/x"], "published_at": "2026-10-16T09:00:00+00:00", "score": {"total": 50}}
        dates = {"https://youtube.com/watch?v=abcDEF123": "2026-10-07T07:00:00+00:00", "https://example.com/new": "2026-10-15T00:00:00+00:00"}
        with mock.patch.object(linkdates, "published", lambda u: dates.get(linkdates.canon(u))), mock.patch.object(linkdates, "save_cache", lambda: None):
            generate.mark_stale([item, fresh, unknown])
        self.assertEqual(item["stale_link"]["url"], "https://youtu.be/abcDEF123"); self.assertEqual(item["score"]["total"], 30)
        self.assertNotIn("stale_link", fresh); self.assertNotIn("stale_link", unknown)

    def test_seen_index(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as t, mock.patch.object(linkdates, "SEEN", Path(t) / "s.json"):
            linkdates.update_seen({"date": "2026-10-15", "items": [{"url": "https://ex.com/a?utm_source=x", "linked": "https://youtu.be/abcDEF123"}]})
            linkdates.update_seen({"date": "2026-10-16", "items": [{"url": "https://ex.com/a"}]})
            s = linkdates.load_seen()
            self.assertEqual(s["https://ex.com/a"], "2026-10-15"); self.assertEqual(s["https://youtube.com/watch?v=abcDEF123"], "2026-10-15")


if __name__ == "__main__":
    unittest.main()
