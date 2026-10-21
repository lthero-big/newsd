"""DEPTAI-216 (3c) admin console: user groups + members, invite → group, user e-mail / last seen,
daily user activity, structured log events

Revision ID: 0012_216_admin
Revises: 0011_216_zones
Create Date: 2026-10-21 13:30:00.000000

Purely additive: no existing row changes meaning. Users without a group keep the old default permissions
(read / own sources / models / push). Downgrade drops the new tables and columns.

The two ``users`` columns are added / dropped with plain ALTER TABLE, never a batch (copy-and-recreate)
operation: on SQLite with foreign_keys=ON, dropping the old ``users`` table would cascade-delete every
subscription, issue, mark... ``invites`` is referenced by no other table, so a batch rebuild is safe there.
Column adds skip columns that already exist (databases created by create_all from a newer model).
"""
from alembic import op
import sqlalchemy as sa


revision = '0012_216_admin'
down_revision = '0011_216_zones'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'user_groups',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('color', sa.String(length=16), nullable=False, server_default='#64748b'),
        sa.Column('role', sa.String(length=16), nullable=False, server_default='user'),
        sa.Column('perms', sa.JSON(), nullable=True),
        sa.Column('default_sources', sa.JSON(), nullable=True),
        sa.Column('quota', sa.Integer(), nullable=True),
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_user_groups_name'),
    )
    op.create_table(
        'user_group_members',
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['group_id'], ['user_groups.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('group_id', 'user_id'),
    )
    op.create_index('ix_user_group_members_user_id', 'user_group_members', ['user_id'], unique=False)
    op.create_table(
        'user_activity',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('day', sa.String(length=10), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'day'),
    )
    op.create_index('ix_user_activity_day', 'user_activity', ['day'], unique=False)
    op.create_table(
        'log_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('level', sa.String(length=8), nullable=False),
        sa.Column('module', sa.String(length=32), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('source_key', sa.String(length=191), nullable=True),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('username', sa.String(length=64), nullable=True),
        sa.Column('detail', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    for col in ('at', 'level', 'module', 'source_key', 'run_id', 'user_id'):
        op.create_index(f'ix_log_events_{col}', 'log_events', [col], unique=False)
    have = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('users')}
    for col in (sa.Column('email', sa.String(length=255), nullable=True), sa.Column('last_seen_at', sa.DateTime(), nullable=True)):
        if col.name not in have:
            op.add_column('users', col)  # plain ALTER TABLE ADD COLUMN (see the module docstring)
    have = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('invites')}
    fks = {fk.get('name') for fk in sa.inspect(op.get_bind()).get_foreign_keys('invites')}
    with op.batch_alter_table('invites', schema=None) as batch_op:
        if 'group_id' not in have:
            batch_op.add_column(sa.Column('group_id', sa.Integer(), nullable=True))
        if 'note' not in have:
            batch_op.add_column(sa.Column('note', sa.String(length=255), nullable=True))
        if 'fk_invites_group_id' not in fks:
            batch_op.create_foreign_key('fk_invites_group_id', 'user_groups', ['group_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('invites', schema=None) as batch_op:
        batch_op.drop_constraint('fk_invites_group_id', type_='foreignkey')
        batch_op.drop_column('note')
        batch_op.drop_column('group_id')
    op.drop_column('users', 'last_seen_at')  # plain ALTER TABLE DROP COLUMN, never a users rebuild
    op.drop_column('users', 'email')
    for col in ('at', 'level', 'module', 'source_key', 'run_id', 'user_id'):
        op.drop_index(f'ix_log_events_{col}', table_name='log_events')
    op.drop_table('log_events')
    op.drop_index('ix_user_activity_day', table_name='user_activity')
    op.drop_table('user_activity')
    op.drop_index('ix_user_group_members_user_id', table_name='user_group_members')
    op.drop_table('user_group_members')
    op.drop_table('user_groups')
