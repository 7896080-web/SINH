"""Порядок в интерфейсе (14.10): меню без «Утверждения цен», «Отклонить все ожидающие»,
кто ставит цены на какой площадке, Ламода не передаётся с «Цен товаров»."""
from priceapp.models import PriceChange
from tests import factories as F


def _nav(html):
    return html.split("<nav>", 1)[1].split("</nav>", 1)[0]


def test_menu_has_no_approval_but_page_still_opens(client, db):
    F.account(db, "wb", "ИП 1")
    page = client.get("/attention").text
    assert "/approval" not in _nav(page)
    assert client.get("/approval").status_code == 200


def test_attention_says_who_sets_prices_on_each_platform(client, db):
    F.account(db, "wb", "ИП 1")
    page = client.get("/attention").text
    assert "Кто сейчас ставит цены" in page
    for name in ("Wildberries", "Яндекс KIT", "Lamoda", "Ozon"):
        assert name in page
    assert "lamoda_apply.py" in page


def _change(db, acc, status, item="s1"):
    ch = PriceChange(item_id=item, account_id=acc.id, barcode="b1", new_price=1000, status=status)
    db.add(ch)
    db.commit()
    return ch


def test_reject_all_rejects_only_waiting(client, db):
    a = F.account(db, "kit", "КИТ")
    p, b, ap = _change(db, a, "proposed"), _change(db, a, "blocked", "s2"), _change(db, a, "approved", "s3")
    page = client.get("/prices?view=proposals").text
    assert "Отклонить все ожидающие (2)" in page
    r = client.post("/prices/reject-all")
    assert "Отклонено предложений: 2" in r.text
    db.expire_all()
    assert (db.get(PriceChange, p.id).status, db.get(PriceChange, b.id).status,
            db.get(PriceChange, ap.id).status) == ("rejected", "rejected", "approved")


def test_lamoda_is_not_sent_from_sku_prices_nor_approved(client, db):
    lam = F.account(db, "lamoda", "Ламода")
    F.manual_rate(db)
    page = client.get("/sku-prices?scope=lamoda").text
    assert "Передать на Ламоду отсюда нельзя" in page and 'formaction="/sku-prices/send"' not in page
    r = client.post("/sku-prices/send", data={"scope": "lamoda", "arts": ["100"]})
    assert "На Ламоду отсюда цены не передаются" in r.text
    ch = _change(db, lam, "proposed")
    r = client.post("/prices/approve", data={"ids": [str(ch.id)]})
    assert "lamoda_apply.py" in r.text
    db.expire_all()
    assert db.get(PriceChange, ch.id).status == "proposed"


def test_wb_page_explains_etalon_and_still_sends(client, db):
    F.account(db, "wb", "ИП 1")
    F.manual_rate(db)
    page = client.get("/sku-prices?scope=wb").text
    assert "эталону" in page and 'formaction="/sku-prices/send"' in page
