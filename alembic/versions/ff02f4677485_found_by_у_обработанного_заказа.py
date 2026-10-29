"""found_by у обработанного заказа: откуда мы про него узнали

`None` — живой опрос (и ВСЕ строки, заведённые до этой миграции), `recalc` —
расчёт по кнопке, `audit` — суточная сверка заказов с площадками. Бэкфилла нет
и не нужно: умолчание и есть штатный случай, а имя ставят только те, кто
догоняет прошлое.

Колонка нужна, чтобы вообще можно было спросить «сколько заказов живой опрос
теряет в сутки» — до неё этот вопрос ответа не имел, а именно он решает, искать
ли причину.

Revision ID: ff02f4677485
Revises: 2089fc7ac08c
Create Date: 2026-10-29 04:39:39.433702
"""
from alembic import op
import sqlalchemy as sa


revision = 'ff02f4677485'
down_revision = '2089fc7ac08c'
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _has_index(table: str, name: str) -> bool:
    bind = op.get_bind()
    return name in {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def upgrade():
    # Уборка остатков оборванного прогона идёт ДО вопроса «сделано ли уже»:
    # обрыв бывает и ПОСЛЕ перестройки таблицы, но до сдвига версии, и тогда
    # временная таблица осталась бы в базе навсегда, мешая следующей
    # batch-правке этой же таблицы. `DROP TABLE IF EXISTS` идемпотентен, так
    # что делать его всегда дешевле, чем однажды не сделать.
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_processed_orders")

    # БЕЗ `batch_alter_table`, и это не придирка: batch на SQLite перестраивает
    # таблицу целиком через `_alembic_tmp_`, а `ADD COLUMN` и `CREATE INDEX`
    # SQLite умеет и так, обычным ALTER. `processed_orders` — строка на каждый
    # заказ за всю историю, и переливать её при живых службах незачем:
    # alembic на SQLite миграцию НЕ откатывает, то есть обрыв посреди
    # перестройки оставил бы накат без хода вовсе.
    if not _has_column("processed_orders", "found_by"):
        op.add_column("processed_orders",
                      sa.Column("found_by", sa.String(length=16), nullable=True))
    if not _has_index("processed_orders", "ix_processed_orders_found_by"):
        op.create_index("ix_processed_orders_found_by", "processed_orders",
                        ["found_by"], unique=False)


def downgrade():
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_processed_orders")
    if _has_index("processed_orders", "ix_processed_orders_found_by"):
        op.drop_index("ix_processed_orders_found_by", table_name="processed_orders")
    if _has_column("processed_orders", "found_by"):
        # А здесь batch неизбежен: `DROP COLUMN` на старых сборках SQLite
        # делается только перестройкой таблицы.
        with op.batch_alter_table("processed_orders", schema=None) as batch_op:
            batch_op.drop_column("found_by")
