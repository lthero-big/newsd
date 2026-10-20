from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="newsd")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the web server")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    sub.add_parser("initdb", help="create tables")
    d = sub.add_parser("db", help="schema migrations and catalog import")
    dsub = d.add_subparsers(dest="dcmd", required=True)
    dsub.add_parser("upgrade", help="alembic upgrade head")
    im = dsub.add_parser("import-config", help="import config/collect-sources.json + tags.json into the catalog")
    im.add_argument("--sources"); im.add_argument("--tags")
    a = sub.add_parser("admin", help="admin utilities")
    asub = a.add_subparsers(dest="acmd", required=True)
    c = asub.add_parser("create", help="create an admin user")
    c.add_argument("username")
    c.add_argument("--password", help="omit to be prompted")
    asub.add_parser("invite", help="print a new invite code")
    r = asub.add_parser("registration", help="show or set the registration mode")
    r.add_argument("mode", nargs="?", choices=["invite", "open", "closed"], help="omit to show the current mode")
    args = p.parse_args(argv)

    from .db import SessionLocal, init_db
    from .models import Invite, User
    from .security import hash_password, new_token, token_hash

    if args.cmd == "serve":
        import uvicorn
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
    if args.cmd == "initdb":
        print("ok")
        return 0
    db = SessionLocal()
    try:
        if args.acmd == "create":
            if db.scalar(select(User).where(User.username == args.username)):
                print("user exists", file=sys.stderr)
                return 1
            pw = args.password or getpass.getpass("password: ")
            if len(pw) < 8:
                print("password must be >= 8 chars", file=sys.stderr)
                return 1
            db.add(User(username=args.username, password_hash=hash_password(pw), role="admin"))
            db.commit()
            print(f"admin {args.username} created")
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
