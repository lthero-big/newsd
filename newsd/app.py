"""FastAPI app: M1 covers accounts (login/logout/register by invite or open sign-up), API tokens and the admin user list.
Edition pages, settings and pipeline endpoints arrive in M2/M3."""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select

from . import __version__
from .auth import COOKIE, authenticate, client_ip, create_session, current_user, get_db, require_admin, require_user
from .db import init_db
from .models import ApiToken, Invite, User, WebSession, now
from .registration import REG_MODES, get_registration_mode, register_user, set_registration_mode
from .security import hash_password, new_token, token_hash
from .settings import get_settings

templates = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))


def create_app() -> FastAPI:
    init_db()
    app = FastAPI(title="newsd", version=__version__)
    from .prefs import router as prefs_router
    app.include_router(prefs_router)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, user=Depends(current_user)):
        if user is None:
            return RedirectResponse("/login", 303)
        return templates.TemplateResponse(request, "home.html", {"user": user})

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, db=Depends(get_db)):
        return templates.TemplateResponse(request, "login.html", {"error": None, "registration": get_registration_mode(db)})

    @app.post("/login")
    def login(request: Request, username: str = Form(...), password: str = Form(...), db=Depends(get_db)):
        user = authenticate(db, client_ip(request), username, password)
        if not user:
            return templates.TemplateResponse(request, "login.html", {"error": "用户名或密码错误", "registration": get_registration_mode(db)}, status_code=401)
        resp = RedirectResponse("/", 303)
        resp.set_cookie(COOKIE, create_session(db, user), httponly=True, samesite="lax",
                        secure=get_settings().cookie_secure, max_age=get_settings().session_days * 86400)
        return resp

    @app.post("/logout")
    def logout(request: Request, db=Depends(get_db)):
        c = request.cookies.get(COOKIE)
        if c:
            db.execute(delete(WebSession).where(WebSession.token_hash == token_hash(c)))
            db.commit()
        resp = RedirectResponse("/login", 303)
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/register", response_class=HTMLResponse)
    def register_page(request: Request, db=Depends(get_db)):
        return templates.TemplateResponse(request, "register.html", {"error": None, "registration": get_registration_mode(db)})

    @app.post("/register")
    def register_form(request: Request, username: str = Form(...), password: str = Form(...), invite: str = Form(""), db=Depends(get_db)):
        try:
            register_user(db, username, password, invite)
        except HTTPException as e:
            msg = {"registration closed": "注册已关闭", "invalid invite": "邀请码无效或已用过",
                   "username taken": "用户名已被占用"}.get(e.detail, "用户名至少 2 个字符，密码至少 8 个字符")
            return templates.TemplateResponse(request, "register.html", {"error": msg, "registration": get_registration_mode(db)}, status_code=e.status_code)
        return RedirectResponse("/login?registered=1", 303)

    @app.post("/api/register")
    def register(username: str = Form(...), password: str = Form(...), invite: str = Form(""), db=Depends(get_db)):
        u = register_user(db, username, password, invite)
        return {"id": u.id, "username": u.username}

    @app.get("/api/registration")
    def registration_info(db=Depends(get_db)):
        return {"mode": get_registration_mode(db)}

    @app.get("/api/me")
    def me(user=Depends(require_user)):
        return {"id": user.id, "username": user.username, "role": user.role}

    @app.post("/api/tokens")
    def create_token(kind: str = Form("api"), name: str = Form(""), user=Depends(require_user), db=Depends(get_db)):
        if kind not in ("api", "mcp"):
            raise HTTPException(400, "kind must be api or mcp")
        tok = new_token("nd" if kind == "api" else "nm")
        db.add(ApiToken(user_id=user.id, kind=kind, name=name, token_hash=token_hash(tok), last4=tok[-4:]))
        db.commit()
        return {"token": tok, "kind": kind, "note": "shown once; only a hash is stored"}

    @app.get("/api/tokens")
    def list_tokens(user=Depends(require_user), db=Depends(get_db)):
        rows = db.scalars(select(ApiToken).where(ApiToken.user_id == user.id)).all()
        return [{"id": t.id, "kind": t.kind, "name": t.name, "last4": t.last4} for t in rows]

    @app.get("/api/admin/users")
    def admin_users(_=Depends(require_admin), db=Depends(get_db)):
        return [{"id": u.id, "username": u.username, "role": u.role, "active": u.active}
                for u in db.scalars(select(User).order_by(User.id)).all()]

    @app.post("/api/admin/invites")
    def admin_invite(admin=Depends(require_admin), db=Depends(get_db)):
        code = new_token("inv")
        db.add(Invite(code_hash=token_hash(code), created_by=admin.id))
        db.commit()
        return {"invite": code}

    @app.post("/api/admin/registration")
    def admin_registration(mode: str = Form(...), _=Depends(require_admin), db=Depends(get_db)):
        if mode not in REG_MODES:
            raise HTTPException(400, "mode must be one of: " + ", ".join(REG_MODES))
        set_registration_mode(db, mode)
        return {"mode": mode}

    @app.post("/api/admin/users/{uid}/active")
    def admin_set_active(uid: int, active: bool = Form(...), _=Depends(require_admin), db=Depends(get_db)):
        u = db.get(User, uid)
        if not u:
            raise HTTPException(404, "no such user")
        u.active = active
        db.commit()
        return {"id": u.id, "active": u.active}

    @app.exception_handler(HTTPException)
    def http_exc(request: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app
