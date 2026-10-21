"""Turn a stored issue into push messages.

``build_digest`` flattens an issue into plain items; ``build_messages`` renders them in one *flavor* (markdown, Slack
mrkdwn, Telegram HTML, plain text) as either one summary message or an overview plus one message per item, trimmed
to the target's length limit. Everything that comes from feeds is untrusted and escaped for the flavor."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..issues import issue_view
from ..settings import get_settings

MODES = ("summary", "summary_and_items")
SNIPPET = 160
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def clean_text(s: str | None, limit: int = SNIPPET, strip_tags: bool = True) -> str:
    s = html.unescape(_TAGS.sub(" ", s or "") if strip_tags else s or "")
    s = _WS.sub(" ", s).strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def safe_url(url) -> str:
    u = (url or "").strip()
    return u if re.match(r"^https?://[^\s]+$", u) else ""


class Flavor:
    name = "plain"

    def esc(self, t: str) -> str: return t
    def bold(self, t: str) -> str: return self.esc(t)
    def link(self, title: str, url: str) -> str:
        url = safe_url(url)
        return f"{self.esc(title)} {url}" if url else self.esc(title)
    def italic(self, t: str) -> str: return self.esc(t)


class Markdown(Flavor):
    name = "md"

    def esc(self, t): return re.sub(r"([\[\]*_`])", r"\\\1", t)
    def bold(self, t): return f"**{self.esc(t)}**"
    def link(self, title, url):
        url = safe_url(url)
        return f"[{self.esc(title)}]({url.replace(')', '%29')})" if url else self.esc(title)


class Slack(Flavor):
    name = "slack"

    def esc(self, t): return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    def bold(self, t): return f"*{self.esc(t).replace('*', '')}*"
    def link(self, title, url):
        url = safe_url(url)
        return f"<{url.replace('>', '%3E').replace('|', '%7C')}|{self.esc(title)}>" if url else self.esc(title)


class Html(Flavor):
    name = "html"  # Telegram's HTML subset, also used for the e-mail body

    def esc(self, t): return html.escape(t, quote=False)
    def bold(self, t): return f"<b>{self.esc(t)}</b>"
    def link(self, title, url):
        url = safe_url(url)
        return f'<a href="{html.escape(url, quote=True)}">{self.esc(title)}</a>' if url else self.esc(title)
    def italic(self, t): return f"<i>{self.esc(t)}</i>"


FLAVORS = {f.name: f() for f in (Flavor, Markdown, Slack, Html)}


@dataclass
class Digest:
    user: str
    key: str
    items: list[dict]
    url: str = ""  # link to the issue page, when NEWSD_PUBLIC_URL is set
    sample: bool = False

    @property
    def title(self) -> str:
        return f"newsd 日报 {self.key}"


@dataclass
class Message:
    kind: str  # summary | overview | item
    title: str
    text: str
    vars: dict = field(default_factory=dict)


def build_digest(db, issue, username: str) -> Digest:
    view = issue_view(db, issue)
    items = []
    for it in view["items"]:
        if it["state"] == "trash":
            continue
        items.append({"title": clean_text(it["title_translated"] or it["title"], 200, strip_tags=False), "url": safe_url(it["url"]),
                      "summary": clean_text(it["summary_translated"] or it["summary"]),
                      "source": ", ".join(it["sources"][:2]), "module": it["module"], "score": round(float(it["score"]), 1)})
    base = get_settings().public_url
    return Digest(username, issue.key, items, f"{base}/issue/{issue.key}" if base else "")


def sample_digest(username: str) -> Digest:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    items = [{"title": "这是一条测试推送", "url": "https://example.com/newsd-test", "summary": "如果你看到了这条消息，说明这个渠道已经配置成功。",
              "source": "newsd", "module": "news", "score": 90.0},
             {"title": "Test item #2", "url": "https://example.com/newsd-test-2", "summary": "Second sample item, to preview the layout.",
              "source": "newsd", "module": "model", "score": 80.0}]
    return Digest(username, now, items, sample=True)


def _meta(f: Flavor, it: dict) -> str:
    bits = [b for b in (it["source"], it["module"]) if b]
    return f.italic(" · ".join(bits)) if bits else ""


def _entry(f: Flavor, n: int, it: dict, with_summary: bool = True) -> str:
    lines = [f"{n}. {f.link(it['title'], it['url'])}"]
    if with_summary and it["summary"]:
        lines.append("   " + f.esc(it["summary"]))
    return "\n".join(lines)


def _header(f: Flavor, d: Digest, shown: int) -> str:
    h = f.bold(d.title) + f"\n共 {len(d.items)} 条" + ("（测试）" if d.sample else "")
    if shown < len(d.items):
        h += f"，下面列出前 {shown} 条"
    return h


def _fit(f: Flavor, d: Digest, limit: int, with_summary: bool, cap: int | None = None) -> str:
    """Header + as many entries as fit under ``limit`` characters; the footer says how many were left out."""
    items = d.items[:cap] if cap else d.items
    footer_for = lambda left: ("\n\n" + f"…另 {left} 条" + (f"，完整日报：{f.link(d.title, d.url)}" if d.url else "")) if left else (
        ("\n\n" + f.link("查看完整日报", d.url)) if d.url else "")
    for shown in range(len(items), -1, -1):
        body = "\n\n".join(_entry(f, i + 1, it, with_summary) for i, it in enumerate(items[:shown]))
        left = len(d.items) - shown
        text = _header(f, d, shown) + ("\n\n" + body if body else "") + footer_for(left)
        if len(text) <= limit or shown == 0:
            return text if len(text) <= limit else text[: limit - 1] + "…"
    return ""  # unreachable


def build_messages(d: Digest, flavor: str, mode: str, limit: int, max_items: int | None = None) -> list[Message]:
    f = FLAVORS[flavor]
    common = {"user": d.user, "date": d.key, "key": d.key, "item_count": str(len(d.items)), "issue_url": d.url,
              "message_title": d.title}
    blank_item = {"item_index": "", "item_title": "", "item_url": "", "item_summary": "", "item_source": "", "item_module": "", "item_score": ""}
    if mode == "summary" or not d.items:
        text = _fit(f, d, limit, with_summary=True) if d.items else f.bold(d.title) + "\n今天没有符合条件的内容。"
        return [Message("summary", d.title, text, {**common, **blank_item, "message_kind": "summary", "summary": text})]
    cap = max_items if max_items is not None else get_settings().push_max_items
    shown = d.items[:cap]
    text = _fit(f, d, limit, with_summary=False, cap=cap)
    msgs = [Message("overview", d.title, text, {**common, **blank_item, "message_kind": "overview", "summary": text})]
    for i, it in enumerate(shown, 1):
        body = f.bold(it["title"]) if not it["url"] else f.link(it["title"], it["url"])
        parts = [f"{i}/{len(d.items)} " + body]
        if it["summary"]:
            parts.append(f.esc(it["summary"]))
        if _meta(f, it):
            parts.append(_meta(f, it))
        text = "\n".join(parts)[:limit]
        msgs.append(Message("item", it["title"], text, {**common, "message_kind": "item", "summary": text, "item_index": str(i),
                                                          "item_title": it["title"], "item_url": it["url"], "item_summary": it["summary"],
                                                          "item_source": it["source"], "item_module": it["module"], "item_score": str(it["score"])}))
    return msgs
