"""Утилизация возврата идёт через 1С: новый статус ожидания.

Что здесь важно.

**Длина колонки.** Статусы лежат как `VARCHAR(N)`, где N — длина самого
длинного значения, и до сих пор это было `back_to_sale` — двенадцать символов.
Новый `awaiting_scrap` длиннее, четырнадцать. SQLite объявленную длину не
проверяет и записал бы значение целиком, то есть на боевом сервере всё
работало бы и так — а схема молча разошлась бы с моделью. Разойдясь однажды,
она выстрелит в день, когда базу переносят на что-нибудь, где длина значит
ровно то, что написана, и статус обрежется до «awaiting_scr»: вещь, ждущая
списания, перестанет читаться как ждущая чего-либо вовсе.

**Перестройка через batch и её остаток.** `return_items` невелика (одна строка
на физическую вещь), но правило общее: обрыв посреди `batch_alter_table`
оставляет `_alembic_tmp_<имя>`, и повторный `alembic upgrade head` падает на
«table … already exists» НАВСЕГДА — продолжить нечем, повторить нечем, службы
не перезапущены. Поэтому каждый шаг спрашивает, не сделан ли он уже.

**Данные не трогаем вовсе.** Ни одной вещи в новом статусе ещё быть не может:
он появляется вместе с этим кодом. А старые `scrapped` остаются как есть —
переводить их в ожидание было бы неправдой, документов по ним в 1С нет и не
будет, и разбирать это надо руками, а не миграцией.
"""
from alembic import op
import sqlalchemy as sa


revision = 'c4a17e9b2f33'
down_revision = 'bd8f521a8957'
branch_labels = None
depends_on = None

# Длина по самому длинному значению: `awaiting_scrap`.
NEW = sa.Enum('accepted', 'cleaning', 'repack', 'held', 'awaiting_1c',
              'awaiting_scrap', 'back_to_sale', 'rejected_1c', 'scrapped',
              name='returnstatus')
OLD = sa.Enum('accepted', 'cleaning', 'repack', 'held', 'awaiting_1c',
              'back_to_sale', 'rejected_1c', 'scrapped', name='returnstatus')


def _has_table(bind, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def _status_width(bind, table: str, column: str) -> int:
    """Объявленная длина колонки статуса, 0 — если таблицы или колонки нет."""
    if not _has_table(bind, table):
        return 0
    for col in sa.inspect(bind).get_columns(table):
        if col["name"] == column:
            return getattr(col["type"], "length", 0) or 0
    return 0


def _drop_tmp(bind, table: str) -> None:
    """Остаток оборванного batch-прогона. Без этого повтор падает навсегда."""
    op.execute(sa.text(f'DROP TABLE IF EXISTS _alembic_tmp_{table}'))


def _widen(bind, table: str, columns: list[tuple[str, bool]], to_type) -> None:
    # Остаток убираем ПЕРВЫМ делом, ДО вопроса «сделано ли уже». Обрыв бывает и
    # ПОСЛЕ того, как batch перестроил таблицу, но до того, как сдвинулась
    # версия: тогда длина уже новая, шаг честно пропускается — и временная
    # таблица остаётся в базе НАВСЕГДА, мешая следующей batch-правке этой же
    # таблицы. Уборка идемпотентна (`IF EXISTS`), так что делать её всегда
    # дешевле, чем однажды не сделать.
    _drop_tmp(bind, table)
    # Вопрос «сделано ли уже» задаём ДЛИНЕ, а не наличию колонки: колонка была
    # и до правки, и повтор после обрыва обязан отличать «расширена» от «нет».
    if all(_status_width(bind, table, name) >= 14 for name, _ in columns):
        return
    with op.batch_alter_table(table, schema=None) as batch_op:
        for name, nullable in columns:
            batch_op.alter_column(name, existing_type=sa.VARCHAR(length=12),
                                  type_=to_type, existing_nullable=nullable)


def upgrade():
    bind = op.get_bind()
    _widen(bind, 'return_items', [('status', False)], NEW)
    _widen(bind, 'return_item_log',
           [('from_status', True), ('to_status', False)], NEW)


def downgrade():
    bind = op.get_bind()
    # Назад сужаем только форму колонки. Строки в `awaiting_scrap` при этом
    # остаются: обрезать их молча значило бы потерять вещь, по которой в 1С
    # прямо сейчас едет задание на списание.
    for table, columns in (('return_items', [('status', False)]),
                           ('return_item_log',
                            [('from_status', True), ('to_status', False)])):
        if not _has_table(bind, table):
            continue
        _drop_tmp(bind, table)
        with op.batch_alter_table(table, schema=None) as batch_op:
            for name, nullable in columns:
                batch_op.alter_column(name, existing_type=sa.VARCHAR(length=14),
                                      type_=OLD, existing_nullable=nullable)
