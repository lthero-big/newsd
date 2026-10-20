import json, tempfile, unittest
from pathlib import Path
import prompts, source_config, x_from_connector


class PromptTests(unittest.TestCase):
    def test_marker(self):
        r = prompts.extract("GPT image 2 on chatgpt\n\nPrompt\n\nTransform the uploaded photo into a vintage collage, warm paper, soft light #AIart https://t.co/x")
        self.assertEqual(r["from"], "post"); self.assertTrue(r["text"].startswith("Transform")); self.assertNotIn("#AIart", r["text"])

    def test_chinese_and_quotes_alt_thread(self):
        self.assertEqual(prompts.extract("Nano Banana 出图，提示词：一只橘猫坐在窗边，胶片质感，柔和晨光，35mm")["text"], "一只橘猫坐在窗边，胶片质感，柔和晨光，35mm")
        self.assertEqual(prompts.extract('Try this: "a cinematic portrait of an astronaut in a sunflower field, golden hour"')["from"], "post")
        self.assertEqual(prompts.extract("look at this", thread=["prompt: a tiny isometric bakery diorama, pastel colors, soft shadows"])["from"], "thread")
        self.assertEqual(prompts.extract("cool", alts=["A tiny isometric bakery diorama with pastel colors and soft shadows"])["from"], "alt")
        self.assertIsNone(prompts.extract("just a picture of my dog, no prompt today"))
        # pointer-only text after a marker ("Paste the prompt\n… Prompt given below ⤵️") is not a prompt
        self.assertIsNone(prompts.extract("Open Gemini, Grok or GPT Image 2.0\n② Upload your image\n③ Paste the prompt\n④ Generate your image and video\n\nPrompt given below ⤵️ prompt https://t.co/7KzT9hn0WQ"))
        self.assertEqual(prompts.extract("look", thread=["Prompt:\nA rain-soaked neon alley at night, a girl under a clear umbrella, cinematic"])["from"], "thread")

    def test_query(self):
        q = [x for x in source_config.x_queries({"x": {"suffix": "lang:en", "queries": [{"id": "prompts", "terms": ["prompt"], "and": ["flux"], "suffix": "has:media", "min_likes": 30, "max_results": 50}]}}, {}) if x["id"] == "prompts"][0]
        self.assertEqual(q["query"], "(prompt) (flux) has:media"); self.assertEqual(q["min_likes"], 30)
        self.assertEqual(source_config.x_queries({"x": {"queries": [{"id": "prompts", "terms": ["p"]}]}}, {"disabled": ["xq:prompts"]}), [])


class FenceAndHeuristicTests(unittest.TestCase):
    def test_strip_untrusted_fences(self):
        inner = {"data": [{"id": "1"}], "meta": {"result_count": 1}}
        body = json.dumps(inner)
        fenced = (
            '<cursor_untrusted_data_redacted source="CallDynamicTool">\n\n'
            + body
            + '\n\nmax_results was lowered from 50 to 25.\n'
            + 'X reads left for this user: 27 of 30 this minute.\n\n'
            + '</cursor_untrusted_data_redacted>'
        )
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "prompts.json"
            p.write_text(fenced, encoding="utf-8")
            self.assertEqual(x_from_connector.load_any(p), inner)
            # bare fence tags without JSON must fail loudly
            bad = Path(tmp) / "prompts-bad.json"
            bad.write_text('<cursor_untrusted_data_1337>not json</cursor_untrusted_data_1337>', encoding="utf-8")
            with self.assertRaises(ValueError):
                x_from_connector.load_any(bad)

    def test_looks_like_prompt_share(self):
        self.assertTrue(x_from_connector.looks_like_prompt_share("GPT Image 2\n\nPROMPT:\nConvert the photo into a cartoon"))
        self.assertTrue(x_from_connector.looks_like_prompt_share("分享一个带完整提示词的出图例子，很好用"))
        self.assertTrue(x_from_connector.looks_like_prompt_share("Nano Banana 出图，提示词：一只橘猫坐在窗边，胶片质感"))
        self.assertFalse(x_from_connector.looks_like_prompt_share("OpenAI ships a new model today with better reasoning"))
        self.assertTrue(x_from_connector.looks_like_prompt_share("Made with Midjourney v8, full prompt in the alt text"))
        self.assertTrue(x_from_connector.looks_like_prompt_share("Seedance 2.5 short film, prompts below"))
        # plain mentions of the word are not prompt shares
        for s in ("New paper on prompt injection defenses for coding agents",
                  "Anthropic cuts prompt caching prices by half for long contexts",
                  "Leaked system prompt of a popular assistant shows its tool list",
                  "提示词工程已死？一篇关于 Agent 设计的长文"):
            self.assertFalse(x_from_connector.looks_like_prompt_share(s), s)

    def test_extra_and_prompts_files_fail_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "prompts.json"
            bad.write_text('<cursor_untrusted_data_x source="CallDynamicTool">\nquota exceeded\n</cursor_untrusted_data_x>', encoding="utf-8")
            out = Path(tmp) / "out.json"
            self.assertEqual(x_from_connector.main([str(bad), "--out", str(out)]), 2)
            self.assertFalse(out.exists())
            # a non-prompts raw file that can't parse is only skipped
            other = Path(tmp) / "models.json"
            other.write_text("garbage", encoding="utf-8")
            self.assertEqual(x_from_connector.main([str(other), "--out", str(out)]), 0)
            # fenced --extra file still loads
            ex = Path(tmp) / "rss.json"
            ex.write_text('<cursor_untrusted_data_y>{"items":[{"url":"https://x.com/a/status/9","text":"hello"}]}</cursor_untrusted_data_y>', encoding="utf-8")
            self.assertEqual(x_from_connector.main(["--extra", str(ex), "--out", str(out)]), 0)
            self.assertEqual(len(json.loads(out.read_text())["items"]), 1)

    def test_heuristic_marks_prompt_q_from_kw_file(self):
        """A prompt share that only arrives via kw-*.json (below prompts min_likes) still gets prompt_q."""
        post = {
            "id": "2111325304493162560",
            "author_id": "u1",
            "text": "GPT Image 2 On ChatGPT\n\nPROMPT:\nConvert the entire uploaded photograph into a clean cartoon illustration with flat pastel colors and soft outlines for storytelling.",
            "created_at": "2026-10-17T06:00:00.000Z",
            "public_metrics": {"like_count": 23, "retweet_count": 1, "reply_count": 0},
            "attachments": {"media_keys": ["3_1"]},
        }
        doc = {
            "data": [post],
            "includes": {
                "users": [{"id": "u1", "username": "Sairah_0", "name": "S", "profile_image_url": "https://pbs.twimg.com/profile_images/x_normal.jpg", "verified": False}],
                "media": [{"media_key": "3_1", "type": "photo", "url": "https://pbs.twimg.com/media/A.jpg", "width": 1200, "height": 800}],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "kw-openai-1.json"
            raw.write_text(json.dumps(doc), encoding="utf-8")
            out = Path(tmp) / "out.json"
            rc = x_from_connector.main([str(raw), "--out", str(out), "--min-likes", "20"])
            self.assertEqual(rc, 0)
            items = json.loads(out.read_text())["items"]
            self.assertEqual(len(items), 1)
            self.assertTrue(items[0]["prompt_q"])
            self.assertEqual(items[0]["prompt"]["from"], "post")
            self.assertTrue(items[0]["prompt"]["text"].startswith("Convert"))


if __name__ == "__main__":
    unittest.main()


class ThreadFallbackTests(unittest.TestCase):
    def test_thread_reply_used_when_conversation_id_missing(self):
        post = {"id": "77", "author_id": "u1", "text": "Waiting for Tomorrow, the city glows and the rain keeps falling. Created with GPT Image. Prompt below 👇 https://t.co/x",
                "public_metrics": {"like_count": 40}}
        reply = {"id": "78", "author_id": "u1", "conversation_id": "77",
                 "text": "Prompt ⤵️\n\nA lone woman waits beneath a bus shelter in a rain-soaked cyberpunk city, neon reflections"}
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "prompts.json"; a.write_text(json.dumps({"data": [post], "includes": {"users": [{"id": "u1", "username": "n"}]}}))
            b = Path(tmp) / "prompts-thread-77.json"; b.write_text("<cursor_untrusted_data_z>" + json.dumps({"data": [reply]}) + "</cursor_untrusted_data_z>")
            out = Path(tmp) / "o.json"
            self.assertEqual(x_from_connector.main([str(a), str(b), "--out", str(out)]), 0)
            it = json.loads(out.read_text())["items"][0]
            self.assertEqual(it["prompt"]["from"], "thread")
            self.assertTrue(it["prompt"]["text"].startswith("A lone woman"))

