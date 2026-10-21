"""Webhook request-body templates, modeled on Horizon's ``request_body``: ``#{name}`` placeholders, optional
``#{name?limit=N&split=DELIM}`` truncation. A template that parses as a JSON object/array is rendered string by string
(so values never break the JSON); any other template is plain text that the platform adapter wraps in its envelope."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs

VARS = ("date", "key", "user", "message_title", "message_kind", "summary", "item_count", "issue_url", "timestamp",
        "item_index", "item_title", "item_url", "item_summary", "item_source", "item_module", "item_score")
_PH = re.compile(r"#\{([A-Za-z_][A-Za-z0-9_]*)(?:\?([^}]*))?\}")
MAX_TEMPLATE = 8000


def placeholders(text: str) -> list[str]:
    return [m.group(1) for m in _PH.finditer(text)]


def _limit(value: str, params: str | None) -> str:
    if not params:
        return value
    q = parse_qs(params, keep_blank_values=True)
    try:
        n = int(q.get("limit", ["0"])[0])
    except ValueError:
        return value
    if n <= 0 or len(value) <= n:
        return value
    cut = value[:n]
    delim = q.get("split", [""])[0]
    if delim and delim in cut:
        cut = cut[: cut.rindex(delim)].rstrip()
    return cut


def render_string(text: str, vars: dict) -> str:
    return _PH.sub(lambda m: _limit(str(vars.get(m.group(1), "")), m.group(2)), text)


def walk(node, vars):
    if isinstance(node, str):
        return render_string(node, vars)
    if isinstance(node, list):
        return [walk(x, vars) for x in node]
    if isinstance(node, dict):
        return {k: walk(v, vars) for k, v in node.items()}
    return node


def parse_json_body(template: str):
    """The parsed object when the template is a JSON object/array, else None (plain text)."""
    try:
        v = json.loads(template)
    except ValueError:
        return None
    return v if isinstance(v, (dict, list)) else None


def render(template: str, vars: dict):
    """JSON object/array templates -> rendered object; anything else -> rendered text."""
    body = parse_json_body(template)
    return walk(body, vars) if body is not None else render_string(template, vars)


def validate(template: str) -> None:
    if len(template) > MAX_TEMPLATE:
        raise ValueError(f"template too long (max {MAX_TEMPLATE} characters)")
    unknown = sorted({n for n in placeholders(template) if n not in VARS})
    if unknown:
        raise ValueError("unknown placeholder: " + ", ".join("#{" + n + "}" for n in unknown) + "; allowed: " + ", ".join(VARS))
