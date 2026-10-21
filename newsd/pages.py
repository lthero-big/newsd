"""M3 HTML pages, rendered per logged-in user: daily report, calendar, favorites, trash, settings, onboarding, admin.
Every query is scoped to ``user.id``; there is no way to name another user in a URL."""
from __future__ import annotations

import calendar as _cal
import re
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from .auth import current_user, get_db
from .feedback import FEATURE_W, HALF_LIFE_DAYS, TRASH_DAYS, W_DELETE, W_DISLIKE, learn_settings, trash_items
from .issues import issue_view, item_cards
from .llm import keys as llm_keys
from .llm.client import base_url
from .llm.presets import REGIONS, VISIBLE
from .llm.profiles import all_profiles, get_picks
from .marks import trash_count
from .models import Issue, IssueItem, ItemFeedback, ItemMark, PoolItem, Keyword, Quota, Source, Subscription
from .onboarding import is_onboarded, preset_groups
from .registration import get_registration_mode
from .settings import get_settings
from .push.prefs import WEBHOOK_TYPES, get_push
from .zones import view as zone_view
from .userprefs import DEFAULT_PALETTE, PALETTES, THEME_COOKIE, THEMES, get_palette, get_ui, normalize_theme

templates = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))
router = APIRouter()
MODULES = (("model", "模型新闻", "新模型、版本发布与榜单"), ("opensource", "开源项目发现", "新仓库、热门模型权重与工具发布"),
           ("community", "社区新闻", "HN、Reddit 与 X 上的讨论"), ("news", "资讯", "媒体与博客报道"))
MODULE_NAMES = {m: label for m, label, _sub in MODULES}
READ_NAV = (("/", "日报", "issue", "◐"), ("/favorites", "收藏", "favorites", "♡"), ("/calendar", "归档", "calendar", "▦"))
# (group, [(href, label, page key)], admin only). 3b: 订阅源 / 模型 have their own pages; 3c splits 管理.
CONSOLE_NAV = (("个人", (("/sources", "订阅源", "sources"), ("/models", "模型", "models"), ("/settings", "偏好与主题", "settings"),
                         ("/settings#learn", "学到的偏好", "learn"), ("/settings#more", "关键词、配额与推送", "more"),
                         ("/settings#acct", "账号", "acct")), False),
               ("管理", (("/admin", "概览", "admin"), ("/admin/users", "用户与分组", "users"), ("/admin/health", "源健康", "health"),
                        ("/admin/logs", "日志与任务", "logs")), True))
LOGS_NAV = ("管理", (("/admin/logs", "日志与任务", "logs"),), False)  # 3c: a member granted the logs permission
WEEK = "一二三四五六日"
_STATIC = Path(__file__).parent / "web" / "static" / "v2"
ASSET_V = format(int(max((f.stat().st_mtime for f in _STATIC.iterdir()), default=0)), "x")
KIND_LABELS = {"rss": "RSS", "reddit": "Reddit", "rsshub": "RSSHub", "x_query": "X 搜索", "x_account": "X 账号", "plugin": "聚合源"}


def _safe_url(url) -> str:
    """Only http(s) links are rendered as hrefs (feed data is untrusted)."""
    u = (url or "").strip()
    return u if urlparse(u).scheme in ("http", "https") else ""


templates.env.filters["safe_url"] = _safe_url


def render(request: Request, name: str, user, db, status_code: int = 200, **ctx):
    if user is not None:
        theme, ui, trash_n = get_palette(db, user.id), get_ui(db, user.id), trash_count(db, user.id)
    else:
        theme, ui, trash_n = normalize_theme(request.cookies.get(THEME_COOKIE)) or DEFAULT_PALETTE, {"feedback": "both", "motion": "on"}, 0
    nav = CONSOLE_NAV
    if user is not None and user.role != "admin":
        from .perms import has
        nav = CONSOLE_NAV + ((LOGS_NAV,) if has(db, user, "logs") else ())
    base = {"user": user, "palette": theme, "theme": theme, "themes": THEMES, "ui": ui, "trash_n": trash_n, "read_nav": READ_NAV,
            "console_nav": nav, "asset_v": ASSET_V, "modules": MODULES}
    return templates.TemplateResponse(request, name, base | ctx, status_code=status_code)


def login_redirect():
    return RedirectResponse("/login", 303)


@router.get("/", response_class=HTMLResponse)
def index(request: Request, user=Depends(current_user), db=Depends(get_db)):
    if user is None:
        return login_redirect()
    if not is_onboarded(db, user.id):
        return RedirectResponse("/welcome", 303)
    return _issue_page(request, user, db, None)


@router.get("/issue/{key}", response_class=HTMLResponse)
def issue_page(key: str, request: Request, user=Depends(current_user), db=Depends(get_db)):
    if user is None:
        return login_redirect()
    return _issue_page(request, user, db, key)


def _day_counts(db, user_id: int) -> dict[str, int]:
    return {k: (stats or {}).get("items_out", 0) for k, stats in db.execute(select(Issue.key, Issue.stats).where(Issue.user_id == user_id))
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", k)}


def _issue_page(request, user, db, key: str | None):
    q = select(Issue).where(Issue.user_id == user.id)
    issue = db.scalar(q.order_by(Issue.key.desc()).limit(1) if key is None else q.where(Issue.key == key))
    keys = list(db.scalars(select(Issue.key).where(Issue.user_id == user.id).order_by(Issue.key)))
    view = groups = prev_key = next_key = feature = headline = None
    trashed = n_down = n_sources = 0
    kicker, date_parts, tags = "", None, []
    if issue is not None:
        view = issue_view(db, issue)
        shown = [i for i in view["items"] if i["state"] not in ("trash", "purged")]
        trashed = sum(1 for i in view["items"] if i["state"] == "trash")
        view["items"] = shown
        mods = list(MODULES) + [(m, m, "") for m in dict.fromkeys(i["module"] for i in shown) if m not in MODULE_NAMES]
        groups = [(m, label, sub, [i for i in shown if i["module"] == m]) for m, label, sub in mods]
        groups = [g for g in groups if g[3]]
        at = keys.index(issue.key)
        prev_key = keys[at - 1] if at > 0 else None
        next_key = keys[at + 1] if at + 1 < len(keys) else None
        n_sources = len({s for i in shown for s in i["sources"][:1]})
        n_down = sum(1 for i in shown if i["ranked_down"])
        # headline / feature card: prefer real headlines over bare release tags like "b11787"
        good = [i for i in shown if not i["ranked_down"] and len(i["title_translated"] or i["title"]) >= 16] or shown
        headline = good[0] if good else None
        feature = next((i for i in good if i is not headline and (i["summary_translated"] or i["summary"])),
                       next((i for i in good if i is not headline), None))
        tags = Counter(i["group"] for i in shown if i.get("group")).most_common(8)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", issue.key):
            d = date.fromisoformat(issue.key)
            date_parts = [issue.key[:4], issue.key[5:7], issue.key[8:]]
            when = "今天" if d == date.today() else "周" + WEEK[d.weekday()]
        else:
            when = issue.key
        kicker = f"{when} · {len(shown)} 条 · {n_sources} 个来源" + (f" · AI：{view['model']}" if view.get("model") else " · 规则排序")
    return render(request, "issue.html", user, db, status_code=200 if issue or key is None else 404, area="read", page="issue",
                  issue=view, key=key or (issue.key if issue else None), groups=groups, prev_key=prev_key, next_key=next_key,
                  trashed=trashed, n_down=n_down, n_sources=n_sources, feature=feature, headline=headline, tags=tags, kicker=kicker,
                  date_parts=date_parts, day_counts=_day_counts(db, user.id))


@router.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, month: str | None = None, user=Depends(current_user), db=Depends(get_db)):
    if user is None:
        return login_redirect()
    today = date.today()
    if month and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        y, m = int(month[:4]), int(month[5:])
    else:
        y, m = today.year, today.month
    issues = {k: (stats or {}).get("items_out", 0) for k, stats in
              db.execute(select(Issue.key, Issue.stats).where(Issue.user_id == user.id))}
    cells = [None] * date(y, m, 1).weekday()
    cells += [{"n": d, "key": f"{y:04d}-{m:02d}-{d:02d}"} for d in range(1, _cal.monthrange(y, m)[1] + 1)]
    pm, nm = (y, m - 1) if m > 1 else (y - 1, 12), (y, m + 1) if m < 12 else (y + 1, 1)
    in_month = {c["key"] for c in cells if c}
    others = sorted(((k, n) for k, n in issues.items() if k not in in_month and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", k)),
                    reverse=True)
    marked = {k for k, in db.execute(
        select(Issue.key).join(IssueItem, IssueItem.issue_id == Issue.id).where(Issue.user_id == user.id).where(
            IssueItem.item_id.in_(select(ItemMark.item_id).where(ItemMark.user_id == user.id, ItemMark.state == "favorite"))
            | IssueItem.item_id.in_(select(ItemFeedback.item_id).where(ItemFeedback.user_id == user.id, ItemFeedback.vote == 1))).distinct())}
    heads = {k: t for k, t in db.execute(select(Issue.key, func.coalesce(IssueItem.title_translated, PoolItem.title))
                                         .join(IssueItem, IssueItem.issue_id == Issue.id).join(PoolItem, PoolItem.id == IssueItem.item_id)
                                         .where(Issue.user_id == user.id, IssueItem.rank == 0))}
    listing = [(k, n, heads.get(k)) for k, n in sorted(issues.items(), reverse=True)[:120]]
    in_month_keys = sorted((k for k in in_month if k in issues), reverse=True)
    sel = today.isoformat() if today.isoformat() in issues and today.isoformat() in in_month else (in_month_keys[0] if in_month_keys else None)
    return render(request, "calendar.html", user, db, area="read", page="calendar", month=f"{y:04d}-{m:02d}", cells=cells,
                  issues=issues, others=others, prev_month=f"{pm[0]:04d}-{pm[1]:02d}", next_month=f"{nm[0]:04d}-{nm[1]:02d}",
                  today=today.isoformat(), sel=sel, marked=marked, listing=listing)


@router.get("/favorites", response_class=HTMLResponse)
def favorites_page(request: Request, user=Depends(current_user), db=Depends(get_db)):
    if user is None:
        return login_redirect()
    favs = {i: at for i, at in db.execute(select(ItemMark.item_id, ItemMark.created_at)
                                          .where(ItemMark.user_id == user.id, ItemMark.state == "favorite")
                                          .order_by(ItemMark.created_at.desc(), ItemMark.id.desc()))}
    hidden = set(db.scalars(select(ItemMark.item_id).where(ItemMark.user_id == user.id, ItemMark.state.in_(("trash", "purged")))))
    liked = [i for i in db.scalars(select(ItemFeedback.item_id).where(ItemFeedback.user_id == user.id, ItemFeedback.vote == 1)
                                   .order_by(ItemFeedback.updated_at.desc())) if i not in favs and i not in hidden]
    rows = item_cards(db, user.id, list(favs) + liked)
    for r in rows:
        at = favs.get(r["item_id"])
        r["marked_at"] = at.isoformat() if at else None
    tags = sorted({t for r in rows for t in ([r["group"]] if r.get("group") else []) + r["matched"][:3]})
    return render(request, "favorites.html", user, db, area="read", page="favorites", rows=rows, tags=tags[:40],
                  favorites_n=sum(1 for r in rows if r["state"] == "favorite"))


@router.get("/trash", response_class=HTMLResponse)
def trash_page(request: Request, user=Depends(current_user), db=Depends(get_db)):
    if user is None:
        return login_redirect()
    rows = trash_items(db, user.id)
    cards = {c["item_id"]: c for c in item_cards(db, user.id, [r["item_id"] for r in rows])}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for r in rows:
        c = cards.get(r["item_id"], {})
        r["source"] = (c.get("sources") or [None])[0]
        r["source_cat"], r["key"] = c.get("source_cat", "rss"), c.get("key")
        left = datetime.fromisoformat(r["expires_at"]) - now
        r["days_left"] = max(0, left.days + (1 if left.seconds else 0))
    return render(request, "trash.html", user, db, area="read", page="trash", rows=rows, trash_days=TRASH_DAYS)


@router.get("/welcome", response_class=HTMLResponse)
def welcome_page(request: Request, user=Depends(current_user), db=Depends(get_db)):
    if user is None:
        return login_redirect()
    return render(request, "welcome.html", user, db, page="welcome", groups=preset_groups(db))


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user=Depends(current_user), db=Depends(get_db)):
    if user is None:
        return login_redirect()
    kws: dict[str, list] = {"interest": [], "boost": [], "block": []}
    for k in db.scalars(select(Keyword).where(Keyword.user_id == user.id).order_by(Keyword.term)):
        kws.setdefault(k.kind, []).append({"id": k.id, "term": k.term, "weight": k.weight})
    quotas = [{"module": q.module, "group_name": q.group_name, "max_items": q.max_items, "categories": q.categories or []}
              for q in db.scalars(select(Quota).where(Quota.user_id == user.id).order_by(Quota.module, Quota.group_name))]
    learn = learn_settings(db, user.id)
    weights = {"delete": W_DELETE, "dislike": W_DISLIKE, **FEATURE_W, "half_life": HALF_LIFE_DAYS}
    return render(request, "settings.html", user, db, area="console", page="settings", learn=learn, w=weights, palettes=PALETTES,
                  keywords=kws, quotas=quotas, push=get_push(db, user.id), webhook_types=WEBHOOK_TYPES,
                  smtp_ready=bool(get_settings().smtp_host))


@router.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, user=Depends(current_user), db=Depends(get_db)):
    """订阅源 (DEPTAI-216 3b): categories, zones and the sources in them; the page script talks to /api/me/zones."""
    if user is None:
        return login_redirect()
    return render(request, "sources.html", user, db, area="console", page="sources", zones=zone_view(db, user.id),
                  n_subs=db.scalar(select(func.count()).select_from(Subscription).where(Subscription.user_id == user.id)) or 0)


@router.get("/models", response_class=HTMLResponse)
def models_page(request: Request, user=Depends(current_user), db=Depends(get_db)):
    """模型 (DEPTAI-216 3b): providers in failover order, options and the AI content categories."""
    if user is None:
        return login_redirect()
    llm_presets = [{**p.public(), "base_url": base_url(p.id)} for p in VISIBLE]
    picked = {p["name"]: p["threshold"] for p in get_picks(db, user.id)}
    return render(request, "models.html", user, db, area="console", page="models", llm_presets=llm_presets,
                  llm_regions=REGIONS, llm_view=llm_keys.overview(db, user.id), langs=[("zh", "简体中文"), ("en", "English"), ("ja", "日本語")],
                  profiles=list(all_profiles().values()), picked=picked)


def _admin_or_403(user):
    if user.role != "admin":
        raise HTTPException(403, "admin only")


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, user=Depends(current_user), db=Depends(get_db)):
    """3c 管理概览: summary numbers only (users → 用户与分组, sources → 源健康, runs → 日志与任务)."""
    if user is None:
        return login_redirect()
    _admin_or_403(user)
    from .admin_console import overview
    return render(request, "admin.html", user, db, area="console", page="admin", ov=overview(user, db))


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, user=Depends(current_user), db=Depends(get_db)):
    if user is None:
        return login_redirect()
    _admin_or_403(user)
    from .admin_console import list_groups, people
    from .zones import CATS, category
    catalog = [{"key": s.key, "name": s.name or s.key, "cat": category(s.key, s.kind)}
               for s in db.scalars(select(Source).where(Source.enabled.is_(True)).order_by(Source.key))]
    return render(request, "admin_users.html", user, db, area="console", page="users", mode=get_registration_mode(db),
                  init={"people": people(user, db), "groups": list_groups(user, db), "me": user.id, "catalog": catalog,
                        "cats": {k: v[0] for k, v in CATS.items()}, "mode": get_registration_mode(db)})


@router.get("/admin/health", response_class=HTMLResponse)
def admin_health_page(request: Request, user=Depends(current_user), db=Depends(get_db)):
    if user is None:
        return login_redirect()
    _admin_or_403(user)
    from .admin_console import health
    return render(request, "admin_health.html", user, db, area="console", page="health", init=health(user, db))


@router.get("/admin/logs", response_class=HTMLResponse)
def admin_logs_page(request: Request, user=Depends(current_user), db=Depends(get_db)):
    """Admins, and members whose group grants ``logs`` (read-only: no rerun / no source actions)."""
    if user is None:
        return login_redirect()
    from .perms import has
    if not has(db, user, "logs"):
        raise HTTPException(403, "permission required: logs")
    from .admin_console import timeline
    admin = user.role == "admin"
    push = []
    if admin:
        from .push.api import recent_deliveries
        push = recent_deliveries(db, 50)
    return render(request, "admin_logs.html", user, db, area="console", page="logs",
                  init={"timeline": timeline(12, user, db), "admin": admin, "push": push})
