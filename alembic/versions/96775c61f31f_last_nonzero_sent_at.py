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


def upgrade():
    with op.batch_alter_table('sync_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_nonzero_sent_at', sa.DateTime(), nullable=True))

    reasons = ", ".join(f"'{r}'" for r in _WITHDRAWAL_REASONS)
    op.execute(f"""
        UPDATE sync_settings SET last_nonzero_sent_at = (
            SELECT MAX(d.sent_at) FROM dispatch_queue d
            WHERE d.uid_1c = sync_settings.uid_1c
              AND d.account_id = sync_settings.account_id
              AND d.status = 'sent'
              AND d.sent_at IS NOT NULL
              AND d.is_test = 0
              AND (d.sent_quantity > 0
                   OR (d.sent_quantity IS NULL AND d.reason NOT IN ({reasons})))
        )
        WHERE last_nonzero_sent_at IS NULL
    """)

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
