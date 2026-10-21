"""m4 push delivery records

Revision ID: 0008_m4_push
Revises: 0007_m2_worker
Create Date: 2026-10-21 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0008_m4_push'
down_revision = '0007_m2_worker'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('push_deliveries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('channel', sa.String(length=16), nullable=False),
    sa.Column('issue_key', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('progress', sa.Integer(), nullable=False),
    sa.Column('total', sa.Integer(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_attempt_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('log', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('push_deliveries', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_push_deliveries_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_push_deliveries_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_push_deliveries_next_attempt_at'), ['next_attempt_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_push_deliveries_created_at'), ['created_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('push_deliveries', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_push_deliveries_created_at'))
        batch_op.drop_index(batch_op.f('ix_push_deliveries_next_attempt_at'))
        batch_op.drop_index(batch_op.f('ix_push_deliveries_status'))
        batch_op.drop_index(batch_op.f('ix_push_deliveries_user_id'))

    op.drop_table('push_deliveries')
