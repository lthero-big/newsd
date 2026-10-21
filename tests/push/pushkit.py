"""Fakes for the push tests: a recording HTTP "internet" (httpx.MockTransport), a tiny real SMTP server on
127.0.0.1, and helpers that put an issue straight into the DB."""
import base64
import json
import socketserver
import threading
from email import message_from_bytes, policy

import httpx

from newsd.models import Issue, IssueItem, PoolItem, now


class Net:
    """Routes: url prefix -> handler(request) -> httpx.Response | (status, body) | list of those (consumed in order, last repeats)."""

    def __init__(self):
        self.routes, self.requests = [], []

    def on(self, prefix, *responses):
        self.routes.append([prefix, list(responses)])

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for prefix, queue in self.routes:
            if str(request.url).startswith(prefix):
                r = queue.pop(0) if len(queue) > 1 else queue[0]
                if callable(r):
                    r = r(request)
                if isinstance(r, tuple):
                    status, body, *hdr = r
                    return httpx.Response(status, json=body, headers=hdr[0] if hdr else None) if not isinstance(body, str) \
                        else httpx.Response(status, text=body, headers=hdr[0] if hdr else None)
                return r
        raise AssertionError(f"unexpected request to {request.url}")

    def to(self, prefix):
        return [r for r in self.requests if str(r.url).startswith(prefix)]

    @staticmethod
    def body(request):
        return json.loads(request.content)


class SMTPHandler(socketserver.StreamRequestHandler):
    def reply(self, line):
        self.wfile.write((line + "\r\n").encode())

    def handle(self):
        srv = self.server
        self.reply("220 fake.smtp ESMTP")
        mail_from, rcpts = None, []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode(errors="replace").strip()
            cmd = line.split(" ", 1)[0].upper()
            if cmd == "EHLO" or cmd == "HELO":
                self.wfile.write(b"250-fake.smtp\r\n250-AUTH PLAIN LOGIN\r\n250 8BITMIME\r\n")
            elif cmd == "AUTH":
                parts = line.split()
                creds = base64.b64decode(parts[2]).split(b"\0") if len(parts) > 2 else []
                ok = srv.accept_auth and len(creds) == 3 and (creds[1].decode(), creds[2].decode()) == srv.credentials
                srv.auth_attempts.append(tuple(c.decode() for c in creds[1:]))
                self.reply("235 ok" if ok else "535 5.7.8 bad credentials")
            elif cmd == "MAIL":
                mail_from = line.split(":", 1)[1].strip()
                self.reply("250 ok")
            elif cmd == "RCPT":
                if srv.reject_rcpt:
                    self.reply("550 no such user")
                else:
                    rcpts.append(line.split(":", 1)[1].strip()); self.reply("250 ok")
            elif cmd == "DATA":
                if srv.data_error:
                    self.reply(srv.data_error); continue
                self.reply("354 go")
                buf = []
                while True:
                    l = self.rfile.readline()
                    if l in (b".\r\n", b""):
                        break
                    buf.append(l[1:] if l.startswith(b"..") else l)
                srv.messages.append({"from": mail_from, "to": rcpts, "msg": message_from_bytes(b"".join(buf), policy=policy.default)})
                self.reply("250 queued")
            elif cmd == "QUIT":
                self.reply("221 bye")
                return
            else:
                self.reply("250 ok")


class FakeSMTP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, credentials=("mailer", "smtp-pass-123")):
        super().__init__(("127.0.0.1", 0), SMTPHandler)
        self.messages, self.auth_attempts = [], []
        self.credentials, self.accept_auth, self.reject_rcpt, self.data_error = credentials, True, False, None
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server_address[1]

    def stop(self):
        self.shutdown(); self.server_close()


def add_issue(db, user, key, titles, *, state_trash=()):
    """An issue for ``user`` with one pool item per title ("title" or (title, url, summary))."""
    import hashlib
    issue = Issue(user_id=user.id, key=key, status="ok", ai="off", created_at=now(), updated_at=now(), stats={})
    db.add(issue); db.flush()
    for i, t in enumerate(titles, 1):
        title, url, summary = (t, f"https://news.example/{key}/{i}", f"summary of {t}") if isinstance(t, str) else t
        p = PoolItem(url_key=hashlib.sha256(f"{user.id}{key}{i}{title}".encode()).hexdigest(), canonical_url=url, url=url, title=title,
                     summary=summary, kind="post", published_at=now())
        db.add(p); db.flush()
        db.add(IssueItem(issue_id=issue.id, item_id=p.id, rank=i, module="news", score=90 - i, rule_score=90 - i))
    db.commit()
    return issue


def configure(db, user, channels, *, time="08:00", tz="UTC", enabled=True):
    from newsd.push.prefs import save_push
    return save_push(db, user.id, {"enabled": enabled, "time": time, "timezone": tz, "channels": channels})


def make_user(db, name):
    from newsd.models import User
    u = User(username=name, password_hash="x")
    db.add(u); db.commit()
    return u


WEBHOOK = "https://hooks.example.com/services/T000/B000/SECRETPATH1234"
TG_TOKEN = "123456789:AAH-secret_token_value_0123456789"
