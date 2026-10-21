"""Each channel against fakes: Telegram/webhook over a recording HTTP transport, e-mail over a real local SMTP server."""
import base64
import hashlib
import hmac
from urllib.parse import parse_qs, urlparse

import pytest

from newsd.push import channels as ch
from newsd.push.digest import Digest, build_messages
from newsd.push.prefs import ChannelConfig
from pushkit import TG_TOKEN, WEBHOOK

D = Digest("alice", "2026-10-21", [{"title": "Qwen 3 ships", "url": "https://x.example/1", "summary": "open weights", "source": "hf",
                                    "module": "model", "score": 91.0},
                                   {"title": "Sora & friends", "url": "https://x.example/2", "summary": "video", "source": "oa",
                                    "module": "news", "score": 80.0}])


def webhook(wtype="generic", **kw):
    return ChannelConfig("webhook", True, url=WEBHOOK, wtype=wtype, **kw)


def msgs(wtype="generic", mode="summary", **kw):
    return build_messages(D, ch.FLAVOR_OF[wtype], mode, ch.LIMITS[wtype])


# ------------------------------------------------------------------------------------------------ webhook adapters

def test_generic_default_body_and_ok(net):
    net.on(WEBHOOK, (200, {"ok": True}))
    ch.send_webhook(webhook(), msgs()[0])
    body = net.body(net.requests[0])
    assert body["title"] == "newsd 日报 2026-10-21" and body["kind"] == "summary" and body["count"] == "2" and body["date"] == "2026-10-21"
    assert "[Qwen 3 ships](https://x.example/1)" in body["text"]
    assert net.requests[0].headers["content-type"] == "application/json"


def test_feishu_card_and_response_codes(net):
    net.on(WEBHOOK, (200, {"code": 0, "msg": "success"}), (200, {"code": 19001, "msg": "param invalid"}), (200, {"code": 11232, "msg": "frequency limit"}))
    ch.send_webhook(webhook("feishu"), msgs("feishu")[0])
    body = net.body(net.requests[0])
    assert body["msg_type"] == "interactive" and body["card"]["schema"] == "2.0" and "sign" not in body
    assert body["card"]["header"]["title"]["content"] == "newsd 日报 2026-10-21"
    assert "Qwen 3 ships" in body["card"]["body"]["elements"][0]["content"]
    with pytest.raises(ch.PushError, match="19001") as e:  # HTTP 200 but code != 0 is a failure, and not worth retrying
        ch.send_webhook(webhook("feishu"), msgs("feishu")[0])
    assert e.value.retryable is False
    with pytest.raises(ch.PushError) as e:
        ch.send_webhook(webhook("feishu"), msgs("feishu")[0])
    assert e.value.retryable is True


def test_feishu_signature(net):
    net.on(WEBHOOK, (200, {"code": 0}))
    url, body = ch.build_webhook_request(webhook("feishu", secret="s3cr3t"), msgs("feishu")[0], now=1700000000)
    expect = base64.b64encode(hmac.new(b"1700000000\ns3cr3t", b"", hashlib.sha256).digest()).decode()
    assert body["timestamp"] == "1700000000" and body["sign"] == expect and url == WEBHOOK


def test_dingtalk_markdown_signature_and_errcode(net):
    net.on(WEBHOOK, (200, {"errcode": 0, "errmsg": "ok"}), (200, {"errcode": 310000, "errmsg": "keywords not in content"}))
    cfg = webhook("dingtalk", secret="ding-secret")
    ch.send_webhook(cfg, msgs("dingtalk")[0])
    req = net.requests[0]
    body = net.body(req)
    assert body["msgtype"] == "markdown" and body["markdown"]["title"] == "newsd 日报 2026-10-21" and "Qwen 3 ships" in body["markdown"]["text"]
    q = parse_qs(urlparse(str(req.url)).query)
    ts = q["timestamp"][0]
    expect = base64.b64encode(hmac.new(b"ding-secret", f"{ts}\nding-secret".encode(), hashlib.sha256).digest()).decode()
    assert q["sign"][0] == expect and len(ts) == 13  # milliseconds
    with pytest.raises(ch.PushError, match="310000") as e:
        ch.send_webhook(cfg, msgs("dingtalk")[0])
    assert e.value.retryable is False and "ding-secret" not in str(e.value)


def test_slack_mrkdwn_and_non_ok_body(net):
    net.on(WEBHOOK, (200, "ok"), (200, "invalid_payload"))
    ch.send_webhook(webhook("slack"), msgs("slack")[0])
    text = net.body(net.requests[0])["text"]
    assert "<https://x.example/1|Qwen 3 ships>" in text and "Sora &amp; friends" in text and "**" not in text
    with pytest.raises(ch.PushError, match="invalid_payload"):
        ch.send_webhook(webhook("slack"), msgs("slack")[0])


def test_discord_content_204_and_length_limit(net):
    net.on(WEBHOOK, (204, ""))
    big = Digest("a", "k", [{"title": f"Item {i} " + "x" * 80, "url": f"https://x.example/{i}", "summary": "s" * 150, "source": "", "module": "",
                             "score": 1.0} for i in range(60)])
    ms = build_messages(big, "md", "summary", ch.LIMITS["discord"])
    ch.send_webhook(webhook("discord"), ms[0])
    body = net.body(net.requests[0])
    assert len(body["content"]) <= 1900 and body["allowed_mentions"] == {"parse": []} and "另" in body["content"]


def test_custom_json_template_and_plain_text_template(net):
    net.on(WEBHOOK, (200, {"ok": 1}))
    item = build_messages(D, "md", "summary_and_items", 2000)[1]
    cfg = webhook("generic", template='{"t": "#{message_title} / #{item_count}", "who": "#{user}"}',
                  item_template='{"i": "#{item_index}", "title": "#{item_title}", "url": "#{item_url}", "s": "#{summary?limit=12}"}')
    _, body = ch.build_webhook_request(cfg, msgs()[0])
    assert body == {"t": "newsd 日报 2026-10-21 / 2", "who": "alice"}
    _, body = ch.build_webhook_request(cfg, item)
    assert body["i"] == "1" and body["title"] == "Qwen 3 ships" and body["url"] == "https://x.example/1" and len(body["s"]) <= 12
    # plain text template: wrapped by the platform envelope, #{summary} replaced by the rendered template
    plain = webhook("slack", template="*#{date}* — #{item_count} items\n#{summary}")
    _, body = ch.build_webhook_request(plain, msgs("slack")[0])
    assert list(body) == ["text", "unfurl_links"] and body["text"].startswith("*2026-10-21* — 2 items\n")
    _, body = ch.build_webhook_request(webhook("dingtalk", template="hello #{user}"), msgs("dingtalk")[0])
    assert body["markdown"] == {"title": "newsd 日报 2026-10-21", "text": "hello alice"}


# ------------------------------------------------------------------------------------------------ HTTP failure handling

def test_http_errors_are_classified_and_scrubbed(net):
    net.on(WEBHOOK, (500, "upstream exploded " + WEBHOOK), (429, {"retry_after": 7}, {"retry-after": "7"}), (404, "no such webhook"),
           (302, "moved"))
    cfg = webhook()
    with pytest.raises(ch.PushError) as e:
        ch.send_webhook(cfg, msgs()[0])
    assert e.value.retryable and "SECRETPATH1234" not in str(e.value)
    with pytest.raises(ch.PushError) as e:
        ch.send_webhook(cfg, msgs()[0])
    assert e.value.retryable and e.value.retry_after == 7
    for status in (404, 302):  # permanent: bad URL / redirects are never followed
        with pytest.raises(ch.PushError) as e:
            ch.send_webhook(cfg, msgs()[0])
        assert e.value.retryable is False and str(status) in str(e.value)


def test_network_exception_is_retryable_and_url_is_not_leaked(net):
    import httpx

    def boom(request):
        raise httpx.ConnectError(f"connection refused for {request.url}")

    net.on(WEBHOOK, boom)
    with pytest.raises(ch.PushError) as e:
        ch.send_webhook(webhook(), msgs()[0])
    assert e.value.retryable and "SECRETPATH1234" not in str(e.value) and "ConnectError" in str(e.value)


def test_webhook_ssrf_guard(net, monkeypatch):
    for bad in ("http://127.0.0.1:8080/hook", "http://localhost/hook", "http://10.0.0.5/x", "http://169.254.169.254/latest", "http://[::1]/x"):
        with pytest.raises(ch.PushError, match="not allowed") as e:
            ch.send_webhook(ChannelConfig("webhook", True, url=bad), msgs()[0])
        assert e.value.retryable is False
    monkeypatch.setattr(ch, "_resolve", lambda host: ["192.168.1.9"])  # a public-looking name that resolves to a private address
    with pytest.raises(ch.PushError, match="not allowed"):
        ch.send_webhook(webhook(), msgs()[0])
    assert net.requests == []  # nothing left the building
    monkeypatch.setenv("NEWSD_PUSH_ALLOW_PRIVATE", "1")
    from newsd.settings import reset_settings
    reset_settings()
    net.on("http://10.0.0.5/", (200, {"ok": 1}))
    ch.send_webhook(ChannelConfig("webhook", True, url="http://10.0.0.5/x"), msgs()[0])
    assert len(net.requests) == 1


# ------------------------------------------------------------------------------------------------ Telegram

def tg(**kw):
    return ChannelConfig("telegram", True, target="12345", token=TG_TOKEN, **kw)


def test_telegram_send_message_payload(net):
    net.on("https://api.telegram.org/bot", (200, {"ok": True, "result": {}}))
    m = build_messages(D, "html", "summary", ch.LIMITS["telegram"])[0]
    ch.send_telegram(tg(), m)
    req = net.requests[0]
    assert str(req.url) == f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    body = net.body(req)
    assert body["chat_id"] == "12345" and body["parse_mode"] == "HTML" and body["disable_web_page_preview"] is True
    assert '<a href="https://x.example/1">Qwen 3 ships</a>' in body["text"] and "Sora &amp; friends" in body["text"]


def test_telegram_errors(net):
    net.on("https://api.telegram.org/bot", (401, {"ok": False, "description": "Unauthorized"}),
           (429, {"ok": False, "parameters": {"retry_after": 11}}), (502, "bad gateway"), (200, {"ok": False, "description": "chat not found"}))
    m = build_messages(D, "html", "summary", 4000)[0]
    with pytest.raises(ch.PushError, match="401") as e:
        ch.send_telegram(tg(), m)
    assert e.value.retryable is False and TG_TOKEN not in str(e.value)
    with pytest.raises(ch.PushError) as e:
        ch.send_telegram(tg(), m)
    assert e.value.retryable and e.value.retry_after == 11
    with pytest.raises(ch.PushError) as e:
        ch.send_telegram(tg(), m)
    assert e.value.retryable
    with pytest.raises(ch.PushError, match="chat not found") as e:
        ch.send_telegram(tg(), m)
    assert e.value.retryable is False
    with pytest.raises(ch.PushError, match="no bot token"):
        ch.send_telegram(ChannelConfig("telegram", True, target="1"), m)


def test_telegram_token_never_appears_in_transport_errors(net):
    import httpx

    def boom(request):
        raise httpx.ReadTimeout(f"timed out talking to {request.url}")

    net.on("https://api.telegram.org/bot", boom)
    with pytest.raises(ch.PushError) as e:
        ch.send_telegram(tg(), build_messages(D, "html", "summary", 4000)[0])
    assert TG_TOKEN not in str(e.value) and "123456789" not in str(e.value) and e.value.retryable


# ------------------------------------------------------------------------------------------------ e-mail (real SMTP on loopback)

def mail_cfg():
    return ChannelConfig("email", True, target="alice@example.com")


def test_email_is_delivered_multipart_with_login(smtp):
    ch.send_email(mail_cfg(), "newsd 日报 2026-10-21", "plain body https://x.example/1", "<b>html body</b>")
    [got] = smtp.messages
    msg = got["msg"]
    assert smtp.auth_attempts == [("mailer", "smtp-pass-123")]
    assert got["to"] == ["<alice@example.com>"] and "newsd@example.org" in got["from"]
    assert msg["Subject"] == "newsd 日报 2026-10-21" and msg["To"] == "alice@example.com"
    assert "plain body" in msg.get_body(("plain",)).get_content() and "<b>html body</b>" in msg.get_body(("html",)).get_content()


def test_email_failures_are_classified(smtp):
    smtp.accept_auth = False
    with pytest.raises(ch.PushError, match="login rejected") as e:
        ch.send_email(mail_cfg(), "s", "t", "h")
    assert e.value.retryable is False and "smtp-pass-123" not in str(e.value)
    smtp.accept_auth, smtp.reject_rcpt = True, True
    with pytest.raises(ch.PushError, match="recipient") as e:
        ch.send_email(mail_cfg(), "s", "t", "h")
    assert e.value.retryable is False
    smtp.reject_rcpt, smtp.data_error = False, "451 4.3.0 try again later"
    with pytest.raises(ch.PushError, match="451") as e:
        ch.send_email(mail_cfg(), "s", "t", "h")
    assert e.value.retryable is True
    smtp.data_error = "554 5.7.1 rejected as spam"
    with pytest.raises(ch.PushError, match="554") as e:
        ch.send_email(mail_cfg(), "s", "t", "h")
    assert e.value.retryable is False
    assert smtp.messages == []


def test_email_unreachable_server_and_missing_config(smtp, monkeypatch):
    smtp.stop()
    with pytest.raises(ch.PushError) as e:  # connection refused: transient
        ch.send_email(mail_cfg(), "s", "t", "h")
    assert e.value.retryable and "smtp-pass-123" not in str(e.value)
    monkeypatch.setenv("NEWSD_SMTP_HOST", "")
    from newsd.settings import reset_settings
    reset_settings()
    with pytest.raises(ch.PushError, match="no SMTP") as e:
        ch.send_email(mail_cfg(), "s", "t", "h")
    assert e.value.retryable is False
