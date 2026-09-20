"""Одна непринятая позиция не должна ронять весь запрос остатков на Kit.

Найдено на бою 20.09.2026. По кабинету КИТ в очереди лежало 642 записи, и ВСЕ
они были в `error`, исчерпав по пять попыток. Ответ на каждый запрос один и тот
же:

    400 Bad Request /v1/variants/stocks/bulk_update
    {"code": "VALIDATION_ERROR", "message": "Некорректные данные запроса", ...}

Причина — 11% позиций уходили с баркодом вместо `variant_id`: карточек этих
товаров в каталоге кабинета нет. Kit проверяет тело целиком и бракует его
целиком, поэтому в каждой пачке по сто хватало одной такой позиции, чтобы не
уехало НИ ОДНОГО остатка. За всё время на Kit не ушло ничего — ни по массовой
переотправке, ни по обычным событиям.

Спека (`BulkOperationError`) при этом называет виновных поимённо: рядом с общим
`code` идёт список `errors` с `variant_id` и кодом ошибки элемента. Его мы не
читали, а в `last_error` он и не попадал — рассылка режет текст, и список
обрезало.

Лечится с двух сторон, и обе тут проверяются: позиция без нужного площадке
идентификатора в запрос не попадает вовсе, а названных площадкой виновных
клиент вынимает и досылает остальное.
"""

import pytest
import requests

from app.models import (Barcode, DispatchQueueItem, DispatchStatus, Platform,
                        PlatformCatalogItem, Product, SyncSetting)
from app.workers.dispatch import run_dispatch_cycle
from app.workers.platform_clients.base import StockPushItem
from app.workers.platform_clients.kit import KitClient
from tests.factories import make_account


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """Паузы повторов здесь проверять нечего, а ждать их — секунды на тест."""
    monkeypatch.setattr("app.workers.http_retry.time.sleep", lambda _s: None)
    monkeypatch.setattr("app.workers.platform_clients.kit.time.sleep", lambda _s: None)


class _Resp:
    def __init__(self, status_code=204, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = "" if payload is None else str(payload)
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("нет тела")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error", response=self)


def _rejecting(bad: dict[str, str]):
    """Клиент Kit, у которого площадка бракует перечисленные варианты.

    `bad` — {variant_id: код ошибки элемента}, как в спеке.
    """
    c = KitClient(token="t")
    c.sent = []

    def post(url, json=None, **kw):
        variants = [row["variant_id"] for row in json["items"]]
        c.sent.append(variants)
        hit = {v: bad[v] for v in variants if v in bad}
        if not hit:
            return _Resp()
        return _Resp(400, payload={
            "code": "VALIDATION_ERROR",
            "message": "Некорректные данные запроса",
            "trace_id": "t-1",
            "errors": [{"variant_id": v, "warehouse_id": "wh", "code": code,
                        "message": "..."} for v, code in hit.items()],
        })

    c.session.post = post
    return c


def _item(barcode, variant_id, quantity=5):
    return StockPushItem(barcode=barcode, quantity=quantity, external_id=variant_id)


# ----------------------------------------------------- запрос больше не падает

def test_the_good_variants_go_through_despite_one_unknown():
    """Ровно случай 20.09: одной карточки в кабинете нет, остальные обязаны уехать."""
    c = _rejecting({"v-нет": "VARIANT_NOT_FOUND"})
    items = [_item(f"bc{i}", f"v{i}") for i in range(5)]
    items.insert(2, _item("bc-нет", "v-нет"))

    res = c.push_stock("wh", items)

    assert sorted(res["ok"]) == sorted(f"bc{i}" for i in range(5))
    assert len(c.sent) == 2, "первый запрос отвергнут, второй — уже без виновника"
    assert "v-нет" not in c.sent[1]


def test_the_unknown_variant_is_reported_as_terminal():
    """Повторять нечего: пока карточки в кабинете нет, ответ будет тот же."""
    c = _rejecting({"v-нет": "VARIANT_NOT_FOUND"})

    res = c.push_stock("wh", [_item("bc-нет", "v-нет"), _item("bc", "v")])

    bad = [e for e in res["errors"] if e.get("sku") == "bc-нет"]
    assert bad, "виновник обязан быть назван, и именно баркодом — рассылка ищет по нему"
    assert bad[0]["terminal"] is True
    assert "v-нет" in bad[0]["detail"], "в тексте должен быть variant_id, с которым идти в кабинет"


def test_an_archived_card_is_terminal_too():
    """Архивную карточку площадка не примет, сколько ни повторяй, — и текст
    обязан отличаться: чинится это не мэппингом, а разархивацией."""
    c = _rejecting({"v-арх": "VARIANT_ARCHIVED"})

    res = c.push_stock("wh", [_item("bc-арх", "v-арх"), _item("bc", "v")])

    assert res["ok"] == ["bc"]
    assert "архив" in res["errors"][0]["detail"]


def test_several_bad_variants_are_all_dropped():
    c = _rejecting({"v1": "VARIANT_NOT_FOUND", "v2": "VARIANT_ARCHIVED"})

    res = c.push_stock("wh", [_item("bc1", "v1"), _item("bc2", "v2"), _item("bc3", "v3")])

    assert res["ok"] == ["bc3"]
    assert {e["sku"] for e in res["errors"]} == {"bc1", "bc2"}


def test_a_broken_warehouse_never_drops_positions():
    """`WAREHOUSE_NOT_FOUND` — про настройку кабинета, а не про товар. Выкинув по
    нему позиции, мы похоронили бы очередь целиком, хотя чинится это одной
    правкой поля «ID склада»: пусть висят в повторе и уедут после правки."""
    c = _rejecting({"v1": "WAREHOUSE_NOT_FOUND", "v2": "WAREHOUSE_NOT_FOUND"})

    res = c.push_stock("wh", [_item("bc1", "v1"), _item("bc2", "v2")])

    assert res["ok"] == []
    assert len(c.sent) == 1, "второго запроса быть не должно — вычитать нечего"
    assert all("sku" not in e for e in res["errors"]), "позиции не виноваты"
    assert not any(e.get("terminal") for e in res["errors"])


def test_an_error_code_we_do_not_know_is_not_silently_trimmed():
    """Незнакомый код значит что-то, чего мы не разбирали. Выкидывать по нему
    позиции нельзя: мы не знаем, что именно площадка забраковала."""
    c = _rejecting({"v1": "SOMETHING_NEW"})

    res = c.push_stock("wh", [_item("bc1", "v1"), _item("bc2", "v2")])

    assert res["ok"] == []
    assert len(c.sent) == 1
    assert all("sku" not in e for e in res["errors"])


def test_a_position_without_a_variant_id_never_enters_the_request():
    """Падение на баркод («вдруг поймёт») — это и есть дефект 20.09: площадка
    отвечает на такое отказом ВСЕМУ телу, а не одной строке."""
    c = _rejecting({})

    res = c.push_stock("wh", [_item("bc-нет", ""), _item("bc", "v")])

    assert c.sent == [["v"]], "в запрос ушла только позиция с variant_id"
    assert res["ok"] == ["bc"]
    assert res["errors"][0]["sku"] == "bc-нет"
    assert res["errors"][0]["terminal"] is True


def test_a_batch_where_everything_is_bad_ends_without_a_send():
    c = _rejecting({"v1": "VARIANT_NOT_FOUND"})

    res = c.push_stock("wh", [_item("bc1", "v1")])

    assert res["ok"] == []
    assert {e["sku"] for e in res["errors"]} == {"bc1"}


# --------------------------------------------- рассылка не отправляет вслепую

def _catalogued(db, account, uid, barcode, variant_id=None):
    db.add(Product(uid_1c=uid, article=uid, name="Товар", stock_on_hand=5,
                   broadcast_enabled=True, recalc_account_ids=str(account.id)))
    db.add(Barcode(barcode=barcode, uid_1c=uid))
    db.add(SyncSetting(uid_1c=uid, account_id=account.id, enabled=True))
    db.add(DispatchQueueItem(uid_1c=uid, account_id=account.id, quantity=5,
                             reason="manual_resend_all"))
    if variant_id:
        db.add(PlatformCatalogItem(account_id=account.id, external_id=variant_id,
                                   barcode=barcode, article="A", name="Карточка"))


def test_dispatch_does_not_send_a_product_missing_from_the_cabinet_catalogue(db):
    """Товар без строки каталога кабинета отправлять не по чему. Раньше он
    уходил с баркодом в поле variant_id и ронял весь запрос — вместе с сотней
    исправных соседей."""
    account = make_account(db, Platform.kit, name="КИТ", warehouse_id="wh-1")
    _catalogued(db, account, "u-есть", "111", variant_id="v-1")
    _catalogued(db, account, "u-нет", "222")
    db.commit()
    client = _rejecting({})

    run_dispatch_cycle(db, {account.id: client}, [account])

    assert client.sent == [["v-1"]], "в запрос ушла только карточка из каталога"
    good = db.query(DispatchQueueItem).filter(DispatchQueueItem.uid_1c == "u-есть").one()
    bad = db.query(DispatchQueueItem).filter(DispatchQueueItem.uid_1c == "u-нет").one()
    assert good.status == DispatchStatus.sent
    assert bad.status == DispatchStatus.error
    assert bad.next_attempt_at is None, "повтор не поможет — карточки нет"
    assert "каталог" in bad.last_error


def test_dispatch_closes_a_rejected_variant_at_once(db):
    """Пять попыток с паузами ответа не изменят, зато на полчаса оттянут момент,
    когда человек узнает про неразрешённую пару товар+кабинет."""
    account = make_account(db, Platform.kit, name="КИТ", warehouse_id="wh-1")
    _catalogued(db, account, "u1", "111", variant_id="v-1")
    _catalogued(db, account, "u2", "222", variant_id="v-нет")
    db.commit()

    run_dispatch_cycle(db, {account.id: _rejecting({"v-нет": "VARIANT_NOT_FOUND"})}, [account])

    good = db.query(DispatchQueueItem).filter(DispatchQueueItem.uid_1c == "u1").one()
    bad = db.query(DispatchQueueItem).filter(DispatchQueueItem.uid_1c == "u2").one()
    assert good.status == DispatchStatus.sent, "исправный товар уехал, несмотря на соседа"
    assert bad.status == DispatchStatus.error
    assert bad.attempts == 1
    assert bad.next_attempt_at is None


def test_two_products_on_one_platform_card_do_not_kill_the_request(db):
    """Пара товар+склад не может повторяться в одном запросе (`DUPLICATE_ITEM`),
    а повтор уронил бы ВЕСЬ запрос. Дедупликация по uid_1c этого не ловит: два
    разных товара 1С могут вести на одну карточку площадки. Число уезжает по
    первому, второй закрывается внятным текстом — это дефект мэппинга, и решать
    его человеку: продажи спишутся не на тот товар."""
    account = make_account(db, Platform.kit, name="КИТ", warehouse_id="wh-1")
    _catalogued(db, account, "u1", "111", variant_id="v-общий")
    _catalogued(db, account, "u2", "222")
    db.add(PlatformCatalogItem(account_id=account.id, external_id="v-общий",
                               barcode="222", article="A", name="Та же карточка"))
    db.commit()
    client = _rejecting({})

    run_dispatch_cycle(db, {account.id: client}, [account])

    assert client.sent == [["v-общий"]], "карточка ушла ровно один раз"
    statuses = {r.uid_1c: r.status for r in db.query(DispatchQueueItem).all()}
    assert statuses["u1"] == DispatchStatus.sent
    assert statuses["u2"] == DispatchStatus.error
    second = db.query(DispatchQueueItem).filter(DispatchQueueItem.uid_1c == "u2").one()
    assert "мэппинг" in second.last_error


def test_wb_still_sends_a_product_without_a_catalogue_row(db):
    """У WB ключ остатка — сам баркод, и строка каталога для отправки не нужна.
    Гейт по идентификатору не должен превратиться в требование каталога для всех."""
    account = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ", warehouse_id="wh-1")
    _catalogued(db, account, "u1", "111")
    db.commit()

    class _Wb:
        stock_key = "barcode"

        def __init__(self):
            self.sent = []

        def push_stock(self, warehouse_id, items):
            self.sent.append([i.barcode for i in items])
            return {"ok": [i.barcode for i in items], "errors": []}

    client = _Wb()
    run_dispatch_cycle(db, {account.id: client}, [account])

    assert client.sent == [["111"]]
    assert db.query(DispatchQueueItem).one().status == DispatchStatus.sent
