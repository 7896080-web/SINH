"""product broadcast_offset (порог трансляции)

Revision ID: c1f5a9d2e3b4
Revises: b8d4f2a6c1e9
Create Date: 2026-09-16 18:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c1f5a9d2e3b4'
down_revision = 'b8d4f2a6c1e9'
branch_labels = None
depends_on = None


def upgrade():
    # Порог трансляции: nullable, значение может быть отрицательным. server_default
    # не нужен — NULL корректно означает «порог не задан».
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(sa.Column('broadcast_offset', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_column('broadcast_offset')
