"""m3 favorites and trash marks

Revision ID: 0006_m3_marks
Revises: 0005_m2_issues
Create Date: 2026-10-21 09:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0006_m3_marks'
down_revision = '0005_m2_issues'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('item_marks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('item_id', sa.Integer(), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['item_id'], ['pool_items.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'item_id')
    )
    with op.batch_alter_table('item_marks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_item_marks_item_id'), ['item_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_item_marks_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('item_marks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_item_marks_user_id'))
        batch_op.drop_index(batch_op.f('ix_item_marks_item_id'))
    op.drop_table('item_marks')
