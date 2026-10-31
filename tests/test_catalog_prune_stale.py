"""Строка каталога, пропавшая из выгрузки площадки, УБИРАЕТСЯ — и только она.

Загрузка каталога была ТОЛЬКО апсертом: правка карточки в кабинете до нас не
доезжала вовсе. Убрали чужой баркод из карточки на WB — а у нас он по-прежнему
ведёт на неё, то есть остаётся КЛЮЧОМ ОТПРАВКИ для своего товара и держит
ячейку двужильной. Починить это починкой кабинета было нельзя.

Цена ошибки несимметрична, и поэтому половина проверок здесь — про МОЛЧАНИЕ:
не убрали лишнего — сумма по ячейке уедет завышенной, но это видно находкой и
чинится следующей выгрузкой; убрали лишнее — у живой пары исчез ключ отправки,
рассылка закроет её терминально, и до следующей суточной выгрузки остаток не
уедет вовсе.
"""
from app.models import Barcode, Platform, PlatformCatalogItem, Product
from app.workers import catalog_sync
from app.workers.catalog_sync import load_platform_catalog
from app.workers.platform_clients.base import CatalogItem
from tests.factories import make_account


class FakeClient:
    def __init__(self, items, truncated=False):
        self._items = items
        self.last_truncated = truncated

    def get_catalog_items(self):
        return list(self._items)


def _item(barcode, external_id="100:200"):
    return CatalogItem(barcode=barcode, external_id=external_id, article="A", name="Товар")


def _account_with_catalog(db, barcodes):
    account = make_account(db, Platform.wb)
    for i, bc in enumerate(barcodes):
        db.add(Product(uid_1c=f"u{i}", article="A", name="Товар", stock_on_hand=1))
        db.add(Barcode(barcode=bc, uid_1c=f"u{i}"))
        db.add(PlatformCatalogItem(account_id=account.id, barcode=bc,
                                   external_id="100:200", article="A", name="Товар"))
    db.commit()
    return account


def _saved(db, account):
    return {r.barcode for r in db.query(PlatformCatalogItem).filter(
        PlatformCatalogItem.account_id == account.id).all()}


def test_a_barcode_gone_from_the_feed_is_removed(db):
    account = _account_with_catalog(db, ["b1", "b2", "b3", "b4"])

    stats = load_platform_catalog(db, FakeClient([
        _item("b1"), _item("b2"), _item("b3")]), account)

    assert stats["stale_removed"] == 1
    assert _saved(db, account) == {"b1", "b2", "b3"}, "убрана ровно пропавшая"
    assert not stats["stale_skipped"]


def test_the_1c_link_itself_is_not_touched(db):
    """Убираем строку КАТАЛОГА ПЛОЩАДКИ, а не привязку баркода к товару 1С.

    Привязка — первичный учёт, и снять её умеет только переподвязка под
    отдельной галочкой. Снеси уборка заодно и её, заказ по этому баркоду стало
    бы некуда разносить, а конфликт сопоставления завёлся бы на ровном месте.
    """
    account = _account_with_catalog(db, ["b1", "b2"])

    load_platform_catalog(db, FakeClient([_item("b1")]), account)

    assert {r.barcode for r in db.query(Barcode).all()} == {"b1", "b2"}


def test_another_cabinet_is_not_touched(db):
    """Та же карточка живёт в нескольких кабинетах — выгрузка одного ничего не
    говорит про каталог соседнего."""
    first = _account_with_catalog(db, ["b1", "b2"])
    second = make_account(db, Platform.wb, name="ИП второй")
    db.add(PlatformCatalogItem(account_id=second.id, barcode="b2",
                               external_id="100:200", article="A", name="Товар"))
    db.commit()

    load_platform_catalog(db, FakeClient([_item("b1")]), first)

    assert _saved(db, second) == {"b2"}


def test_a_truncated_feed_removes_nothing(db):
    """Пропуск в обрезанной выгрузке значит «мы не дочитали», а не «карточки
    нет»: уборка по такому снимку унесла бы ключи отправки у всего хвоста."""
    account = _account_with_catalog(db, ["b1", "b2", "b3", "b4"])

    stats = load_platform_catalog(db, FakeClient([
        _item("b1"), _item("b2"), _item("b3")], truncated=True), account)

    assert stats["stale_removed"] == 0
    assert _saved(db, account) == {"b1", "b2", "b3", "b4"}
    assert catalog_sync.STALE_SKIP_MARK in stats["stale_skipped"]
    assert catalog_sync.TRUNCATED_MARK in stats["stale_skipped"], \
        "причина отказа названа, иначе отказ читается как поломка уборки"


def test_an_empty_feed_removes_nothing(db):
    """Пустая выгрузка — это отказ площадки, прикинувшийся успехом. Поверь мы
    ей, кабинет остался бы вообще без ключей отправки."""
    account = _account_with_catalog(db, ["b1", "b2"])

    stats = load_platform_catalog(db, FakeClient([]), account)

    assert stats["stale_removed"] == 0
    assert _saved(db, account) == {"b1", "b2"}
    assert "пуста" in stats["stale_skipped"]


def test_a_feed_that_lost_half_the_catalogue_removes_nothing(db):
    """Карточка исчезает поштучно; исчезнувшая половина каталога — это беда на
    стороне площадки, а не правка в кабинете."""
    account = _account_with_catalog(db, ["b1", "b2", "b3", "b4"])

    stats = load_platform_catalog(db, FakeClient([_item("b1")]), account)

    assert stats["stale_removed"] == 0
    assert _saved(db, account) == {"b1", "b2", "b3", "b4"}
    assert "меньше половины" in stats["stale_skipped"]


def test_the_floor_lets_an_ordinary_fix_through(db):
    """Обратная сторона порога: он обязан ПРОПУСКАТЬ обычную правку кабинета.

    Без этой проверки порог можно было бы задрать до единицы, и уборка не
    сработала бы никогда — молча и выглядя исправной.
    """
    account = _account_with_catalog(db, ["b1", "b2", "b3", "b4"])

    stats = load_platform_catalog(db, FakeClient([
        _item("b1"), _item("b2")]), account)

    assert stats["stale_removed"] == 2, "ровно половина подтверждена — убираем"
    assert _saved(db, account) == {"b1", "b2"}


def test_a_fresh_cabinet_says_nothing(db):
    """Кабинет, каталог которого ещё не грузили, отсюда неотличим от кабинета с
    пустой выгрузкой. Сказать по нему «уборка пропущена» значило бы ругаться на
    исправную установку при КАЖДОЙ загрузке."""
    account = make_account(db, Platform.wb)

    stats = load_platform_catalog(db, FakeClient([]), account)

    assert stats["stale_skipped"] == ""


def test_a_full_feed_removes_nothing_and_says_nothing(db):
    account = _account_with_catalog(db, ["b1", "b2"])

    stats = load_platform_catalog(db, FakeClient([_item("b1"), _item("b2")]), account)

    assert stats["stale_removed"] == 0
    assert stats["stale_skipped"] == ""


def test_removal_goes_in_chunks(db, monkeypatch):
    """`IN (...)` упирается в предел параметров SQLite (999 у сборок до 3.32,
    чего на машине разработки не увидеть), а протухших строк после починки
    кабинета бывают тысячи."""
    account = _account_with_catalog(db, [f"b{i}" for i in range(20)])
    monkeypatch.setattr(catalog_sync, "STALE_DELETE_CHUNK", 3)
    # Считаем по СЛЕДСТВИЮ — числу запросов DELETE, а не по виду кода: порции
    # ради того и нужны, чтобы в один запрос не ушло больше параметров, чем
    # движок принимает.
    from sqlalchemy import event
    calls = []
    engine = db.get_bind()

    def before(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith("delete from platform_catalog_items"):
            calls.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        stats = load_platform_catalog(db, FakeClient(
            [_item(f"b{i}") for i in range(12)]), account)
    finally:
        event.remove(engine, "before_cursor_execute", before)

    assert stats["stale_removed"] == 8
    assert len(calls) == 3, \
        f"восемь строк порциями по три — три запроса, а не {len(calls)}"


def test_the_floor_is_the_knob_that_decides(db, monkeypatch):
    """Порог — настоящая ручка, а не украшение: подняв его, получаем отказ на
    той же выгрузке, которую исправный порог пропускает."""
    account = _account_with_catalog(db, ["b1", "b2", "b3", "b4"])
    monkeypatch.setattr(catalog_sync, "STALE_COVERAGE_FLOOR", 0.9)

    stats = load_platform_catalog(db, FakeClient(
        [_item("b1"), _item("b2"), _item("b3")]), account)

    assert stats["stale_removed"] == 0
    assert _saved(db, account) == {"b1", "b2", "b3", "b4"}


# ----------------------------------------- и об уборке обязаны сказать человеку

def _web_account(web_db):
    from app.models import PlatformAccount

    account = PlatformAccount(platform="wb", name="Кабинет", warehouse_id="wh")
    web_db.add(account)
    web_db.commit()
    web_db.refresh(account)
    return account


def _web_catalog(web_db, account, barcodes):
    for bc in barcodes:
        web_db.add(PlatformCatalogItem(account_id=account.id, barcode=bc,
                                       external_id="100:200", article="A", name="Товар"))
    web_db.commit()


def test_the_page_says_how_many_cards_were_removed(logged_in_client, web_db, monkeypatch):
    """Уборка — это УДАЛЕНИЕ, пусть и выверенное: человек имеет право знать,
    сколько строк каталога исчезло после его нажатия."""
    account = _web_account(web_db)
    _web_catalog(web_db, account, ["b1", "b2", "b3", "b4"])
    monkeypatch.setattr("app.routers.mapping.build_client",
                        lambda db, aid: FakeClient([_item("b1"), _item("b2"), _item("b3")]))

    page = logged_in_client.post(f"/mapping/load-catalog/{account.id}").text

    assert "Убрано карточек, пропавших из выгрузки: 1" in page


def test_the_page_says_why_the_cleanup_was_refused(logged_in_client, web_db, monkeypatch):
    """Отказ уборки — причина, по которой каталог после нажатия остался
    прежним. Зелёное «загружено N» оставило бы уверенность, что перестал."""
    account = _web_account(web_db)
    _web_catalog(web_db, account, ["b1", "b2", "b3", "b4"])
    monkeypatch.setattr("app.routers.mapping.build_client",
                        lambda db, aid: FakeClient([_item("b1")]))

    page = logged_in_client.post(f"/mapping/load-catalog/{account.id}").text

    assert "борка каталога пропущена" in page
    assert "меньше половины" in page


def test_an_ordinary_load_says_nothing_about_cleanup(logged_in_client, web_db, monkeypatch):
    """Строка на КАЖДОЙ загрузке обесценила бы её так же верно, как молчание."""
    account = _web_account(web_db)
    _web_catalog(web_db, account, ["b1", "b2"])
    monkeypatch.setattr("app.routers.mapping.build_client",
                        lambda db, aid: FakeClient([_item("b1"), _item("b2")]))

    page = logged_in_client.post(f"/mapping/load-catalog/{account.id}").text

    assert "борка каталога пропущена" not in page
    assert "Убрано карточек" not in page


# ------------------------------- оговорок у отметки теперь ДВЕ, и они не слипаются

def _run_catalog_poll(db, account_id, monkeypatch, stats):
    from app.workers import scheduler as sch

    monkeypatch.setattr(sch, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(sch, "build_client", lambda *a, **k: object())
    monkeypatch.setattr(sch, "load_platform_catalog", lambda *a, **k: stats)
    monkeypatch.setattr(sch, "poll_catalog", lambda *a, **k: {})
    sch.job_catalog_poll(account_id)

    from app.models import WorkerHeartbeat
    return db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == f"{sch.CATALOG_POLL_JOB_PREFIX}{account_id}").first()


def test_a_refused_cleanup_reaches_the_heartbeat(db, monkeypatch):
    """Лог читают, когда уже что-то случилось; оговорку успешной отметки
    показывает «Диагностика»."""
    account = make_account(db, Platform.wb)

    hb = _run_catalog_poll(db, account.id, monkeypatch, {
        "truncated": False,
        "stale_skipped": f"{catalog_sync.STALE_SKIP_MARK}: выгрузка пуста"})

    assert hb.last_success is True, "задание отработало — это оговорка, а не отказ"
    assert "выгрузка пуста" in hb.last_error


def test_a_truncated_feed_does_not_say_the_same_thing_twice(db, monkeypatch):
    """Уборка при обрыве отказывает ПО ТОЙ ЖЕ причине. Две оговорки об одном
    приучили бы пролистывать обе."""
    account = make_account(db, Platform.wb)

    hb = _run_catalog_poll(db, account.id, monkeypatch, {
        "truncated": True,
        "stale_skipped": f"{catalog_sync.STALE_SKIP_MARK}: {catalog_sync.TRUNCATED_MARK}"})

    assert catalog_sync.STALE_SKIP_MARK not in hb.last_error
    assert catalog_sync.TRUNCATED_MARK in hb.last_error


def test_a_clean_run_leaves_no_remark(db, monkeypatch):
    account = make_account(db, Platform.wb)

    hb = _run_catalog_poll(db, account.id, monkeypatch,
                           {"truncated": False, "stale_skipped": ""})

    assert not hb.last_error


# ------------------------------- и находка берёт из оговорки ТОЛЬКО свою часть

def test_the_truncation_finding_ignores_a_cleanup_remark(db):
    """Возьми находка поле целиком, она объявила бы «каталог выгружен не
    полностью» про кабинет, у которого выгрузка как раз полная."""
    from app.models import WorkerHeartbeat
    from app.report import _check_truncated_catalog
    from app.timeutils import now_utc

    db.add(WorkerHeartbeat(worker_name="catalog_poll_account_1", last_run_at=now_utc(),
                           last_success=True,
                           last_error=f"{catalog_sync.STALE_SKIP_MARK}: выгрузка пуста"))
    db.commit()

    assert _check_truncated_catalog(db) is None


def test_the_truncation_finding_still_sees_old_remarks(db):
    """Метка — подстрока ПРЕЖНЕГО текста: отметки, написанные до этой правки,
    находка обязана видеть по-прежнему. Иначе правка молча погасила бы находку
    ровно на тех кабинетах, где она уже сработала."""
    from app.models import WorkerHeartbeat
    from app.report import _check_truncated_catalog
    from app.timeutils import now_utc

    db.add(WorkerHeartbeat(worker_name="catalog_poll_account_1", last_run_at=now_utc(),
                           last_success=True,
                           last_error="выгрузка каталога оборвана пределом страниц — "
                                      "снимок неполон"))
    db.commit()

    found = _check_truncated_catalog(db)

    assert found is not None and found.count == 1
