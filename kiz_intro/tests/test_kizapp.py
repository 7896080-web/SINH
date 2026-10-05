"""«Ввод в оборот» вне Lamoda: PDF → коды → ввод → проверка → этикетки и txt.
ЧЗ подменён; PDF с DataMatrix собирается в тесте (настоящих кодов нет)."""
import base64
import io
import json
from datetime import timedelta

import pytest
from PIL import Image

from kizapp import chz, service as S
from kizapp.crypto import decrypt, encrypt
from kizapp.models import Batch, Card, Code, now_utc

GTIN = "04630688315972"
GS = "\x1d"


def full(n, gtin=GTIN):
    return f"01{gtin}21{n:013d}{GS}91EE10{GS}92{'B' * 43}{n % 10}"


def pdf_of(codes):
    """PDF «как из ЛК»: страница на два кода DataMatrix."""
    import zxingcpp
    pages = []
    for i in range(0, len(codes), 2):
        page = Image.new("L", (1240, 1754), 255)
        for k, code in enumerate(codes[i:i + 2]):
            img = zxingcpp.create_barcode(code, zxingcpp.BarcodeFormat.DataMatrix).to_image(scale=5)
            h, w = img.shape[:2]
            page.paste(Image.frombytes("L", (w, h), bytes(memoryview(img))), (150, 150 + k * 800))
        pages.append(page.convert("RGB"))
    buf = io.BytesIO()
    pages[0].save(buf, "PDF", save_all=True, append_images=pages[1:], resolution=150)
    return buf.getvalue()


def org(db, inn="910223073620", name="ИП Яворская Т.Н."):
    o = S.save_org(db, None, name, inn)
    db.commit()
    return o


def logged(db, o):
    o.token_enc, o.token_until = encrypt("T-" + o.inn), now_utc() + timedelta(hours=9)
    db.commit()


def card_data(name, tnved, number, day, kind="CONFORMITY_DECLARATION"):
    return {"name": name, "tnved": tnved, "permit_type": kind, "permit_number": number, "permit_date": day}


@pytest.fixture
def chz_fake(monkeypatch):
    state = {"cises": {}, "docs": [], "cards": {}}
    monkeypatch.setattr(chz, "cises_info", lambda cis, t: {c: state["cises"].get(c, "EMITTED") for c in cis})

    def create_document(doc, sig, t):
        state["docs"].append((doc, sig, t))
        return "DOC-1"
    monkeypatch.setattr(chz, "create_document", create_document)
    monkeypatch.setattr(chz, "document_status", lambda d, t: ("CHECKED_OK", ""))
    monkeypatch.setattr(chz, "nk_products",
                        lambda gtins, t: {g: state["cards"][(t, g)] for g in gtins if (t, g) in state["cards"]})
    return state


def test_codes_are_read_from_datamatrix_in_pdf():
    codes = [full(i) for i in range(1, 6)]
    assert S.codes_from_pdf(pdf_of(codes)) == codes


def test_load_checks_quantity_and_duplicates(db):
    o = org(db)
    pdf = pdf_of([full(1), full(2)])
    with pytest.raises(S.KizError, match="в имени файла 3"):
        S.load_pdf(db, o, pdf, f"order_x_gtin_{GTIN}_quantity_3.pdf")
    b = S.load_pdf(db, o, pdf, f"order_x_gtin_{GTIN}_quantity_2.pdf")
    db.commit()
    c = db.query(Code).first()
    assert b.expected == 2 and c.cis == f"01{GTIN}21{1:013d}" and decrypt(c.full_enc) == full(1)
    assert "EE10" not in c.full_enc                                   # полный код зашифрован
    with pytest.raises(S.KizError, match="уже загружены"):
        S.load_pdf(db, o, pdf, "again.pdf")


def test_card_permit_is_parsed_from_nk_answer():
    item = {"good_name": "27643 TABA L базовый свитшот", "good_attrs": [
        {"attr_name": "Код ТНВЭД", "attr_value": "6110909000"},
        {"attr_name": "Декларация о соответствии", "attr_value": "ЕАЭС N RU Д-TR.РА02.В.60472/24:::2024-03-11"},
        {"attr_name": "Сертификат соответствия", "attr_value": "ЕАЭС KG 417/043.RU.02.06303:::2024-12-26"}]}
    c = chz.parse_card(item)
    assert c["tnved"] == "6110909000"
    assert (c["permit_type"], c["permit_number"], c["permit_date"]) == (
        "CONFORMITY_CERTIFICATE", "ЕАЭС KG 417/043.RU.02.06303", "2024-12-26")   # самый новый


def test_each_ip_has_own_cards(db, chz_fake):
    a, b = org(db), org(db, "910223073099", "ИП Ребрик Р.В.")
    logged(db, a)
    logged(db, b)
    chz_fake["cards"][("T-" + a.inn, GTIN)] = card_data("A", "6110909000", "Д-1", "2026-01-01")
    chz_fake["cards"][("T-" + b.inn, GTIN)] = card_data("B", "6110209100", "Д-2", "2026-02-02")
    S.fetch_cards(db, a, [GTIN])
    S.fetch_cards(db, b, [GTIN])
    db.commit()
    assert S.card(db, a.id, GTIN).permit_number == "Д-1" and S.card(db, b.id, GTIN).permit_number == "Д-2"


def test_full_cycle(client, db, chz_fake):
    o = org(db)
    logged(db, o)
    chz_fake["cards"][("T-" + o.inn, GTIN)] = card_data("27643 TABA L базовый свитшот", "6110909000",
                                                        "ЕАЭС KG 417/043.RU.02.06303", "2024-12-26",
                                                        "CONFORMITY_CERTIFICATE")
    r = client.post("/upload", data={"org_id": str(o.id)}, follow_redirects=False,
                    files={"file": (f"order_1_gtin_{GTIN}_quantity_2.pdf", pdf_of([full(1), full(2)]),
                                    "application/pdf")})
    assert r.status_code == 303
    b = db.query(Batch).one()
    db.expire_all()
    assert S.card(db, o.id, GTIN).tnved == "6110909000"            # карточка запрошена при загрузке
    with pytest.raises(S.KizError, match="дата производства"):
        S.prepare(db, b)
    S.set_production_date(b, "2026-10-01")
    with pytest.raises(S.KizError, match="Нанесён"):
        S.prepare(db, b)
    chz_fake["cises"] = {c.cis: "APPLIED" for c in db.query(Code)}
    S.refresh_statuses(db, b)
    d = S.prepare(db, b)
    body = json.loads(base64.b64decode(d.document))
    assert body["owner_inn"] == o.inn and body["production_type"] == "OWN_PRODUCTION"
    assert body["products"][0]["certificate_document_data"][0]["certificate_number"].endswith("06303")
    db.commit()
    S.send(db, d, "SIG\n")
    db.commit()
    assert d.status == "sent" and chz_fake["docs"][0][2] == "T-" + o.inn and S.ready_codes(db, b) == []
    assert client.post(f"/batch/{b.id}/txt", follow_redirects=False).status_code == 303   # ещё не в обороте
    chz_fake["cises"] = {c.cis: "INTRODUCED" for c in db.query(Code)}
    S.refresh_statuses(db, b)
    db.expire_all()
    assert S.summary(db, b)["done"]
    r = client.post(f"/batch/{b.id}/txt")
    assert r.status_code == 200 and r.content == (full(1) + "\r\n" + full(2) + "\r\n").encode("ascii")
    r = client.post(f"/batch/{b.id}/labels")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")


def test_timeout_makes_document_unknown(db, chz_fake, monkeypatch):
    o = org(db)
    logged(db, o)
    db.add(Card(org_id=o.id, gtin=GTIN, name="x", tnved="6110909000", permit_type="CONFORMITY_DECLARATION",
                permit_number="Д-1", permit_date="2026-01-01", fetched_at=now_utc()))
    b = S.load_pdf(db, o, pdf_of([full(3)]), "x.pdf")
    S.set_production_date(b, "2026-10-01")
    db.commit()
    chz_fake["cises"] = {c.cis: "APPLIED" for c in db.query(Code)}
    S.refresh_statuses(db, b)
    d = S.prepare(db, b)
    db.commit()

    def boom(*a):
        raise chz.ChzError("таймаут")
    monkeypatch.setattr(chz, "create_document", boom)
    with pytest.raises(S.KizError):
        S.send(db, d, "S")
    assert d.status == "unknown" and S.ready_codes(db, b) == []
    with pytest.raises(S.KizError, match="уже"):
        S.send(db, d, "S")


def test_login_refuses_foreign_inn(db, monkeypatch):
    o = org(db)
    monkeypatch.setattr(chz, "signer_inn", lambda s: "910223073099")
    monkeypatch.setattr(chz, "sign_in", lambda u, s: "T")
    with pytest.raises(S.KizError, match="910223073099"):
        S.login(db, o, "u", "sig", "")


def test_foreign_origin_post_is_refused(client, db):
    r = client.post("/org", data={"inn": "910223073620"}, headers={"origin": "http://127.0.0.1:8000"})
    assert r.status_code == 403


def test_pages_open(client, db):
    o = org(db)
    assert "Добавить ИП" in client.get("/").text
    assert "Справочник Нацкаталога" in client.get(f"/org/{o.id}/cards").text
    assert "Вход в Честный знак" in client.get(f"/org/{o.id}/login").text
