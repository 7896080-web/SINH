"""Индексы под реальные запросы страниц

Замер на боевом каталоге (152 235 товаров, 154 232 баркода, 276 856 строк
сверки) показал, что поиск по штрихкоду не отвечает вовсе: коррелированный
подзапрос выполнялся на каждый товар, а `barcodes.uid_1c` индекса не имел —
то есть на каждый из 152 тысяч товаров читались все 154 тысячи баркодов.

Остальные индексы — по тем же замерам: очередь рассылки спрашивают по паре
товар+кабинет и по статусу, сверку — сутками по `checked_at`, задания 1С —
по статусу. Ни одного индекса там не было, каждое обращение читало таблицу
целиком.

Данных миграция НЕ трогает: индекс — это только способ добраться до строки.
Обратная миграция их снимает, поведение системы от этого не меняется, только
скорость. На боевой базе создание занимает секунды.

Revision ID: 5c7ec8206dc3
Revises: 404c393f7af7
Create Date: 2026-09-20 22:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '5c7ec8206dc3'
down_revision = 'f7af79b9e455'
branch_labels = None
depends_on = None

# (таблица, имя индекса, колонки). Одним списком, чтобы upgrade и downgrade не
# разошлись: индекс, забытый в downgrade, ломает откат на ровном месте.
INDEXES = [
    ("barcodes", "ix_barcodes_uid_1c", ["uid_1c"]),
    ("dispatch_queue", "ix_dispatch_queue_pair", ["uid_1c", "account_id"]),
    ("dispatch_queue", "ix_dispatch_queue_status", ["status"]),
    ("ftp_tasks", "ix_ftp_tasks_order_id", ["order_id"]),
    ("ftp_tasks", "ix_ftp_tasks_status", ["status"]),
    ("processed_orders", "ix_processed_orders_uid_1c", ["uid_1c"]),
    ("reconciliation_log", "ix_reconciliation_log_checked_at", ["checked_at"]),
    ("reconciliation_log", "ix_reconciliation_log_uid_1c", ["uid_1c"]),
    ("sync_anomalies", "ix_sync_anomalies_status", ["status"]),
]


def upgrade():
    # Автогенерация просилась заодно переписать `ftp_tasks.status` (VARCHAR(7) →
    # Enum): это фантом — колонка и так хранит те же строки, а batch_alter_table
    # на SQLite пересоздаёт таблицу целиком, а там живая очередь заданий 1С.
    for table, name, columns in INDEXES:
        op.create_index(name, table, columns, unique=False)


def downgrade():
    for table, name, _ in INDEXES:
        op.drop_index(name, table_name=table)
