"""dispatch_queue.card_missing — «карточки нет» признаком, а не русским текстом

Revision ID: 3af1c62b7d05
Revises: cfa36a1f1f2f
Create Date: 2026-09-22

Аудит 22.09, блок 4. Отчёт отличал «площадка не знает этот sku» от «рассылка не
доехала» поиском ДВУХ русских подстрок в `last_error`. Совпадал с ними ровно
один текст — WB-шный `409 NotFound`. У Kit слова другие («площадка не знает
такой товар (variant_id …)», «карточка товара в архиве», «нет variant_id:
карточки этого товара нет в каталоге кабинета» — порядок слов не тот), а Ozon
кладёт в `detail` сообщение площадки ПО-АНГЛИЙСКИ.

Всё, что не совпало, попадало в КРИТИЧНУЮ находку «остаток списан, площадка
продаёт то, чего нет» — при том, что продавать там нечего вовсе: карточки на
складе нет. И попадало НАВСЕГДА: запись терминальная (`next_attempt_at=None`),
успешной отправки по паре не будет, снять её нечем ни `latest_queue_ids`, ни
`_not_overtaken_by_a_later_send`, ни `_only_live_pairs`. Отчёт по кабинетам Kit
и Ozon оставался красным вечно, а вечно красный отчёт пролистывают не читая —
и тогда он бесполезен весь.

Признак теперь несут ДАННЫЕ: клиент говорит `card_missing` в ответе по позиции,
рассылка кладёт его в колонку, отчёт её читает. Русские подстроки в `report.py`
остались ТОЛЬКО ради записей, уже лежащих в базе, — их и добирает бэкфилл ниже.
Ozon-овские этим бэкфиллом не поднять (текст английский и произвольный), и это
осознанно: новые записи будут размечены правильно, а старые разбираются как
«рассылка не доехала» — то есть как раньше, не хуже.
"""
from alembic import op
import sqlalchemy as sa


revision = '3af1c62b7d05'
down_revision = 'cfa36a1f1f2f'
branch_labels = None
depends_on = None

MARKS = ("площадка не знает этот sku", "нет карточки в каталоге кабинета")


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade():
    # Каждый необратимый шаг спрашивает, не сделан ли он уже: alembic на SQLite
    # оборванную миграцию НЕ откатывает (проверено 21.09), и повторный
    # `upgrade head` падал бы на «duplicate column name» навсегда — продолжить
    # нечем, повторить нечем, службы не перезапущены.
    bind = op.get_bind()
    if not _has_column(bind, 'dispatch_queue', 'card_missing'):
        op.add_column('dispatch_queue', sa.Column(
            'card_missing', sa.Boolean(), nullable=False, server_default='0'))

    # Бэкфилл идемпотентен по условию: повторный прогон просто не найдёт строк.
    for mark in MARKS:
        bind.execute(sa.text(
            "UPDATE dispatch_queue SET card_missing = 1 "
            "WHERE card_missing = 0 AND last_error LIKE :pat"
        ), {"pat": f"%{mark}%"})


def downgrade():
    op.drop_column('dispatch_queue', 'card_missing')
