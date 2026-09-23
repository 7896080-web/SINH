"""products.stock_discrepancy — расхождение учёта со складом как СВОЙСТВО ТОВАРА.

До сих пор расхождение нигде не хранилось: оно выводилось на лету из пары
«остаток ЦС на дату − факт на дату», то есть носителем выступала ДАТА. Отсюда
весь класс бед — сменили дату, факт справедливо стёрся, расхождение исчезло,
порог схлопнулся до брони, и наружу поехал остаток, завышенный ровно на
расхождение. 23.09 так схлопнулись пороги у 62 товаров.

Теперь `порог = расхождение + бронь`, и дата к этому отношения не имеет.

Бэкфилл переносит нынешние пороги ОДИН В ОДИН: `расхождение = порог − бронь`.
Считать его из учёта и факта нельзя — у части строк факт ПОДОБРАН механизмом
удержания (`pin_offset`), и разойдись бэкфилл с текущим порогом хоть на единицу,
на площадки уехало бы другое число. Сохраняем ровно то, что уходит сейчас.

Историю тоже заводим сразу: число появилось не из воздуха, и через месяц надо
будет понимать, откуда оно взялось.

Revision ID: 930c4c4db5e9
Revises: b4e9d1c07a52
"""
from alembic import op
import sqlalchemy as sa

revision = '930c4c4db5e9'
down_revision = 'b4e9d1c07a52'
branch_labels = None
depends_on = None


def _has_column(bind, table: str, name: str) -> bool:
    return any(c["name"] == name for c in sa.inspect(bind).get_columns(table))


def _has_table(bind, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def upgrade():
    # Alembic на SQLite НЕ откатывает упавшую посередине миграцию: накат встаёт
    # намертво, продолжить нечем и повторить нечем. Поэтому каждый необратимый
    # шаг спрашивает, не сделан ли он уже.
    bind = op.get_bind()

    if not _has_table(bind, "stock_discrepancy_log"):
        op.create_table(
            "stock_discrepancy_log",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("uid_1c", sa.String(length=36), nullable=False),
            sa.Column("old_value", sa.Integer(), nullable=True),
            sa.Column("new_value", sa.Integer(), nullable=True),
            sa.Column("source", sa.Enum("fact", "manual", "offset", "reset", "migration",
                                        name="discrepancysource"), nullable=False),
            sa.Column("username", sa.String(length=64), nullable=True),
            sa.Column("base_date", sa.Date(), nullable=True),
            sa.Column("base_stock", sa.Integer(), nullable=True),
            sa.Column("fact", sa.Integer(), nullable=True),
            sa.Column("note", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["uid_1c"], ["products.uid_1c"]),
            sa.PrimaryKeyConstraint("id"),
        )

    existing = {i["name"] for i in sa.inspect(bind).get_indexes("stock_discrepancy_log")}
    if "ix_stock_discrepancy_log_uid_1c" not in existing:
        op.create_index("ix_stock_discrepancy_log_uid_1c",
                        "stock_discrepancy_log", ["uid_1c"])
    if "ix_stock_discrepancy_log_created_at" not in existing:
        op.create_index("ix_stock_discrepancy_log_created_at",
                        "stock_discrepancy_log", ["created_at"])

    if not _has_column(bind, "products", "stock_discrepancy"):
        # Обычным ALTER, а не через batch: batch на SQLite перестраивает таблицу
        # целиком, а в `products` 152 тысячи строк при живых службах.
        op.add_column("products", sa.Column("stock_discrepancy", sa.Integer(),
                                            nullable=True))

    # Бэкфилл идемпотентен по условию `IS NULL`: повтор после обрыва не удвоит
    # ничего и не тронет то, что уже перенесено.
    op.execute(sa.text("""
        UPDATE products
           SET stock_discrepancy = broadcast_offset - COALESCE(reserve, 0)
         WHERE stock_discrepancy IS NULL
           AND broadcast_offset IS NOT NULL
    """))

    # `offset_pinned` осиротел этой же правкой и гасится здесь. Он хранил
    # «порог, который надо удержать, когда 1С ответит на новую дату» — задача,
    # которой больше нет: порог держится на расхождении и смену даты переживает
    # сам. Оставить непустые значения значило бы оставить колонку, которую никто
    # не читает, но которую видно в `probe_offset` и в базе, — то есть ловушку
    # для того, кто через полгода будет разбираться, почему она не действует.
    # Колонку не роняем: `DROP COLUMN` на SQLite перестраивает таблицу в 152
    # тысячи строк при живых службах.
    op.execute(sa.text(
        "UPDATE products SET offset_pinned = NULL WHERE offset_pinned IS NOT NULL"))

    # История — одной вставкой из той же таблицы, а не построчно: на боевом
    # масштабе это полторы тысячи строк, и отдельный запрос на каждую означал бы
    # полторы тысячи обращений под блокировкой записи.
    op.execute(sa.text("""
        INSERT INTO stock_discrepancy_log
              (uid_1c, old_value, new_value, source, username,
               base_date, base_stock, fact, note, created_at)
        SELECT uid_1c, NULL, stock_discrepancy, 'migration', NULL,
               offset_base_date, offset_base_stock, fact_at_date,
               'перенос при появлении колонки: порог − бронь',
               CURRENT_TIMESTAMP
          FROM products
         WHERE stock_discrepancy IS NOT NULL
           AND uid_1c NOT IN (SELECT uid_1c FROM stock_discrepancy_log
                               WHERE source = 'migration')
    """))


def downgrade():
    op.drop_column("products", "stock_discrepancy")
    op.drop_index("ix_stock_discrepancy_log_created_at", "stock_discrepancy_log")
    op.drop_index("ix_stock_discrepancy_log_uid_1c", "stock_discrepancy_log")
    op.drop_table("stock_discrepancy_log")
