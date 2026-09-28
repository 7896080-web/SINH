"""пачка возвратов для массового решения

Черновик массового решения по коробке возвратов: что отсканировали, но ещё не
передали в 1С. Статус вещи запись в пачке не трогает.

Каждый шаг переживает СВОЙ обрыв. Alembic на SQLite миграцию не откатывает
(проверено 21.09): упавшая посередине оставляет сделанное и не двигает версию, а
повторный `alembic upgrade head` падает на «table already exists» НАВСЕГДА —
накат при этом встаёт намертво, уже после копии базы, и службы не перезапущены.

Revision ID: a3cadcda0234
Revises: c4a17e9b2f33
Create Date: 2026-09-28
"""
from alembic import op
import sqlalchemy as sa


revision = 'a3cadcda0234'
down_revision = 'c4a17e9b2f33'
branch_labels = None
depends_on = None

TABLE = "return_batch_entries"
INDEX = "ix_return_batch_entries_added_at"


def _tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade():
    # Остаток оборванного batch-прогона — ДО вопроса «сделано ли уже»: обрыв
    # бывает и после перестройки таблицы, но до сдвига версии, и тогда шаг
    # честно пропускается, а временная таблица остаётся в базе навсегда.
    op.execute(f"DROP TABLE IF EXISTS _alembic_tmp_{TABLE}")

    if TABLE not in _tables():
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("return_id", sa.Integer(), nullable=False),
            sa.Column("added_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["return_id"], ["return_items.id"],
                                    ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("return_id"),
        )

    have = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(TABLE)}
    if INDEX not in have:
        op.create_index(INDEX, TABLE, ["added_at"], unique=False)


def downgrade():
    op.execute(f"DROP TABLE IF EXISTS _alembic_tmp_{TABLE}")
    if TABLE in _tables():
        op.drop_table(TABLE)
