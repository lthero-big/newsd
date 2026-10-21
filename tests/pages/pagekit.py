"""Helpers: log users in, give them subscriptions/keywords over a shared pool."""
from sqlalchemy import select


def login(client, name, password=None):
    client.cookies.clear()
    r = client.post("/login", data={"username": name, "password": password or name + "pass123"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


def register(client, name):
    from newsd.cli import main
    main(["admin", "registration", "open"])
    client.cookies.clear()
    assert client.post("/api/register", data={"username": name, "password": name + "pass123"}).status_code == 200
    return login(client, name)


def subscribe_all(db, name, interest=(), block=()):
    from newsd.models import Keyword, Source, Subscription, User
    u = db.scalar(select(User).where(User.username == name))
    for s in db.scalars(select(Source)):
        db.add(Subscription(user_id=u.id, source_id=s.id))
    for t in interest:
        db.add(Keyword(user_id=u.id, kind="interest", term=t))
    for t in block:
        db.add(Keyword(user_id=u.id, kind="block", term=t))
    db.commit()
    return u
