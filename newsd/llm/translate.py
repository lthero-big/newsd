"""Translation with a cache shared by all users (``translation_cache``), keyed by target language +
normalized source text. Only cache misses go to the model, batched; a batch whose reply is invalid
JSON (after one repair) is left untranslated, never failing the run."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re

from sqlalchemy import select

from newsd.models import TranslationCache

from .client import LLMClient, LLMError

BATCH = 20
MAX_CHARS = 2000
LANG_NAMES = {"zh": "简体中文", "en": "English", "ja": "日本語"}
_CJK = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_LATIN = re.compile(r"[A-Za-z]")


def normalize(text: str) -> str:
    return " ".join((text or "").split())[:MAX_CHARS]


def text_key(text: str, lang: str) -> str:
    return hashlib.sha256(f"{lang}\n{normalize(text)}".encode("utf-8")).hexdigest()


def needs_translation(text: str, target: str) -> bool:
    t = normalize(text)
    if not t:
        return False
    cjk, latin = len(_CJK.findall(t)), len(_LATIN.findall(t))
    if target in ("zh", "ja"):
        return latin > 0 and cjk < max(2, 0.2 * latin)
    return cjk > 0.2 * max(1, latin)  # target en: translate mostly-CJK text


def cached(db, texts: list[str], lang: str) -> dict[str, str]:
    keys = {text_key(t, lang): t for t in texts if normalize(t)}
    if not keys:
        return {}
    out = {}
    for row in db.scalars(select(TranslationCache).where(TranslationCache.text_key.in_(list(keys)))):
        out[keys[row.text_key]] = row.translated
        row.hits += 1
    db.commit()
    return out


def _system(lang: str) -> str:
    name = LANG_NAMES.get(lang, lang)
    return (f"把输入的每段文字翻译成{name}。保留专有名词、产品名、模型名和数字原样；不要增删信息。\n"
            '只输出 JSON：{"translations": [{"i": <输入编号>, "text": "<译文>"}]}')


async def translate_texts(db, client: LLMClient | None, texts: list[str], lang: str = "zh") -> tuple[dict[str, str], dict]:
    """Return ({source text: translation}, stats). Texts that need no translation are skipped.
    With client None only cache hits are returned (no model calls)."""
    todo = list(dict.fromkeys(t for t in texts if needs_translation(t, lang)))
    stats = {"wanted": len(todo), "cache_hits": 0, "translated": 0, "failed": 0}
    result = cached(db, todo, lang)
    stats["cache_hits"] = len(result)
    misses = [t for t in todo if t not in result]
    if client is None or not misses:
        return result, stats
    chunks = [misses[i:i + BATCH] for i in range(0, len(misses), BATCH)]

    async def one(chunk):
        payload = json.dumps([{"i": i, "text": normalize(t)} for i, t in enumerate(chunk)], ensure_ascii=False)
        try:
            return await client.complete_json(_system(lang), payload, max_tokens=4096)
        except LLMError:
            return None

    replies = await asyncio.gather(*(one(c) for c in chunks))  # the client caps concurrency and rate
    for chunk, data in zip(chunks, replies):
        got = {}
        rows = data.get("translations") if isinstance(data, dict) else data if isinstance(data, list) else None
        for r in rows or []:
            if (isinstance(r, dict) and isinstance(r.get("i"), int) and 0 <= r["i"] < len(chunk)
                    and isinstance(r.get("text"), str) and r["text"].strip()):
                got[chunk[r["i"]]] = r["text"].strip()
        for src, dst in got.items():
            k = text_key(src, lang)
            if db.scalar(select(TranslationCache.id).where(TranslationCache.text_key == k)) is None:
                db.add(TranslationCache(text_key=k, target_lang=lang, source_text=normalize(src), translated=dst,
                                        model=client.label))
            result[src] = dst
        db.commit()
        stats["translated"] += len(got)
        stats["failed"] += len(chunk) - len(got)
    return result, stats
