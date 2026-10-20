"""FastAPI app: M1 covers accounts (login/logout/register by invite), API tokens and the admin user list.
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
from .security import hash_password, new_token, token_hash
from .settings import get_settings

templates = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))


def create_app() -> FastAPI:
    init_db()
    app = FastAPI(title="newsd", version=__version__)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, user=Depends(current_user)):
        if user is None:
            return RedirectResponse("/login", 303)
        return templates.TemplateResponse(request, "home.html", {"user": user})

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return templates.TemplateResponse(request, "login.html", {"error": None, "registration": get_settings().registration})

    @app.post("/login")
    def login(request: Request, username: str = Form(...), password: str = Form(...), db=Depends(get_db)):
        user = authenticate(db, client_ip(request), username, password)
        if not user:
            return templates.TemplateResponse(request, "login.html", {"error": "用户名或密码错误", "registration": get_settings().registration}, status_code=401)
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

    @app.post("/api/register")
    def register(username: str = Form(...), password: str = Form(...), invite: str = Form(""), db=Depends(get_db)):
        mode = get_settings().registration
        if mode == "closed":
            raise HTTPException(403, "registration closed")
        inv = None
        if mode == "invite":
            inv = db.scalar(select(Invite).where(Invite.code_hash == token_hash(invite), Invite.used_by.is_(None)))
            if not inv or (inv.expires_at and inv.expires_at < now()):
                raise HTTPException(403, "invalid invite")
        if len(username) < 2 or len(password) < 8:
            raise HTTPException(400, "username >= 2 chars, password >= 8 chars")
        if db.scalar(select(User).where(User.username == username)):
            raise HTTPException(409, "username taken")
        u = User(username=username, password_hash=hash_password(password))
        db.add(u)
        db.flush()
        if inv:
            inv.used_by = u.id
        db.commit()
        return {"id": u.id, "username": u.username}

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
