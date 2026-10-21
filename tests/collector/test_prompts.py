import unittest
import prompts


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


if __name__ == "__main__":
    unittest.main()
