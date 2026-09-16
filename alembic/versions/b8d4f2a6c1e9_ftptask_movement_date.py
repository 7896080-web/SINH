"""ftp_task movement_date (backdated start)

Revision ID: b8d4f2a6c1e9
Revises: a7c3e5f1b208
Create Date: 2026-09-15

Добавляет ftp_tasks.movement_date — дата документа перемещения в 1С (старт
задним числом). NULL = текущая дата. Проставляется со страницы тестирования,
уходит в строке CREATE_MOVEMENT/CONFIRM_MOVEMENT, .epf ставит Документ.Дата.
"""
from alembic import op
import sqlalchemy as sa

revision = "b8d4f2a6c1e9"
down_revision = "a7c3e5f1b208"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ftp_tasks", sa.Column("movement_date", sa.Date(), nullable=True))


def downgrade():
    op.drop_column("ftp_tasks", "movement_date")
