"""Senders for the three channels. Each raises PushError (retryable or not) with a message that has already had the
channel's secrets and any URL path scrubbed -- the text ends up in the database and on the admin page."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
import smtplib
import socket
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from urllib.parse import quote_plus, urlparse

import httpx

from ..settings import get_settings
from . import template as tpl
from .digest import Message
from .prefs import ChannelConfig

# Tests (and embedders) may set an httpx transport; every outgoing HTTP call uses it.
TRANSPORT = None
# Tests may replace the SMTP connection factory: (host, port, ssl, timeout) -> smtplib.SMTP-like object.
SMTP_FACTORY = None

LIMITS = {"telegram": 4000, "email": 100000, "generic": 20000, "feishu": 8000, "dingtalk": 5000, "slack": 3500, "discord": 1900}


class PushError(Exception):
    def __init__(self, message: str, retryable: bool = True, retry_after: float | None = None):
        super().__init__(message)
        self.retryable, self.retry_after = retryable, retry_after


_URL_PATH = re.compile(r"(https?://[^/\s'\"<>]+)/[^\s'\"<>]*")
_BOT = re.compile(r"bot\d+:[\w-]+")


def redact(text: str, secrets=()) -> str:
    for s in sorted((x for x in secrets if x and len(x) >= 4), key=len, reverse=True):
        text = text.replace(s, "***")
    text = _BOT.sub("bot***", text)
    return _URL_PATH.sub(r"\1/***", text)[:400]


# ------------------------------------------------------------------------------------------------ SSRF guard

def _resolve(host: str) -> list[str]:
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})
    except socket.gaierror as exc:
        raise PushError(f"cannot resolve webhook host: {exc}", retryable=True) from exc


def assert_public_url(url: str, resolve: bool = True) -> None:
    """Webhooks are user-supplied URLs, so refuse loopback / private / link-local targets (NEWSD_PUSH_ALLOW_PRIVATE=1
    lifts this for self-hosted receivers). With resolve=False only literal IPs and 'localhost' are checked (save time)."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise PushError("webhook must be an http(s) URL", retryable=False)
    if get_settings().push_allow_private:
        return
    host = u.hostname
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".internal"):
        raise PushError("webhook host is not allowed (local address)", retryable=False)
    try:
        addrs = [host]
        ipaddress.ip_address(host)
    except ValueError:
        addrs = _resolve(host) if resolve else []
    for a in addrs:
        ip = ipaddress.ip_address(a)
        if not ip.is_global or ip.is_multicast:
            raise PushError("webhook host is not allowed (private or local address); set NEWSD_PUSH_ALLOW_PRIVATE=1 to permit", retryable=False)


# ------------------------------------------------------------------------------------------------ HTTP

def _retry_after(r: httpx.Response) -> float | None:
    try:
        v = r.headers.get("retry-after")
        if v:
            return float(v)
        j = r.json()
        for cand in (j.get("retry_after"), (j.get("parameters") or {}).get("retry_after")):
            if cand is not None:
                return float(cand)
    except Exception:  # noqa: BLE001
        pass
    return None


def http_post(url: str, *, json_body=None, secrets=(), what: str = "request") -> httpx.Response:
    cfg = get_settings()
    try:
        with httpx.Client(timeout=cfg.push_timeout, transport=TRANSPORT, follow_redirects=False) as c:
            r = c.post(url, json=json_body)
    except httpx.HTTPError as exc:
        raise PushError(redact(f"{what}: {type(exc).__name__}: {exc}", secrets)) from None
    if r.status_code == 429:
        raise PushError(redact(f"{what}: rate limited (HTTP 429)", secrets), retryable=True, retry_after=_retry_after(r))
    if r.status_code >= 500:
        raise PushError(redact(f"{what}: HTTP {r.status_code} {r.text[:120]}", secrets), retryable=True)
    if r.status_code >= 300:
        raise PushError(redact(f"{what}: HTTP {r.status_code} {r.text[:200]}", secrets), retryable=False)
    return r


def _json(r: httpx.Response) -> dict:
    try:
        v = r.json()
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


# ------------------------------------------------------------------------------------------------ Telegram

def send_telegram(cfg: ChannelConfig, msg: Message) -> None:
    if not cfg.token:
        raise PushError("telegram: no bot token configured", retryable=False)
    if not cfg.target:
        raise PushError("telegram: no chat id configured", retryable=False)
    url = f"{get_settings().telegram_api_base}/bot{cfg.token}/sendMessage"
    try:
        r = http_post(url, json_body={"chat_id": cfg.target, "text": msg.text, "parse_mode": "HTML", "disable_web_page_preview": True},
                      secrets=cfg.secrets(), what="telegram")
    except PushError as exc:
        # Telegram answers 4xx with {"ok":false,"description":...}; http_post already folded the body into the message
        raise exc
    j = _json(r)
    if not j.get("ok", True):
        raise PushError(redact(f"telegram: {j.get('description', 'rejected')}", cfg.secrets()), retryable=False)


# ------------------------------------------------------------------------------------------------ e-mail

def send_email(cfg: ChannelConfig, subject: str, text: str, html_body: str) -> None:
    s = get_settings()
    if not s.smtp_host:
        raise PushError("email: the server has no SMTP configured (NEWSD_SMTP_HOST)", retryable=False)
    if not cfg.target:
        raise PushError("email: no recipient configured", retryable=False)
    sender = s.smtp_from or s.smtp_user
    if not sender:
        raise PushError("email: NEWSD_SMTP_FROM is not set", retryable=False)
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = subject, sender, cfg.target
    m["Date"], m["Message-ID"] = formatdate(localtime=False), make_msgid(domain="newsd.local")
    m.set_content(text)
    m.add_alternative(html_body, subtype="html")
    secrets = [s.smtp_password]
    smtp = None
    try:
        factory = SMTP_FACTORY or _default_smtp
        smtp = factory(s.smtp_host, s.smtp_port, s.smtp_security == "ssl", s.push_timeout)
        if s.smtp_security == "starttls":
            smtp.starttls(context=ssl.create_default_context())
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(m)
    except smtplib.SMTPAuthenticationError as exc:
        raise PushError(redact(f"email: SMTP login rejected ({exc.smtp_code})", secrets), retryable=False) from None
    except smtplib.SMTPRecipientsRefused:
        raise PushError("email: the recipient address was refused by the SMTP server", retryable=False) from None
    except smtplib.SMTPResponseException as exc:
        raise PushError(redact(f"email: SMTP {exc.smtp_code} {exc.smtp_error!r}", secrets), retryable=exc.smtp_code < 500) from None
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise PushError(redact(f"email: {type(exc).__name__}: {exc}", secrets)) from None
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001
                pass


def _default_smtp(host, port, use_ssl, timeout):
    if use_ssl:
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
    return smtplib.SMTP(host, port, timeout=timeout)


# ------------------------------------------------------------------------------------------------ webhook

# Built-in request bodies per platform (Horizon-style templates). A user template that is a JSON object replaces the whole
# body; a plain-text template replaces #{summary} inside the platform envelope.
DEFAULT_BODIES = {
    "generic": {"title": "#{message_title}", "text": "#{summary}", "kind": "#{message_kind}", "date": "#{date}", "count": "#{item_count}",
                "url": "#{issue_url}", "item": {"index": "#{item_index}", "title": "#{item_title}", "url": "#{item_url}",
                                                  "summary": "#{item_summary}", "source": "#{item_source}", "score": "#{item_score}"}},
    "feishu": {"msg_type": "interactive", "card": {"schema": "2.0", "config": {"wide_screen_mode": True},
                                                    "header": {"title": {"tag": "plain_text", "content": "#{message_title}"}, "template": "blue"},
                                                    "body": {"elements": [{"tag": "markdown", "content": "#{summary}"}]}}},
    "dingtalk": {"msgtype": "markdown", "markdown": {"title": "#{message_title}", "text": "#{summary}"}},
    "slack": {"text": "#{summary}", "unfurl_links": False},
    "discord": {"content": "#{summary}", "allowed_mentions": {"parse": []}},
}
FLAVOR_OF = {"generic": "md", "feishu": "md", "dingtalk": "md", "slack": "slack", "discord": "md"}


def sign_feishu(secret: str, ts: str) -> str:
    return base64.b64encode(hmac.new(f"{ts}\n{secret}".encode(), b"", hashlib.sha256).digest()).decode()


def sign_dingtalk(secret: str, ts: str) -> str:
    return quote_plus(base64.b64encode(hmac.new(secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest()))


def build_webhook_request(cfg: ChannelConfig, msg: Message, now: float | None = None) -> tuple[str, object]:
    """(url, json body) for one message, signature included. Pure -- the tests check it without a server."""
    ts_s = int(now if now is not None else time.time())
    vars = {**msg.vars, "timestamp": str(ts_s)}
    template = cfg.item_template if msg.kind == "item" and cfg.item_template else cfg.template
    if template and tpl.parse_json_body(template) is not None:  # a full request body of the user's own
        body = tpl.render(template, vars)
    else:  # built-in envelope; a plain-text template replaces the message text
        if template:
            vars["summary"] = tpl.render_string(template, vars)
        body = tpl.walk(DEFAULT_BODIES[cfg.wtype], vars)
    url = cfg.url
    if cfg.secret and cfg.wtype == "feishu" and isinstance(body, dict):
        body = {"timestamp": str(ts_s), "sign": sign_feishu(cfg.secret, str(ts_s)), **body}
    elif cfg.secret and cfg.wtype == "dingtalk":
        ms = str(ts_s * 1000)
        url += ("&" if "?" in url else "?") + f"timestamp={ms}&sign={sign_dingtalk(cfg.secret, ms)}"
    return url, body


def send_webhook(cfg: ChannelConfig, msg: Message) -> None:
    if not cfg.url:
        raise PushError("webhook: no URL configured", retryable=False)
    assert_public_url(cfg.url)
    url, body = build_webhook_request(cfg, msg)
    r = http_post(url, json_body=body, secrets=cfg.secrets(), what=f"webhook({cfg.wtype})")
    j = _json(r)
    if cfg.wtype == "feishu":
        code = j.get("code", j.get("StatusCode", 0))
        if code not in (0, None):
            raise PushError(redact(f"webhook(feishu): code {code} {j.get('msg') or j.get('StatusMessage', '')}", cfg.secrets()),
                            retryable=code == 11232)
    elif cfg.wtype == "dingtalk":
        code = j.get("errcode", 0)
        if code != 0:
            raise PushError(redact(f"webhook(dingtalk): errcode {code} {j.get('errmsg', '')}", cfg.secrets()),
                            retryable=code in (130101, 130102))
    elif cfg.wtype == "slack" and r.text.strip().lower() not in ("ok", ""):
        raise PushError(redact(f"webhook(slack): {r.text[:120]}", cfg.secrets()), retryable=False)
