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


def test_keys_page_opens_when_secrets_key_changed(client, db):
    """Аудит 14.10: ключи, зашифрованные другим REPRICER_SECRETS_KEY, роняли «API-ключи» в 500 —
    вписать их заново было негде; отправка писала ошибку без текста."""
    import pytest
    from cryptography.fernet import Fernet

    from priceapp import accounts
    from priceapp.models import ApiCredential
    acc = F.account(db, "wb", "ИП 1")
    db.add(ApiCredential(account_id=acc.id, field_name="token",
                         encrypted_value=Fernet(Fernet.generate_key()).encrypt(b"old").decode()))
    db.commit()
    page = client.get("/api-keys")
    assert page.status_code == 200 and accounts.UNREADABLE in page.text
    with pytest.raises(RuntimeError, match="ИП 1"):
        accounts.credentials(db, acc)


def test_backup_retention_keeps_a_copy_per_day_and_everything_from_today():
    """Аудит 14.10: «14 последних файлов» — копии наката за два-три дня вытесняли суточные."""
    from datetime import datetime, timedelta
    from pathlib import Path

    from priceapp import backup
    now = datetime(2026, 10, 14, 12, 0)
    names = []
    for d in range(20):                                   # по две копии в день за 20 дней
        for h in (3, 9):
            t = now - timedelta(days=d, hours=h)
            names.append(Path(f"repricer-{t:%Y%m%d-%H%M%S}.db"))
    names.append(Path("repricer-ручная.db"))
    drop = set(backup.names_to_drop(names, now))
    kept = [n for n in names if n not in drop]
    assert Path("repricer-ручная.db") in kept
    assert all(n in kept for n in names[:2])              # моложе суток — обе
    days = {n.stem[9:17] for n in kept if n.stem[9:].replace("-", "").isdigit()}
    assert len(days) == 15                                # сегодня + 14 дней по одной


def test_proposal_older_than_a_day_is_not_approved():
    """Аудит 14.10: предложение недельной давности подтверждалось и уходило с тогдашним шагом."""
    from datetime import timedelta

    from priceapp.pricing import approve
    from priceapp.timeutils import now_utc
    ch = PriceChange(item_id="s", account_id=1, barcode="b", new_price=1000, status="proposed",
                     created_at=now_utc() - timedelta(days=2))
    assert "больше суток" in approve(ch, "op") and ch.status == "proposed"
    ch.created_at = now_utc()
    assert approve(ch, "op") is None and ch.status == "approved"


def test_queued_price_can_be_cancelled_from_the_log(client, db):
    acc = F.account(db, "wb", "ИП 1")
    _change(db, acc, "approved")
    page = client.get("/prices?view=log").text
    assert 'name="back" value="log"' in page
    ch = db.query(PriceChange).one()
    r = client.post("/prices/reject", data={"ids": str(ch.id), "back": "log"})
    assert "не уйдёт" in r.text
    db.expire_all()
    assert db.query(PriceChange).one().status == "rejected"


def test_who_sets_prices_follows_the_settings(client, db):
    """Аудит 14.10: карточка была зашитым текстом — сдвиги поменяли, а она писала прежнее."""
    from priceapp import settings
    F.account(db, "wb", "ИП 1")
    settings.put(db, settings.ETALON_SHIFT_KIT, "40")
    settings.put(db, settings.ETALON_SHIFT_OZON, "20")
    db.commit()
    page = client.get("/attention").text
    assert "эталон − 40%" in page and "эталон − 20%" in page and "эталон − 45%" not in page


def test_exchange_archive_is_pruned_by_age(tmp_path):
    import os
    import time

    from priceapp import exchange
    old = tmp_path / "pricing" / "2026" / "dict_old.txt"
    new = tmp_path / "pricing" / "dict_new.txt"
    old.parent.mkdir(parents=True)
    old.write_text("x")
    new.write_text("y")
    t = time.time() - 61 * 86400
    os.utime(old, (t, t))
    assert exchange.prune_archive(tmp_path) == 1 and new.exists() and not old.exists()


def test_orphan_markups_can_be_seen_and_deleted(client, db):
    """Аудит 14.10: находка «наценки-сироты» вела туда, где их нет, и горела вечно."""
    from decimal import Decimal

    from priceapp.models import ArticleCoef
    F.account(db, "wb", "ИП 1")
    db.add(ArticleCoef(article="ПРОПАВШИЙ-1", platform="wb", account_id=0, coef=Decimal("2"), kind="coef"))
    db.commit()
    assert "/orphans" in client.post("/attention/refresh").text
    assert "ПРОПАВШИЙ-1" in client.get("/orphans").text
    r = client.post("/orphans/delete")
    assert "Удалено наценок без товаров: 1" in r.text
    assert db.query(ArticleCoef).count() == 0


def test_retention_drops_only_old_finished_records(db):
    from datetime import timedelta

    from priceapp import retention
    from priceapp.models import AuditLog
    from priceapp.timeutils import now_utc
    acc = F.account(db, "wb", "ИП 1")
    old = now_utc() - timedelta(days=200)
    for status in ("sent", "rejected", "approved", "error", "proposed", "blocked"):
        db.add(PriceChange(item_id="s", account_id=acc.id, barcode="b", new_price=1, status=status, created_at=old))
    db.add(PriceChange(item_id="s", account_id=acc.id, barcode="b", new_price=1, status="sent"))   # свежая
    db.add(AuditLog(username="x", action="a", object="", details="", at=now_utc() - timedelta(days=400)))
    db.add(AuditLog(username="x", action="b", object="", details=""))
    db.commit()
    st = retention.prune(db)
    assert st == {"price_changes": 2, "audit": 1}
    assert sorted(c.status for c in db.query(PriceChange)) == ["approved", "blocked", "error", "proposed", "sent"]
