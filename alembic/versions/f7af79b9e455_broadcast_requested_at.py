"""products.broadcast_requested_at — отложенная просьба включить трансляцию

Ставится импортом Excel, когда в файле «Трансляция = Да», а расчёт по строке ещё
не закончен. Срабатывает сама, когда расчёт поставит отметку и ворота откроются.

Автогенерация просилась заодно переписать `ftp_tasks.status` (VARCHAR(7) →
Enum): это фантом — колонка и так хранит те же строки, а `batch_alter_table` на
SQLite пересоздаёт таблицу целиком. На боевой базе там живая очередь заданий 1С,
и переливать её ради ничего нельзя. Оставлено только добавление колонки.

Revision ID: f7af79b9e455
Revises: da5512561de5
Create Date: 2026-09-20 21:41:10.403788
"""
from alembic import op
import sqlalchemy as sa


revision = 'f7af79b9e455'
down_revision = 'da5512561de5'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(sa.Column('broadcast_requested_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_column('broadcast_requested_at')
