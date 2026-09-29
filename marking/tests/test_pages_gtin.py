from conftest import FIXTURES, fixture_bytes
from markapp import settings
from markapp.crypto import decrypt_value
from markapp.models import GtinPair, NkCard, Supply

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_gtin_pages_open(client):
    for url in ("/gtin", "/gtin?only=new", "/gtin/card/04620180403734"):
        assert client.get(url).status_code == 200, url


def test_import_several_product_gtin_files(client, db):
    files = [("files", (p.name, p.read_bytes(), XLSX)) for p in sorted((FIXTURES / "product_gtin").glob("*.xlsx"))]
    r = client.post("/gtin/import", files=files)
    assert "product_gtin: добавлено 1012" in r.text
    assert db.query(NkCard).count() == 1012          # все ждут карточку НК


def test_manual_pair_needs_catalog_article(client, db):
    r = client.post("/gtin/add", data={"supplier_sku": "нет такого", "gtin": "04620180403734"})
    assert "нет в справочнике" in r.text
    assert db.query(GtinPair).count() == 0


def test_export_downloads_and_marks(client, db):
    client.post("/catalog/import", files={"file": ("c.xlsx", fixture_bytes("lamoda_catalog_full_2026-09-28.xlsx"), XLSX)})
    r = client.post("/gtin/add", data={"supplier_sku": "3030 KAHVE 4XL АВЕР Рубашка Д/р сатин",
                                       "gtin": "04630688318072"})
    assert "добавлено 1" in r.text
    r = client.get("/gtin/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith(XLSX)
    assert db.query(GtinPair).one().exported_at is not None


def test_api_key_is_saved_encrypted_and_never_shown(client, db):
    org = settings.lamoda_org(db)
    data = {k: getattr(org, k) or "" for k in ("name", "surname", "firstname", "patronymic", "inn",
                                              "ogrnip", "address", "signer_role", "contract_number",
                                              "sticker_sender", "edo_sender_id")}
    data.update(vat_rate="5", nk_api_key="SECRET-KEY-42")
    client.post(f"/organizations/{org.id}", data=data)
    db.refresh(org)
    assert decrypt_value(org.nk_api_key_enc) == "SECRET-KEY-42"
    page = client.get(f"/organizations/{org.id}").text
    assert "SECRET-KEY-42" not in page and "сохранён" in page
    # Пустое поле ключ не стирает.
    data["nk_api_key"] = ""
    client.post(f"/organizations/{org.id}", data=data)
    db.refresh(org)
    assert decrypt_value(org.nk_api_key_enc) == "SECRET-KEY-42"


def test_fbo_upload_feeds_the_gtin_directory(client, db):
    client.post("/catalog/import", files={"file": ("c.xlsx", fixture_bytes("lamoda_catalog_full_2026-09-28.xlsx"), XLSX)})
    client.post("/supplies/new", data={"number": "12550", "doc_number": "", "supply_date": "2026-10-02"},
                files={"file": ("in.xlsx", fixture_bytes("lamoda_shipment_input_typical.xlsx"), XLSX)})
    s = db.query(Supply).one()
    s.status = "moved"
    db.commit()
    r = client.post(f"/supplies/{s.id}/fbo", files={"file": ("fbo.xlsx", fixture_bytes("lamoda_postavki_fbo_12550.xlsx"), XLSX)})
    assert "Справочник GTIN: новых пар 80" in r.text
    page = client.get(f"/supplies/{s.id}").text
    assert "Без GTIN: <b class=\"\">0</b>" in page
