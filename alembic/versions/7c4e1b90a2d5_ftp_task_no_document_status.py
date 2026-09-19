"""ftp_tasks.status += 'no_document' (разобрано человеком: документа в 1С нет)

Revision ID: 7c4e1b90a2d5
Revises: 253485348f2d
Create Date: 2026-09-19 00:20:00.000000

Значение перечисления добавлено в app/models.py, поэтому миграция обязательна по
правилам проекта. Физически работа нужна только на PostgreSQL: там Enum —
нативный тип. На SQLite (боевая база) колонка `status` — обычный VARCHAR без
CHECK-ограничения, новое значение пишется туда как есть, менять нечего.
Ровно так же поступала миграция a3f70c5b8e14, добавлявшая 'failed'.
"""
from alembic import op


revision = '7c4e1b90a2d5'
down_revision = '253485348f2d'
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE ftptaskstatus ADD VALUE IF NOT EXISTS 'no_document'")


def downgrade():
    # Удалить значение из нативного типа PostgreSQL нельзя без пересоздания типа,
    # а на SQLite удалять нечего. Строки с этим статусом при откате кода
    # останутся и будут видны как есть — это честнее, чем молча их переписать.
    pass
