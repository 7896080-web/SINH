"""Справочник артикулов Lamoda (артикул → EAN) из партнёрского отчёта Lamoda.

С 14.09.2026 Lamoda присылает на ту же почту, что и недельный отчёт, ещё и
партнёрский отчёт («Lamoda Sales Report - AWER ГГГГ-ММ-ДД.xlsx»). Программа его
не знала: каждое письмо легло в `mail_messages` со статусом «failed» и словами
«Отчёт не опознан», и больше не перечитывается — письмо считается разобранным.
Здесь такие записи (и только они) удаляются: при ближайшей проверке почты письма
перечитаются, и справочник наполнится из них сам. Отказы по любым другим
причинам не трогаются.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-18 21:30:00

"""
import sqlalchemy as sa
from alembic import op

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None

INDEX = 'ix_lamoda_articles_short_sku'


def _has_table() -> bool:
    return 'lamoda_articles' in sa.inspect(op.get_bind()).get_table_names()


def upgrade():
    # Оборванный прогон на SQLite alembic не откатывает: повтор не должен падать.
    if not _has_table():
        op.create_table(
            'lamoda_articles',
            sa.Column('lamoda_sku', sa.String(length=64), nullable=False),
            sa.Column('short_sku', sa.String(length=32), nullable=False),
            sa.Column('partner_size', sa.String(length=32), nullable=False),
            sa.Column('lamoda_size', sa.String(length=32), nullable=True),
            sa.Column('ean', sa.String(length=64), nullable=True),
            sa.Column('partner_article', sa.String(length=255), nullable=True),
            sa.Column('report_date', sa.Date(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('lamoda_sku'),
        )
    if not any(i["name"] == INDEX for i in sa.inspect(op.get_bind()).get_indexes('lamoda_articles')):
        op.create_index(INDEX, 'lamoda_articles', ['short_sku'], unique=False)

    bind = op.get_bind()
    requeued = bind.execute(sa.text(
        "DELETE FROM mail_messages WHERE status = 'failed' AND note LIKE :unknown "
        "AND note LIKE :short AND note LIKE :ean"),
        {"unknown": "%Отчёт не опознан%", "short": "%Короткий артикул Lamoda%",
         "ean": "%EAN (штрихкод)%"}).rowcount
    if requeued:
        bind.execute(sa.text(
            "INSERT INTO audit_log (at, actor, action, details) VALUES (CURRENT_TIMESTAMP, 'миграция', "
            "'mail_requeue', :d)"),
            {"d": f"письма с партнёрским отчётом Lamoda перечитаются при следующей проверке почты: {requeued}"})


def downgrade():
    if _has_table():
        op.drop_index(INDEX, table_name='lamoda_articles')
        op.drop_table('lamoda_articles')
