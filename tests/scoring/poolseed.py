"""Build a small shared pool directly in the DB."""
import hashlib
from datetime import datetime, timedelta, timezone

from newsd.models import Extraction, Keyword, PoolItem, PoolItemSource, Quota, Source, Subscription, TagTerm, User


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Pool:
    def __init__(self, db):
        self.db = db
        self.sources = {}
        self.n = 0

    def source(self, key, kind="rss", name=None):
        if key not in self.sources:
            s = Source(key=key, kind=kind, name=name or key, url=f"https://{key.replace(':', '-')}.example/feed", config={})
            self.db.add(s); self.db.commit()
            self.sources[key] = s
        return self.sources[key]

    def item(self, title, *, source="rss:feed", kind="post", summary="", url=None, canonical=None, hours=1.0, text=None,
             extra=None, src_kind="rss"):
        self.n += 1
        url = url or f"https://news.example/{self.n}"
        canonical = canonical or url
        it = PoolItem(url_key=hashlib.sha256(f"{canonical}#{self.n}".encode()).hexdigest(), canonical_url=canonical, url=url,
                      title=title, summary=summary, kind=kind, extra=extra, published_at=now() - timedelta(hours=hours))
        if text is not None:
            ex = Extraction(url_key=hashlib.sha256(url.encode()).hexdigest(), canonical_url=canonical, status="ok",
                            extractor="trafilatura", text=text)
            self.db.add(ex); self.db.flush()
            it.extraction_id = ex.id
        self.db.add(it); self.db.flush()
        src = self.source(source, src_kind)
        self.db.add(PoolItemSource(item_id=it.id, source_id=src.id, url=url, cycle="c1"))
        self.db.commit()
        return it

    def user(self, name, *, interest=(), block=(), boost=(), quotas=()):
        u = User(username=name, password_hash="x")
        self.db.add(u); self.db.commit()
        for s in self.sources.values():
            self.db.add(Subscription(user_id=u.id, source_id=s.id))
        for t in interest:
            self.db.add(Keyword(user_id=u.id, kind="interest", term=t))
        for t in block:
            self.db.add(Keyword(user_id=u.id, kind="block", term=t))
        for t, w in boost:
            self.db.add(Keyword(user_id=u.id, kind="boost", term=t, weight=w))
        for q in quotas:
            self.db.add(Quota(user_id=u.id, module=q.get("module", ""), group_name=q.get("group_name", ""),
                              max_items=q["max_items"], categories=q.get("categories")))
        self.db.commit()
        return u

    def tag(self, tag, term, aliases=(), broad=False):
        self.db.add(TagTerm(tag=tag, term=term, aliases=list(aliases), broad=broad)); self.db.commit()
