from newsd.collector import linkdates, media
from newsd.pool.extract import extract_page, html_hash
from poolkit import ARTICLE, JS_SHELL


def test_success_text_and_metadata_in_one_pass():
    r = extract_page(ARTICLE, "https://news.example/qwen")
    assert r.ok and r.status == "ok" and r.extractor == "trafilatura"
    assert "open-weights language model" in r.text and "Home | World" not in r.text
    assert r.title == "Qwen ships a new open model"
    assert r.author == "Jane Writer"
    assert r.published_at == "2026-10-19T07:30:00+00:00"
    assert r.sitename == "Example News"
    assert r.image == "https://cdn.example.com/img/qwen-launch-hero.jpg"
    assert r.meta_from["published_at"] == "trafilatura" and r.meta_from["image"] == "trafilatura"


def test_precision_and_recall_switches_are_accepted():
    for kw in ({"favor_precision": True}, {"favor_recall": True}):
        r = extract_page(ARTICLE, "https://news.example/qwen", **kw)
        assert r.ok and "Hugging Face" in r.text


def test_js_shell_fails_cleanly():
    r = extract_page(JS_SHELL, "https://app.example/")
    assert not r.ok and r.status == "failed:js_shell" and r.text == ""


def test_empty_and_garbage_never_raise():
    assert extract_page("", None).status == "failed:empty"
    assert extract_page("plain text, no markup", None).status == "failed:empty"
    assert not extract_page("<html><body><p>hi</p></body></html>", None).ok


def test_regex_fallback_for_date_and_image():
    # trafilatura does not read datePublished from a non-LD script or this meta order; the old regexes do
    html = ('<html><head><script>{"datePublished":"2026-09-30"}</script>'
            '<meta content="https://img.example/a/launch-photo.png" property="og:image"></head><body><p>x</p></body></html>')
    r = extract_page(html, "https://news.example/a")
    assert r.published_at == "2026-09-30T00:00:00+00:00" and r.meta_from["published_at"] == "fallback"
    assert r.image == "https://img.example/a/launch-photo.png"


def test_linkdates_from_html_prefers_trafilatura_keeps_time():
    assert linkdates.from_html('<meta property="article:published_time" content="2026-10-01T08:00:00Z">') == "2026-10-01T08:00:00+00:00"
    assert linkdates.from_html('<script>{"datePublished":"2026-09-30"}</script>') == "2026-09-30T00:00:00+00:00"
    assert linkdates.from_html("<p>no date</p>") is None
    assert linkdates.from_html(ARTICLE) == "2026-10-19T07:30:00+00:00"


def test_og_candidates_trafilatura_then_regex_and_generic_filter():
    assert media.og_candidates(ARTICLE, "https://news.example/qwen") == ["https://cdn.example.com/img/qwen-launch-hero.jpg"]
    generic = '<html><head><meta property="og:image" content="https://news.example/og-image.png"></head></html>'
    assert media.og_candidates(generic, "https://news.example/") == []
    rel = '<html><head><meta property="og:image" content="/img/launch-photo.jpg"></head></html>'
    assert media.og_candidates(rel, "https://news.example/post") == ["https://news.example/img/launch-photo.jpg"]


def test_html_hash_ignores_whitespace():
    assert html_hash("<p>a  b</p>\n") == html_hash("<p>a b</p>")
    assert html_hash("<p>a</p>") != html_hash("<p>b</p>")


def test_generic_site_card_from_trafilatura_is_dropped():
    html = ARTICLE.replace("https://cdn.example.com/img/qwen-launch-hero.jpg",
                           "https://opengraph.githubassets.com/abc/ollama/ollama/releases/tag/v1")
    assert extract_page(html, "https://github.com/ollama/ollama/releases/tag/v1").image is None
