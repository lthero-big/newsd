from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select

ROLES = ("user", "admin", "readonly")  # 3c: readonly = can read and set up push, not change sources / models


def _read_password(args) -> str:
    if getattr(args, "password_file", None):
        with open(args.password_file, encoding="utf-8") as f:
            return f.readline().strip()
    return args.password or getpass.getpass("password: ")


def create_user(db, username: str, password: str, role: str = "user"):
    """Create a local account. Raises ValueError with a printable message."""
    from .models import User
    from .security import hash_password
    if role not in ROLES:
        raise ValueError("role must be one of: " + ", ".join(ROLES))
    if db.scalar(select(User).where(User.username == username)):
        raise ValueError("user exists")
    if len(password) < 8:
        raise ValueError("password must be >= 8 chars")
    u = User(username=username, password_hash=hash_password(password), role=role)
    db.add(u)
    db.commit()
    return u


def set_role(db, username: str, role: str):
    """Change a user's role; refuses to demote the last active admin."""
    from .models import User
    if role not in ROLES:
        raise ValueError("role must be one of: " + ", ".join(ROLES))
    u = db.scalar(select(User).where(User.username == username))
    if u is None:
        raise ValueError("no such user")
    if u.role == "admin" and role != "admin":
        others = db.scalars(select(User).where(User.role == "admin", User.active.is_(True), User.id != u.id)).all()
        if not others:
            raise ValueError("refusing to demote the last active admin")
    u.role = role
    db.commit()
    return u


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="newsd")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the web server")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    sub.add_parser("initdb", help="create tables")
    upd = sub.add_parser("updater", help="docker update sidecar (needs /var/run/docker.sock; see docs/updates.md)")
    upd.add_argument("--host", default="0.0.0.0")
    upd.add_argument("--port", type=int, default=8090)
    sub.add_parser("version", help="print the version and the latest release")
    d = sub.add_parser("db", help="schema migrations and catalog import")
    dsub = d.add_subparsers(dest="dcmd", required=True)
    dsub.add_parser("upgrade", help="alembic upgrade head")
    im = dsub.add_parser("import-config", help="import config/collect-sources.json + tags.json into the catalog")
    im.add_argument("--sources"); im.add_argument("--tags")
    pl = sub.add_parser("pool", help="shared collection pool")
    plsub = pl.add_subparsers(dest="pcmd", required=True)
    pr = plsub.add_parser("run", help="fetch every subscribed source once for this cycle, then extract full text")
    pr.add_argument("--cycle", help="cycle id (default: current UTC hour, e.g. 2026-10-20T09:00)")
    pr.add_argument("--period", type=int, default=60, help="cycle length in minutes when --cycle is omitted")
    pr.add_argument("--kinds", help="comma-separated source kinds to include (rss,reddit,rsshub,x_account,x_query,plugin)")
    pr.add_argument("--no-extract", action="store_true", help="skip full-text extraction")
    pr.add_argument("--lookback-hours", type=float, default=26, help="only items published this recently get full-text extraction")
    pc = sub.add_parser("process", help="per-user model processing (profiles, AI re-rank, dedup, translation)")
    pcsub = pc.add_subparsers(dest="xcmd", required=True)
    px = pcsub.add_parser("run", help="rank one user's pool items; without a model key only rule ranking runs")
    px.add_argument("username")
    px.add_argument("--since-hours", type=float, default=26)
    px.add_argument("--max-items", type=int)
    px.add_argument("--no-ai", action="store_true", help="skip every model call (rule ranking + cached translations)")
    px.add_argument("--no-rerank", action="store_true"); px.add_argument("--no-dedupe", action="store_true")
    px.add_argument("--no-translate", action="store_true")
    iss = sub.add_parser("issue", help="per-user issues: rule scoring, dedup, quotas (+ model layer with a key)")
    isub = iss.add_subparsers(dest="icmd", required=True)
    ib = isub.add_parser("build", help="build and store one user's issue (or --all users)")
    ib.add_argument("username", nargs="?")
    ib.add_argument("--all", action="store_true")
    ib.add_argument("--key", help="issue key (default: today's UTC date)")
    ib.add_argument("--since-hours", type=float, default=26)
    ib.add_argument("--no-ai", action="store_true", help="rules only, no model calls")
    ish = isub.add_parser("show", help="print a stored issue as JSON")
    ish.add_argument("username"); ish.add_argument("--key", default="latest")
    w = sub.add_parser("worker", help="standalone scheduler: collect once, then build every user's issue")
    wsub = w.add_subparsers(dest="wcmd", required=True)
    wsub.add_parser("start", help="run forever; fires at NEWSD_WORKER_TIMES in NEWSD_WORKER_TZ")
    wr = wsub.add_parser("run-once", help="run one pipeline tick now and record it")
    wr.add_argument("--cycle"); wr.add_argument("--key", help="issue key (default: today in NEWSD_WORKER_TZ)")
    wr.add_argument("--user", action="append", help="only this user (repeatable)")
    wr.add_argument("--no-pool", action="store_true", help="skip collection, build issues from the existing pool")
    wr.add_argument("--no-extract", action="store_true"); wr.add_argument("--no-ai", action="store_true")
    wr.add_argument("--since-hours", type=float)
    wl = wsub.add_parser("runs", help="list recent runs"); wl.add_argument("--limit", type=int, default=10)
    ps = sub.add_parser("push", help="push delivery (Telegram / e-mail / webhook)")
    pssub = ps.add_subparsers(dest="pscmd", required=True)
    pssub.add_parser("run", help="send what is due now (the worker does this every minute)")
    pt = pssub.add_parser("test", help="send a sample message through a user's saved channel config")
    pt.add_argument("username"); pt.add_argument("channel", choices=["email", "telegram", "webhook"])
    pls = pssub.add_parser("list", help="recent delivery records"); pls.add_argument("--limit", type=int, default=20)
    for name, hlp, default_role in (("create-user", "create a user (default role: user)", "user"),
                                    ("create-admin", "create an admin user (same as create-user --role admin)", "admin")):
        cu = sub.add_parser(name, help=hlp)
        cu.add_argument("username")
        if default_role == "user":
            cu.add_argument("--role", choices=ROLES, default="user", help="user (default) or admin")
        cu.set_defaults(default_role=default_role)
        cu.add_argument("--password", help="omit to be prompted")
        cu.add_argument("--password-file", help="read the password from this file (first line)")
    sr = sub.add_parser("set-role", help="change an existing user's role")
    sr.add_argument("username"); sr.add_argument("role", choices=ROLES)
    a = sub.add_parser("admin", help="admin utilities")
    asub = a.add_subparsers(dest="acmd", required=True)
    c = asub.add_parser("create", help="create an admin user")
    c.add_argument("username")
    c.add_argument("--password", help="omit to be prompted")
    c.add_argument("--password-file", help="read the password from this file (first line)")
    asr = asub.add_parser("set-role", help="change an existing user's role (same as `newsd set-role`)")
    asr.add_argument("username"); asr.add_argument("role", choices=ROLES)
    asub.add_parser("invite", help="print a new invite code")
    r = asub.add_parser("registration", help="show or set the registration mode")
    r.add_argument("mode", nargs="?", choices=["invite", "open", "closed"], help="omit to show the current mode")
    args = p.parse_args(argv)

    from .db import SessionLocal, init_db
    from .models import Invite, User
    from .security import new_token, token_hash

    if args.cmd == "updater":
        from .updater import serve as updater_serve
        return updater_serve(args.host, args.port)
    if args.cmd == "version":
        from . import __version__
        from .updates import fetch_latest, newer
        print(f"newsd {__version__}")
        try:
            latest = fetch_latest(timeout=8).get("latest")
        except Exception as exc:  # offline is fine
            print(f"latest release: unknown ({type(exc).__name__})")
            return 0
        print(f"latest release: {latest or 'none'}" + ("  (update available)" if newer(latest) else ""))
        return 0
    if args.cmd == "serve":
        import uvicorn

        from .eventlog import install
        install()  # 3c: WARNING+ records of the newsd loggers also go to the admin log
        uvicorn.run("newsd.app:create_app", factory=True, host=args.host, port=args.port)
        return 0
    init_db()
    if args.cmd == "db":
        if args.dcmd == "upgrade":
            print("ok")
            return 0
        import json as _json
        from .catalog import import_config
        db = SessionLocal()
        try:
            print(_json.dumps(import_config(db, args.sources, args.tags), ensure_ascii=False))
        finally:
            db.close()
        return 0
    if args.cmd == "pool":
        import json as _json
        from .pool import cycle_key, run_cycle_sync
        db = SessionLocal()
        try:
            kinds = {k.strip() for k in args.kinds.split(",") if k.strip()} if args.kinds else None
            report = run_cycle_sync(db, args.cycle or cycle_key(period_minutes=args.period), kinds=kinds, extract=not args.no_extract,
                                    lookback_hours=args.lookback_hours)
            print(_json.dumps(report, ensure_ascii=False))
        finally:
            db.close()
        return 0
    if args.cmd == "process":
        import json as _json
        from .llm.process import process_user_sync
        db = SessionLocal()
        try:
            u = db.scalar(select(User).where(User.username == args.username))
            if u is None:
                print("no such user", file=sys.stderr)
                return 1
            report = process_user_sync(db, u.id, client=None if args.no_ai else "auto", since_hours=args.since_hours,
                                       max_items=args.max_items, rerank=not args.no_rerank, dedupe=not args.no_dedupe,
                                       translate=not args.no_translate)
            print(_json.dumps(report, ensure_ascii=False, default=str))
        finally:
            db.close()
        return 0
    if args.cmd == "issue":
        import json as _json
        from .issues import build_all, build_issue_sync, issue_view
        from .models import Issue
        db = SessionLocal()
        try:
            if args.icmd == "build" and args.all:
                print(_json.dumps(build_all(db, args.key, client=None if args.no_ai else "auto", since_hours=args.since_hours),
                                  ensure_ascii=False))
                return 0
            u = db.scalar(select(User).where(User.username == (args.username or "")))
            if u is None:
                print("no such user (or pass --all)", file=sys.stderr)
                return 1
            if args.icmd == "build":
                rep = build_issue_sync(db, u.id, args.key, client=None if args.no_ai else "auto", since_hours=args.since_hours)
                print(_json.dumps(rep, ensure_ascii=False, default=str))
            else:
                q = select(Issue).where(Issue.user_id == u.id)
                q = q.order_by(Issue.key.desc()).limit(1) if args.key == "latest" else q.where(Issue.key == args.key)
                issue = db.scalar(q)
                if issue is None:
                    print("no such issue", file=sys.stderr)
                    return 1
                print(_json.dumps(issue_view(db, issue), ensure_ascii=False))
        finally:
            db.close()
        return 0
    if args.cmd == "worker":
        import json as _json
        from . import worker
        from .settings import get_settings
        if args.wcmd == "start":
            return worker.serve()
        db = SessionLocal()
        try:
            if args.wcmd == "runs":
                from .models import WorkerRun
                for r in db.scalars(select(WorkerRun).order_by(WorkerRun.id.desc()).limit(args.limit)):
                    v = worker.run_view(r, detail=False)
                    print(f"#{v['id']} {v['status']:<11} {v['trigger']:<8} key={v['issue_key']} started={v['started_at']} "
                          f"users ok/failed={v['users_ok']}/{v['users_failed']} errors={v['error_count']}")
                return 0
            cfg = get_settings()
            rep = worker.run_pipeline_sync(db, trigger="manual", cycle=args.cycle, key=args.key, pool=not args.no_pool,
                                           extract=cfg.worker_extract and not args.no_extract, client=None if args.no_ai else "auto",
                                           since_hours=args.since_hours or cfg.worker_since_hours, usernames=args.user,
                                           tz=worker.parse_tz(cfg.worker_tz))
            print(_json.dumps(rep, ensure_ascii=False, default=str))
            return 0 if rep["status"] != "failed" else 1
        finally:
            db.close()
    if args.cmd == "push":
        import json as _json
        db = SessionLocal()
        try:
            from .models import User
            from .push import service
            if args.pscmd == "run":
                print(_json.dumps(service.dispatch_due(db)))
                return 0
            if args.pscmd == "list":
                from .push.api import recent_deliveries
                for v in recent_deliveries(db, args.limit):
                    print(f"#{v['id']} {v['status']:<8} {v['kind']:<9} {v['channel']:<8} user={v['user']} key={v['issue_key'] or '-'} "
                          f"attempts={v['attempts']}/{v['max_attempts']} sent={v['progress']}/{v['total']} {v['error'] or ''}")
                return 0
            user = db.scalar(select(User).where(User.username == args.username))
            if user is None:
                print("no such user", file=sys.stderr)
                return 1
            try:
                d = service.send_test(db, user, args.channel)
            except service.PushRequestError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(d.status, d.error or "")
            return 0 if d.status == "ok" else 1
        finally:
            db.close()
    if args.cmd == "initdb":
        print("ok")
        return 0
    if args.cmd in ("create-user", "create-admin", "set-role"):
        db = SessionLocal()
        try:
            if args.cmd == "set-role":
                u = set_role(db, args.username, args.role)
                print(f"{u.username} is now {u.role}")
                return 0
            role = getattr(args, "role", None) or args.default_role
            u = create_user(db, args.username, _read_password(args), role)
            print(f"{u.role} {u.username} created")
            return 0
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        finally:
            db.close()
    db = SessionLocal()
    try:
        if args.acmd == "create":
            try:
                create_user(db, args.username, _read_password(args), "admin")
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(f"admin {args.username} created")
        elif args.acmd == "set-role":
            try:
                u = set_role(db, args.username, args.role)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(f"{u.username} is now {u.role}")
        elif args.acmd == "invite":
            code = new_token("inv")
            db.add(Invite(code_hash=token_hash(code)))
            db.commit()
            print(code)
        elif args.acmd == "registration":
            from .registration import get_registration_mode, set_registration_mode
            if args.mode:
                set_registration_mode(db, args.mode)
            print(get_registration_mode(db))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
