"""ftp_task repost_count

Revision ID: 253485348f2d
Revises: 22dc047e2f83
Create Date: 2026-09-18 23:43:29.543877

"""
from alembic import op
import sqlalchemy as sa


revision = '253485348f2d'
down_revision = '22dc047e2f83'
branch_labels = None
depends_on = None


def upgrade():
    # server_default обязателен: колонка NOT NULL, а в таблице уже есть строки
    # (на бою 114 заданий) — без умолчания апгрейд на них и упал бы. Autogenerate
    # его не поставил, это правка руками.
    with op.batch_alter_table('ftp_tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('repost_count', sa.Integer(),
                                      nullable=False, server_default='0'))


def downgrade():
    with op.batch_alter_table('ftp_tasks', schema=None) as batch_op:
        batch_op.drop_column('repost_count')
