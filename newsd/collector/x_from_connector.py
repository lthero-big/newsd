#!/usr/bin/env python3
"""Convert raw X connector responses (search_posts_all / search_news JSON) into x-items.json.

The agent saves connector output to files, then:
    python3 scripts/collect/x_from_connector.py raw1.json [raw2.json ...] --out var/x-items.json [--min-likes 20]
Spam filter is rule based: minimum likes, no pure link/hashtag posts, dedupe by id.
"""
import argparse, html, json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import prompts

# CallDynamicTool write_to_file may wrap JSON in <cursor_untrusted_data_…> … </…> (or *_redacted).
# Trailing quota prose after the JSON object is also common — strip both before parsing.
_FENCE = re.compile(r"</?cursor_untrusted[_a-zA-Z0-9-]*[^>]*>", re.I)
# Share phrasings without a "Prompt:" marker. Deliberately narrow: plain mentions such as
# "system prompt", "prompt injection" or "prompt caching" must NOT turn a news post into a prompt card.
_EXTRA_PROMPT_HINT = re.compile(
    r"(?:附|带|含)?完整提示词|附(?:上)?提示词|提示词(?:见|在)(?:评论|下方|图|alt)"
    r"|\bfull\s+prompts?\b|\bprompts?\s+(?:below|in\s+(?:the\s+)?(?:alt|comments?|replies|reply|thread))\b",
    re.I,
)


def strip_untrusted(text: str) -> str:
    """Remove Cursor untrusted-data / redacted fences; leave the inner payload."""
    return _FENCE.sub("", text or "").strip()


def looks_like_prompt_share(text: str) -> bool:
    """Conservative: same markers as prompts.extract, plus a few share-phrasing hints."""
    t = text or ""
    return bool(prompts.MARK.search(t) or _EXTRA_PROMPT_HINT.search(t))


def load_any(path):
    """Load connector JSON; tolerate untrusted fences and trailing non-JSON prose."""
    raw = Path(path).read_text(encoding="utf-8")
    text = strip_untrusted(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start < 0:
        start = text.find("[")
    if start < 0:
        raise ValueError(f"{path}: no JSON object/array after stripping fences")
    dec = json.JSONDecoder()
    try:
        obj, _ = dec.raw_decode(text, start)
        return obj
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: JSON parse failed after fence strip: {e}") from e


MIN_MEDIA_WIDTH = 300  # gallery photos; generate.py still needs ≥600 for a single big image


def best_mp4(variants, cap=2_500_000):
    """Highest-bitrate mp4 at or under ~2.5 Mbps (else the lightest one); HLS is skipped."""
    mp4 = [v for v in (variants or []) if v.get("content_type") == "video/mp4" and str(v.get("url", "")).startswith("https://video.twimg.com/")]
    if not mp4:
        return None
    ok = [v for v in mp4 if (v.get("bit_rate") or 0) <= cap]
    pick = max(ok, key=lambda v: v.get("bit_rate") or 0) if ok else min(mp4, key=lambda v: v.get("bit_rate") or 0)
    return pick["url"]


def pick_media(post, media):
    """Keep full-size photos (or video posters) only; tiny images would be upscaled and look blurry."""
    out = []
    for key in ((post.get("attachments") or {}).get("media_keys") or []):
        m = media.get(key) or {}
        url = m.get("url") if m.get("type") == "photo" else m.get("preview_image_url")
        w, h = m.get("width") or 0, m.get("height") or 0
        if url and url.startswith("https://pbs.twimg.com/") and w >= MIN_MEDIA_WIDTH and h > 0:
            row = {"url": url, "width": int(w), "height": int(h), "type": m.get("type")}
            if m.get("alt_text"):
                row["alt"] = m["alt_text"][:2000]
            v = best_mp4(m.get("variants"))
            if v:
                row["video"] = v
            out.append(row)
    return out[:4]


def _apply_prompt(item, text, thread_texts):
    item["prompt_q"] = True
    if item.get("prompt"):
        return
    pr = prompts.extract(text, [m.get("alt", "") for m in item.get("media") or []], thread_texts)
    if pr:
        item["prompt"] = pr


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--extra", action="append", default=[], help="x_rsshub.py output(s) whose items are merged (deduped by status id)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-likes", type=int, default=20)
    ap.add_argument("--queries", help="x_queries.py output; a file named <id>.json uses that query's min_likes")
    a = ap.parse_args(argv)
    per = {}
    if a.queries and Path(a.queries).is_file():
        per = {q["id"]: int(q.get("min_likes", a.min_likes)) for q in json.loads(Path(a.queries).read_text(encoding="utf-8"))}
    seen, items = set(), []
    # self-replies (thread files from `conversation_id:<id> from:<author>` searches) feed prompt extraction only
    thread = {}
    files = [f for f in a.files if Path(f).is_file()]
    missing = [f for f in a.files if not Path(f).is_file()]
    for f in missing:
        print(f"x-items: skip missing {f}", file=sys.stderr)
    # prompts*.json first so prompt_q wins when the same id also appears in kw-* / models
    files.sort(key=lambda f: (0 if Path(f).stem.startswith("prompts") else 1, Path(f).name))
    for f in files:
        if "thread" in Path(f).stem:
            try:
                doc = load_any(f)
            except ValueError as e:
                print(f"x-items: FATAL {e}", file=sys.stderr)
                return 2
            for p in (doc.get("data") or []):
                thread.setdefault(p.get("conversation_id"), []).append((p.get("author_id"), html.unescape(p.get("note_tweet", {}).get("text") or p.get("text") or "")))
    for f in files:
        if "thread" in Path(f).stem:
            continue
        stem = Path(f).stem
        is_prompts = stem.startswith("prompts")
        try:
            doc = load_any(f)
        except ValueError as e:
            # prompts*.json must not be silently skipped — otherwise prompt shares fall into community
            if is_prompts:
                print(f"x-items: FATAL prompts file unreadable: {e}", file=sys.stderr)
                return 2
            print(f"x-items: skip unreadable {f}: {e}", file=sys.stderr)
            continue
        min_likes = per.get(stem, a.min_likes)
        # prompts query min_likes is a ranking preference; still accept posts that cleared the global floor
        # so a soft-engagement prompt share is not dropped from the prompts file entirely
        if is_prompts:
            min_likes = min(min_likes, a.min_likes)
        inc = doc.get("includes") or {}
        users = {u.get("id"): u.get("username") for u in inc.get("users", [])}
        uinfo = {u.get("id"): u for u in inc.get("users", [])}
        media = {m.get("media_key"): m for m in inc.get("media", [])}
        for p in doc.get("data") or []:
            pm = p.get("public_metrics") or {}
            text = html.unescape((p.get("note_tweet") or {}).get("text") or p.get("text") or "").strip()
            pid = p.get("id")
            # queries use -is:reply, so a post missing conversation_id (the connector sometimes drops it) is its own root
            th = [t for au, t in thread.get(p.get("conversation_id") or pid, []) if au == p.get("author_id") and t != text]
            if pid in seen:
                for it in items:
                    if not (pid and pid in it.get("url", "")):
                        continue
                    # later file may carry note_tweet (full text); prefer the longer body and re-extract
                    if len(text) > len(it.get("text") or ""):
                        it["text"] = text
                        it.pop("prompt", None)
                    body = it.get("text") or text
                    if is_prompts or looks_like_prompt_share(body):
                        _apply_prompt(it, body, th)
                    break
                continue
            if pm.get("like_count", 0) < min_likes:
                continue
            if len(re.sub(r"(https?://\S+|#\w+|@\w+)", "", text).strip()) < 40:
                continue
            seen.add(pid)
            items.append({"url": p.get("url") or f"https://x.com/i/status/{p['id']}", "text": text,
                          "author": users.get(p.get("author_id")) or (p.get("url") or "").split("/")[3:4][0] if p.get("url") else None,
                          "created_at": p.get("created_at"), "likes": pm.get("like_count", 0),
                          "reposts": pm.get("retweet_count", 0), "replies": pm.get("reply_count", 0),
                          "media": pick_media(p, media),
                          "author_name": (uinfo.get(p.get("author_id")) or {}).get("name"),
                          "avatar": ((uinfo.get(p.get("author_id")) or {}).get("profile_image_url") or "").replace("_normal.", "_400x400.") or None,
                          "verified": bool((uinfo.get(p.get("author_id")) or {}).get("verified")),
                          "reposts_n": pm.get("retweet_count", 0)})
            outs = [u.get("unwound_url") or u.get("expanded_url") for u in ((p.get("entities") or {}).get("urls") or [])]
            outs = [u for u in outs if u and not re.match(r"https?://(www\.)?(x|twitter)\.com/", u)]
            if outs:
                items[-1]["links_out"] = outs[:3]
            if is_prompts or looks_like_prompt_share(text):
                _apply_prompt(items[-1], text, th)
    ids = {re.sub(r"\D", "", i["url"].rsplit("/", 1)[-1]) for i in items}
    for f in a.extra:
        if Path(f).is_file():
            try:
                extra = load_any(f)
            except ValueError as e:
                print(f"x-items: FATAL extra file unreadable: {e}", file=sys.stderr)
                return 2
            for r in (extra.get("items") if isinstance(extra, dict) else None) or []:
                sid = re.sub(r"\D", "", r["url"].rsplit("/", 1)[-1])
                if sid not in ids:
                    ids.add(sid); items.append(r)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps({"items": items}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"x-items: {len(items)} -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
