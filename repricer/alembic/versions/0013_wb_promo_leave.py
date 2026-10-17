"""Срок участия в акции WB

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-17

Колонки `wb_promotion_items.leave_at / leave_by / left_at`: «участвовать до» и когда карточка
вышла из акции по сроку. ADD COLUMN на SQLite — без перестройки таблицы; каждый шаг спрашивает,
не сделан ли он уже.
"""
from alembic import op
import sqlalchemy as sa


revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade():
    cols = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('wb_promotion_items')}
    if 'leave_at' not in cols:
        op.add_column('wb_promotion_items', sa.Column('leave_at', sa.DateTime(), nullable=True))
    if 'leave_by' not in cols:
        op.add_column('wb_promotion_items', sa.Column('leave_by', sa.String(length=64), nullable=False,
                                                      server_default=''))
    if 'left_at' not in cols:
        op.add_column('wb_promotion_items', sa.Column('left_at', sa.DateTime(), nullable=True))


def downgrade():
    raise NotImplementedError("откат не поддерживается — восстанавливайте копию базы")
