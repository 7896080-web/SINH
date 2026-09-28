"""«Есть на складе — нет на площадке»: список того, что не продаётся молча.

Товар лежит на ЦС, а карточки на площадке нет — он не продаётся, и само это не
всплывает: рассылке по такой паре отправлять не по чему, заказов по ней не
будет, в отчёте о расхождениях она появляется только после того, как рассылка
попробует и откажется. 20.09 на бою так нашлось пятьдесят пар по Kit.

Главное решение здесь — площадка, а не кабинет. У WB три кабинета (разные ИП), и
карточка заводится в ОДНОМ: товар, заведённый у одного ИП, на витрине WB есть.
Считать его отсутствующим у двух других значит выдать двести лишних строк на
каждую сотню настоящих — список перестанут открывать.
"""

from app.missing_cards import collect_missing, platforms_in_use
from app.models import (Barcode, Platform, PlatformAccount, PlatformCatalogItem,
                        Product)
from tests.factories import make_account


def _product(db, uid, article="A1", stock=5, barcode="111"):
    db.add(Product(uid_1c=uid, article=article, name=f"Товар {article}",
                   size="46", color="чёрный", stock_on_hand=stock))
    db.add(Barcode(barcode=barcode, uid_1c=uid))


def _card(db, account, barcode, external_id="x1"):
    db.add(PlatformCatalogItem(account_id=account.id, external_id=external_id,
                               barcode=barcode, article="A", name="Карточка"))


# ------------------------------------------------------------ сам расчёт

def test_a_stocked_product_without_a_card_is_listed(db):
    make_account(db, Platform.kit, name="КИТ")
    _product(db, "u1")
    db.commit()

    rows, total = collect_missing(db)

    assert total == 1
    assert rows[0].article == "A1" and rows[0].stock == 5


def test_a_product_with_a_card_is_not_listed(db):
    account = make_account(db, Platform.kit, name="КИТ")
    _product(db, "u1")
    _card(db, account, "111")
    db.commit()

    assert collect_missing(db) == ([], 0)


def test_a_product_without_stock_is_never_listed(db):
    """Товара нет на складе — заводить карточку незачем. Попади он сюда, список
    стал бы каталогом всей номенклатуры, то есть бесполезным."""
    make_account(db, Platform.kit, name="КИТ")
    _product(db, "u1", stock=0)
    db.commit()

    assert collect_missing(db) == ([], 0)


def test_one_wb_cabinet_covers_the_whole_platform(db):
    """Карточка заводится у одного ИП, а продаётся на WB. Считать товар
    отсутствующим у двух других кабинетов значит утроить список впустую."""
    yav = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    make_account(db, Platform.wb, name="ИП РЕБРИК", warehouse_id="wh-2")
    make_account(db, Platform.wb, name="ИП КАРАМАН", warehouse_id="wh-3")
    _product(db, "u1")
    _card(db, yav, "111")
    db.commit()

    assert collect_missing(db) == ([], 0)


def test_platforms_are_counted_apart_from_each_other(db):
    """Kit и Ozon — разные витрины: карточка на одной ничего не говорит о другой."""
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    make_account(db, Platform.kit, name="КИТ", warehouse_id="wh-2")
    _product(db, "u1")
    _card(db, wb, "111")
    db.commit()

    rows, total = collect_missing(db)

    assert total == 1
    assert rows[0].missing[Platform.kit] is True
    assert rows[0].missing[Platform.wb] is False


def test_the_filter_narrows_down_to_one_platform(db):
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    make_account(db, Platform.kit, name="КИТ", warehouse_id="wh-2")
    _product(db, "u1")
    _card(db, wb, "111")
    db.commit()

    assert collect_missing(db, Platform.wb) == ([], 0)
    assert collect_missing(db, Platform.kit)[1] == 1


def test_a_platform_without_cabinets_is_not_asked_about(db):
    """Спрашивать про площадку, к которой не подключались, бессмысленно: карточек
    там нет ни одной, и весь склад оказался бы «отсутствующим»."""
    make_account(db, Platform.kit, name="КИТ")
    _product(db, "u1")
    db.commit()

    rows, _ = collect_missing(db)

    assert set(rows[0].missing) == {Platform.kit}
    assert platforms_in_use(db) == [Platform.kit]


def test_a_switched_off_cabinet_still_counts(db):
    """Каталог остаётся от кабинета, который выключили или который погасил
    предохранитель, — карточка на площадке от этого никуда не делась."""
    account = make_account(db, Platform.kit, name="КИТ")
    _product(db, "u1")
    _card(db, account, "111")
    account.is_active = False
    db.commit()

    assert collect_missing(db) == ([], 0)


def test_any_barcode_of_the_product_counts_as_found(db):
    """У размер-цвета бывает несколько баркодов, и площадка знает не обязательно
    первый. Искать по одному значило бы звать заводить существующие карточки."""
    account = make_account(db, Platform.kit, name="КИТ")
    _product(db, "u1")
    db.add(Barcode(barcode="222", uid_1c="u1"))
    _card(db, account, "222")
    db.commit()

    assert collect_missing(db) == ([], 0)


def test_the_total_counts_everything_while_the_page_shows_a_portion(db):
    """«Показано 2 из 5» честнее, чем показать два и промолчать: по этому списку
    заводят карточки, и человек должен знать, сколько работы осталось."""
    make_account(db, Platform.kit, name="КИТ")
    for i in range(5):
        _product(db, f"u{i}", article=f"A{i}", barcode=f"bc{i}")
    db.commit()

    rows, total = collect_missing(db, limit=2)

    assert len(rows) == 2 and total == 5


# -------------------------------------------------------------- страница

def _seed_web(web_db):
    account = PlatformAccount(platform=Platform.kit, name="КИТ", warehouse_id="wh-1")
    web_db.add(account)
    web_db.commit()
    web_db.refresh(account)
    web_db.add(Product(uid_1c="u1", article="НЕТКАРТОЧКИ", name="Куртка",
                       size="46", color="чёрный", stock_on_hand=7))
    web_db.add(Barcode(barcode="111", uid_1c="u1"))
    web_db.commit()
    return account


def test_the_page_lists_the_product(logged_in_client, web_db):
    _seed_web(web_db)

    page = logged_in_client.get("/report/missing-cards")

    assert page.status_code == 200
    assert "НЕТКАРТОЧКИ" in page.text


def test_the_page_refreshes_itself(logged_in_client, web_db):
    """Список меняется после выгрузки каталога и прихода остатков из 1С.
    Оператор держит страницу открытой и работает по ней — руками он её не
    перезагрузит и будет заводить карточки по вчерашнему списку."""
    _seed_web(web_db)

    page = logged_in_client.get("/report/missing-cards")

    assert 'hx-trigger="every' in page.text
    assert "/report/missing-cards/rows" in page.text


def test_the_fragment_answers_on_its_own(logged_in_client, web_db):
    """Автообновление тянет именно фрагмент: если он отвечает только целой
    страницей, в таблицу вложится вся вёрстка вместе с меню."""
    _seed_web(web_db)

    page = logged_in_client.get("/report/missing-cards/rows")

    assert page.status_code == 200
    assert "НЕТКАРТОЧКИ" in page.text
    assert "<nav" not in page.text


def test_the_export_gives_a_file(logged_in_client, web_db):
    _seed_web(web_db)

    r = logged_in_client.get("/report/missing-cards/export?platform=kit")

    assert r.status_code == 200
    assert len(r.content) > 0


def test_the_report_page_links_here(logged_in_client, web_db):
    _seed_web(web_db)

    page = logged_in_client.get("/report")

    assert "/report/missing-cards" in page.text


def test_the_report_page_refreshes_itself(logged_in_client, web_db):
    """Отчёт собирается воркером раз в час, но находки меняются и между
    прогонами: очередь разгребается, задания 1С закрываются."""
    _seed_web(web_db)

    page = logged_in_client.get("/report")

    assert 'hx-trigger="every' in page.text
    assert "/report/fragment" in page.text


def test_the_report_fragment_answers_on_its_own(logged_in_client, web_db):
    _seed_web(web_db)

    page = logged_in_client.get("/report/fragment")

    assert page.status_code == 200
    assert "<nav" not in page.text


def test_the_page_needs_a_login(client):
    r = client.get("/report/missing-cards", follow_redirects=False)

    assert r.status_code in (302, 303, 307)
    assert "/login" in r.headers.get("location", "")
