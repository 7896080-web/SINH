"""stock_as_of: на какой момент склада стоит остаток

Колонка решает одно: СТАРЫЕ ДАННЫЕ НЕ ЗАТИРАЮТ НОВЫЕ. Остаток приходит из 1С
двумя каналами с разной скоростью — минутной дельтой и часовым полным снимком,
— а снимок 1С формирует ЗАРАНЕЕ. 06.10 на бою по `2617 C24-2317CQ BRICK RED`
размер 52: 12:29:39 перемещение увезло 4 штуки (15 -> 11), 12:29:58 дельта
принесла 11, 12:33 часовая выгрузка принесла 15 — её файл сформирован ДО
перемещения. Час на площадку уходило 15 при реальных 11.

NULL у всех существующих строк — это ПРАВИЛЬНО и бэкфилла не требует: «не
знаем, на какой момент», и тогда сверка работает как раньше. Первый же снимок
проставит время сам.

Revision ID: 2089fc7ac08c
Revises: a3cadcda0234
Create Date: 2026-10-06
"""
import sqlalchemy as sa
from alembic import op

revision = '2089fc7ac08c'
down_revision = 'a3cadcda0234'
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade():
    # Уборка остатков оборванного прогона идёт ДО вопроса «сделано ли уже»:
    # обрыв бывает и после перестройки таблицы, но до сдвига версии, и тогда
    # временная таблица осталась бы в базе навсегда, мешая следующей
    # batch-правке этой же таблицы. `DROP TABLE IF EXISTS` идемпотентен.
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_products")

    # ADD COLUMN, а НЕ `batch_alter_table`: автогенератор предложил batch, но он
    # на SQLite ПЕРЕСТРАИВАЕТ таблицу целиком — на боевых 152 тысячах товаров
    # это долгая монопольная блокировка записи при живых службах, то есть
    # `database is locked` у приёма заказов и рассылки. Добавление колонки
    # SQLite делает на месте и мгновенно.
    #
    # Вопрос «не сделано ли уже» обязателен: alembic на SQLite упавшую миграцию
    # НЕ откатывает, и повтор без проверки падал бы на «duplicate column name»
    # навсегда — накат встал бы намертво, уже после копии базы.
    if not _has_column("products", "stock_as_of"):
        op.add_column("products", sa.Column("stock_as_of", sa.DateTime(),
                                            nullable=True))


def downgrade():
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_products")
    if _has_column("products", "stock_as_of"):
        with op.batch_alter_table("products", schema=None) as batch_op:
            batch_op.drop_column("stock_as_of")
