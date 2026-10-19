"""Разбор отчётов площадок в единую форму.

Главное здесь — деньги: итог строк должен совпадать с тем, что площадка
переведёт на счёт, по её же формуле. Ошибка знака у возврата или лишняя
вычитаемая колонка не роняют импорт — они тихо искажают прибыль и сверку,
поэтому каждая такая ловушка закреплена отдельным тестом.
"""
from datetime import date
from decimal import Decimal

import pytest

from finapp.finance import values as v
from finapp.finance.formats import (KIT_FORMAT, KIT_PAY_FORMAT, LAMODA_FORMAT, UNIFIED_FORMAT,
                                 WB_FORMAT, classify_wb, parse_sheets)
from finapp.finance.readers import ReportReadError, find_header, normalize_header, read_sheets
from tests.finance_factories import WB_HEADERS, make_xlsx, wb_file, wb_row, zip_bytes


def _parse(data: bytes, platform: str, filename: str = "report.xlsx"):
    return parse_sheets(read_sheets(data, filename), platform)


def _total(result) -> int:
    return sum(v.line_total(line) for line in result.lines)


# ------------------------------------------------------------ значения

@pytest.mark.parametrize("raw,kop", [
    (1234.56, 123456), ("1 234,56", 123456), ("1\u00a0234,56 ₽", 123456), ("−150", -15000),
    ("1,234.56", 123456), ("1.234,56", 123456), (0.1, 10), ("(5)", -500), (Decimal("2.005"), 201),
    (7, 700), ("57,6", 5760), ("-34,56", -3456),
])
def test_parse_money(raw, kop):
    assert v.parse_money(raw) == kop


@pytest.mark.parametrize("raw", [None, "", "  ", "-", "—"])
def test_blank_money_is_none(raw):
    assert v.parse_money(raw) is None


@pytest.mark.parametrize("raw", ["abc", "12,5,0", True, "1e999", "2000000000012000"])
def test_garbage_money_is_an_error_not_zero(raw):
    """Тихий ноль вместо суммы — неверная прибыль, которую никто не заметит."""
    with pytest.raises(v.ValueParseError):
        v.parse_money(raw)


def test_parse_date_variants():
    assert v.parse_date("2026-09-03") == date(2026, 9, 3)
    assert v.parse_date("03.09.2026") == date(2026, 9, 3)
    assert v.parse_date("2024-07-23T19:59:41+03:00") == date(2024, 7, 23)
    assert v.parse_date("2026-09-03T10:00:00.123456789Z") == date(2026, 9, 3)
    assert v.parse_date(46268) == date(2026, 9, 3)      # серийный номер Excel
    with pytest.raises(v.ValueParseError):
        v.parse_date("вчера")


def test_quantity_must_be_whole():
    assert v.parse_quantity("2") == 2
    assert v.parse_quantity(2.0) == 2
    with pytest.raises(v.ValueParseError):
        v.parse_quantity("1,5")


def test_barcode_stored_as_number_keeps_its_digits():
    assert v.parse_text(2000000000012.0) == "2000000000012"


def test_format_rub():
    assert v.format_rub(123456) == "1\u00a0234,56"
    assert v.format_rub(-5) == "−0,05"
    assert v.format_rub_short(123456) == "1\u00a0235\u00a0₽"


def test_header_normalization_ignores_case_yo_spaces_and_units():
    assert normalize_header(" Платная  приёмка ") == "платная приемка"
    assert normalize_header("Цена реализации, руб") == "цена реализации"
    assert normalize_header("Комиссия (₽)") == "комиссия"


# ------------------------------------------------------------ чтение файлов

def test_wb_zip_with_xlsx_inside():
    data = zip_bytes("Детализация.xlsx", wb_file([wb_row()]))
    result = _parse(data, "wb", "report.zip")
    assert result.format is WB_FORMAT
    assert len(result.lines) == 1


def test_zip_with_two_tables_is_refused():
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.xlsx", wb_file([wb_row()]))
        zf.writestr("b.xlsx", wb_file([wb_row()]))
    with pytest.raises(ReportReadError):
        read_sheets(buf.getvalue(), "x.zip")


def test_old_xls_is_refused_with_explanation():
    with pytest.raises(ReportReadError, match="xlsx"):
        read_sheets(b"\xd0\xcf\x11\xe0" + b"\x00" * 100, "report.xls")


def test_csv_in_cp1251_with_semicolons():
    text = ("Заказ;Штрих-код партнера (EAN);Количество;Согласованная с комитентом цена, руб. вкл. НДС;"
            "К перечислению комитенту, руб. вкл. НДС;Дата продажи\n"
            "RU1;2000000000012;1;1000,00;600,00;05.09.2026\n")
    result = _parse(text.encode("cp1251"), "lamoda", "r.csv")
    assert result.format is LAMODA_FORMAT
    assert result.lines[0]["revenue"] == 100000
    assert result.lines[0]["payout"] == 60000


def test_header_found_below_title_rows():
    rows = [["Отчёт о реализации"], ["Период: с 01.09.2026 по 07.09.2026"], [],
            ["Заказ", "Штрих-код партнера (EAN)", "Количество", "Цена реализации",
             "К перечислению комитенту"]]
    found = find_header(rows, LAMODA_FORMAT.aliases, LAMODA_FORMAT.required,
                        LAMODA_FORMAT.required_any, 4)
    assert found is not None and found[0] == 3


def test_xlsx_without_sheet_dimensions_is_read_in_full():
    """Детализация WB приходит с <dimension ref="A1"/>: в режиме read_only без
    сброса размеров openpyxl отдавал одну колонку из 88 — отчёт «не опознавался»."""
    import io
    import re
    import zipfile
    data = wb_file([wb_row()])
    src, out = zipfile.ZipFile(io.BytesIO(data)), io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            body = src.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                body = re.sub(rb'<dimension ref="[^"]+"\s*/>', b'<dimension ref="A1"/>', body)
            dst.writestr(item, body)
    sheets = read_sheets(out.getvalue(), "r.xlsx")
    assert len(sheets[0][1][0]) == len(WB_HEADERS)
    assert _parse(out.getvalue(), "wb").format is not None


# ------------------------------------------------------------ Wildberries

def test_wb_sale_return_and_expenses_follow_wb_formula():
    """Итого к оплате (справка WB) = Σforpay[Продажа] − Σforpay[Возврат] − доставка
    − хранение − обработка − штрафы − удержания."""
    rows = [
        wb_row(),                                                         # продажа 2500/2100
        wb_row(**{"Тип документа": "Возврат", "Обоснование для оплаты": "Возврат"}),
        wb_row(**{"Тип документа": "", "Обоснование для оплаты": "Доставка", "Кол-во": 0,
                  "Вайлдберриз реализовал Товар (Пр)": 0,
                  "К перечислению Продавцу за реализованный Товар": 0,
                  "Услуги по доставке товара покупателю": "150,50"}),
        wb_row(**{"Тип документа": "", "Обоснование для оплаты": "Хранение", "Кол-во": 0,
                  "Вайлдберриз реализовал Товар (Пр)": 0, "Баркод": "",
                  "К перечислению Продавцу за реализованный Товар": 0, "Хранение": 300}),
        wb_row(**{"Тип документа": "", "Обоснование для оплаты": "Штраф", "Кол-во": 0,
                  "Вайлдберриз реализовал Товар (Пр)": 0,
                  "К перечислению Продавцу за реализованный Товар": 0, "Общая сумма штрафов": 100}),
        wb_row(**{"Тип документа": "", "Обоснование для оплаты": "Услуга по обработке товара",
                  "Кол-во": 0, "Вайлдберриз реализовал Товар (Пр)": 0,
                  "К перечислению Продавцу за реализованный Товар": 0,
                  "Услуга по обработке товара": 50}),
    ]
    result = _parse(wb_file(rows), "wb")
    assert result.errors == []
    ops = [line["operation"] for line in result.lines]
    assert ops == [v.SALE, v.RETURN, v.LOGISTICS, v.STORAGE, v.PENALTY, v.ACCEPTANCE]

    sale, ret = result.lines[0], result.lines[1]
    assert (sale["revenue"], sale["payout"], sale["commission"]) == (250000, 210000, 40000)
    # Суммы возврата в детализации положительные — минус ставит «Тип документа».
    assert (ret["revenue"], ret["payout"], ret["commission"]) == (-250000, -210000, -40000)
    assert ret["quantity"] == 1
    assert _total(result) == 210000 - 210000 - 15050 - 30000 - 10000 - 5000


def test_wb_rebill_logistic_cost_is_not_deducted():
    """«Возмещение издержек по перевозке…» по справке WB не влияет на доход
    продавца — вычитание дало бы итог меньше реальной выплаты."""
    row = wb_row(**{
        "Тип документа": "", "Кол-во": 2,
        "Обоснование для оплаты": "Возмещение издержек по перевозке/по складским операциям с товаром",
        "Вайлдберриз реализовал Товар (Пр)": 0, "К перечислению Продавцу за реализованный Товар": 0,
        "Возмещение издержек по перевозке/по складским операциям с товаром": 4000})
    result = _parse(wb_file([row]), "wb")
    assert _total(result) == 0


def test_wb_correction_of_reward_is_a_withholding():
    """«Корректировка ВВ»: удержание без знака, выплата продавцу со знаком минус."""
    held = wb_row(**{"Корректировка Вознаграждения Вайлдберриз (ВВ)": 120})
    paid = wb_row(**{"Корректировка Вознаграждения Вайлдберриз (ВВ)": -80})
    result = _parse(wb_file([held, paid]), "wb")
    assert result.lines[0]["deduction"] == 12000
    assert result.lines[1]["deduction"] == -8000
    assert _total(result) == 2 * 210000 - 12000 + 8000


def test_wb_old_additional_payment_column_is_income():
    """До 2026 года колонка называлась «Доплаты» и была выплатой продавцу."""
    headers = [h if h != "Корректировка Вознаграждения Вайлдберриз (ВВ)" else "Доплаты"
               for h in WB_HEADERS]
    result = _parse(make_xlsx(headers, [wb_row(**{
        "Корректировка Вознаграждения Вайлдберриз (ВВ)": 70})]), "wb")
    assert result.lines[0]["compensation"] == 7000


def test_wb_loyalty_columns():
    headers = WB_HEADERS + ["Стоимость участия в программе лояльности",
                            "Сумма, удержанная за начисленные баллы программы лояльности",
                            "Компенсация скидки по программе лояльности"]
    sale = wb_row() + [30, 20, 10]
    ret = wb_row(**{"Тип документа": "Возврат", "Обоснование для оплаты": "Возврат"}) + [30, 20, 10]
    result = _parse(make_xlsx(headers, [sale, ret]), "wb")
    assert result.lines[0]["deduction"] == 5000          # (30 + 20) по продаже
    assert result.lines[1]["deduction"] == -5000         # по возврату — со знаком минус
    # Компенсация лояльности уже внутри «К перечислению» — второй раз её не прибавляем.
    assert result.lines[0]["compensation"] == 0
    assert result.lines[0]["payout"] == 210000


def test_wb_compensation_without_sale_is_income_signed_by_document():
    paid = wb_row(**{"Обоснование для оплаты": "Компенсация ущерба", "Тип документа": "Продажа",
                     "К перечислению Продавцу за реализованный Товар": 1800})
    taken = wb_row(**{"Обоснование для оплаты": "Добровольная компенсация при возврате",
                      "Тип документа": "Возврат",
                      "К перечислению Продавцу за реализованный Товар": 300})
    result = _parse(wb_file([paid, taken]), "wb")
    assert [l["operation"] for l in result.lines] == [v.COMPENSATION, v.COMPENSATION]
    assert result.lines[0]["compensation"] == 180000
    assert result.lines[1]["compensation"] == -30000
    assert result.lines[0]["revenue"] == 0                # это не продажа товара


@pytest.mark.parametrize("oper,doc,expected", [
    ("Сторно продаж", "Возврат", v.RETURN),
    ("Сторно возвратов", "Продажа", v.SALE),
    ("Корректная продажа", "Продажа", v.SALE),
    ("Коррекция возвратов", "Возврат", v.RETURN),
    ("Доставка", "", v.LOGISTICS),
    ("Логистика", "", v.LOGISTICS),
    ("Сторно логистики", "", v.LOGISTICS),
    ("Пересчет хранения", "", v.STORAGE),
    ("Платная приемка", "", v.ACCEPTANCE),
    ("Возмещение издержек по перемещению и операционной обработке товара", "", v.LOGISTICS),
    ("Штраф МП", "", v.PENALTY),
    ("Удержания", "", v.DEDUCTION),
    ("Стоимость участия в программе лояльности", "", v.DEDUCTION),
    ("Компенсация скидки по программе лояльности", "", v.COMPENSATION),
    ("Оплата брака", "Продажа", v.COMPENSATION),
    ("Бронирование товара через самовывоз", "", v.OTHER),
])
def test_wb_operation_classification(oper, doc, expected):
    assert classify_wb(oper, doc) == expected


def test_wb_name_prefers_title_over_subject():
    """«Предмет» (категория) стоит левее «Названия», но наименованием товара
    должно стать «Название»."""
    result = _parse(wb_file([wb_row()]), "wb")
    assert result.lines[0]["name"] == "Джинсы прямые"


def test_wb_order_id_is_assembly_task_and_zero_means_fbo():
    result = _parse(wb_file([wb_row(**{"Номер сборочного задания": 555}), wb_row()]), "wb")
    assert result.lines[0]["order_id"] == "555"
    assert result.lines[1]["order_id"] == ""


def test_wb_api_field_names_are_understood():
    """Поля API finance-api — чтобы будущая загрузка по API легла в этот же разбор."""
    headers = ["sellerOperName", "docTypeName", "saleDt", "quantity", "retailAmount", "forPay",
               "deliveryService", "sku", "vendorCode", "orderId", "reportId"]
    rows = [["Продажа", "Продажа", "2026-09-03", 1, "2500.00", "2100.00", "0", "200", "A-1",
             "777", "999"]]
    result = _parse(make_xlsx(headers, rows), "wb")
    assert result.format is WB_FORMAT
    assert result.lines[0]["payout"] == 210000
    assert result.lines[0]["barcode"] == "200"
    assert result.report_numbers == {"999"}


def test_wb_bad_money_cell_is_reported_with_row_and_column():
    result = _parse(wb_file([wb_row(), wb_row(**{"Хранение": "много"})]), "wb")
    assert result.errors_total == 1
    assert "строка 3" in result.errors[0] and "Хранение" in result.errors[0]


# ------------------------------------------------------------ Lamoda
#
# Раскладка — как в настоящем отчёте комиссионера № CR000179664 за сентябрь
# 2026 (заголовки слово в слово, с переводами строк внутри, как в файле).

LAMODA_HEADERS = ["Заказ", "Дата заказа", "Артикул", "SellerSKU", "Штрих-код партнера (EAN)",
                  "Штрих-код", "Метод доставки", "Количество", "Цена реализации, руб. вкл. НДС",
                  "Согласованная с комитентом цена, руб. вкл. НДС",
                  "К перечислению комитенту, \nруб. вкл. НДС",
                  "Комиссия Купишуз от Согласованной цены, %", "Комиссия Купишуз,\n руб. вкл. НДС",
                  "НДС с ин. компаний, руб", "Расходы по программе лояльности, руб", "Дата продажи",
                  "Номер заказа в учете комитента", "Uit"]
LAMODA_TITLE = [["Отчет комиссионера № CR000179664 от 30.09.2026 по договору комиссии от 29.12.2021"],
                ["Период:", "С 01.09.2026 по 30.09.2026"],
                ["Комиссионер:", "Общество с ограниченной ответственностью \"Купишуз\""], [], [], []]


def _lamoda_row(order="RU260920-1", qty=1, price=18670, agreed=18900, payout=10962, commission=7938,
                vat=0, loyalty=230, sold="29.09.2026", ean="2000932310718", sku="C23-2251C Авер Куртка  48 черный"):
    return [order, "20.09.2026", "MP002XM05TLKINM", sku, ean, "SL112259246", "XDC", qty, price, agreed,
            payout, 42, commission, vat, loyalty, sold, None, "0104630688313435215PGCYcL"]


def _lamoda_file(rows, total=None, footer=False):
    body = list(rows)
    if total is not None:
        body.append([None] * 6 + ["Итого:", None, None, None, total] + [None] * 7)
    if footer:
        body += [[], [None, "ИСПОЛНИТЕЛЬ"] + [None] * 8 + ["ЗАКАЗЧИК"]]
    return make_xlsx(LAMODA_HEADERS, body, title_rows=LAMODA_TITLE)


def test_lamoda_commission_report_real_layout():
    """Выручка — согласованная цена (скидку покупателю по лояльности оплачивает
    Lamoda), выплата — «К перечислению комитенту», комиссия — разница; возврат —
    минус в количестве и суммах; номер и период — из шапки."""
    sale = _lamoda_row()
    ret = _lamoda_row(order="RU260916-2", qty=-1, price=-18670, agreed=-18900, payout=-10962,
                      commission=-7938, loyalty=-230)
    result = _parse(_lamoda_file([sale, ret], total=0), "lamoda")
    assert result.format is LAMODA_FORMAT
    assert result.errors == []
    s, r = result.lines
    assert s["operation"] == v.SALE and r["operation"] == v.RETURN
    assert (s["revenue"], s["payout"], s["commission"]) == (1890000, 1096200, 793800)
    assert (r["revenue"], r["payout"], r["quantity"]) == (-1890000, -1096200, 1)
    # Наш баркод — «Штрих-код партнера (EAN)»; «Штрих-код» — внутренний код Lamoda.
    assert (s["barcode"], s["srid"]) == ("2000932310718", "SL112259246")
    assert (s["platform_sku"], s["article"]) == ("MP002XM05TLKINM", "C23-2251C")
    assert result.period == (date(2026, 9, 1), date(2026, 9, 30))
    assert result.report_numbers == {"CR000179664"}
    assert result.declared_payout == 0


def test_lamoda_foreign_vat_is_part_of_what_lamoda_keeps():
    """Заказ из Казахстана: из согласованной цены вычитаются и комиссия, и «НДС с
    ин. компаний» — так в отчёте (5568,26 − 2338,67 − 739,49 = 2490,10)."""
    row = _lamoda_row(order="KZ260906-1", price=4621.79, agreed=5568.26, payout=2490.10,
                      commission=2338.67, vat=-739.49, loyalty=946.47)
    line = _parse(_lamoda_file([row]), "lamoda").lines[0]
    assert (line["revenue"], line["payout"], line["commission"]) == (556826, 249010, 233867 + 73949)


def test_lamoda_total_row_and_signatures_below_it():
    sale = _lamoda_row(payout=600)
    result = _parse(_lamoda_file([sale, sale], total=1200, footer=True), "lamoda")
    assert result.errors == []
    assert len(result.lines) == 2                      # «Итого:» — не строка продажи
    assert result.declared_payout == 120000
    assert _total(result) == 120000
    assert any("подписи" in w for w in result.warnings)


# ------------------------------------------------------------ Яндекс KIT

KIT_HEADERS = ["№", "Дата", "Статус заказа", "Итого к оплате", "№ дочернего заказа", "Артикул",
               "Штрихкод", "Наименование товара", "Цена до скидки", "Цена"]


def test_kit_orders_export_counts_only_delivered_and_refunds():
    rows = [
        ["1001", "01.09.2026", "Доставлен", 5000, "1001-1", "A-1", "2000000000012", "Джинсы", 3500, 3000],
        ["1002", "02.09.2026", "Отменён", 2000, "1002-1", "A-1", "2000000000012", "Джинсы", 2500, 2000],
        ["1003", "02.09.2026", "Новый", 2000, "1003-1", "A-1", "2000000000012", "Джинсы", 2500, 2000],
        ["1004", "03.09.2026", "FULL_REFUND", 2000, "1004-1", "A-1", "2000000000012", "Джинсы", 2500, 2000],
        ["1005", "03.09.2026", "Частичный возврат", 2000, "1005-1", "A-1", "2000000000012", "Джинсы", 2500, 2000],
    ]
    result = _parse(make_xlsx(KIT_HEADERS, rows), "kit")
    assert result.format is KIT_FORMAT
    # FULL_REFUND — «полный возврат» заказа (спека KIT): в выгрузке по текущему
    # статусу продажи у него уже нет — не возврат с минусом, а пропуск.
    assert [(l["order_id"], l["operation"]) for l in result.lines] == [("1001", v.SALE)]
    # Выручка — цена позиции (после скидки), а не «Итого к оплате» заказа.
    assert result.lines[0]["revenue"] == 300000
    assert result.lines[0]["payout"] == 300000        # своей комиссии KIT не берёт
    assert len(result.warnings) == 4                  # отмена, «Новый», полный и частичный возврат
    assert any("Частичный возврат" in w for w in result.warnings)


def test_kit_refusals_are_neither_sale_nor_return():
    """«Отказ при получении», «Не доставлен», «Полный возврат» — по подстроке
    они читались бы как «получен»/«доставлен» (продажа) или «возврат»."""
    rows = [[str(1100 + i), "01.09.2026", status, 2000, f"{1100 + i}-1", "A-1", "2000000000012", "Джинсы",
             2500, 2000] for i, status in enumerate(
        ["Отказ при получении", "Не доставлен", "Не получен", "Полный возврат", "Получен", "Возврат"])]
    result = _parse(make_xlsx(KIT_HEADERS, rows), "kit")
    assert [(l["order_id"], l["operation"]) for l in result.lines] == [("1104", v.SALE), ("1105", v.RETURN)]


def test_yandex_pay_detail_gives_signed_acquiring_fees():
    """Как в образце Яндекс Пэй: у оплаты комиссия «−57,6», при возврате
    покупателю она возвращается «+36»."""
    text = ("﻿Дата транзакции;merchant_id;ID транзакции;Источник транзакции;Номер заказа;"
            "Сумма транзакции;Комиссия за сервис;Комиссия за обслуживание;"
            "Сумма транзакции попавшая в перечисление\n"
            "2024-07-23T14:54:42+03:00;m;t1;payment;o1;1600;-57,6;-34,56;1600\n"
            "2024-07-23T19:59:41+03:00;m;t2;user_payment_refund;o1;-1000;36;0;-1000\n")
    result = _parse(text.encode("utf-8"), "kit", "pay.csv")
    assert result.format is KIT_PAY_FORMAT
    pay, refund = result.lines
    assert (pay["commission"], pay["payout"]) == (9216, -9216)
    assert (refund["commission"], refund["payout"]) == (-3600, 3600)
    assert pay["revenue"] == 0 and pay["operation"] == v.DEDUCTION
    assert _total(result) == -9216 + 3600


# ------------------------------------------------------------ единая форма

def _unified(rows):
    return make_xlsx([c.header for c in v.UNIFIED_COLUMNS], rows)


def _unified_row(**kw):
    base = {c.header: None for c in v.UNIFIED_COLUMNS}
    base.update({"Тип операции": "Продажа", "Дата операции": "05.09.2026", "Баркод": "200",
                 "Кол-во": 1, "Выручка": 1000, "К перечислению за товар": 800})
    base.update(kw)
    return [base[c.header] for c in v.UNIFIED_COLUMNS]


def test_unified_template_is_recognized_for_any_platform():
    for platform in ("ozon", "wb", "lamoda", "kit"):
        result = _parse(_unified([_unified_row()]), platform)
        assert result.format is UNIFIED_FORMAT, platform


def test_unified_return_sign_is_forced():
    result = _parse(_unified([_unified_row(**{"Тип операции": "Возврат", "Комиссия площадки": 200})]),
                    "ozon")
    line = result.lines[0]
    assert (line["revenue"], line["payout"], line["commission"]) == (-100000, -80000, -20000)


def test_unified_unknown_operation_is_an_error():
    result = _parse(_unified([_unified_row(**{"Тип операции": "Подарок"})]), "ozon")
    assert result.errors and "Подарок" in result.errors[0]


# ------------------------------------------------------------ не опознан

def test_unknown_report_lists_found_columns():
    data = make_xlsx(["Дата", "Что-то", "Ещё что-то"], [["01.09.2026", 1, 2]])
    result = _parse(data, "wb")
    assert result.format is None
    assert "«Что-то»" in result.errors[0]
    assert "единой форме" in result.errors[0]


def test_wb_report_uploaded_as_lamoda_is_not_misread():
    """Детализация WB, загруженная с площадкой Lamoda, не должна «опознаться»
    общим разбором по совпавшим «Баркод»/«Кол-во» с неверными суммами — у
    Lamoda нет ни цены, ни выплаты с такими заголовками."""
    result = _parse(wb_file([wb_row()]), "lamoda")
    assert result.format is None
