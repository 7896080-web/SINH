"""publish_hidden_on_stock

Возвращать ли на витрину карточки, которые площадка спрятала за нулевой
остаток (Kit, настройка «скрывать товары с нулевым остатком»). По умолчанию
ВЫКЛЮЧЕНО: публикация — действие наружу, и включать его должен человек по
конкретному кабинету.

Revision ID: da5512561de5
Revises: f0452d7775ba
Create Date: 2026-09-20 18:23:12.325140

Из автогенерации убран `alter_column` по `ftp_tasks.status` (VARCHAR(7) →
Enum): колонка и так хранит те же строки, а batch-режим SQLite ради смены типа
ПЕРЕСОЗДАЁТ таблицу целиком — на боевой базе это лишний риск ради нуля пользы.
Та же правка руками уже делалась в `35843d7a5cdc` и `f0452d7775ba`.
"""
from alembic import op
import sqlalchemy as sa


revision = 'da5512561de5'
down_revision = 'f0452d7775ba'
branch_labels = None
depends_on = None


def upgrade():
    # server_default обязателен: колонка NOT NULL, а строки кабинетов уже есть —
    # без значения по умолчанию апгрейд упал бы на боевой базе. Выключено (false)
    # — потому что автопубликацию включают осознанно, по одному кабинету.
    with op.batch_alter_table('platform_accounts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('publish_hidden_on_stock', sa.Boolean(),
                                      nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table('platform_accounts', schema=None) as batch_op:
        batch_op.drop_column('publish_hidden_on_stock')
