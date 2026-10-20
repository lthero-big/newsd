"""Deterministic prompt extraction for X prompt-sharing posts (no AI). Original language is kept verbatim."""
import re

MARK = re.compile(r"(?:^|[\s，,。.;；(（])(?:full\s+)?(?:prompt|prompts|提示词|咒语|プロンプト)\s*(?:[:：👇⬇️↓⤵️\-–—]|\n)+\s*", re.I)
QUOTE = re.compile(r"```(.+?)```|“([^”]{40,})”|\"([^\"]{40,})\"|「([^」]{40,})」", re.S)
# "Paste the prompt\n④ …" / "Prompt given below ⤵️" point elsewhere; text after them is not the prompt itself
POINTER = re.compile(r"\b(?:given\s+)?below\b|\bin\s+(?:the\s+)?(?:replies|reply|comments?|alt|thread)\b|[👇⬇⤵↓]|评论区|见下|在下方", re.I)
TAIL = re.compile(r"(\s*(https?://\S+|#\S+|@\w+))+\s*$")


def _clean(s: str) -> str:
    s = TAIL.sub("", s.strip()).strip(" \n\"“”「」")
    return s


def extract(text: str, alts: list[str] | None = None, thread: list[str] | None = None) -> dict | None:
    """Return {'text', 'from': post|thread|alt} or None. Order: explicit marker in post, then thread, then quotes, then ALT."""
    for src, blobs in (("post", [text or ""]), ("thread", thread or [])):
        for b in blobs:
            for m in MARK.finditer(b):
                t = _clean(b[m.end():])
                if len(t) >= 20 and not (len(t) < 160 and POINTER.search(t)):
                    return {"text": t[:5000], "from": src}
    for src, blobs in (("post", [text or ""]), ("thread", thread or [])):
        for b in blobs:
            m = QUOTE.search(b)
            if m:
                t = _clean(next(g for g in m.groups() if g))
                if len(t) >= 30:
                    return {"text": t[:5000], "from": src}
    for a in alts or []:
        a = (a or "").strip()
        if len(a) >= 40 and not re.match(r"^(image|photo|picture)\b", a, re.I):
            return {"text": a[:5000], "from": "alt"}
    return None
