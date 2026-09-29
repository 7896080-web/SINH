"""Сквозной путь через страницы: то, что увидит человек, а не только функции."""
from conftest import fixture_bytes
from markapp import onec, settings
from markapp.models import OnecTask, Supply

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _upload_catalog(client):
    r = client.post("/catalog/import", files={"file": ("cat.xlsx", fixture_bytes(
        "lamoda_catalog_full_2026-09-28.xlsx"), XLSX)})
    assert "новых 801" in r.text


def _create(client, date="2026-10-02"):
    r = client.post("/supplies/new", data={"number": "", "doc_number": "", "supply_date": date},
                    files={"file": ("in.xlsx", fixture_bytes("lamoda_shipment_input_typical.xlsx"), XLSX)})
    assert r.status_code == 200
    return r


def test_login_required(db):
    from fastapi.testclient import TestClient
    from markapp.main import app
    with TestClient(app) as c:
        r = c.get("/supplies", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"


def test_every_page_opens(client):
    for url in ("/supplies", "/supplies/new", "/catalog", "/organizations", "/organizations/1",
                "/organizations/0", "/diagnostics"):
        r = client.get(url)
        assert r.status_code == 200, url


def test_supply_flow_through_pages(client, db, exchange_dirs):
    _upload_catalog(client)
    r = _create(client)
    assert "Поставка 12560 создана: 80 строк" in r.text
    s = db.query(Supply).one()
    # Без ответа на PING отправка в 1С выключена и объяснена.
    r = client.post(f"/supplies/{s.id}/check")
    assert "обработка 1С ещё не обновлена" in r.text
    settings.put(db, onec.EPF_VERSION, "mark-1")
    db.commit()
    r = client.post(f"/supplies/{s.id}/check")
    assert "Проверка остатка отправлена в 1С" in r.text
    # Переместить до ответа 1С нельзя.
    r = client.post(f"/supplies/{s.id}/move")
    assert "только после успешной проверки" in r.text


def test_moved_supply_cannot_be_deleted_or_edited(client, db):
    _upload_catalog(client)
    _create(client)
    s = db.query(Supply).one()
    s.status = "moved"
    db.commit()
    r = client.post(f"/supplies/{s.id}/delete")
    assert "состав зафиксирован" in r.text
    r = client.post(f"/supplies/{s.id}/rows/{s.rows[0].id}", data={"qty": "1"})
    assert "состав зафиксирован" in r.text
    assert db.query(Supply).count() == 1


def test_manual_scheme_needs_reason(client, db):
    _upload_catalog(client)
    _create(client)
    s = db.query(Supply).one()
    data = {"number": s.number, "doc_number": s.doc_number, "supply_date": "2026-10-02",
            "planned_upd_date": "", "scheme_choice": "commission", "scheme_reason": ""}
    r = client.post(f"/supplies/{s.id}/header", data=data)
    assert "требует причины" in r.text
    data["scheme_reason"] = "договор ещё не переоформлен"
    r = client.post(f"/supplies/{s.id}/header", data=data)
    assert "Сохранено" in r.text and "выбрана вручную" in r.text


def test_fbo_upd_and_stickers_through_pages(client, db):
    _upload_catalog(client)
    r = client.post("/supplies/new", data={"number": "12550", "doc_number": "", "supply_date": "2026-10-02"},
                    files={"file": ("in.xlsx", fixture_bytes("lamoda_shipment_input_typical.xlsx"), XLSX)})
    s = db.query(Supply).one()
    s.status = "moved"
    db.commit()
    r = client.post(f"/supplies/{s.id}/fbo", files={"file": ("fbo.xlsx", fixture_bytes(
        "lamoda_postavki_fbo_12550.xlsx"), XLSX)})
    assert "Выгрузка сверена с поставкой: 338 строк" in r.text
    r = client.post(f"/supplies/{s.id}/upd", data={"doc_date": "2026-10-01", "totals_mode": "rows"})
    assert "УПД 12550 выпущен: 338 поз." in r.text
    r = client.get(f"/supplies/{s.id}/upd.xml")
    assert r.status_code == 200
    assert "12550.xml" in r.headers["content-disposition"]
    assert b'encoding="windows-1251"' in r.content[:80]
    r = client.post(f"/supplies/{s.id}/stickers", data={"boxes": "5"})
    assert r.status_code == 200 and r.headers["content-type"].startswith(XLSX)
    db.refresh(s)
    assert s.status == "upd_issued"


def test_ping_button_queues_ping(client, db):
    client.post("/diagnostics/ping")
    t = db.query(OnecTask).one()
    assert t.command == "PING" and t.line == f"PING|{t.order_id}"


def test_health_needs_onec_exchange(client, db):
    r = client.get("/health")
    assert r.status_code == 503 and "onec_exchange" in r.text
    from markapp.workers.heartbeat import beat
    beat(db, "onec_exchange")
    assert client.get("/health").status_code == 200


def test_lamoda_settings_change_keeps_existing_supply(client, db):
    _upload_catalog(client)
    _create(client)
    from markapp.models import Organization
    other = Organization(name="ИП Другой", surname="Д", firstname="И", inn="123456789012",
                         ogrnip="1", address="a")
    db.add(other)
    db.commit()
    client.post("/lamoda-settings", data={"org_id": other.id, "agency_from": "01.10.2026",
                                          "last_number": "12560", "step": "10"})
    s = db.query(Supply).one()
    db.refresh(s)
    assert s.organization.name == "ИП Яворская Т.Н."
    assert settings.lamoda_org(db).id == other.id
