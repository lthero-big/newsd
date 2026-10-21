"""M3 HTML pages, rendered per logged-in user: daily report, calendar, favorites, trash, settings, onboarding, admin.
Every query is scoped to ``user.id``; there is no way to name another user in a URL."""
from __future__ import annotations

import calendar as _cal
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from .admin import admin_runs, source_rows
from .auth import current_user, get_db
from .issues import issue_view
from .llm import keys as llm_keys
from .llm.client import KEYLESS, PROVIDER_LABELS, PROVIDERS, base_url
from .llm.profiles import all_profiles, get_picks
from .marks import marked_items
from .models import Invite, Issue, Keyword, Quota, Source, Subscription, User
from .onboarding import is_onboarded, preset_groups
from .registration import get_registration_mode
from .settings import get_settings
from .push.prefs import WEBHOOK_TYPES, get_push
from .userprefs import PALETTES, THEME_COOKIE, get_palette

templates = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))
router = APIRouter()
MODULES = (("model", "模型"), ("opensource", "开源"), ("community", "社区"), ("news", "资讯"))
KIND_LABELS = {"rss": "RSS", "reddit": "Reddit", "rsshub": "RSSHub", "x_query": "X 搜索", "x_account": "X 账号", "plugin": "聚合源"}


def _safe_url(url) -> str:
    """Only http(s) links are rendered as hrefs (feed data is untrusted)."""
    u = (url or "").strip()
    return u if urlparse(u).scheme in ("http", "https") else ""


templates.env.filters["safe_url"] = _safe_url


def render(request: Request, name: str, user, db, status_code: int = 200, **ctx):
    palette = get_palette(db, user.id) if user is not None else request.cookies.get(THEME_COOKIE)
    return templates.TemplateResponse(request, name, {"user": user, "palette": palette if palette in PALETTES else None, **ctx},
                                      status_code=status_code)


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


def _issue_page(request, user, db, key: str | None):
    q = select(Issue).where(Issue.user_id == user.id)
    issue = db.scalar(q.order_by(Issue.key.desc()).limit(1) if key is None else q.where(Issue.key == key))
    keys = list(db.scalars(select(Issue.key).where(Issue.user_id == user.id).order_by(Issue.key)))
    view = groups = prev_key = next_key = None
    trashed = 0
    if issue is not None:
        view = issue_view(db, issue)
        shown = [i for i in view["items"] if i["state"] != "trash"]
        trashed = len(view["items"]) - len(shown)
        view["items"] = shown
        groups = [(m, label, [i for i in shown if i["module"] == m]) for m, label in MODULES]
        groups += [(m, m, [i for i in shown if i["module"] == m]) for m in dict.fromkeys(i["module"] for i in shown)
                   if m not in dict(MODULES)]
        groups = [g for g in groups if g[2]]
        at = keys.index(issue.key)
        prev_key = keys[at - 1] if at > 0 else None
        next_key = keys[at + 1] if at + 1 < len(keys) else None
    return render(request, "issue.html", user, db, status_code=200 if issue or key is None else 404, nav="home", page="issue",
                  issue=view, key=key, groups=groups, prev_key=prev_key, next_key=next_key, trashed=trashed)


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
    return render(request, "calendar.html", user, db, nav="calendar", page="calendar", month=f"{y:04d}-{m:02d}", cells=cells,
                  issues=issues, others=others, prev_month=f"{pm[0]:04d}-{pm[1]:02d}", next_month=f"{nm[0]:04d}-{nm[1]:02d}")


@router.get("/favorites", response_class=HTMLResponse)
def favorites_page(request: Request, user=Depends(current_user), db=Depends(get_db)):
    if user is None:
        return login_redirect()
    return render(request, "marks.html", user, db, nav="favorites", page="favorites", title="收藏",
                  rows=marked_items(db, user.id, "favorite"), hint="你在日报里收藏的内容。", empty="还没有收藏。在日报里点“☆ 收藏”。")


@router.get("/trash", response_class=HTMLResponse)
def trash_page(request: Request, user=Depends(current_user), db=Depends(get_db)):
    if user is None:
        return login_redirect()
    return render(request, "marks.html", user, db, nav="trash", page="trash", title="回收站",
                  rows=marked_items(db, user.id, "trash"), hint="从日报里移除的内容，点“恢复”放回去。", empty="回收站是空的。")


@router.get("/welcome", response_class=HTMLResponse)
def welcome_page(request: Request, user=Depends(current_user), db=Depends(get_db)):
    if user is None:
        return login_redirect()
    return render(request, "welcome.html", user, db, nav="", page="welcome", groups=preset_groups(db))


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user=Depends(current_user), db=Depends(get_db)):
    if user is None:
        return login_redirect()
    providers = [{"provider": p, "label": PROVIDER_LABELS[p], "available": bool(base_url(p)), "keyless": p in KEYLESS}
                 for p in PROVIDERS]
    picked = {p["name"]: p["threshold"] for p in get_picks(db, user.id)}
    subscribed = set(db.scalars(select(Subscription.source_id).where(Subscription.user_id == user.id, Subscription.enabled.is_(True))))
    by_kind: dict[str, list] = {}
    for s in db.scalars(select(Source).where(Source.enabled.is_(True)).order_by(Source.kind, Source.key)):
        by_kind.setdefault(KIND_LABELS.get(s.kind, s.kind), []).append({"key": s.key, "name": s.name, "subscribed": s.id in subscribed})
    kws: dict[str, list] = {"interest": [], "boost": [], "block": []}
    for k in db.scalars(select(Keyword).where(Keyword.user_id == user.id).order_by(Keyword.term)):
        kws.setdefault(k.kind, []).append({"id": k.id, "term": k.term, "weight": k.weight})
    quotas = [{"module": q.module, "group_name": q.group_name, "max_items": q.max_items, "categories": q.categories or []}
              for q in db.scalars(select(Quota).where(Quota.user_id == user.id).order_by(Quota.module, Quota.group_name))]
    return render(request, "settings.html", user, db, nav="settings", page="settings", providers=providers,
                  llm=llm_keys.public_settings(db, user.id), langs=[("zh", "简体中文"), ("en", "English"), ("ja", "日本語")],
                  profiles=list(all_profiles().values()), picked=picked, palettes=PALETTES, sources=list(by_kind.items()),
                  keywords=kws, quotas=quotas, push=get_push(db, user.id), webhook_types=WEBHOOK_TYPES,
                  smtp_ready=bool(get_settings().smtp_host))


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, user=Depends(current_user), db=Depends(get_db)):
    if user is None:
        return login_redirect()
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    from .admin import admin_invites, admin_users
    from .admin_runs import recent_runs
    from .push.api import recent_deliveries
    from .worker import run_view
    return render(request, "admin.html", user, db, nav="admin", page="admin", mode=get_registration_mode(db),
                  invites=admin_invites(user, db), users=admin_users(user, db), sources=source_rows(db, 30), days=30,
                  runs=admin_runs(30, user, db), worker_runs=[run_view(r) for r in recent_runs(db, 20)],
                  push_deliveries=recent_deliveries(db, 50))
