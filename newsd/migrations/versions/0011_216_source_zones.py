"""DEPTAI-216 (3b) source zones: per-user zones inside each source category, plus zone / position / weight
on subscriptions

Revision ID: 0011_216_zones
Revises: 0010_216_feedback
Create Date: 2026-10-21 12:40:00.000000

Every existing subscription is placed into a default zone of its category (中文 / 英文 by the source language,
else 默认), in source-key order, weight 0. Downgrade drops the zones and the three columns again; the
subscriptions themselves (and their enabled flags) are untouched both ways.
"""
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = '0011_216_zones'
down_revision = '0010_216_feedback'
branch_labels = None
depends_on = None

PLUGIN_CATS = {"plugin:hn": "hn", "plugin:github_search": "github", "plugin:hf_trending": "hf", "plugin:ossinsight": "github"}


def category(key: str, kind: str) -> str:  # frozen copy of newsd.zones.category at the time of this migration
    if kind == "plugin":
        return PLUGIN_CATS.get(key, "rss")
    if kind in ("x_account", "x_query") or (kind == "rsshub" and "twitter" in key):
        return "x"
    if kind in ("reddit", "hn", "github", "hf", "arxiv"):
        return kind
    return "rss"


def zone_name(lang: str | None) -> str:
    lang = (lang or "").lower()
    return "中文" if lang.startswith("zh") else "英文" if lang.startswith("en") else "默认"


def upgrade() -> None:
    op.create_table('source_zones',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('category', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('special', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'category', 'name', name='uq_source_zones_user_category_name')
    )
    with op.batch_alter_table('source_zones', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_source_zones_user_id'), ['user_id'], unique=False)
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('zone_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('position', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('weight', sa.Integer(), nullable=False, server_default='0'))
        batch_op.create_index(batch_op.f('ix_subscriptions_zone_id'), ['zone_id'], unique=False)
        batch_op.create_foreign_key('fk_subscriptions_zone_id', 'source_zones', ['zone_id'], ['id'], ondelete='SET NULL')

    conn = op.get_bind()
    zones = sa.table('source_zones', *[sa.column(c) for c in ('id', 'user_id', 'category', 'name', 'position', 'enabled',
                                                                'special', 'created_at')])
    subs = sa.table('subscriptions', sa.column('id'), sa.column('zone_id'), sa.column('position'))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = conn.execute(sa.text("SELECT s.id, s.user_id, src.key, src.kind, src.language FROM subscriptions s "
                                "JOIN sources src ON src.id = s.source_id ORDER BY s.user_id, src.key")).fetchall()
    rank = {"中文": 0, "英文": 1}
    rows = sorted(rows, key=lambda r: (r[1], rank.get(zone_name(r[4]), 2), r[2]))  # 中文, 英文, 默认; then by key
    made: dict[tuple, int] = {}
    count: dict[int, int] = {}
    for sid, uid, key, kind, lang in rows:
        z = (uid, category(key, kind), zone_name(lang))
        if z not in made:
            pos = sum(1 for k in made if k[:2] == z[:2])
            conn.execute(zones.insert().values(user_id=uid, category=z[1], name=z[2], position=pos, enabled=True,
                                               special=False, created_at=now))
            made[z] = conn.execute(sa.text("SELECT id FROM source_zones WHERE user_id = :u AND category = :c AND name = :n"),
                                   {"u": uid, "c": z[1], "n": z[2]}).scalar()
        zid = made[z]
        conn.execute(subs.update().where(subs.c.id == sid).values(zone_id=zid, position=count.get(zid, 0)))
        count[zid] = count.get(zid, 0) + 1


def downgrade() -> None:
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.drop_constraint('fk_subscriptions_zone_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_subscriptions_zone_id'))
        batch_op.drop_column('weight')
        batch_op.drop_column('position')
        batch_op.drop_column('zone_id')
    with op.batch_alter_table('source_zones', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_source_zones_user_id'))
    op.drop_table('source_zones')
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM user_settings WHERE key = 'source_cats'"))
