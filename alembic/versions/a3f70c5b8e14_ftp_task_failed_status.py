"""ftp_tasks.status += 'failed' (ответ ERROR от 1С больше не считается успехом)

Revision ID: a3f70c5b8e14
Revises: e5b1c7a94d20
Create Date: 2026-09-17 15:00:00.000000

Значение перечисления добавлено в app/models.py, поэтому миграция обязательна по
правилам проекта. Физически работа нужна только на PostgreSQL: там Enum — нативный
тип, и новое значение приходится добавлять явно. На SQLite (боевая база) колонка
`status` — обычный VARCHAR(7) без CHECK-ограничения, 'failed' туда пишется как
есть, менять нечего.
"""
from alembic import op


revision = 'a3f70c5b8e14'
down_revision = 'e5b1c7a94d20'
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE ftptaskstatus ADD VALUE IF NOT EXISTS 'failed'")


def downgrade():
    # Удалить значение из нативного типа PostgreSQL нельзя без пересоздания типа,
    # а на SQLite удалять нечего. Откат ничего не делает осознанно: строки со
    # статусом 'failed' при откате кода останутся и будут видны как есть.
    pass
