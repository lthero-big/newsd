import pytest

from newsd.push import template as tpl
from newsd.push.digest import Digest, build_messages, clean_text


def digest(n=3, **kw):
    items = [{"title": f"Title {i}", "url": f"https://x.example/{i}", "summary": f"Summary {i} " * 5, "source": "src", "module": "news", "score": 80.0 - i}
             for i in range(1, n + 1)]
    return Digest("alice", "2026-10-21", items, **kw)


def test_placeholders_and_truncation():
    v = {"date": "2026-10-21", "summary": "a---b---c---d"}
    assert tpl.render_string("D=#{date} #{missing}|", v) == "D=2026-10-21 |"
    assert tpl.render_string("#{summary?limit=8}", v) == "a---b---"
    assert tpl.render_string("#{summary?limit=8&split=---}", v) == "a---b"[:5]  # cut back to the last delimiter inside the limit
    assert tpl.render_string("#{summary?limit=100}", v) == v["summary"]


def test_json_template_is_rendered_per_string_so_values_cannot_break_it():
    out = tpl.render('{"text": "#{summary}", "n": 3, "l": ["#{date}"]}', {"summary": 'quote " and \\ and \n newline', "date": "d"})
    assert out == {"text": 'quote " and \\ and \n newline', "n": 3, "l": ["d"]}
    assert tpl.render("plain #{date}", {"date": "d"}) == "plain d"
    assert tpl.parse_json_body('"just a string"') is None and tpl.parse_json_body("[1]") == [1]


def test_validate_rejects_unknown_placeholders_and_huge_templates():
    tpl.validate("#{summary} #{item_title?limit=3}")
    with pytest.raises(ValueError, match="unknown placeholder: #{nope}"):
        tpl.validate("x #{nope}")
    with pytest.raises(ValueError, match="too long"):
        tpl.validate("x" * 9000)


def test_summary_message_fits_the_limit_and_says_what_was_left_out():
    d = digest(30, url="https://newsd.example/issue/2026-10-21")
    [m] = build_messages(d, "md", "summary", 900)
    assert len(m.text) <= 900 and m.kind == "summary"
    assert "共 30 条" in m.text and "[Title 1](https://x.example/1)" in m.text
    assert "另" in m.text and "https://newsd.example/issue/2026-10-21" in m.text
    [full] = build_messages(digest(2), "md", "summary", 5000)
    assert "Title 2" in full.text and "另" not in full.text


def test_summary_and_items_is_overview_plus_one_message_per_item():
    msgs = build_messages(digest(5), "md", "summary_and_items", 2000, max_items=3)
    assert [m.kind for m in msgs] == ["overview", "item", "item", "item"]
    assert msgs[0].vars["item_count"] == "5" and "Summary 1" not in msgs[0].text  # overview lists titles only
    assert msgs[2].vars["item_index"] == "2" and msgs[2].vars["item_title"] == "Title 2" and msgs[2].vars["item_url"] == "https://x.example/2"
    assert msgs[2].vars["summary"] == msgs[2].text
    assert build_messages(digest(0), "md", "summary_and_items", 2000)[0].kind == "summary"  # empty issue: one short note


def test_every_flavor_escapes_feed_content():
    evil = Digest("a", "k", [{"title": "<script>alert(1)</script> & <b>x</b>", "url": "javascript:alert(1)", "summary": "<i>s</i> > 1",
                              "source": "s", "module": "m", "score": 1.0}])
    html = build_messages(evil, "html", "summary", 4000)[0].text
    assert "<script>" not in html and "&lt;script&gt;" in html and "<i>" not in html.replace("<i>s", "") and "javascript:" not in html
    slack = build_messages(evil, "slack", "summary", 4000)[0].text
    assert "<script>" not in slack and "&lt;script&gt;" in slack and "javascript:" not in slack
    md = build_messages(evil, "md", "summary", 4000)[0].text
    assert "javascript:" not in md and "\\[" not in md
    ok = Digest("a", "k", [{"title": "a [b] (c)", "url": "https://x.example/p(1)", "summary": "", "source": "", "module": "", "score": 1.0}])
    assert "[a \\[b\\] (c)](https://x.example/p(1%29)" in build_messages(ok, "md", "summary", 4000)[0].text


def test_clean_text_strips_html_and_truncates():
    assert clean_text("<p>Hello&nbsp;<b>world</b> &amp; co</p>") == "Hello world & co"
    assert len(clean_text("x" * 500)) == 160 and clean_text(None) == ""
