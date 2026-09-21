"""Индекс ftp_tasks.barcode — час сверки перестаёт расти вместе с таблицей

Revision ID: cfa36a1f1f2f
Revises: 96775c61f31f
Create Date: 2026-09-21

Аудит 21.09. `reconciliation._in_flight_adjustment` спрашивает задания 1С ПО
КАЖДОМУ товару снимка, и без индекса каждый такой вопрос читал `ftp_tasks`
целиком (`EXPLAIN QUERY PLAN` -> `SCAN`). Замер на боевом масштабе (152 235
товаров, снимок 1700 позиций): 60 тысяч заданий — 16,3 с на прогон сверки,
180 тысяч — 42,4 с, с индексом — 3,6 с. Зависимость линейная, а задания хранятся
180 суток, то есть таблица только растёт.

Блокировок это не чинит — их тут и нет; оно возвращает часовой сверке запас по
времени. Построение индекса на 180 тысячах строк заняло меньше десятой секунды.
"""
from alembic import op
import sqlalchemy as sa


revision = 'cfa36a1f1f2f'
down_revision = '96775c61f31f'
branch_labels = None
depends_on = None


def upgrade():
    # С проверкой, потому что alembic на SQLite оборванную миграцию не
    # откатывает (см. 96775c61f31f): индекс мог остаться от неудачного прогона,
    # а `CREATE INDEX` по существующему имени падает — и повторить накат было
    # бы нечем.
    existing = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes('ftp_tasks')}
    if 'ix_ftp_tasks_barcode' not in existing:
        op.create_index('ix_ftp_tasks_barcode', 'ftp_tasks', ['barcode'], unique=False)


def downgrade():
    op.drop_index('ix_ftp_tasks_barcode', table_name='ftp_tasks')
