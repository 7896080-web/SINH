"""sent_sku и сверка отправленного с площадкой

Три колонки в очереди рассылки:
  * `sent_sku` — каким идентификатором число ушло на площадку. У товара бывает
    несколько баркодов, и выбор делает `dispatch._resolve_push_target` уже в
    момент отправки, поэтому по базе ключ было не восстановить.
  * `verified_at` / `verified_quantity` — что площадка держит по этому sku,
    когда мы спросили ПОСЛЕ отправки. Успешный ответ на отправку не означает,
    что число там осталось: 19.09 на бою в кабинет писала ещё одна система и
    перетирала наши остатки за три минуты.

Все три NULL-able и без server_default — заполняются только новыми отправками,
существующие 114 строк остаются как есть. Данных это не трогает.

ВНИМАНИЕ. Autogenerate добавил сюда ещё и `alter_column` на `ftp_tasks.status`
(VARCHAR(7) → Enum) — УДАЛЁН ВРУЧНУЮ, как и в миграции 35843d7a5cdc. На SQLite
`batch_alter_table` с изменением типа пересобирает таблицу целиком: создаёт
копию, переливает строки, удаляет оригинал. На боевой базе с живыми заданиями в
работе это ничем не оправданный риск ради косметики: значения в колонке и так
совпадают с именами элементов перечисления, и Python читает их правильно.

Revision ID: f0452d7775ba
Revises: 35843d7a5cdc
Create Date: 2026-09-20 10:00:11.595760
"""
from alembic import op
import sqlalchemy as sa


revision = 'f0452d7775ba'
down_revision = '35843d7a5cdc'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('dispatch_queue', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sent_sku', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('verified_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('verified_quantity', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('dispatch_queue', schema=None) as batch_op:
        batch_op.drop_column('verified_quantity')
        batch_op.drop_column('verified_at')
        batch_op.drop_column('sent_sku')
