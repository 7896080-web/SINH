"""массовая актуализация: recalc_jobs, recalc_items, products.recalc_done_at

Задание на массовый пересчёт остатков и отметка «по товару актуализация
пройдена». Только добавления: две новые таблицы и одна nullable-колонка,
существующие данные не трогаются. У всех товаров `recalc_done_at` = NULL, то
есть «актуализация не проводилась», — это честное исходное состояние.

Revision ID: 44ea43d91ca5
Revises: edffb59ad0db
Create Date: 2026-09-18 09:59:11.976946

"""
from alembic import op
import sqlalchemy as sa


revision = '44ea43d91ca5'
down_revision = 'edffb59ad0db'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('recalc_jobs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('pending', 'running', 'done', 'failed', 'cancelled', name='recalcstatus'), nullable=False),
    sa.Column('created_by', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('total', sa.Integer(), nullable=False),
    sa.Column('processed', sa.Integer(), nullable=False),
    sa.Column('orders_applied', sa.Integer(), nullable=False),
    sa.Column('orders_skipped', sa.Integer(), nullable=False),
    sa.Column('failed_items', sa.Integer(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('recalc_jobs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_recalc_jobs_status'), ['status'], unique=False)

    op.create_table('recalc_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('job_id', sa.Integer(), nullable=False),
    sa.Column('uid_1c', sa.String(length=36), nullable=False),
    sa.Column('done', sa.Boolean(), nullable=False),
    sa.Column('orders_applied', sa.Integer(), nullable=False),
    sa.Column('orders_skipped', sa.Integer(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['job_id'], ['recalc_jobs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('recalc_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_recalc_items_done'), ['done'], unique=False)
        batch_op.create_index(batch_op.f('ix_recalc_items_job_id'), ['job_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_recalc_items_uid_1c'), ['uid_1c'], unique=False)

    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(sa.Column('recalc_done_at', sa.DateTime(), nullable=True))



def downgrade():
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_column('recalc_done_at')

    with op.batch_alter_table('recalc_items', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_recalc_items_uid_1c'))
        batch_op.drop_index(batch_op.f('ix_recalc_items_job_id'))
        batch_op.drop_index(batch_op.f('ix_recalc_items_done'))

    op.drop_table('recalc_items')
    with op.batch_alter_table('recalc_jobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_recalc_jobs_status'))

    op.drop_table('recalc_jobs')
