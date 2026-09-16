import io

from openpyxl import Workbook, load_workbook


def _seed_basic_product(web_db):
    from app.models import Product, Barcode

    web_db.add(Product(uid_1c="u1", article="ART-001", name="Кроссовки", stock_on_hand=5))
    web_db.add(Barcode(barcode="4600000000001", uid_1c="u1", source_platform="wb"))
    web_db.commit()
    return "u1"


def _seed_account(web_db, platform="ozon", name="Основной"):
    from app.models import PlatformAccount

    account = PlatformAccount(platform=platform, name=name, warehouse_id="wh-1")
    web_db.add(account)
    web_db.commit()
    web_db.refresh(account)
    return account


def test_mapping_page_shows_mapped_barcode(logged_in_client, web_db):
    _seed_basic_product(web_db)
    r = logged_in_client.get("/mapping")
    assert r.status_code == 200
    assert "4600000000001" in r.text
    assert "ART-001" in r.text


def test_mapping_rows_htmx_fragment_filters_by_query(logged_in_client, web_db):
    _seed_basic_product(web_db)
    r = logged_in_client.get("/mapping/rows?q=4600000000001")
    assert "4600000000001" in r.text
    r2 = logged_in_client.get("/mapping/rows?q=nothing-matches")
    assert "4600000000001" not in r2.text


def test_mapping_conflicts_tab_empty_by_default(logged_in_client, web_db):
    r = logged_in_client.get("/mapping?view=conflicts")
    assert "Конфликтов сопоставления не найдено" in r.text


def test_mapping_export_mapped_view(logged_in_client, web_db):
    _seed_basic_product(web_db)
    r = logged_in_client.get("/mapping/export")
    assert r.status_code == 200

    wb = load_workbook(io.BytesIO(r.content))
    ws = wb.active
    assert [c.value for c in ws[1]] == ["ID_1С", "Баркод", "Артикул", "Наименование", "Источник", "Добавлен"]
    assert ws.cell(row=2, column=2).value == "4600000000001"


def test_mapping_import_adds_new_barcode(logged_in_client, web_db):
    uid = _seed_basic_product(web_db)

    wb = Workbook()
    ws = wb.active
    ws.append(["ID_1С", "Баркод", "Артикул", "Наименование", "Источник", "Добавлен"])
    ws.append([uid, "4600000000099", "ART-001", "Кроссовки", "", ""])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    r = logged_in_client.post(
        "/mapping/import",
        files={"file": ("import.xlsx", buf.getvalue(),
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        follow_redirects=False,
    )
    assert r.status_code == 303

    r2 = logged_in_client.get("/mapping")
    assert "Добавлено баркодов: 1" in r2.text
    assert "4600000000099" in r2.text


def test_mapping_import_reports_error_for_unknown_product(logged_in_client, web_db):
    wb = Workbook()
    ws = wb.active
    ws.append(["ID_1С", "Баркод", "Артикул", "Наименование", "Источник", "Добавлен"])
    ws.append(["does-not-exist", "111", "", "", "", ""])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    logged_in_client.post(
        "/mapping/import",
        files={"file": ("import.xlsx", buf.getvalue(),
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )

    r = logged_in_client.get("/mapping")
    assert "не найден" in r.text


def test_mapping_conflicts_page_shows_account_name(logged_in_client, web_db):
    from app.models import MappingConflict

    account = _seed_account(web_db, platform="ozon", name="ИП Яворская")
    web_db.add(MappingConflict(barcode="777", account_id=account.id, attempts=1))
    web_db.commit()

    r = logged_in_client.get("/mapping?view=conflicts")
    assert "777" in r.text
    assert "ИП Яворская" in r.text


def test_mapping_conflicts_filter_by_account(logged_in_client, web_db):
    from app.models import MappingConflict

    a1 = _seed_account(web_db, platform="wb", name="Кабинет 1")
    a2 = _seed_account(web_db, platform="wb", name="Кабинет 2")
    web_db.add(MappingConflict(barcode="111", account_id=a1.id, attempts=1))
    web_db.add(MappingConflict(barcode="222", account_id=a2.id, attempts=1))
    web_db.commit()

    r = logged_in_client.get(f"/mapping/rows?view=conflicts&account_id={a1.id}")
    assert "111" in r.text and "222" not in r.text


def test_mapping_load_catalog_without_credentials_shows_friendly_error(logged_in_client, web_db):
    account = _seed_account(web_db, platform="ozon", name="Основной")

    r = logged_in_client.post(f"/mapping/load-catalog/{account.id}", follow_redirects=False)
    assert r.status_code == 303

    r2 = logged_in_client.get("/mapping?view=conflicts")
    assert "Не удалось загрузить" in r2.text


def test_mapping_load_catalog_enriches_conflict_with_platform_name(logged_in_client, web_db, monkeypatch):
    from app.models import MappingConflict
    from app.workers.platform_clients.base import CatalogItem
    import app.routers.mapping as mapping_router

    account = _seed_account(web_db, platform="ozon", name="Основной")
    web_db.add(MappingConflict(barcode="777", account_id=account.id, attempts=1))
    web_db.commit()

    class FakeClient:
        def get_catalog_items(self):
            return [CatalogItem(external_id="e1", barcode="777", article="ART-777", name="Товар с Ozon")]

    monkeypatch.setattr(mapping_router, "build_client", lambda db, account_id: FakeClient())

    r = logged_in_client.post(f"/mapping/load-catalog/{account.id}", follow_redirects=False)
    assert r.status_code == 303

    r2 = logged_in_client.get("/mapping?view=conflicts")
    assert "загружено карточек 1" in r2.text
    assert "Товар с Ozon" in r2.text
    assert "ART-777" in r2.text


def test_mapping_conflict_resolution_full_cycle_via_excel(logged_in_client, web_db):
    """Экспорт конфликта с пустым ID_1С -> заполнить -> загрузить обратно ->
    баркод переезжает в «Сопоставленные», конфликт исчезает."""
    from app.models import Product, MappingConflict

    account = _seed_account(web_db, platform="wb", name="ИП Яворская")
    web_db.add(Product(uid_1c="u9", article="A9", name="Шапка", stock_on_hand=1))
    web_db.add(MappingConflict(barcode="555", account_id=account.id, attempts=2))
    web_db.commit()

    r = logged_in_client.get("/mapping/export?view=conflicts")
    wb = load_workbook(io.BytesIO(r.content))
    headers = [c.value for c in wb.active[1]]
    assert headers[0] == "ID_1С"

    wb2 = Workbook()
    ws2 = wb2.active
    ws2.append(headers)
    ws2.append(["u9", "555", "ИП Яворская", "wb", "", "", 2, "", ""])
    buf = io.BytesIO()
    wb2.save(buf)
    buf.seek(0)

    logged_in_client.post(
        "/mapping/import",
        files={"file": ("resolve.xlsx", buf.getvalue(),
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )

    r2 = logged_in_client.get("/mapping?view=conflicts")
    assert "555" not in r2.text

    r3 = logged_in_client.get("/mapping")
    assert "555" in r3.text
