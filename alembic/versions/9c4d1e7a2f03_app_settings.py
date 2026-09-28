"""Настройки уведомлений в базе: таблица app_settings.

Каналы уведомлений задавались только через `.env`, и это было плохо сразу по
трём причинам: файл читается ОДИН РАЗ на импорте (правка без перезапуска службы
не действует, а про перезапуск забывают), токен бота лежал открытым текстом в
одном файле с `SECRETS_ENCRYPTION_KEY`, и правку файла потом не отследить.

Миграция ничего не переносит из `.env` намеренно. Читать окружение из миграции
значило бы вписать в базу то, что видит процесс `alembic`, а не веб-служба, —
на Windows под NSSM это РАЗНЫЕ окружения. Совместимость обеспечивает не
миграция, а `settings_store.get`: нет строки в базе — берём из окружения.

Revision ID: 9c4d1e7a2f03
Revises: 7b2e9c04a1d8
"""
from alembic import op
import sqlalchemy as sa

revision = '9c4d1e7a2f03'
down_revision = '7b2e9c04a1d8'
branch_labels = None
depends_on = None


def upgrade():
    # Alembic на SQLite НЕ откатывает упавшую посередине миграцию: проверено
    # 21.09, повторный `alembic upgrade head` падал на уже созданном объекте, а
    # накат вставал намертво. Поэтому шаг спрашивает, не сделан ли он уже.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "app_settings" in inspector.get_table_names():
        return

    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("encrypted_value", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("key", name="uq_app_settings_key"),
    )


def downgrade():
    op.drop_table("app_settings")
