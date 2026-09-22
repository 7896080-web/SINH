"""alert_state — память о том, что система в последний раз сказала наружу

Revision ID: 7b2e9c04a1d8
Revises: 3af1c62b7d05
Create Date: 2026-09-22

Аудит 22.09 назвал главной слабостью то, что уведомлений наружу нет вообще:
внутри система видит про себя всё, а узнать об этом может только тот, кто придёт
и откроет страницу. Упади обе службы ночью — до утра не узнал бы никто.

Таблица нужна не для отправки, а для того, чтобы уведомлению ВЕРИЛИ. Она хранит
отпечаток последней сообщённой картины и время: пока картина та же, повторяться
незачем (пять одинаковых сообщений в час — вернейший способ добиться, чтобы
канал отключили), а когда всё прошло — отбой уходит ровно один раз и только
если до него была тревога.

Строка одна на систему. Пустая таблица — штатное состояние свежей установки:
первый же прогон заведёт её сам.
"""
from alembic import op
import sqlalchemy as sa


revision = '7b2e9c04a1d8'
down_revision = '3af1c62b7d05'
branch_labels = None
depends_on = None


def upgrade():
    # С проверкой: alembic на SQLite оборванную миграцию НЕ откатывает, и
    # повторный `upgrade head` падал бы на «table already exists» навсегда —
    # продолжить нечем, повторить нечем, службы не перезапущены.
    if 'alert_state' not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            'alert_state',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('key', sa.String(length=64), nullable=False),
            sa.Column('signature', sa.Text(), nullable=True),
            sa.Column('level', sa.String(length=16), nullable=True),
            sa.Column('last_sent_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('key', name='uq_alert_state_key'),
        )


def downgrade():
    op.drop_table('alert_state')
