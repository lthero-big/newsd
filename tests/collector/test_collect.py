import os; os.environ.setdefault("NEWS_NO_MEDIA", "1")
"""Offline fixtures for the interest collector. No network."""

import json
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from collect import (
    DropSampler,
    UnsafeURL,
    check_url,
    dedupe,
    filter_and_dedupe,
    html_to_text,
    iri_to_uri,
    match_terms,
    normalize_url,
    parse_feed,
    pipeline,
    safe_get,
    titles_similar,
)

ROOT = Path(__file__).resolve().parents[2]
TERMS = json.loads((ROOT / "tests" / "fixtures" / "interests.json").read_text(encoding="utf-8"))["terms"]


def _item(**kwargs):
    base = {
        "source": "hn",
        "url": "https://example.com/post",
        "title": "Example",
        "published_at": "2026-10-12T12:00:00Z",
        "points": 10,
        "comments": 2,
        "listing_text": "",
        "source_links": [{"source": "hn", "url": "https://example.com/post"}],
    }
    base.update(kwargs)
    return base


class MatchTests(unittest.TestCase):
    def test_hit_title_and_url_locations(self):
        hits = match_terms(
            "Qwen-Image-2.1 GGUF release",
            "",
            "https://huggingface.co/lightx2v/Qwen-Image-2.1-PE-T2I-GGUF-Q8_0",
            TERMS,
        )
        by_term = {hit["term"]: hit["locations"] for hit in hits}
        self.assertIn("Qwen-Image", by_term)
        self.assertIn("title", by_term["Qwen-Image"])
        self.assertIn("url", by_term["Qwen-Image"])
        self.assertIn("GGUF", by_term)

    def test_drop_unrelated_title(self):
        hits = match_terms("How to bake sourdough this weekend", "a recipe for bread", "https://example.com/bread", TERMS)
        self.assertEqual(hits, [])

    def test_subreddit_name_is_not_a_match(self):
        hits = match_terms(
            "Wow, civitai, this is a great image",
            "",
            "https://www.reddit.com/r/comfyui/comments/1x4ir8w/wow_civitai_this_is_a_great_image/",
            TERMS,
        )
        self.assertEqual(hits, [])

    def test_slug_can_still_match(self):
        hits = match_terms(
            "camera edit",
            "",
            "https://www.reddit.com/r/comfyui/comments/1x46xsn/qwen_imafe_21_camera_edit/",
            TERMS,
        )
        self.assertTrue(any(hit["term"] == "Qwen" for hit in hits))

    def test_word_boundary_avoids_formatting_and_floral(self):
        self.assertEqual(match_terms("formatting the layout", "", "https://example.com/a", TERMS), [])
        self.assertEqual(match_terms("floral wallpaper ideas", "", "https://example.com/floral", TERMS), [])

    def test_gpt_hits_title_not_unrelated_substring(self):
        hits = match_terms("We shipped GPT-5.5 today", "", "https://example.com/gpt", TERMS)
        self.assertTrue(any(hit["term"] == "GPT" and "title" in hit["locations"] for hit in hits))


class DedupeTests(unittest.TestCase):
    def test_tracking_params_collapse(self):
        self.assertEqual(
            normalize_url("https://www.Example.com/a/b/?utm_source=hn&id=1"),
            normalize_url("https://example.com/a/b?id=1"),
        )
        items = dedupe(
            [
                _item(url="https://example.com/a?utm_source=hn", title="Qwen-Image-2.1 lands", source="hn"),
                _item(
                    url="https://example.com/a",
                    title="Qwen-Image-2.1 lands",
                    source="rss",
                    source_links=[{"source": "rss", "url": "https://example.com/a"}],
                ),
            ]
        )
        self.assertEqual(len(items), 1)
        sources = {link["source"] for link in items[0]["source_links"]}
        self.assertEqual(sources, {"hn", "rss"})

    def test_similar_titles_across_sources_merge(self):
        left = "FLUX 3 Image Klein open weights and ComfyUI presets"
        right = "FLUX 3 Image Klein open weights ComfyUI presets released"
        self.assertTrue(titles_similar(left, right))
        items = dedupe(
            [
                _item(
                    url="https://bfl.ai/blog/flux-3-image-klein",
                    title=left,
                    source="rss",
                    source_links=[{"source": "rss", "url": "https://bfl.ai/blog/flux-3-image-klein"}],
                ),
                _item(
                    url="https://www.reddit.com/r/comfyui/comments/1x6y9cy/flux_3_klein_comfyui_presets/",
                    title=right,
                    source="reddit",
                    source_links=[
                        {
                            "source": "reddit",
                            "url": "https://www.reddit.com/r/comfyui/comments/1x6y9cy/flux_3_klein_comfyui_presets/",
                        }
                    ],
                ),
            ]
        )
        self.assertEqual(len(items), 1)
        sources = {link["source"] for link in items[0]["source_links"]}
        self.assertEqual(sources, {"rss", "reddit"})

    def test_same_hn_discussion_merges_even_if_titles_differ(self):
        items = dedupe(
            [
                _item(
                    url="https://www.phoronix.com/news/Linus-Torvalds-Claude-Code",
                    title="Linus Torvalds Working on a Hefty Linux Kernel Improvement with Claude Code",
                    source="hn",
                    source_links=[
                        {"source": "hn", "url": "https://news.ycombinator.com/item?id=50050367"},
                        {"source": "hn", "url": "https://www.phoronix.com/news/Linus-Torvalds-Claude-Code"},
                    ],
                ),
                _item(
                    url="https://news.ycombinator.com/item?id=50050367",
                    title="A different headline for the same thread",
                    source="hn",
                    source_links=[{"source": "hn", "url": "https://news.ycombinator.com/item?id=50050367"}],
                ),
            ]
        )
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["url"].startswith("https://news.ycombinator.com/"))

    def test_different_stories_stay_apart(self):
        items = dedupe(
            [
                _item(
                    url="https://example.com/one",
                    title="Claude releases a new code tool",
                    source_links=[{"source": "hn", "url": "https://example.com/one"}],
                ),
                _item(
                    url="https://example.com/two",
                    title="Gemini image editor gets a minor patch",
                    source_links=[{"source": "hn", "url": "https://example.com/two"}],
                ),
            ]
        )
        self.assertEqual(len(items), 2)


class PipelineTests(unittest.TestCase):
    def test_filter_drops_are_counted_and_sampled(self):
        sampler = DropSampler(limit=10, seed=1)
        matched, counts = filter_and_dedupe(
            [
                _item(title="Qwen-Image-2.1 GGUF weights", url="https://example.com/qwen"),
                _item(title="Sourdough starter tips", url="https://example.com/bread"),
            ],
            TERMS,
            sampler,
        )
        self.assertEqual(len(matched), 1)
        self.assertEqual(counts["dropped"], 1)
        self.assertEqual(counts["reasons"]["no_term_match"], 1)
        self.assertEqual(sampler.rows[0]["reason"], "no_term_match")
        self.assertNotIn("bread", matched[0]["url"])

    def test_body_failure_is_kept(self):
        def boom(url):
            raise TimeoutError("timed out")

        result = pipeline(
            [_item(title="DeepSeek API outage notes", url="https://status.example.com/deepseek", listing_text="")],
            TERMS,
            boom,
            min_body=200,
        )
        self.assertEqual(len(result["candidates"]), 1)
        cand = result["candidates"][0]
        self.assertTrue(cand["fetch_status"].startswith("failed:"))
        self.assertIn("DeepSeek", {term["term"] for term in cand["matched_terms"]})
        self.assertTrue(cand["content_hash"])

    def test_listing_body_skips_fetch(self):
        called = []

        def boom(url):
            called.append(url)
            raise AssertionError("should not fetch")

        long_text = "<p>" + ("Qwen-Image-2.1 release notes. " * 30) + "</p>"
        result = pipeline(
            [_item(title="Qwen-Image-2.1", url="https://example.com/q", listing_text=long_text, source="rss")],
            TERMS,
            boom,
        )
        self.assertEqual(called, [])
        self.assertEqual(result["candidates"][0]["fetch_status"], "ok")
        self.assertEqual(result["candidates"][0]["extractor"], "rss")
        self.assertLessEqual(len(result["candidates"][0]["text"]), 4000)


class SafetyTests(unittest.TestCase):
    def test_reject_private_and_non_http(self):
        blocked = [
            "http://127.0.0.1/secret",
            "http://10.1.2.3/a",
            "http://192.168.1.9/",
            "http://169.254.169.254/latest/meta-data",
            "http://172.16.0.4/",
            "http://localhost/admin",
            "http://metadata.google.internal/computeMetadata/v1/",
            "file:///etc/passwd",
            "ftp://example.com/file",
            "gopher://example.com/",
        ]
        for url in blocked:
            with self.subTest(url=url):
                with self.assertRaises(UnsafeURL):
                    check_url(url, resolve=lambda host: ["93.184.216.34"])

    def test_unicode_url_is_percent_encoded(self):
        encoded = iri_to_uri("https://www.reddit.com/r/comfyui/comments/1x44aqy/minimax_h3动作迁移/")
        self.assertTrue(encoded.startswith("https://www.reddit.com/"))
        self.assertNotIn("动作", encoded)
        encoded.encode("ascii")

    def test_public_literal_and_resolved_host_ok(self):
        check_url("https://1.1.1.1/dns-query", resolve=lambda host: [_ for _ in ()])
        check_url("https://example.com/a", resolve=lambda host: ["93.184.216.34"])

    def test_resolved_private_is_rejected(self):
        with self.assertRaises(UnsafeURL) as ctx:
            check_url("https://evil.example/a", resolve=lambda host: ["10.0.0.8"])
        self.assertEqual(ctx.exception.reason, "resolves-private")

    def test_redirect_to_private_is_not_fetched(self):
        class FakeOpener:
            def open(self, req, timeout=None):
                if "127.0.0.1" in req.full_url or "169.254.169.254" in req.full_url:
                    raise AssertionError("connected to private address")
                raise urllib.error.HTTPError(
                    req.full_url,
                    302,
                    "found",
                    {"Location": "http://169.254.169.254/latest/meta-data"},
                    None,
                )

        with self.assertRaises(UnsafeURL):
            safe_get(
                "https://example.com/jump",
                resolve=lambda host: ["93.184.216.34"],
                opener=FakeOpener(),
            )


class FeedFixtureTests(unittest.TestCase):
    def test_atom_fixture_parses_release(self):
        xml = b"""<?xml version="1.0" encoding="utf-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <title>v0.41.0</title>
            <updated>2026-10-15T22:57:45Z</updated>
            <link rel="alternate" href="https://github.com/Comfy-Org/ComfyUI/releases/tag/v0.41.0"/>
            <content type="html">&lt;p&gt;ComfyUI adds FLUX 3 Image.&lt;/p&gt;</content>
          </entry>
        </feed>
        """
        entries = parse_feed(xml)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["url"], "https://github.com/Comfy-Org/ComfyUI/releases/tag/v0.41.0")
        self.assertIn("FLUX", html_to_text(entries[0]["listing_text"]))
        self.assertEqual(entries[0]["published_at"], "2026-10-15T22:57:45Z")

    def test_window_helper_used_by_callers(self):
        from collect import in_window, parse_time

        start = datetime(2026, 10, 15, 0, 30, tzinfo=timezone.utc)
        end = datetime(2026, 10, 16, 0, 41, 23, tzinfo=timezone.utc)
        self.assertTrue(in_window(parse_time("2026-10-15T22:57:45Z"), start, end))
        self.assertFalse(in_window(parse_time("2026-10-13T21:29:16Z"), start, end))


if __name__ == "__main__":
    unittest.main()
