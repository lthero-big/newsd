"""Import config/collect-sources.json + config/tags.json into the global catalog and the "default" subscription template."""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from .models import GlobalSetting, Source, SubscriptionTemplate, SubscriptionTemplateItem, TagTerm

ROOT = Path(__file__).resolve().parents[1]


def flatten_sources(cfg: dict) -> list[dict]:
    out = []
    for f in cfg.get("rss") or []:
        out.append({"key": f"rss:{f['id']}", "kind": "rss", "name": f.get("label") or f["id"], "url": f["url"], "config": {}})
    for f in (cfg.get("reddit") or {}).get("feeds") or []:
        out.append({"key": f"reddit:{f['subreddit']}", "kind": "reddit", "name": f"r/{f['subreddit']}",
                    "url": f"https://www.reddit.com/r/{f['subreddit']}/", "config": {k: v for k, v in f.items() if k != "subreddit"}})
    for r in (cfg.get("rsshub") or {}).get("routes") or []:
        out.append({"key": f"rsshub:{r['id']}", "kind": "rsshub", "name": r.get("label") or r["id"], "url": None,
                    "config": {k: v for k, v in r.items() if k not in ("id", "label")}})
    x = cfg.get("x") or {}
    for q in x.get("queries") or []:
        out.append({"key": f"xq:{q['id']}", "kind": "x_query", "name": q.get("label") or q["id"], "url": None,
                    "config": {k: v for k, v in q.items() if k not in ("id", "label")}})
    for a in x.get("accounts") or []:
        a = {"handle": a, "enabled": True} if isinstance(a, str) else a
        h = str(a["handle"]).lstrip("@")
        out.append({"key": f"xa:{h}", "kind": "x_account", "name": f"@{h}", "url": f"https://x.com/{h}",
                    "enabled": bool(a.get("enabled", True)), "config": {k: v for k, v in a.items() if k not in ("handle", "enabled")}})
    for name in ("hn", "github_search", "hf_trending", "ossinsight"):
        if name in cfg:
            out.append({"key": f"plugin:{name}", "kind": "plugin", "name": name, "url": None, "config": cfg[name]})
    # source-level defaults for X live on its sources' config; keep the shared X settings too
    if x:
        shared = {k: v for k, v in x.items() if k not in ("queries", "accounts") and not k.startswith("_")}
        for s in out:
            if s["kind"] in ("x_query", "x_account"):
                s["config"] = {**{"x": shared}, **s["config"]}
    return out


def import_config(db, sources_path: Path | None = None, tags_path: Path | None = None, template: str = "default") -> dict:
    """Idempotent upsert. Returns counts per kind plus tag terms."""
    cfg = json.loads(Path(sources_path or ROOT / "config/collect-sources.json").read_text(encoding="utf-8"))
    tags = json.loads(Path(tags_path or ROOT / "config/tags.json").read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    tpl = db.scalar(select(SubscriptionTemplate).where(SubscriptionTemplate.name == template))
    if tpl is None:
        tpl = SubscriptionTemplate(name=template, description="Imported from config/collect-sources.json + tags.json")
        db.add(tpl); db.flush()
    for s in flatten_sources(cfg):
        row = db.scalar(select(Source).where(Source.key == s["key"]))
        if row is None:
            row = Source(key=s["key"]); db.add(row)
        row.kind, row.name, row.url, row.config = s["kind"], s["name"], s["url"], s["config"]
        row.enabled = s.get("enabled", True)
        db.flush()
        if row.enabled and not db.scalar(select(SubscriptionTemplateItem).where(
                SubscriptionTemplateItem.template_id == tpl.id, SubscriptionTemplateItem.source_id == row.id)):
            db.add(SubscriptionTemplateItem(template_id=tpl.id, source_id=row.id))
        counts[s["kind"]] = counts.get(s["kind"], 0) + 1
    n_terms = 0
    interest = []
    for tag, terms in (tags.get("tags") or {}).items():
        for t in terms:
            row = db.scalar(select(TagTerm).where(TagTerm.tag == tag, TagTerm.term == t["term"]))
            if row is None:
                row = TagTerm(tag=tag, term=t["term"]); db.add(row)
            row.aliases = t.get("aliases") or []
            row.broad = bool(t.get("broad", False))
            row.extra = {k: v for k, v in t.items() if k not in ("term", "aliases", "broad")} or None
            n_terms += 1
            interest.append({"kind": "interest", "term": t["term"], "weight": 1.0})
    tpl.keywords = interest
    if "model_release_patterns" in tags:
        g = db.get(GlobalSetting, "model_release_patterns") or GlobalSetting(key="model_release_patterns")
        g.value = tags["model_release_patterns"]; db.add(g)
    db.commit()
    counts["tag_terms"] = n_terms
    counts["sources"] = sum(v for k, v in counts.items() if k != "tag_terms")
    return counts
