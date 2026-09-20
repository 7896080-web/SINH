"""Один неизвестный sku не должен ронять всю пачку.

Найдено на бою 20.09.2026. После массовой переотправки в очереди на ИП ЯВОРСКАЯ
лежала сотня позиций, и все сто висели в повторе. В теле ответа WB:

    409 Conflict: [{"data":[{"sku":"2000932279695","chrtId":0,"amount":0}],
                    "code":"NotFound","message":"Not found"}]

Один баркод из ста площадка на этом складе не знала — и не применилось НИ ОДНО
из остальных девяноста девяти. Повтор тут бессмыслен: неизвестным sku он и
останется, то есть сотня живых карточек не получила бы свой остаток никогда.

Поэтому виновника вынимаем из запроса и шлём остальное, а его самого закрываем
сразу: пять попыток с паузами ответа не изменят, зато на полчаса оттянут момент,
когда человек узнает, что товар отмечен для кабинета без карточки.
"""

import requests

from app.models import (Barcode, DispatchQueueItem, DispatchStatus, Platform,
                        Product, SyncSetting)
from app.workers.dispatch import run_dispatch_cycle
from app.workers.platform_clients.base import StockPushItem
from app.workers.platform_clients.wb import WbClient
from tests.factories import make_account


class _Resp:
    def __init__(self, status_code=204, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("нет тела")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error", response=self)


def _not_found(skus):
    return _Resp(409, text="конфликт", payload=[{
        "data": [{"sku": s, "chrtId": 0, "amount": 0} for s in skus],
        "code": "NotFound", "message": "Not found",
    }])


def _client_rejecting(bad):
    """Клиент WB, у которого площадка не знает перечисленные sku."""
    c = WbClient(token="t", warehouse_id="wh")
    c.sent = []

    def put(url, json=None, **kw):
        skus = [row["sku"] for row in json["stocks"]]
        c.sent.append(skus)
        hit = [s for s in skus if s in bad]
        return _not_found(hit) if hit else _Resp()

    c.session.put = put
    return c


# ----------------------------------------------------- пачка больше не падает

def test_the_good_skus_go_through_despite_one_unknown():
    """Ровно случай 20.09: один из ста неизвестен, девяносто девять обязаны уйти."""
    c = _client_rejecting({"плохой"})
    items = [StockPushItem(barcode=f"ok{i}", quantity=i) for i in range(5)]
    items.insert(2, StockPushItem(barcode="плохой", quantity=7))

    res = c.push_stock("wh", items)

    assert sorted(res["ok"]) == sorted(f"ok{i}" for i in range(5))
    assert len(c.sent) == 2, "первый запрос отвергнут, второй — уже без виновника"
    assert "плохой" not in c.sent[1]


def test_the_unknown_sku_is_reported_as_terminal():
    """Повторять нечего: пока карточки на складе нет, ответ будет тот же."""
    c = _client_rejecting({"плохой"})

    res = c.push_stock("wh", [StockPushItem(barcode="плохой", quantity=1),
                              StockPushItem(barcode="ok", quantity=2)])

    bad = [e for e in res["errors"] if e.get("sku") == "плохой"]
    assert bad, "виновник обязан быть назван поимённо"
    assert bad[0]["terminal"] is True
    assert "не знает этот sku" in bad[0]["detail"]


def test_several_unknown_skus_are_all_dropped():
    """WB не обещал называть все неизвестные сразу — вынимаем, пока не кончатся."""
    c = _client_rejecting({"плохой1", "плохой2"})

    res = c.push_stock("wh", [StockPushItem(barcode="плохой1", quantity=1),
                              StockPushItem(barcode="плохой2", quantity=1),
                              StockPushItem(barcode="хороший", quantity=3)])

    assert res["ok"] == ["хороший"]
    assert {e["sku"] for e in res["errors"]} == {"плохой1", "плохой2"}


def test_a_batch_where_everything_is_unknown_ends_without_a_send():
    c = _client_rejecting({"a", "b"})

    res = c.push_stock("wh", [StockPushItem(barcode="a", quantity=1),
                              StockPushItem(barcode="b", quantity=1)])

    assert res["ok"] == []
    assert {e["sku"] for e in res["errors"]} == {"a", "b"}


def test_a_conflict_we_do_not_understand_is_not_silently_trimmed():
    """Другой код 409 значит что-то иное. Выкидывать по нему позиции нельзя: мы
    не знаем, что именно площадка забраковала, и урезать отправку молча значило
    бы решить за неё."""
    c = WbClient(token="t", warehouse_id="wh")
    c.session.put = lambda *a, **kw: _Resp(409, text="занято", payload=[
        {"code": "Conflict", "message": "Уже обрабатывается"}])

    res = c.push_stock("wh", [StockPushItem(barcode="111", quantity=1)])

    assert res["ok"] == []
    assert res["errors"] and "sku" not in res["errors"][0]


# ------------------------------------------- рассылка не жжёт на этом повторы

def test_dispatch_closes_an_unknown_sku_at_once(db):
    """Пять попыток с паузами ответа не изменят, зато оттянут на полчаса момент,
    когда человек узнает про неразрешённую пару товар+кабинет."""
    account = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ", warehouse_id="wh-1")
    for uid, bc in (("u1", "хороший"), ("u2", "плохой")):
        db.add(Product(uid_1c=uid, article=uid, name="Товар", stock_on_hand=5,
                       broadcast_enabled=True, recalc_account_ids=str(account.id)))
        db.add(Barcode(barcode=bc, uid_1c=uid))
        db.add(SyncSetting(uid_1c=uid, account_id=account.id, enabled=True))
        db.add(DispatchQueueItem(uid_1c=uid, account_id=account.id, quantity=5,
                                 reason="manual_resend_all"))
    db.commit()

    run_dispatch_cycle(db, {account.id: _client_rejecting({"плохой"})}, [account])

    good = db.query(DispatchQueueItem).filter(DispatchQueueItem.uid_1c == "u1").one()
    bad = db.query(DispatchQueueItem).filter(DispatchQueueItem.uid_1c == "u2").one()
    assert good.status == DispatchStatus.sent, "хороший товар уехал, несмотря на соседа"
    assert bad.status == DispatchStatus.error, "виновник закрыт сразу, без пяти попыток"
    assert bad.attempts == 1
    assert bad.next_attempt_at is None
    assert "не знает этот sku" in bad.last_error


# ---------------------------------------------------------- отчёт различает

def test_the_report_tells_an_unknown_sku_apart_from_a_broken_dispatch(db):
    """Следствия разные: там, где карточки нет, продавать нечего и оверселла не
    будет. Написать про неё «площадка продаёт то, чего нет» значит отправить
    человека чинить связь вместо мэппинга."""
    from app.report import collect_findings

    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=5, sent_sku="2000932279695",
        reason="manual_resend_all", status=DispatchStatus.error,
        last_error="площадка не знает этот sku на складе 1923790 (409 NotFound)"))
    db.commit()

    findings = {f.key: f for f in collect_findings(db)}

    assert "unknown_sku" in findings
    assert "dispatch_errors" not in findings, "в «рассылка не доехала» ей не место"
    assert "2000932279695" in findings["unknown_sku"].details[0]


def test_a_dispatch_error_without_any_text_is_still_shown(db):
    """`NOT LIKE` по пустому полю даёт NULL — без явной проверки на NULL такая
    запись выпала бы из отчёта вообще, ни в одну из двух находок."""
    from app.report import collect_findings

    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=5,
                             reason="order", status=DispatchStatus.error))
    db.commit()

    assert "dispatch_errors" in {f.key for f in collect_findings(db)}
