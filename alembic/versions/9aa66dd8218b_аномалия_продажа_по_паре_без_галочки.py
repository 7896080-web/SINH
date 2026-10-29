"""аномалия: продажа по паре без галочки кабинета

`AnomalyReason.order_on_unmanaged` — восемнадцать символов, а колонка стояла
`VARCHAR(17)` по длине прежнего максимума (`order_on_disabled`). SQLite
объявленную длину НЕ проверяет, то есть на бою записалось бы и так, а схема
молча разошлась бы с моделью — и выстрелило бы это при переносе базы туда, где
длина значит ровно то, что написана. Правило записано в `CLAUDE.md`: заводишь
значение длиннее прежних — расширяй колонку миграцией.

Revision ID: 9aa66dd8218b
Revises: ff02f4677485
Create Date: 2026-10-29 18:56:01.887815
"""
from alembic import op
import sqlalchemy as sa

revision = '9aa66dd8218b'
down_revision = 'ff02f4677485'
branch_labels = None
depends_on = None

REASONS = ('order_on_disabled', 'missing_barcode', 'order_on_unmanaged')


def _reason_width(default: int = 0) -> int:
    """Объявленная длина `sync_anomalies.reason`, или 0 — если не определить."""
    bind = op.get_bind()
    for column in sa.inspect(bind).get_columns("sync_anomalies"):
        if column["name"] != "reason":
            continue
        return getattr(column["type"], "length", None) or default
    return default


def upgrade():
    # Уборка остатков оборванного прогона идёт ДО вопроса «сделано ли уже»:
    # обрыв бывает и ПОСЛЕ перестройки таблицы, но до сдвига версии, и тогда
    # временная таблица осталась бы в базе навсегда, мешая следующей
    # batch-правке этой же таблицы. `DROP TABLE IF EXISTS` идемпотентен.
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_sync_anomalies")

    # `batch_alter_table` на SQLite перестраивает таблицу целиком, а аномалии
    # копятся десятками в сутки и в статусе `new` не чистятся НИКОГДА — то
    # есть строк там много, и переливаются они при живых службах. Повторять
    # это зря нельзя: alembic на SQLite миграцию не откатывает, и обрыв
    # посреди перестройки оставил бы накат без хода вовсе. Поэтому спрашиваем,
    # не расширена ли колонка уже.
    if _reason_width() >= max(len(r) for r in REASONS):
        return
    with op.batch_alter_table("sync_anomalies", schema=None) as batch_op:
        batch_op.alter_column(
            "reason",
            existing_type=sa.VARCHAR(length=17),
            type_=sa.Enum(*REASONS, name="anomalyreason"),
            existing_nullable=False)


def downgrade():
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_sync_anomalies")
    # Назад — только если в таблице не осталось значений, которые в прежнюю
    # длину не влезают: откат, молча обрезающий причину аномалии, превратил бы
    # незаконченное дело в строку, которую не прочитает ни страница, ни отчёт.
    bind = op.get_bind()
    stuck = bind.execute(sa.text(
        "SELECT COUNT(*) FROM sync_anomalies WHERE LENGTH(reason) > 17")).scalar()
    if stuck:
        raise RuntimeError(
            f"в sync_anomalies есть {stuck} строк с причиной длиннее 17 "
            f"символов — сначала разберите их на странице «Аномалии»")
    with op.batch_alter_table("sync_anomalies", schema=None) as batch_op:
        batch_op.alter_column(
            "reason",
            existing_type=sa.Enum(*REASONS, name="anomalyreason"),
            type_=sa.VARCHAR(length=17),
            existing_nullable=False)
