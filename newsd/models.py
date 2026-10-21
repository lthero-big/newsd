"""M1 data model: accounts, invites, sessions, API tokens, login throttling, per-user settings."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user")  # admin | user | readonly (3c)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 3c: filled from an e-mail invite; display only
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 3c: last authenticated request


class Invite(Base):
    __tablename__ = "invites"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    used_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # 3c: an invite can put the new user into a group (role / default sources from the group) and remember an e-mail
    group_id: Mapped[int | None] = mapped_column(ForeignKey("user_groups.id", ondelete="SET NULL"), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)


class UserGroup(Base):
    """DEPTAI-216 3c: a user group. Members get the union of their groups' permissions (see newsd/perms.py);
    ``role`` is the role an invited user starts with, ``default_sources`` are subscribed when someone joins,
    ``quota`` caps a member's daily issue (the most generous of their groups wins; None = no group cap)."""
    __tablename__ = "user_groups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    __table_args__ = (UniqueConstraint("name", name="uq_user_groups_name"),)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(255), default="")
    color: Mapped[str] = mapped_column(String(16), default="#64748b")
    role: Mapped[str] = mapped_column(String(16), default="user")
    perms: Mapped[list | None] = mapped_column(JSON, nullable=True)
    default_sources: Mapped[list | None] = mapped_column(JSON, nullable=True)
    quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class UserGroupMember(Base):
    __tablename__ = "user_group_members"
    group_id: Mapped[int] = mapped_column(ForeignKey("user_groups.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class UserActivity(Base):
    """3c: one row per (user, UTC day) with an authenticated request: the admin overview's active-user trend."""
    __tablename__ = "user_activity"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True, index=True)


class LogEvent(Base):
    """3c: structured operational log for the admin 日志与任务 page (worker stages, fetch / push / model failures,
    logins, WARNING+ records of the newsd loggers). Messages are redacted before they are stored; pruned after 30 days."""
    __tablename__ = "log_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    level: Mapped[str] = mapped_column(String(8), default="INFO", index=True)  # DEBUG | INFO | WARN | ERROR
    module: Mapped[str] = mapped_column(String(32), default="api", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    source_key: Mapped[str | None] = mapped_column(String(191), nullable=True, index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class WebSession(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ApiToken(Base):
    """Per-user API key / MCP token. Only the sha256 hash is stored."""
    __tablename__ = "api_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16), default="api")  # api | mcp
    name: Mapped[str] = mapped_column(String(64), default="")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    last4: Mapped[str] = mapped_column(String(4))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LoginFailure(Base):
    __tablename__ = "login_failures"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ip: Mapped[str] = mapped_column(String(64), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class UserSetting(Base):
    """Free-form per-user settings (keywords, quotas, schedule, delivery, palette...).
    Secret values (model API keys) are stored encrypted via newsd.crypto."""
    __tablename__ = "user_settings"
    __table_args__ = (UniqueConstraint("user_id", "key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[dict | list | str | int | None] = mapped_column(JSON, nullable=True)
    secret: Mapped[str | None] = mapped_column(Text, nullable=True)


class LLMProvider(Base):
    """One of a user's model providers (DEPTAI-216). Enabled providers are tried in ``priority``
    order (0 first) with automatic failover. ``secret`` is the API key, encrypted with the server
    master key; only ``key_last4`` is ever shown. ``base_url`` NULL = the preset's default."""
    __tablename__ = "llm_providers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    preset: Mapped[str] = mapped_column(String(32))
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    model: Mapped[str] = mapped_column(String(128))
    secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_last4: Mapped[str | None] = mapped_column(String(8), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_test_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class GlobalSetting(Base):
    """Admin-level defaults and limits (max sources per user, editions per day, min interval...)."""
    __tablename__ = "global_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | str | int | None] = mapped_column(JSON, nullable=True)


# ---------------------------------------------------------------- M2-1: sources, subscriptions, keywords, quotas

class Source(Base):
    """Global source catalog entry. key is the stable source id (rss:<id>, reddit:<sub>, rsshub:<id>, xq:<id>, xa:<handle>, plugin:<name>)."""
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(191), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # rss | reddit | rsshub | x_query | x_account | plugin
    name: Mapped[str] = mapped_column(String(191), default="")
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    default_tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # kind-specific parameters (query terms, route, mode...)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    content_extractor: Mapped[bool] = mapped_column(Boolean, default=False)  # fetch full text (trafilatura)
    extractor_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # e.g. {"favor_precision": true}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Subscription(Base):
    """A user's pick from the catalog, with a few optional per-user overrides."""
    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("user_id", "source_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    overrides: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # whitelisted keys only (see newsd.prefs)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # DEPTAI-216 (3b): the zone (分区) the source sits in on the sources page, its place there and a weight
    # 0 normal / 1 more (x1.3) / 2 much more (x1.6). zone_id NULL = not placed yet (newsd.zones assigns a default).
    zone_id: Mapped[int | None] = mapped_column(ForeignKey("source_zones.id", ondelete="SET NULL"), nullable=True, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    weight: Mapped[int] = mapped_column(Integer, default=0)


class SourceZone(Base):
    """DEPTAI-216 (3b): a user's zone inside one source category (x / reddit / hn / rss / github / hf / arxiv), e.g.
    中文 / 英文 / 特别关注. A disabled zone turns all of its sources off; a ``special`` zone skips every quota and
    is never down-ranked by learned feedback."""
    __tablename__ = "source_zones"
    __table_args__ = (UniqueConstraint("user_id", "category", "name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(64))
    position: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    special: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Keyword(Base):
    """Per-user keyword: interest (include), block (exclude) or boost (weighted)."""
    __tablename__ = "keywords"
    __table_args__ = (UniqueConstraint("user_id", "kind", "term"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # interest | block | boost
    term: Mapped[str] = mapped_column(String(191))
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Quota(Base):
    """Per-user item caps. module="" & group="" is the overall cap; group="" is a module cap;
    module+group is a category-group cap (cf. Horizon category_groups / max_items)."""
    __tablename__ = "quotas"
    __table_args__ = (UniqueConstraint("user_id", "module", "group_name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    module: Mapped[str] = mapped_column(String(64), default="")
    group_name: Mapped[str] = mapped_column(String(64), default="")
    max_items: Mapped[int] = mapped_column(Integer)
    categories: Mapped[list | None] = mapped_column(JSON, nullable=True)  # tags/categories that belong to this group


class TagTerm(Base):
    """Global tag keyword map imported from config/tags.json."""
    __tablename__ = "tag_terms"
    __table_args__ = (UniqueConstraint("tag", "term"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tag: Mapped[str] = mapped_column(String(64), index=True)
    term: Mapped[str] = mapped_column(String(191))
    aliases: Mapped[list | None] = mapped_column(JSON, nullable=True)
    broad: Mapped[bool] = mapped_column(Boolean, default=False)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class SubscriptionTemplate(Base):
    """Named starter set (sources + keywords) applied to new users, e.g. "default"."""
    __tablename__ = "subscription_templates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    keywords: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{"kind","term","weight"}]
    quotas: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{"module","group_name","max_items","categories"}]


class SubscriptionTemplateItem(Base):
    __tablename__ = "subscription_template_items"
    __table_args__ = (UniqueConstraint("template_id", "source_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("subscription_templates.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))


# ---------------------------------------------------------------- M2-2: shared collection pool

class SourceFetch(Base):
    """One row per (source, cycle). The unique key is the "fetch each source once per cycle" guarantee:
    whoever inserts the row first does the request; everyone else reuses the pool."""
    __tablename__ = "source_fetches"
    __table_args__ = (UniqueConstraint("source_id", "cycle"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    cycle: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(64), default="running")  # running | ok | failed:<reason> | skipped:<reason>
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    entries: Mapped[int] = mapped_column(Integer, default=0)
    new_items: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PoolItem(Base):
    """Shared entry pool. Deduped by normalized URL (unique) and, when there is enough text, by content hash."""
    __tablename__ = "pool_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256(canonical_url)
    canonical_url: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    title: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")  # source-provided listing text / feed summary
    author: Mapped[str | None] = mapped_column(String(191), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    image: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str | None] = mapped_column(String(32), nullable=True)  # post | release | repo | model
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # points, comments, enclosure, ...
    first_source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    extraction_id: Mapped[int | None] = mapped_column(ForeignKey("extractions.id", ondelete="SET NULL"), nullable=True)


class PoolItemSource(Base):
    """Which sources carried an item, and when each one first fetched it."""
    __tablename__ = "pool_item_sources"
    __table_args__ = (UniqueConstraint("item_id", "source_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("pool_items.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(Text)  # URL as this source published it
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    cycle: Mapped[str] = mapped_column(String(64))


class Extraction(Base):
    """Full-text + metadata extraction cache, shared by all users. Keyed by canonical URL; html_hash lets a
    page served under another URL reuse the result. text is internal (scoring/summary/translation/dedupe/search)."""
    __tablename__ = "extractions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256(canonical_url)
    canonical_url: Mapped[str] = mapped_column(Text)
    html_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    params: Mapped[str] = mapped_column(String(32), default="")  # "", "precision" or "recall"
    status: Mapped[str] = mapped_column(String(64))  # ok | failed:<reason>
    extractor: Mapped[str] = mapped_column(String(32), default="none")
    text: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(String(191), nullable=True)
    published_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    sitename: Mapped[str | None] = mapped_column(String(191), nullable=True)
    image: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_from: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ---------------------------------------------------------------- M2-4: model layer

class TranslationCache(Base):
    """Shared by all users: one row per (target language, normalized source text)."""
    __tablename__ = "translation_cache"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256(target_lang + "\n" + normalized text)
    target_lang: Mapped[str] = mapped_column(String(16))
    source_text: Mapped[str] = mapped_column(Text)
    translated: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(191), default="")  # provider/model that produced it
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ItemScore(Base):
    """A user's processing-profile verdict for one pool item. status: ok | empty (invalid JSON after one
    repair, or item missing from the reply) | error:<kind>. Empty/error rows are retried on the next run."""
    __tablename__ = "item_scores"
    __table_args__ = (UniqueConstraint("user_id", "item_id", "profile"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("pool_items.id", ondelete="CASCADE"), index=True)
    profile: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="ok")
    belongs: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    section: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(String(191), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ---------------------------------------------------------------- M2-3: per-user issues

class Issue(Base):
    """One user's edition (e.g. key "2026-10-20"). Rebuilding the same key replaces its items."""
    __tablename__ = "issues"
    __table_args__ = (UniqueConstraint("user_id", "key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="ok")
    ai: Mapped[str] = mapped_column(String(191), default="off")  # on | skipped: ... | fallback: ...
    model: Mapped[str | None] = mapped_column(String(191), nullable=True)
    window_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # counts, caps used, drop reasons
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class IssueItem(Base):
    """A selected pool item in an issue, in display order, with why it was picked."""
    __tablename__ = "issue_items"
    __table_args__ = (UniqueConstraint("issue_id", "item_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("pool_items.id", ondelete="CASCADE"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    module: Mapped[str] = mapped_column(String(32))
    group_name: Mapped[str] = mapped_column(String(64), default="")
    score: Mapped[float] = mapped_column(Float)
    rule_score: Mapped[float] = mapped_column(Float)
    llm_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    section: Mapped[str | None] = mapped_column(String(64), nullable=True)
    matched: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{"term","group","locations","kind","weight"}]
    categories: Mapped[list | None] = mapped_column(JSON, nullable=True)
    also: Mapped[list | None] = mapped_column(JSON, nullable=True)  # merged duplicates
    title_translated: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_translated: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------- M3: favorites / trash

class ItemMark(Base):
    """A user's favorite or trash mark on a pool item. One state per (user, item); trash hides the item from the
    user's issue pages, favorite keeps it on the favorites page. Marks never touch the shared pool."""
    __tablename__ = "item_marks"
    __table_args__ = (UniqueConstraint("user_id", "item_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("pool_items.id", ondelete="CASCADE"), index=True)
    state: Mapped[str] = mapped_column(String(16))  # favorite | trash | purged (trash emptied; kept as a learning signal)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ---------------------------------------------------------------- M2-5: worker run records

class ItemFeedback(Base):
    """Per-user reading feedback on one item (DEPTAI-216 item 3): vote 1 = ♥ like (more like this),
    -1 = 👎 dislike (fewer like this), 0 = none; ``folded`` = "seen, collapse it". Favorites and the
    trash stay in ``item_marks``."""
    __tablename__ = "item_feedback"
    __table_args__ = (UniqueConstraint("user_id", "item_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("pool_items.id", ondelete="CASCADE"), index=True)
    vote: Mapped[int] = mapped_column(Integer, default=0)
    folded: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class WorkerRun(Base):
    """One scheduler tick (or manual `newsd worker run-once`): the shared collection, then every user's issue.
    ``stages`` / ``users`` are JSON so the admin page can show per-stage counts and per-user results."""
    __tablename__ = "worker_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(16), default="schedule")  # schedule | manual | startup
    cycle: Mapped[str] = mapped_column(String(64), default="")
    issue_key: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)  # running | ok | partial | failed | interrupted
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stages: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{"stage","status","started_at","finished_at","counts","error"}]
    users: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{"user","status","items_in","items_out","ai","error",...}]
    errors: Mapped[list | None] = mapped_column(JSON, nullable=True)  # short strings, capped


# ---------------------------------------------------------------- M4: push delivery records

class PushDelivery(Base):
    """One delivery of one issue (or a test message) to one channel. ``retrying`` rows are picked up again by the
    dispatcher once ``next_attempt_at`` passes; ``progress`` counts messages already sent so a retry of a
    summary-and-items push resumes instead of re-sending. ``error`` never contains secrets (redacted before storing)."""
    __tablename__ = "push_deliveries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="scheduled")  # scheduled | test
    channel: Mapped[str] = mapped_column(String(16))  # email | telegram | webhook
    issue_key: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending | retrying | ok | failed | skipped
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    log: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{"n","at","ok","sent","error"}] per attempt
