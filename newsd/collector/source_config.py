"""Merge owner 信息源 settings (var/state/sources.json, pulled by sync-state.mjs from D1) into
config/collect-sources.json defaults. Deterministic; missing/invalid settings file = defaults only.

Source ids: plugin:<name> · rss:<id> · reddit:<subreddit> · rsshub:<id> · xq:<query id> · xa:<handle>
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "var/state/sources.json"


def load_settings(path: Path | None = None) -> dict:
    p = path or STATE
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def apply(sources: dict, cfg: dict) -> tuple[dict, set[str]]:
    """Return (effective sources, disabled plugin names)."""
    s = copy.deepcopy(sources)
    off = set(cfg.get("disabled") or [])
    removed = set(cfg.get("removed") or [])
    plugins_off = {i.split(":", 1)[1] for i in off if i.startswith("plugin:")}
    have_rss = {f.get("url") for f in s.get("rss", [])} | {f["id"] for f in s.get("rss", [])}
    s["rss"] = s.get("rss", []) + [{"id": f["id"], "url": f["url"], "label": f.get("label")} for f in (cfg.get("feeds") or [])
                                   if f.get("id") and str(f.get("url", "")).startswith("https://") and f["id"] not in have_rss and f["url"] not in have_rss]
    s["rss"] = [f for f in s["rss"] if f"rss:{f['id']}" not in off]
    if cfg.get("subreddits"):
        rd = s.setdefault("reddit", {"feeds": []}); known = {f["subreddit"].lower() for f in rd.get("feeds", [])}
        rd["feeds"] = rd.get("feeds", []) + [{"subreddit": n, "mode": "new"} for n in cfg["subreddits"] if n.lower() not in known]
    if cfg.get("hf_orgs"):
        s.setdefault("hf_trending", {})["orgs"] = [o for o in cfg["hf_orgs"] if f"hforg:{o}" not in off]
    if "reddit" in s:
        s["reddit"]["feeds"] = [f for f in s["reddit"].get("feeds", []) if f"reddit:{f['subreddit']}" not in off]
    hub = s.setdefault("rsshub", {})
    routes = [r for r in hub.get("routes", []) if r["id"] not in removed]
    have = {r["id"] for r in routes}
    routes += [r for r in (cfg.get("rsshub") or []) if r.get("id") and r.get("route") and r["id"] not in have]
    hub["routes"] = [r for r in routes if f"rsshub:{r['id']}" not in off]
    return s, plugins_off


def x_accounts(sources: dict, cfg: dict) -> list[tuple[str, str, list[str]]]:
    """Enabled accounts in fixed order: (handle, group, extra keywords). Honors per-account, group and plugin toggles."""
    off = set(cfg.get("disabled") or [])
    x = sources.get("x") or {}
    if "plugin:x" in off:
        return []
    groups_off = {g for g, v in (cfg.get("x_groups") or {}).items() if v is False}
    accts = []
    for a in list(x.get("accounts") or []) + list(cfg.get("x_accounts") or []):
        if isinstance(a, str):
            a = {"handle": a, "enabled": True}
        if not isinstance(a, dict) or not a.get("enabled") or not a.get("handle"):
            continue
        h, g = str(a["handle"]).lstrip("@"), a.get("group") or "未分组"
        if f"xa:{h}" in off or g in groups_off or any(h.lower() == o[0].lower() for o in accts):
            continue
        accts.append((h, g, [k for k in (a.get("keywords") or []) if isinstance(k, str) and k.strip()]))
    return accts

