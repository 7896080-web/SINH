"""Убрать задвоенную компенсацию лояльности из отчётов WB, загруженных по API.

Разбор детализации WB прибавлял «Компенсацию скидки по программе лояльности»
(`cashbackDiscount`) к компенсациям, хотя WB уже включил её в «К перечислению»
той же строки. 19.10.2026 на бою итог семи отчётов из двадцати четырёх разошёлся
с «Итого к оплате» WB, у сверенных со сводом — ровно на эту компенсацию
(116,66 / 16 / 79 ₽). Разбор исправлен; здесь — то, что успело записаться.

У строк ПРОДАЖ и ВОЗВРАТОВ отчёта WB из API компенсация бывает только оттуда:
выплата строки ложится в «К перечислению» (`payout`), а единственная другая
прибавка — колонка «Доплаты» — есть лишь в файлах до 2026 года, в API её нет.
Поэтому у таких строк компенсация обнуляется целиком. Строки компенсаций без
продажи (ущерб, брак — выплата строки) и отчёты, загруженные ФАЙЛОМ (там «Доплаты»
законны), не трогаются. В 1С уходит «К перечислению», не компенсация, — документы
в 1С от этого не меняются. Повторный прогон ничего не находит.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-19 01:10:00

"""
import sqlalchemy as sa
from alembic import op

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None

WHERE = ("WHERE operation IN ('sale', 'return') AND compensation_kop != 0 AND report_id IN "
         "(SELECT id FROM sales_reports WHERE platform = 'wb' AND source = 'api')")


def upgrade():
    bind = op.get_bind()
    count, total = bind.execute(sa.text(
        f"SELECT COUNT(*), COALESCE(SUM(compensation_kop), 0) FROM sales_report_lines {WHERE}")).one()
    if not count:
        return
    bind.execute(sa.text(f"UPDATE sales_report_lines SET compensation_kop = 0 {WHERE}"))
    bind.execute(sa.text(
        "INSERT INTO audit_log (at, actor, action, details) VALUES (CURRENT_TIMESTAMP, 'миграция', "
        "'wb_loyalty_comp_cleanup', :d)"),
        {"d": f"убрана задвоенная компенсация лояльности WB: строк {count}, "
              f"{total / 100:.2f} ₽".replace(".", ",")})


def downgrade():
    # Задвоение возвращать незачем.
    pass
