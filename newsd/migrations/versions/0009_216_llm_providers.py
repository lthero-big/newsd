"""DEPTAI-216 multiple model providers per user (priority + failover)

Revision ID: 0009_216_providers
Revises: 0008_m4_push
Create Date: 2026-10-21 10:00:00.000000

Moves each user's single model config (``user_settings`` key ``llm``: provider/model/last4 + the
encrypted key in ``secret``) into one ``llm_providers`` row; ``translate_to`` stays in the setting.
The ciphertext is copied as-is (same master key), never decrypted here.
"""
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = '0009_216_providers'
down_revision = '0008_m4_push'
branch_labels = None
depends_on = None

LEGACY_NAMES = {"openai": "OpenAI", "deepseek": "DeepSeek", "tongyi": "通义千问", "doubao": "豆包",
                "openai_compatible": "OpenAI 兼容", "anthropic": "Anthropic Claude", "gemini": "Google Gemini",
                "ollama": "Ollama"}


def _settings():
    return sa.table('user_settings', sa.column('id', sa.Integer), sa.column('user_id', sa.Integer),
                    sa.column('key', sa.String), sa.column('value', sa.JSON), sa.column('secret', sa.Text))


def upgrade() -> None:
    op.create_table('llm_providers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('preset', sa.String(length=32), nullable=False),
    sa.Column('base_url', sa.String(length=512), nullable=True),
    sa.Column('model', sa.String(length=128), nullable=False),
    sa.Column('secret', sa.Text(), nullable=True),
    sa.Column('key_last4', sa.String(length=8), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('last_test_at', sa.DateTime(), nullable=True),
    sa.Column('last_test_ok', sa.Boolean(), nullable=True),
    sa.Column('last_test_ms', sa.Integer(), nullable=True),
    sa.Column('last_test_error', sa.String(length=300), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('llm_providers', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_llm_providers_user_id'), ['user_id'], unique=False)

    conn = op.get_bind()
    us = _settings()
    prov = sa.table('llm_providers', *[sa.column(c) for c in (
        'user_id', 'name', 'preset', 'base_url', 'model', 'secret', 'key_last4', 'enabled', 'priority',
        'created_at', 'updated_at')])
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for row in conn.execute(sa.select(us.c.id, us.c.user_id, us.c.value, us.c.secret).where(us.c.key == 'llm')).fetchall():
        v = dict(row.value) if isinstance(row.value, dict) else {}
        provider, model = v.pop('provider', None), v.pop('model', None)
        last4 = v.pop('last4', None)
        if provider and model:
            conn.execute(prov.insert().values(
                user_id=row.user_id, name=LEGACY_NAMES.get(provider, provider)[:64], preset=provider, base_url=None,
                model=model, secret=row.secret, key_last4=last4, enabled=True, priority=0, created_at=now, updated_at=now))
        conn.execute(us.update().where(us.c.id == row.id).values(value=v, secret=None))


def downgrade() -> None:
    conn = op.get_bind()
    us = _settings()
    prov = sa.table('llm_providers', sa.column('id', sa.Integer), sa.column('user_id', sa.Integer),
                    sa.column('preset', sa.String), sa.column('model', sa.String), sa.column('secret', sa.Text),
                    sa.column('key_last4', sa.String), sa.column('priority', sa.Integer))
    seen = set()
    for p in conn.execute(sa.select(prov).order_by(prov.c.user_id, prov.c.priority, prov.c.id)).fetchall():
        if p.user_id in seen:
            continue
        seen.add(p.user_id)
        preset = {"qwen": "tongyi", "custom": "openai_compatible"}.get(p.preset, p.preset)
        if preset not in LEGACY_NAMES:
            continue
        cur = conn.execute(sa.select(us.c.id, us.c.value).where(us.c.user_id == p.user_id, us.c.key == 'llm')).first()
        v = dict(cur.value) if cur is not None and isinstance(cur.value, dict) else {}
        v.update(provider=preset, model=p.model, last4=p.key_last4)
        if cur is None:
            conn.execute(us.insert().values(user_id=p.user_id, key='llm', value=v, secret=p.secret))
        else:
            conn.execute(us.update().where(us.c.id == cur.id).values(value=v, secret=p.secret))
    with op.batch_alter_table('llm_providers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_llm_providers_user_id'))
    op.drop_table('llm_providers')
