"""Позиции WB без chrtId должны быть ВИДНЫ, а не жить в голове у того, кто мерил.

21.09 оператор спросил про «107 пар WB» — и был прав: числа этого нет ни на одной
странице. Оно пришло из разового замера, сделанного при переходе на chrtId, и
после замера исчезло. Ровно тот случай, ради которого отчёт и существует: данные
были, смотреть их было негде.

Следствие тут особенное — отложенное. Отправка баркодом РАБОТАЕТ сегодня. Но в
спеке WB тело отправки остатков описано ключом `chrtId`, про `sku` не сказано
ни слова, а отказ `400 SKUUploadDisabled` («uploading stock is not allowed by
'sku'») заготовлен. В день, когда площадка его включит, эти карточки перестанут
получать остаток — молча, до первого отказа рассылки.
"""

from app.models import (Barcode, Platform, PlatformCatalogItem, Product,
                        SyncSetting)
from app.report import _check_wb_without_chrt, collect_findings
from tests.factories import make_account


def _pair(db, account, uid="u1", barcode="2000000000001", external_id=None,
          broadcast=True, enabled=True):
    db.add(Product(uid_1c=uid, article=f"ART-{uid}", name="Куртка",
                   stock_on_hand=5, broadcast_enabled=broadcast))
    db.add(Barcode(barcode=barcode, uid_1c=uid))
    db.add(SyncSetting(uid_1c=uid, account_id=account.id, enabled=enabled))
    if external_id is not None:
        db.add(PlatformCatalogItem(account_id=account.id, barcode=barcode,
                                   external_id=external_id, name="Карточка"))
    db.commit()


# ------------------------------------------------------- находка появляется

def test_a_pair_without_chrt_is_reported(db):
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _pair(db, wb, external_id="")

    finding = _check_wb_without_chrt(db)

    assert finding is not None
    assert finding.count == 1
    assert "ART-u1" in finding.details[0]


def test_a_pair_with_chrt_is_not_reported(db):
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _pair(db, wb, external_id="123456:789012")

    assert _check_wb_without_chrt(db) is None


def test_a_pair_without_a_catalog_row_at_all_is_reported(db):
    """Карточки в нашем снимке каталога нет вовсе — chrtId взять неоткуда."""
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _pair(db, wb, external_id=None)

    assert _check_wb_without_chrt(db).count == 1


# ------------------------------------------- считаем ровно как считает отправка

def test_a_zero_chrt_counts_as_missing(db):
    """`wb._chrt_id` отбрасывает ноль, и отправка уедет баркодом. Считай мы его
    за chrtId — находка обещала бы то, чего не будет."""
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _pair(db, wb, external_id="123456:0")

    assert _check_wb_without_chrt(db).count == 1


def test_a_non_numeric_chrt_counts_as_missing(db):
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _pair(db, wb, external_id="123456:abc")

    assert _check_wb_without_chrt(db).count == 1


def test_one_barcode_with_chrt_is_enough(db):
    """У размер-цвета несколько баркодов, и отправка возьмёт тот, что нашёлся в
    каталоге. Считать пару проблемной из-за соседнего баркода — врать."""
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _pair(db, wb, barcode="2000000000001", external_id="")
    db.add(Barcode(barcode="2000000000002", uid_1c="u1"))
    db.add(PlatformCatalogItem(account_id=wb.id, barcode="2000000000002",
                               external_id="123456:789012", name="Карточка"))
    db.commit()

    assert _check_wb_without_chrt(db) is None


# --------------------------------------------------- и не шумим лишним

def test_other_platforms_are_not_counted(db):
    """chrtId — понятие WB. У Kit адресация variant_id, у Ozon артикул."""
    kit = make_account(db, Platform.kit, name="КИТ", warehouse_id="wh-2")
    _pair(db, kit, external_id="")

    assert _check_wb_without_chrt(db) is None


def test_a_product_without_broadcast_is_not_counted(db):
    """Наружу по нему не уходит ничего — и перестать уходить не может."""
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _pair(db, wb, external_id="", broadcast=False)

    assert _check_wb_without_chrt(db) is None


def test_an_unticked_cabinet_is_not_counted(db):
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _pair(db, wb, external_id="", enabled=False)

    assert _check_wb_without_chrt(db) is None


def test_a_healthy_install_stays_silent(db):
    """Обязательное свойство отчёта: на исправной системе он молчит."""
    assert _check_wb_without_chrt(db) is None


# ----------------------------------------------------------- в общем отчёте

def test_the_finding_reaches_the_report(db):
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _pair(db, wb, external_id="")

    keys = [f.key for f in collect_findings(db)]

    assert "wb_without_chrt" in keys


def test_the_finding_names_the_consequence(db):
    """Правило отчёта: находка называет не факт, а чем это кончится."""
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _pair(db, wb, external_id="")

    finding = _check_wb_without_chrt(db)

    assert "перестанут получать остаток" in finding.consequence
