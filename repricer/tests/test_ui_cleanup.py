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


def test_platform_refusal_goes_quiet_once_a_later_price_went_out(db):
    """Аудит 14.10: «Площадка не приняла цену» считала все `error` за всё время — срочная
    строка не гасла никогда. Отказ, после которого цена по паре ушла, больше не считается."""
    from datetime import timedelta

    from priceapp import overview
    from priceapp.models import ProductPrice
    from priceapp.timeutils import now_utc
    acc = F.account(db, "wb", "ИП 1")
    t = now_utc() - timedelta(hours=2)
    for item in ("a", "b", "c"):
        db.add(PriceChange(item_id=item, account_id=acc.id, barcode=item, new_price=1000, status="error",
                           decided_at=t))
    db.add(PriceChange(item_id="a", account_id=acc.id, barcode="a", new_price=1100, status="sent"))   # очередью
    db.add(ProductPrice(item_id="b", account_id=acc.id, last_sent_price=1200, last_sent_at=now_utc()))  # эталоном
    db.commit()
    assert [c.item_id for c in overview.live_errors(db)] == ["c"]


def test_apostrophe_in_article_does_not_break_the_confirmation(client, db):
    """Аудит 14.10: `confirm('…{{ x }}…')` — `'` раскодировался браузером до разбора JS,
    обработчик падал, и форма уходила БЕЗ вопроса."""
    from priceapp.templating import js
    assert str(js("O'NEIL")) == "O\\&#39;NEIL"


def test_a_failing_job_does_not_kill_the_background_thread(monkeypatch):
    """Аудит 14.10: исключение вне задания (heartbeat при занятой базе) навсегда останавливало
    поток — отправка и 1С стояли до перезапуска службы, а процесс оставался живым."""
    from priceapp.workers import background, jobs
    calls = []

    def boom():
        calls.append(1)
        if len(calls) >= 2:
            background._stop.set()
        raise RuntimeError("database is locked")

    monkeypatch.setattr(jobs, "job_onec_exchange", boom)
    for name in ("job_rate", "job_price_dispatch", "job_backup"):
        monkeypatch.setattr(jobs, name, lambda: None)
    monkeypatch.setattr(background, "TICK", 0)
    background._stop.clear()
    background._loop()                 # раньше — исключение наружу на первом же круге
    assert len(calls) == 2
    background._stop.clear()
