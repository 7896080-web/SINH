"""Моменты для правила «списано с ЦС при создании поставки».

Отгрузку на FBO в 1С списывают с ЦС в момент создания поставки в Lamoda (ответ владельца,
21.10.2026). Товар поставки ещё сидит в срезе остатка ЦС, только если поставку создали
ПОСЛЕ выгрузки этого среза — для сравнения нужны оба момента:

- stock_1c.taken_at — когда 1С выгрузила остаток (UTC);
- fbo_shipments.created_at — когда создана поставка в Lamoda (`createdAt`, UTC).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-21

"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('stock_1c', schema=None) as batch_op:
        batch_op.add_column(sa.Column('taken_at', sa.DateTime(), nullable=True))

    with op.batch_alter_table('fbo_shipments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('created_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('fbo_shipments', schema=None) as batch_op:
        batch_op.drop_column('created_at')

    with op.batch_alter_table('stock_1c', schema=None) as batch_op:
        batch_op.drop_column('taken_at')
