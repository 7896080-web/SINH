"""products.offset_pinned — порог, который надо удержать при сдвиге даты назад.

Дата расчёта служит двум целям сразу: от неё считается порог и от неё же расчёт
поднимает заказы. Оператору, которому нужно догнать продажи за более ранний
период, приходилось двигать дату назад — и он терял порог, полученный СВЕЖИМ
физическим пересчётом склада: факт на прежнюю дату справедливо стирается, а без
факта формула даёт просто бронь.

Колонка нужна только для отложенного случая: снимка 1С на новую дату ещё нет,
подобрать факт не от чего, а к моменту ответа отличить «дата поехала назад» от
«поехала вперёд» будет уже неоткуда.

Revision ID: b4e9d1c07a52
Revises: 9c4d1e7a2f03
"""
from alembic import op
import sqlalchemy as sa

revision = 'b4e9d1c07a52'
down_revision = '9c4d1e7a2f03'
branch_labels = None
depends_on = None


def _has_column(bind, table: str, name: str) -> bool:
    return any(c["name"] == name for c in sa.inspect(bind).get_columns(table))


def upgrade():
    # Alembic на SQLite НЕ откатывает упавшую посередине миграцию: накат встаёт
    # намертво на «duplicate column name», продолжить нечем и повторить нечем.
    bind = op.get_bind()
    if not _has_column(bind, "products", "offset_pinned"):
        op.add_column("products", sa.Column("offset_pinned", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("products", "offset_pinned")
