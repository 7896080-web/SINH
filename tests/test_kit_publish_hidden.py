"""Карточка, спрятанная площадкой за нулевой остаток, сама на витрину не вернётся.

На витрине Kit есть настройка «скрывать товары с нулевым остатком»: когда
остаток доходит до нуля, карточка переходит в статус `HIDDEN`. Обратного хода у
этого НЕТ — приход остатка статус не меняет. Снаружи это выглядит хуже, чем
поломка: остаток мы передали, площадка его приняла, у нас всё зелено, а товара
на витрине нет и продаж по нему не будет никогда.

Спека (`VariantStatus`, `PATCH /v1/variants/{id}`) даёт ровно то, что нужно:
статус `PUBLISHED` | `HIDDEN` | `ARCHIVED` и merge-patch, которым переводится
один статус, не трогая остальную карточку.

Главное, что проверяется здесь, — что мы НЕ публикуем лишнего. Статус `HIDDEN`
у площадки один и на «спрятала автоматика», и на «спрятал человек»: снял с
продажи, спорный товар, не сезон. Отличить их нельзя, поэтому решение принимает
человек один раз по кабинету, а дальше мы трогаем только те карточки, на
которые прямо сейчас ушёл ненулевой остаток.
"""

import pytest
import requests

from app.models import (AuditLog, Barcode, DispatchQueueItem, DispatchStatus,
                        Platform, PlatformCatalogItem, Product, SyncSetting)
from app.workers.dispatch import run_dispatch_cycle
from app.workers.platform_clients.kit import KitClient
from tests.factories import make_account


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("app.workers.http_retry.time.sleep", lambda _s: None)
    monkeypatch.setattr("app.workers.platform_clients.kit.time.sleep", lambda _s: None)


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = ""
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


class _Kit(KitClient):
    """Живой клиент Kit поверх поддельной сессии: разбор ответов — настоящий."""

    def __init__(self, hidden=(), pages=None, patch_fails=()):
        super().__init__(token="t")
        self.published = []
        self._hidden = list(hidden)
        self._pages = pages
        self._patch_fails = set(patch_fails)
        self.pushed = []

        def get(url, params=None, timeout=None):
            if self._pages is not None:
                return _Resp(200, self._pages[(params or {}).get("page", 1)])
            return _Resp(200, {"variants": [
                {"id": v, "status": "HIDDEN"} for v in self._hidden
            ], "total_count": len(self._hidden)})

        def post(url, json=None, timeout=None):
            self.pushed.append(json)
            return _Resp(204, None)

        def patch(url, json=None, headers=None, timeout=None):
            key = url.rsplit("/", 1)[-1]
            if key in self._patch_fails:
                return _Resp(400, {"code": "VALIDATION_ERROR"})
            self.published.append((key, json, (headers or {}).get("Content-Type")))
            return _Resp(200, {"id": key, "status": "PUBLISHED"})

        self.session.get = get
        self.session.post = post
        self.session.patch = patch


# ------------------------------------------------------------ сам клиент

def test_publishing_sends_only_the_status():
    """Merge-patch меняет только переданные поля. Приложить сюда заодно остатки
    было бы опасно: в этом методе они ЗАМЕНЯЮТ весь список складов целиком."""
    c = _Kit()

    assert c.publish_stock_key("v-1") is True
    key, body, content_type = c.published[0]
    assert key == "v-1"
    assert body == {"status": "PUBLISHED"}
    assert content_type == "application/merge-patch+json"


def test_a_refused_patch_is_reported_as_failure():
    c = _Kit(patch_fails={"v-1"})

    assert c.publish_stock_key("v-1") is False


def test_hidden_keys_are_read_page_by_page():
    c = _Kit(pages={
        1: {"variants": [{"id": f"v{i}", "status": "HIDDEN"} for i in range(100)],
            "total_count": 130},
        2: {"variants": [{"id": f"w{i}", "status": "HIDDEN"} for i in range(30)],
            "total_count": 130},
    })

    keys = c.hidden_stock_keys()

    assert len(keys) == 130, "короткая страница — не конец ленты, конец говорит total_count"


def test_a_published_card_never_gets_into_the_hidden_set():
    """Неизвестные параметры Kit молча игнорирует. Если фильтр по статусу
    однажды перестанет действовать, мы получим ВЕСЬ каталог — и без своей
    проверки опубликовали бы всё подряд, включая спрятанное человеком."""
    c = _Kit(pages={1: {"variants": [
        {"id": "v-скрыт", "status": "HIDDEN"},
        {"id": "v-открыт", "status": "PUBLISHED"},
        {"id": "v-архив", "status": "ARCHIVED"},
    ], "total_count": 3}})

    assert c.hidden_stock_keys() == {"v-скрыт"}


def test_a_silent_platform_gives_none_not_an_empty_set():
    """Пустое множество значит «скрытых нет» и безопасно. Молчание площадки —
    это «мы не знаем», и путать их нельзя: по `None` публикация не делается."""
    c = _Kit()

    def boom(url, params=None, timeout=None):
        raise requests.ConnectionError("сеть")

    c.session.get = boom

    assert c.hidden_stock_keys() is None


# -------------------------------------------------------------- рассылка

def _product(db, account, uid, barcode, variant_id, stock=5):
    db.add(Product(uid_1c=uid, article=uid, name="Товар", stock_on_hand=stock,
                   broadcast_enabled=True, recalc_account_ids=str(account.id)))
    db.add(Barcode(barcode=barcode, uid_1c=uid))
    db.add(SyncSetting(uid_1c=uid, account_id=account.id, enabled=True))
    db.add(PlatformCatalogItem(account_id=account.id, external_id=variant_id,
                               barcode=barcode, article="A", name="Карточка"))
    db.add(DispatchQueueItem(uid_1c=uid, account_id=account.id, quantity=stock,
                             reason="reconciliation"))


def _kit_account(db, publish=True):
    account = make_account(db, Platform.kit, name="КИТ", warehouse_id="wh-1")
    account.publish_hidden_on_stock = publish
    db.commit()
    return account


def test_a_restocked_card_returns_to_the_storefront(db):
    account = _kit_account(db)
    _product(db, account, "u1", "111", "v-скрыт", stock=7)
    db.commit()
    client = _Kit(hidden=["v-скрыт"])

    stats = run_dispatch_cycle(db, {account.id: client}, [account])

    assert [k for k, _, _ in client.published] == ["v-скрыт"]
    assert stats["КИТ"]["published"] == 1


def test_publishing_is_recorded_in_the_journal(db):
    """Мы меняем статус чужой карточки. Человек должен потом видеть, что
    изменилось и почему, — иначе «кто её опубликовал» не выяснить."""
    account = _kit_account(db)
    _product(db, account, "u1", "111", "v-скрыт", stock=7)
    db.commit()

    run_dispatch_cycle(db, {account.id: _Kit(hidden=["v-скрыт"])}, [account])

    entry = db.query(AuditLog).filter(AuditLog.action == "variant_published").one()
    assert "v-скрыт" in entry.details


def test_a_failed_publish_is_recorded_too(db):
    """Молча проглотить отказ нельзя: остаток на площадке есть, а товар
    покупателям не виден — и никто об этом не узнает."""
    account = _kit_account(db)
    _product(db, account, "u1", "111", "v-скрыт", stock=7)
    db.commit()

    run_dispatch_cycle(db, {account.id: _Kit(hidden=["v-скрыт"], patch_fails={"v-скрыт"})},
                       [account])

    entry = db.query(AuditLog).filter(AuditLog.action == "variant_publish_failed").one()
    assert "v-скрыт" in entry.details


def test_nothing_is_published_without_the_cabinet_switch(db):
    """Выключено — значит карточки не трогаем вовсе, даже скрытые и с приходом."""
    account = _kit_account(db, publish=False)
    _product(db, account, "u1", "111", "v-скрыт", stock=7)
    db.commit()
    client = _Kit(hidden=["v-скрыт"])

    run_dispatch_cycle(db, {account.id: client}, [account])

    assert client.published == []


def test_a_zero_stock_never_publishes(db):
    """Ноль карточку на витрине не удержит, а показать людям товар, которого
    нет, — хуже, чем не показать ничего."""
    account = _kit_account(db)
    _product(db, account, "u1", "111", "v-скрыт", stock=0)
    db.commit()
    client = _Kit(hidden=["v-скрыт"])

    run_dispatch_cycle(db, {account.id: client}, [account])

    assert client.published == []


def test_a_card_the_platform_does_not_call_hidden_is_left_alone(db):
    """Публикуем только то, что площадка СЕЙЧАС держит скрытым. Иначе каждый
    приход остатка дёргал бы статус карточек, у которых с ним всё в порядке."""
    account = _kit_account(db)
    _product(db, account, "u1", "111", "v-открыт", stock=7)
    db.commit()
    client = _Kit(hidden=["v-другая"])

    run_dispatch_cycle(db, {account.id: client}, [account])

    assert client.published == []


def test_a_silent_platform_publishes_nothing(db):
    """Спросить не удалось — не знаем, что скрыто. Публиковать вслепую нельзя."""
    account = _kit_account(db)
    _product(db, account, "u1", "111", "v-скрыт", stock=7)
    db.commit()
    client = _Kit(hidden=["v-скрыт"])

    def boom(url, params=None, timeout=None):
        raise requests.ConnectionError("сеть")

    client.session.get = boom

    run_dispatch_cycle(db, {account.id: client}, [account])

    assert client.published == []


def test_a_failed_push_never_publishes(db):
    """Остаток не уехал — публиковать нечего: на площадке по-прежнему ноль."""
    account = _kit_account(db)
    _product(db, account, "u1", "111", "v-скрыт", stock=7)
    db.commit()
    client = _Kit(hidden=["v-скрыт"])

    def refuse(url, json=None, timeout=None):
        return _Resp(500, {"code": "INTERNAL"})

    client.session.post = refuse

    run_dispatch_cycle(db, {account.id: client}, [account])

    assert client.published == []


def test_the_sent_key_is_the_one_the_platform_addresses(db):
    """`sent_sku` — это идентификатор, КОТОРЫМ ушло число. У Kit это variant_id,
    а не баркод: по баркоду ни сверить отправку, ни опубликовать карточку."""
    account = _kit_account(db, publish=False)
    _product(db, account, "u1", "111", "v-1", stock=7)
    db.commit()

    run_dispatch_cycle(db, {account.id: _Kit()}, [account])

    assert db.query(DispatchQueueItem).one().sent_sku == "v-1"


# ------------------------------------------------------------------- UI

def test_the_switch_is_offered_for_a_kit_cabinet(logged_in_client, web_db):
    from app.models import PlatformAccount

    web_db.add(PlatformAccount(platform=Platform.kit, name="КИТ", warehouse_id="wh-1"))
    web_db.commit()

    page = logged_in_client.get("/api-keys")

    assert "publish_hidden_on_stock" in page.text
    assert "Возвращать скрытые карточки на витрину" in page.text


def test_the_switch_is_not_offered_where_it_does_nothing(logged_in_client, web_db):
    """У WB и Ozon такой механики нет, и обещать её галочкой значит соврать."""
    from app.models import PlatformAccount

    web_db.add(PlatformAccount(platform=Platform.wb, name="ИП ЯВОРСКАЯ", warehouse_id="wh-1"))
    web_db.commit()

    page = logged_in_client.get("/api-keys")

    assert "publish_hidden_on_stock" not in page.text


def test_turning_the_switch_on_is_written_to_the_journal(logged_in_client, web_db):
    from app.models import PlatformAccount

    account = PlatformAccount(platform=Platform.kit, name="КИТ", warehouse_id="wh-1")
    web_db.add(account)
    web_db.commit()
    web_db.refresh(account)

    r = logged_in_client.post(f"/api-keys/accounts/{account.id}/publish-hidden",
                              data={"publish_hidden_on_stock": "on"},
                              follow_redirects=False)

    assert r.status_code == 303
    web_db.refresh(account)
    assert account.publish_hidden_on_stock is True
    assert web_db.query(AuditLog).filter(AuditLog.action == "publish_hidden_changed").count() == 1


def test_the_switch_turns_off_again(logged_in_client, web_db):
    from app.models import PlatformAccount

    account = PlatformAccount(platform=Platform.kit, name="КИТ", warehouse_id="wh-1",
                              publish_hidden_on_stock=True)
    web_db.add(account)
    web_db.commit()
    web_db.refresh(account)

    logged_in_client.post(f"/api-keys/accounts/{account.id}/publish-hidden", data={})

    web_db.refresh(account)
    assert account.publish_hidden_on_stock is False
