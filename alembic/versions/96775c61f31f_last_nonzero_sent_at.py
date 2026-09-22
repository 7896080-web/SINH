"""Память об отправке на пару товар+кабинет + выравнивание типа ftp_tasks.status

Revision ID: 96775c61f31f
Revises: 5c7ec8206dc3
Create Date: 2026-09-21

Две правки, обе из аудита 21.09.

1. `sync_settings.last_nonzero_sent_at` — когда на этот кабинет в последний раз
   успешно ушёл НЕПУСТОЙ остаток. Раньше единственным доказательством отправки
   были строки `dispatch_queue`, а суточная чистка удаляет терминальные записи
   старше тридцати суток. У медленного размера остаток не меняется месяцами:
   последняя строка `sent` исчезала, `ever_transmitted` начинала отвечать «не
   отправляли», и снятие галочки переставало отзывать остаток. На площадке
   оставалось наше число, она продолжала продавать, а заказы по снятой паре
   живой опрос уже пропускает — ни списания у нас, ни документа в 1С. Оверселл.

   Колонку ОБЯЗАТЕЛЬНО заполнить сразу, а не «впредь». Отметку пишет только
   новая успешная отправка непустого остатка, а рассылка событийная: у медленного
   размера остаток не менялся месяцами, и новой отправки может не случиться
   вовсе. При этом `job_retention` стартует через девять минут после запуска
   воркера — то есть в первый же прогон после наката, раньше любой отправки, — и
   унесёт из очереди всё, что старше тридцати суток. Без бэкфилла правка чинила
   бы дефект только для пар, отправленных после наката, а для остальных он бы
   как раз в этот момент и сработал.

   Условие бэкфилла — ровно то же, что в `transmit.ever_transmitted`: непустая
   отправка с проставленным `sent_at`, тестовые записи не в счёт, а записи без
   `sent_quantity` (они старше этой колонки) судим по причине — отзывы отправками
   не считаются. Разойдись бэкфилл с функцией, он обещал бы не то, что она
   считает.

2. `ftp_tasks.status` объявлен в моделях как Enum, а в базе лежит VARCHAR(7) —
   колонку не расширили, когда в перечисление добавили `no_document` (11
   символов). SQLite длину не проверяет, поэтому на бою это ничего не ломало, но
   `alembic revision --autogenerate` предлагал эту перестройку КАЖДЫЙ раз, и
   очередная правка модели тащила бы её за собой не глядя. Выравниваем здесь,
   осознанно и отдельной строкой.

   Порядок важен: сначала дешёвое добавление колонки, потом перестройка таблицы.
   `batch_alter_table` на SQLite пересоздаёт таблицу целиком, и если что-то
   пойдёт не так, новая колонка уже на месте.
"""
from alembic import op
import sqlalchemy as sa


revision = '96775c61f31f'
down_revision = '5c7ec8206dc3'
branch_labels = None
depends_on = None

_FTP_STATUS = sa.Enum('pending', 'sent', 'done', 'failed', 'timeout', 'no_document',
                      name='ftptaskstatus')


# Те же причины, что в `transmit.WITHDRAWAL_REASONS`: по ним уходил ноль, а не
# остаток, — отправками они не считаются.
_WITHDRAWAL_REASONS = ("manual_disable", "broadcast_off")


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    # Каждый шаг здесь переживает ПОВТОРНЫЙ запуск, и это не перестраховка.
    # Alembic на SQLite миграцию не откатывает: проверено 21.09 — оборванная
    # посередине оставила и новую колонку, и временную таблицу, а версию в базе
    # не сдвинула. Повторный `alembic upgrade head` падал на «duplicate column
    # name», то есть накат вставал намертво: продолжить нельзя, повторить
    # нельзя, службы не перезапущены. Оборваться посередине эта миграция может
    # от чего угодно — кончилось место, `database is locked` от живых служб,
    # закрытая консоль.
    if not _has_column('sync_settings', 'last_nonzero_sent_at'):
        with op.batch_alter_table('sync_settings', schema=None) as batch_op:
            batch_op.add_column(sa.Column('last_nonzero_sent_at', sa.DateTime(), nullable=True))

    reasons = ", ".join(f"'{r}'" for r in _WITHDRAWAL_REASONS)

    # Бэкфилл идёт АГРЕГАТОМ, а не подзапросом на строку, и это не про красоту.
    # Коррелированный `SELECT MAX(...)` на каждую из 16 132 настроек SQLite
    # выполняет через индекс по СТАТУСУ (`ix_dispatch_queue_status`), а не по
    # паре: без `ANALYZE` оптимизатор считает их равноценными, а статус у всех
    # интересных строк один и тот же — то есть на каждую настройку читается вся
    # очередь целиком. Замер на боевом масштабе (16 132 настройки, 50 000 строк
    # очереди): 134,9 с, и всё это время база держит эксклюзивную блокировку
    # записи. `busy_timeout` у служб — 30 с, значит приём заказов, рассылка и
    # веб за это время получили бы `database is locked`, нигде не перехваченный.
    # Один проход с `GROUP BY` даёт ту же таблицу за один скан очереди, а поиск
    # по ней идёт по первичному ключу пары.
    op.execute("DROP TABLE IF EXISTS _backfill_nonzero_sent")
    op.execute("""
        CREATE TABLE _backfill_nonzero_sent (
            uid_1c VARCHAR(100) NOT NULL,
            account_id INTEGER NOT NULL,
            sent_at DATETIME NOT NULL,
            PRIMARY KEY (uid_1c, account_id)
        )
    """)
    op.execute(f"""
        INSERT INTO _backfill_nonzero_sent (uid_1c, account_id, sent_at)
        SELECT d.uid_1c, d.account_id, MAX(d.sent_at)
        FROM dispatch_queue d
        WHERE d.status = 'sent'
          AND d.sent_at IS NOT NULL
          AND d.is_test = 0
          AND (d.sent_quantity > 0
               OR (d.sent_quantity IS NULL AND d.reason NOT IN ({reasons})))
        GROUP BY d.uid_1c, d.account_id
    """)
    op.execute("""
        UPDATE sync_settings SET last_nonzero_sent_at = (
            SELECT b.sent_at FROM _backfill_nonzero_sent b
            WHERE b.uid_1c = sync_settings.uid_1c
              AND b.account_id = sync_settings.account_id
        )
        WHERE last_nonzero_sent_at IS NULL
          AND EXISTS (
            SELECT 1 FROM _backfill_nonzero_sent b
            WHERE b.uid_1c = sync_settings.uid_1c
              AND b.account_id = sync_settings.account_id
          )
    """)
    op.execute("DROP TABLE _backfill_nonzero_sent")

    # Самый долгий шаг миграции и единственный, что оставался без защиты от
    # собственного обрыва. `batch_alter_table` на SQLite перестраивает таблицу
    # целиком: CREATE `_alembic_tmp_ftp_tasks` → INSERT ... SELECT (на бою это
    # десятки тысяч строк заданий 1С при живых службах и `busy_timeout` 30 с) →
    # DROP → RENAME. Alembic на SQLite ничего не откатывает и версию не двигает,
    # поэтому обрыв здесь оставлял временную таблицу, и повторный
    # `alembic upgrade head` падал на «table _alembic_tmp_ftp_tasks already
    # exists» — НАВСЕГДА. Накат вставал на середине: продолжить нечем, повторить
    # нечем, службы не перезапущены, бой на старом коде при новых файлах.
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_ftp_tasks")

    with op.batch_alter_table('ftp_tasks', schema=None) as batch_op:
        batch_op.alter_column('status',
                              existing_type=sa.VARCHAR(length=7),
                              type_=_FTP_STATUS,
                              existing_nullable=False)


def downgrade():
    with op.batch_alter_table('ftp_tasks', schema=None) as batch_op:
        batch_op.alter_column('status',
                              existing_type=_FTP_STATUS,
                              type_=sa.VARCHAR(length=7),
                              existing_nullable=False)

    with op.batch_alter_table('sync_settings', schema=None) as batch_op:
        batch_op.drop_column('last_nonzero_sent_at')
