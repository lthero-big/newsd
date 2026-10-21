"""m2-5 worker run records

Revision ID: 0007_m2_worker
Revises: 0006_m3_marks
Create Date: 2026-10-21 09:10:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0007_m2_worker'
down_revision = '0006_m3_marks'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('worker_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('trigger', sa.String(length=16), nullable=False),
    sa.Column('cycle', sa.String(length=64), nullable=False),
    sa.Column('issue_key', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('stages', sa.JSON(), nullable=True),
    sa.Column('users', sa.JSON(), nullable=True),
    sa.Column('errors', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('worker_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_worker_runs_started_at'), ['started_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_worker_runs_status'), ['status'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('worker_runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_worker_runs_status'))
        batch_op.drop_index(batch_op.f('ix_worker_runs_started_at'))

    op.drop_table('worker_runs')
