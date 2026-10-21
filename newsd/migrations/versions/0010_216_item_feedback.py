"""DEPTAI-216 (3a) reading feedback: like / dislike votes and folded cards

Revision ID: 0010_216_feedback
Revises: 0009_216_providers
Create Date: 2026-10-21 11:00:00.000000

``item_marks.state`` also takes ``purged`` from now on (emptied from the trash; the row is kept so the
deletion still down-ranks similar items). No schema change is needed for that.
"""
from alembic import op
import sqlalchemy as sa


revision = '0010_216_feedback'
down_revision = '0009_216_providers'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('item_feedback',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('item_id', sa.Integer(), nullable=False),
    sa.Column('vote', sa.Integer(), nullable=False),
    sa.Column('folded', sa.Boolean(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['item_id'], ['pool_items.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'item_id')
    )
    with op.batch_alter_table('item_feedback', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_item_feedback_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_item_feedback_item_id'), ['item_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('item_feedback', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_item_feedback_item_id'))
        batch_op.drop_index(batch_op.f('ix_item_feedback_user_id'))
    op.drop_table('item_feedback')
    op.execute("DELETE FROM item_marks WHERE state = 'purged'")
