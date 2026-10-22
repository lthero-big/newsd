"""DEPTAI-216 display helpers: text cleaning, release titles, media extraction."""
from newsd.content import (clean_summary, clean_text, display_title, http_url, media_from_html, safe_media,
                           strip_markdown)

REL = "https://github.com/ggml-org/llama.cpp/releases/tag/b11801"


def test_markdown_and_escapes_are_stripped():
    assert strip_markdown("[**b11801**](https://github.com/x/y)") == "b11801"
    assert clean_text("init `x` in test\\_kpool\\_mask and *fast* __ops__") == "init x in test_kpool_mask and fast ops"
    assert clean_text("&lt;p&gt;Hi &amp;amp; bye&lt;/p&gt;") == "Hi & bye"


def test_changelog_noise_is_dropped():
    s = clean_text("feat: add X by @alice in #12 fix: y ( #13 ) Co-authored-by: Bob b@x.io "
                   "Website: https://llama.app Attestations: https://x macOS/iOS: arm64")
    assert s == "feat: add X fix: y"
    assert clean_text("llama: fused op for QSA mask llama: fused op for QSA mask tests: init") == \
        "llama: fused op for QSA mask tests: init"


def test_clean_summary_truncates_and_drops_bare_links():
    assert clean_summary("ComfyUI v0.42.4", title="ComfyUI v0.42.4") == ""
    assert clean_summary("Is it good? submitted by /u/Some_one [link] [comments]") == "Is it good?"
    assert clean_summary("https://example.com/only-a-link") == ""
    long = "First sentence is here. " + "word " * 100
    assert clean_summary(long, 40) == "First sentence is here."
    out = clean_summary("x" * 10 + " " + "word " * 80, 50)
    assert out.endswith("…") and len(out) <= 51


def test_release_titles():
    notes = "model: Add support for SenseNova U1 model ( #28919 ) model : add sensenova"
    assert display_title("b11801", REL, notes) == "llama.cpp b11801: model: Add support for SenseNova U1 model"
    assert display_title("[**b11801**](%s)" % REL, REL, notes).startswith("llama.cpp b11801: ")
    assert display_title("v0.42.3", "https://github.com/Comfy-Org/ComfyUI/releases/tag/v0.42.3", "ComfyUI v0.42.3") == "ComfyUI v0.42.3"
    assert display_title("b1", REL, "") == "llama.cpp b1"
    assert display_title("Transformers v5.2: Qwen4 support", "https://github.com/huggingface/transformers/releases/tag/v5.2.0",
                         "x") == "Transformers v5.2: Qwen4 support"  # a real title is kept
    assert display_title("v2.0", "https://example.com/blog/v2", "notes") == "v2.0"  # not a GitHub release


def test_only_http_media_survives():
    assert http_url("javascript:alert(1)") == "" and http_url("data:image/png;base64,xx") == ""
    assert http_url("//cdn.example.com/a.png") == "https://cdn.example.com/a.png"
    assert http_url("/a.png", "https://b.example/post") == "https://b.example/a.png"
    assert http_url('https://x.example/a.png" onerror="x') == ""
    m = media_from_html('<img src="https://avatars.githubusercontent.com/u/1"><img src="https://x.example/p.png" width="20">'
                        '<img src="https://x.example/hero.jpg"><a href="https://youtu.be/dQw4w9WgXcQ">v</a>'
                        '<a href="https://v.redd.it/abc">clip</a>')
    assert [e["type"] for e in m] == ["image", "youtube", "videolink"]


def test_safe_media_shapes_players():
    md = safe_media([{"type": "video", "url": "https://cdn.example/clip.mp4"}, {"type": "image", "url": "ftp://x/y.png"}],
                    "https://cdn.example/poster.jpg", {"url": "https://cdn.example/ep.mp3", "type": "audio/mpeg"},
                    "https://news.example/story")
    assert md["image"] == "https://cdn.example/poster.jpg"
    assert [p["type"] for p in md["players"]] == ["video", "audio"]
    assert safe_media(None, None, None, "https://news.example/x") == {"image": None, "players": []}
