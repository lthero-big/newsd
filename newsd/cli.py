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
    a = sub.add_parser("admin", help="admin utilities")
    asub = a.add_subparsers(dest="acmd", required=True)
    c = asub.add_parser("create", help="create an admin user")
    c.add_argument("username")
    c.add_argument("--password", help="omit to be prompted")
    asub.add_parser("invite", help="print a new invite code")
    args = p.parse_args(argv)

    from .db import SessionLocal, init_db
    from .models import Invite, User
    from .security import hash_password, new_token, token_hash

    if args.cmd == "serve":
        import uvicorn
        uvicorn.run("newsd.app:create_app", factory=True, host=args.host, port=args.port)
        return 0
    init_db()
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
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
