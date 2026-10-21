"""Settings page / API: channel config, secret handling, per-channel "send test push", admin delivery log."""
import json

from sqlalchemy import select

from pagekit import login, register
from pushkit import TG_TOKEN, WEBHOOK

TG = {"enabled": True, "target": "424242", "token": TG_TOKEN}
PUSH = {"enabled": True, "time": "07:45", "timezone": "Asia/Shanghai"}


def put(client, channels, **kw):
    return client.put("/api/me/push", json={**PUSH, **kw, "channels": channels})


def raw_settings(user_name):
    from newsd.db import SessionLocal
    from newsd.models import User, UserSetting
    db = SessionLocal()
    try:
        u = db.scalar(select(User).where(User.username == user_name))
        row = db.scalar(select(UserSetting).where(UserSetting.user_id == u.id, UserSetting.key == "push"))
        return json.dumps(row.value), row.secret
    finally:
        db.close()


def test_secrets_are_encrypted_at_rest_and_only_hints_come_back(client):
    register(client, "alice")
    r = put(client, {"telegram": TG, "webhook": {"enabled": True, "target": WEBHOOK, "type": "feishu", "secret": "sign-me-0123"},
                     "email": {"enabled": True, "target": "alice@example.com"}})
    assert r.status_code == 200, r.text
    got = r.json()
    wh, tg = got["channels"]["webhook"], got["channels"]["telegram"]
    assert wh["has_url"] and wh["url_hint"] == "https://hooks.example.com/…1234" and wh["has_secret"] and wh["type"] == "feishu"
    assert tg["has_token"] and tg["token_hint"] == "…6789" and tg["target"] == "424242"
    blob = json.dumps(got) + client.get("/api/me/push").text + client.get("/settings").text
    for secret in (TG_TOKEN, WEBHOOK, "SECRETPATH1234", "sign-me-0123"):
        assert secret not in blob
    value, enc = raw_settings("alice")
    assert enc and all(s not in value + enc for s in (TG_TOKEN, "SECRETPATH1234", "sign-me-0123"))  # not in the JSON, not in the blob
    from newsd.security import decrypt_secret
    assert json.loads(decrypt_secret(enc)) == {"telegram_token": TG_TOKEN, "webhook_url": WEBHOOK, "webhook_secret": "sign-me-0123"}


def test_omitted_secret_keeps_stored_value_and_empty_clears_it(client):
    register(client, "alice")
    put(client, {"telegram": TG, "webhook": {"enabled": True, "target": WEBHOOK}})
    # the settings page re-submits without secrets (null) -> unchanged
    r = put(client, {"telegram": {"enabled": True, "target": "424242"}, "webhook": {"enabled": True, "type": "slack", "mode": "summary_and_items"}})
    ch = r.json()["channels"]
    assert ch["telegram"]["has_token"] and ch["webhook"]["has_url"] and ch["webhook"]["type"] == "slack" and ch["webhook"]["mode"] == "summary_and_items"
    # "" clears; a channel left out of the body is untouched
    r = put(client, {"telegram": {"enabled": False, "target": "", "token": ""}})
    ch = r.json()["channels"]
    assert not ch["telegram"]["has_token"] and ch["webhook"]["has_url"] and ch["webhook"]["enabled"] is True


def test_validation(client):
    register(client, "alice")
    bad = [({"telegram": {"enabled": True, "target": "424242"}}, "bot token"),
           ({"telegram": {"enabled": True, "target": "424242", "token": "nope"}}, "token looks wrong"),
           ({"webhook": {"enabled": True, "target": "ftp://x.example/y"}}, "http(s)"),
           ({"webhook": {"enabled": True, "target": "http://127.0.0.1/hook"}}, "not allowed"),
           ({"webhook": {"enabled": True, "target": WEBHOOK, "type": "teams"}}, "type must be"),
           ({"webhook": {"enabled": True, "target": WEBHOOK, "mode": "digest"}}, "mode must be"),
           ({"webhook": {"enabled": True, "target": WEBHOOK, "template": "hi #{nope}"}}, "unknown placeholder"),
           ({"webhook": {"enabled": True, "target": WEBHOOK, "item_template": "x" * 9001}}, "")]
    for channels, msg in bad:
        r = put(client, channels)
        assert r.status_code in (400, 422), (channels, r.text)
        assert msg in r.text
    assert client.get("/api/me/push").json()["channels"]["webhook"]["has_url"] is False  # nothing half-saved


def test_server_default_bot_lets_users_skip_their_own_token(client, monkeypatch):
    monkeypatch.setenv("NEWSD_TELEGRAM_BOT_TOKEN", TG_TOKEN)
    from newsd.settings import reset_settings
    reset_settings()
    register(client, "alice")
    r = put(client, {"telegram": {"enabled": True, "target": "@mychannel"}})
    assert r.status_code == 200 and r.json()["channels"]["telegram"]["server_bot"] is True


def test_users_cannot_see_or_use_each_others_channels(client, net):
    register(client, "alice"); register(client, "bob")
    login(client, "alice")
    put(client, {"webhook": {"enabled": True, "target": WEBHOOK}})
    login(client, "bob")
    assert client.get("/api/me/push").json()["channels"]["webhook"]["has_url"] is False
    assert "SECRETPATH" not in client.get("/settings").text
    r = client.post("/api/me/push/test/webhook")
    assert r.status_code == 400 and net.requests == []  # bob has nothing configured; alice's URL is not reachable through him
    assert client.get("/api/me/push/deliveries").json() == []


def test_test_push_webhook_from_settings(client, net):
    register(client, "alice")
    net.on(WEBHOOK, (200, {"code": 0}))
    put(client, {"webhook": {"enabled": True, "target": WEBHOOK, "type": "feishu", "mode": "summary_and_items"}})
    r = client.post("/api/me/push/test/webhook")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["error"] is None
    assert r.json()["delivery"]["kind"] == "test" and r.json()["delivery"]["total"] == 3  # the real template path: overview + 2 sample items
    bodies = [net.body(q) for q in net.requests]
    assert len(bodies) == 3 and all(b["msg_type"] == "interactive" for b in bodies)
    assert "测试" in json.dumps(bodies[0], ensure_ascii=False)
    hist = client.get("/api/me/push/deliveries").json()
    assert [(h["kind"], h["channel"], h["status"]) for h in hist] == [("test", "webhook", "ok")]


def test_test_push_telegram_from_settings(client, net):
    register(client, "alice")
    net.on("https://api.telegram.org/bot", (200, {"ok": True}))
    put(client, {"telegram": TG})
    r = client.post("/api/me/push/test/telegram")
    assert r.json()["ok"] is True
    body = net.body(net.requests[0])
    assert body["chat_id"] == "424242" and "测试" in body["text"] and str(net.requests[0].url).endswith(f"/bot{TG_TOKEN}/sendMessage")


def test_test_push_email_from_settings(client, smtp):
    register(client, "alice")
    put(client, {"email": {"enabled": True, "target": "alice@example.com"}})
    r = client.post("/api/me/push/test/email")
    assert r.json()["ok"] is True, r.text
    [m] = smtp.messages
    assert m["to"] == ["<alice@example.com>"] and "测试" in m["msg"]["Subject"]


def test_failed_test_push_returns_the_error_without_secrets(client, net):
    register(client, "alice")
    net.on("https://api.telegram.org/bot", (401, {"ok": False, "description": f"Unauthorized {TG_TOKEN}"}))
    put(client, {"telegram": TG})
    r = client.post("/api/me/push/test/telegram")
    assert r.status_code == 200 and r.json()["ok"] is False and "401" in r.json()["error"]
    assert TG_TOKEN not in r.text and "AAH-secret" not in r.text
    # e-mail without server SMTP: clear message, still no secret
    put(client, {"email": {"enabled": True, "target": "alice@example.com"}})
    r = client.post("/api/me/push/test/email")
    assert r.json()["ok"] is False and "no SMTP" in r.json()["error"]


def test_test_push_errors_and_auth(client):
    assert client.post("/api/me/push/test/webhook").status_code == 401
    register(client, "alice")
    assert client.post("/api/me/push/test/sms").status_code == 404
    assert client.post("/api/me/push/test/webhook").status_code == 400  # not configured yet


def test_settings_page_has_the_channel_forms_and_test_buttons(client):
    register(client, "alice")
    put(client, {"telegram": TG, "email": {"enabled": True, "target": "alice@example.com"}})
    html = client.get("/settings").text
    for needle in ('data-test="email"', 'data-test="telegram"', 'data-test="webhook"', 'name="telegram-token"', 'name="webhook-type"',
                   'name="webhook-mode"', 'name="webhook-template"', "summary_and_items", "07:45", "alice@example.com", "…6789", "飞书", "钉钉", "Slack", "Discord"):
        assert needle in html, needle
    assert 'value="424242"' in html and TG_TOKEN not in html


def test_admin_sees_deliveries_and_errors_are_escaped(client, net):
    register(client, "alice")
    net.on(WEBHOOK, (500, "<script>alert(1)</script>"))
    put(client, {"webhook": {"enabled": True, "target": WEBHOOK}})
    assert client.post("/api/me/push/test/webhook").json()["ok"] is False
    # non-admins are refused
    assert client.get("/api/admin/push-deliveries").status_code == 403
    client.cookies.clear()
    r = client.post("/login", data={"username": "root", "password": "rootpass123"}, follow_redirects=False)
    assert r.status_code == 303
    rows = client.get("/api/admin/push-deliveries").json()
    assert len(rows) == 1 and rows[0]["user"] == "alice" and rows[0]["channel"] == "webhook" and rows[0]["status"] == "failed"
    assert rows[0]["attempts"] == 1 and "500" in rows[0]["error"] and rows[0]["log"][0]["ok"] is False
    assert client.get("/api/admin/push-deliveries?status=ok").json() == []
    assert len(client.get("/api/admin/push-deliveries?channel=webhook").json()) == 1
    page = client.get("/admin/logs").text  # 3c: push records moved to 日志与任务
    assert "推送记录" in page and "alice" in page and "<script>alert(1)</script>" not in page and "SECRETPATH" not in page


def test_cli_push_commands(client, net, capsys):
    from newsd.cli import main
    register(client, "alice")
    net.on(WEBHOOK, (200, {"ok": 1}))
    put(client, {"webhook": {"enabled": True, "target": WEBHOOK}})
    capsys.readouterr()
    assert main(["push", "test", "alice", "webhook"]) == 0
    assert "ok" in capsys.readouterr().out
    assert main(["push", "test", "nobody", "webhook"]) == 1
    assert main(["push", "list"]) == 0
    out = capsys.readouterr().out
    assert "test" in out and "webhook" in out and "user=alice" in out
    assert main(["push", "run"]) == 0
