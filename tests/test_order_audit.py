"""Суточная сверка заказов: заказ у площадки есть, а у нас его нет.

Живой опрос читает ОЧЕРЕДЬ неподтверждённых заказов, и вышедший из неё заказ он
не видит больше никогда. Часовая сверка с 1С такой пропуск МАСКИРУЕТ: документа
нет ни у нас, ни в 1С, обе стороны согласованно врут на одну величину, разница
ноль. До 29.10 ленту целиком поднимал только «Расчёт» по кнопке.

Проверки тут про СЛЕДСТВИЕ, а не про счётчики: уменьшился ли остаток, создалось
ли перемещение в 1С, и — главное — НЕ создалось ли там, где создаваться не
должно. Цена ошибки несимметрична: не провести значит оставить остаток
завышенным (оверселл до суток), а провести лишнее — создать в 1С документ,
закрыть который нечем.
"""
from datetime import date, timedelta

import pytest

from app.models import (Barcode, FtpTask, OrderProcessStatus, Platform,
                        ProcessedOrder, Product, SyncSetting)
from app.workers.order_audit import (AUDIT_WINDOW_DAYS, FOUND_BY_AUDIT,
                                     MAX_PER_ACCOUNT, audit_account, audit_all)
from app.workers.platform_clients.base import PlatformOrder
from tests.factories import make_account

CANCELLED = "cancel"


class FeedClient:
    """Лента площадки: отдаёт то, что ей положили. Статусы — как у WB."""
    orders_history_days = 90

    def __init__(self, orders, truncated=False, unresolved=0):
        self._orders = orders
        self.last_truncated = truncated
        self.last_unresolved = unresolved
        self.asked_since = None

    def get_orders_since(self, date_from):
        self.asked_since = date_from
        return list(self._orders)

    def is_cancelled_status(self, raw_status):
        return (raw_status or "") == CANCELLED


def _pending(_platform):
    return "WB.Ожидает"


def _product(db, uid="u1", barcode="BC-1", stock=10, *, enabled=True,
             account=None):
    db.add(Product(uid_1c=uid, article="ART", name="Свитшот", size="M",
                   color="SIYAH", stock_on_hand=stock))
    db.add(Barcode(barcode=barcode, uid_1c=uid))
    if account is not None:
        db.add(SyncSetting(uid_1c=uid, account_id=account.id, enabled=enabled))
    db.commit()
    return db.query(Product).filter(Product.uid_1c == uid).first()


def _order(order_id="O-1", barcode="BC-1", qty=1, status="new", days_ago=3):
    return PlatformOrder(order_id=order_id, barcode=barcode, quantity=qty,
                         raw_status=status,
                         order_date=date.today() - timedelta(days=days_ago))


def test_an_order_the_poller_lost_is_processed_and_named(db):
    """Главное следствие: остаток уменьшился, перемещение в 1С создано.

    И строка помечена `audit` — без этой пометки вопрос «сколько заказов в
    сутки теряем» ответа не имеет вовсе, а именно он решает, искать ли причину.
    """
    account = make_account(db)
    _product(db, stock=10, account=account)
    client = FeedClient([_order(qty=2)])

    stats = audit_account(db, client, account, _pending)

    assert stats["missed"] == 1, stats
    assert db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand == 8
    row = db.query(ProcessedOrder).filter(ProcessedOrder.order_id == "O-1").first()
    assert row is not None and row.found_by == FOUND_BY_AUDIT
    assert row.status == OrderProcessStatus.processed
    task = db.query(FtpTask).filter(FtpTask.order_id == "O-1").first()
    assert task is not None and task.command == "CREATE_MOVEMENT"
    assert task.is_test is False
    # Документ датируется ДАТОЙ ЗАКАЗА, а не днём сверки: иначе перемещение
    # встанет в 1С не тем днём, при расчёте ещё и раньше базовой даты.
    assert task.movement_date == date.today() - timedelta(days=3)


def test_an_order_we_already_know_is_left_alone(db):
    """Идемпотентность: второго перемещения и второго списания не будет.

    Сверка суточная и ходит по той же тридцатидневной ленте каждый день — без
    этого она списывала бы один и тот же заказ тридцать раз.

    Держат это ДВА механизма, и оба нужны: предвыборка `_already_known` здесь и
    собственная проверка `process_new_order` по паре (кабинет, номер). Второй —
    последний рубеж, он же защищает живой опрос и расчёт; первый про ЦЕНУ,
    и про неё отдельный тест ниже. Эта проверка стоит на следствии и потому
    зелена, даже если снять предвыборку, — так и должно быть, но полагаться на
    неё как на проверку предвыборки нельзя (поймано мутацией).
    """
    account = make_account(db)
    _product(db, stock=10, account=account)
    db.add(ProcessedOrder(account_id=account.id, order_id="O-1", uid_1c="u1",
                          quantity=2, status=OrderProcessStatus.processed))
    db.commit()
    client = FeedClient([_order(qty=2)])

    stats = audit_account(db, client, account, _pending)

    assert stats["missed"] == 0, stats
    assert db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand == 10
    assert db.query(FtpTask).count() == 0


def test_the_known_orders_are_fetched_in_one_go(db):
    """Предвыборка — про ЦЕНУ, и цена здесь настоящая.

    Лента за тридцать суток на боевом кабинете — тысячи строк, и почти все они
    нам уже известны: сверка ходит по одному и тому же периоду каждые сутки.
    Спроси она про каждую отдельным запросом — это тысячи обращений к SQLite, в
    которую в это же время пишут рассылка (раз в 45 секунд) и приём заказов.

    Проверяем ровно это и БЕЗ магического числа: число запросов не растёт с
    числом уже известных заказов. С числом оно росло бы от каждой правки
    условия, а зависимость от объёма не поймало бы.
    """
    from sqlalchemy import event

    account = make_account(db)
    _product(db, stock=1000, account=account)
    seen: list[str] = []

    @event.listens_for(db.get_bind(), "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    def measure(n: int) -> int:
        for i in range(n):
            if db.query(ProcessedOrder).filter(
                    ProcessedOrder.order_id == f"O-{i}").first() is None:
                db.add(ProcessedOrder(account_id=account.id, order_id=f"O-{i}",
                                      uid_1c="u1", quantity=1,
                                      status=OrderProcessStatus.processed))
        db.commit()
        client = FeedClient([_order(order_id=f"O-{i}") for i in range(n)])
        seen.clear()
        stats = audit_account(db, client, account, _pending)
        assert stats["missed"] == 0, "замер пошёл по другому состоянию"
        return len(seen)

    on_ten = measure(10)
    on_fifty = measure(50)

    assert on_fifty == on_ten, (
        f"запросов стало больше от объёма: {on_ten} -> {on_fifty} — значит про "
        f"каждый заказ спрашивают отдельно")


def test_an_order_already_cancelled_at_the_platform_is_not_processed(db):
    """Отменённый заказ не проводим — документ в 1С закрыть было бы нечем.

    У живого опроса заказ жив по построению (он берёт очередь
    неподтверждённых), здесь — нет: в месячной ленте лежат и давно снятые.
    `get_cancelled_orders` у WB считает отменой только `supplierStatus=cancel`,
    то есть отмену ПРОДАВЦА, и покупательский отказ из архива обратно не
    придёт — единица навсегда осталась бы на складе «Ожидает», то есть остаток
    занижен.
    """
    account = make_account(db)
    _product(db, stock=10, account=account)
    client = FeedClient([_order(status=CANCELLED)])

    stats = audit_account(db, client, account, _pending)

    assert stats["cancelled"] == 1 and stats["missed"] == 0, stats
    assert db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand == 10
    assert db.query(FtpTask).count() == 0
    # И НИЧЕГО про него не записываем: `ProcessedOrder` значит «мы по этому
    # заказу действовали», а строка-пустышка на каждую отмену копилась бы
    # навсегда — срока хранения у заказов нет.
    assert db.query(ProcessedOrder).count() == 0


def test_the_status_question_goes_to_the_client(db):
    """Набор статусов отмены спрашиваем у КЛИЕНТА, а не повторяем у себя.

    Тем же набором клиент узнаёт отмену в `get_cancelled_orders`. Разойдись
    они, один механизм считал бы заказ живым, а второй отменённым. Проверяем
    следствием: клиент, который про этот статус ничего не знает, заказ
    ПРОВЕДЁТ — значит ответ берётся именно у него.
    """
    account = make_account(db)
    _product(db, stock=10, account=account)

    class Silent(FeedClient):
        def is_cancelled_status(self, raw_status):
            return False

    stats = audit_account(db, Silent([_order(status=CANCELLED)]), account, _pending)
    assert stats["missed"] == 1, stats


def test_the_disabled_branch_is_only_a_sentinel(db):
    """Ветка «пропущено по галочке» осталась СТОРОЖЕМ, и она обязана молчать.

    До 29.10 это было главным ограничением сверки: только по отмеченным
    парам, иначе она создала бы в 1С перемещения на весь поток чужих продаж.
    Факты поменялись — вторая система выключена, чужих продаж нет, — и
    `respect_enabled` стало `False`. Ветку не убрали: вернись аргумент
    чьей-нибудь правкой, счётчик показал бы это ЧИСЛОМ, а не молчанием.
    Поэтому проверяем, что сейчас он нулевой, а заказ проведён.
    """
    account = make_account(db)
    _product(db, stock=10, account=account, enabled=False)
    client = FeedClient([_order()])

    stats = audit_account(db, client, account, _pending)

    assert stats["disabled"] == 0 and stats["missed"] == 1, stats
    assert db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand == 9
    assert db.query(FtpTask).count() == 1


def test_a_barcode_we_cannot_match_does_not_count_as_a_loss(db):
    """Несопоставленный баркод — своя беда со своим читателем.

    `process_new_order` записал конфликт сопоставления, его разбирают на
    «Мэппинге». Считать такую строку потерянной продажей значило бы отправить
    человека искать причину не там.
    """
    account = make_account(db)
    _product(db, stock=10, account=account)
    client = FeedClient([_order(order_id="O-9", barcode="ЧУЖОЙ")])

    stats = audit_account(db, client, account, _pending)

    assert stats["unmatched"] == 1 and stats["missed"] == 0, stats


def test_the_ceiling_holds_and_says_how_much_is_left(db):
    """Потолок на прогон — про ПЕРВЫЙ запуск, и обрезка не молчит.

    Сколько заказов живой опрос потерял за всё время, заранее неизвестно: это и
    есть то, что сверка выясняет. Молчаливая обрезка читалась бы как «всё
    догнали», а сходится это за несколько суток само.
    """
    account = make_account(db)
    _product(db, stock=1000, account=account)
    extra = 7
    client = FeedClient([_order(order_id=f"O-{i}")
                         for i in range(MAX_PER_ACCOUNT + extra)])

    stats = audit_account(db, client, account, _pending)

    assert stats["missed"] == MAX_PER_ACCOUNT, stats
    assert stats["left"] == extra, stats
    assert db.query(ProcessedOrder).count() == MAX_PER_ACCOUNT


def test_one_broken_order_does_not_take_the_rest(db):
    """Сбой на одной строке не уносит прогон, и причина уходит текстом.

    Без отката сессия после неудачного коммита внутри `process_new_order`
    остаётся сломанной, и следующий заказ падал бы на ровном месте — то есть
    один битый заказ означал бы, что остальные потери этого кабинета не
    догнаны, причём молча.
    """
    account = make_account(db)
    _product(db, stock=10, account=account)
    client = FeedClient([_order(order_id="O-1"), _order(order_id="O-2")])

    import app.workers.order_poller as poller
    real = poller.process_new_order
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return real(*a, **kw)

    poller.process_new_order = flaky
    try:
        stats = audit_account(db, client, account, _pending)
    finally:
        poller.process_new_order = real

    assert stats["failed"] == 1 and stats["missed"] == 1, stats
    assert any("O-1" in p for p in stats["problems"]), stats["problems"]


def test_an_incomplete_feed_still_processes_what_it_saw(db):
    """Обрыв ленты скрывает ЕЩЁ пропуски, но не создаёт ложных.

    Заказ, который в ленте есть, а у нас нет, потерян независимо от того,
    оборвалась ли выдача дальше. Отказаться проводить значило бы оставить
    известный оверселл неисправленным из-за того, что мы не знаем про
    остальные. А вот промолчать о неполноте нельзя: «потерь 1» по обрезанной
    ленте читается как «всего одна».
    """
    account = make_account(db)
    _product(db, stock=10, account=account)
    client = FeedClient([_order()], truncated=True, unresolved=4)

    stats = audit_account(db, client, account, _pending)

    assert stats["missed"] == 1, stats
    joined = "; ".join(stats["problems"])
    assert "оборвалась" in joined, joined
    assert "4" in joined, joined


def test_the_feed_is_asked_from_the_audit_window(db):
    """Окно — тридцать суток, и оно не длиннее окна открытых заказов.

    Заказ, проведённый сверкой, обязан попасть в `OPEN_ORDER_WINDOW_DAYS`,
    иначе ни подтверждение, ни отмена по нему отслеживаться не будут вовсе:
    `_real_open_orders` фильтрует по возрасту строки.
    """
    from app.workers.order_poller import OPEN_ORDER_WINDOW_DAYS

    account = make_account(db)
    _product(db, stock=10, account=account)
    client = FeedClient([])
    audit_account(db, client, account, _pending)

    assert client.asked_since == date.today() - timedelta(days=AUDIT_WINDOW_DAYS)
    assert AUDIT_WINDOW_DAYS <= OPEN_ORDER_WINDOW_DAYS


def test_a_window_deeper_than_the_feed_remembers_is_named(db):
    """Лента помнит не всю историю, и слишком старый запрос отвечает ПУСТОТОЙ.

    Не падает и не жалуется — поэтому про это надо сказать самим. Поле есть
    только там, где предел подтверждён (WB); нет поля — нет и проверки.
    """
    account = make_account(db)
    _product(db, stock=10, account=account)

    class Shallow(FeedClient):
        orders_history_days = 7

    stats = audit_account(db, Shallow([]), account, _pending)
    assert any("глубже" in p for p in stats["problems"]), stats["problems"]

    class NoLimit(FeedClient):
        orders_history_days = None

    assert audit_all(db, lambda _db, _id: NoLimit([]), [account],
                     _pending)["problems"] == []


def test_a_broken_cabinet_does_not_take_the_others(db):
    """Площадки независимы: молчание WB — не повод не сверить Kit."""
    wb = make_account(db, platform=Platform.wb, name="WB")
    kit = make_account(db, platform=Platform.kit, name="КИТ")
    _product(db, stock=10, account=kit)

    def build(_db, account_id):
        if account_id == wb.id:
            raise RuntimeError("Read timed out")
        return FeedClient([_order()])

    total = audit_all(db, build, [wb, kit], _pending)

    assert total["errors"] == 1 and total["missed"] == 1, total
    assert any("WB" in p for p in total["problems"]), total["problems"]


# ---------------------------------------------------------------------------
# Находка отчёта: у потерянных заказов обязан быть читатель
# ---------------------------------------------------------------------------

def _found(db, account, uid="u1", order_id="O-1", by=FOUND_BY_AUDIT, age_days=0):
    from app.timeutils import now_utc

    db.add(ProcessedOrder(
        account_id=account.id, order_id=order_id, uid_1c=uid, quantity=2,
        status=OrderProcessStatus.processed, found_by=by,
        processed_at=now_utc() - timedelta(days=age_days)))
    db.commit()


def test_the_finding_is_silent_on_a_healthy_system(db):
    """Молчание на исправном — обязательное свойство, а не приятная деталь.

    Находка, срабатывающая на норме, приучает пролистывать отчёт целиком, и
    тогда он бесполезен весь. Заказ, который провёл ЖИВОЙ ОПРОС (`found_by`
    пуст — так же стоит у всех строк до появления колонки), и заказ, поднятый
    РАСЧЁТОМ по кнопке человека, потерями не являются.
    """
    from app.report import _check_missed_orders

    account = make_account(db)
    _product(db, account=account)
    assert _check_missed_orders(db) is None

    _found(db, account, order_id="O-poller", by=None)
    _found(db, account, order_id="O-recalc", by="recalc")
    assert _check_missed_orders(db) is None


def test_the_finding_names_the_loss_and_leads_to_its_list(db):
    """ВНИМАНИЕ, а не КРИТИЧНО: сами заказы сверка уже провела.

    Красным тут была бы не беда, а её след. Разбавь мы КРИТИЧНО следами — оно
    перестанет значить «разбирать немедленно», и уведомления обесценятся
    целиком: они будят только на красном.
    """
    from app.report import CRITICAL, WARNING, FULL_LISTS, _check_missed_orders

    account = make_account(db)
    _product(db, account=account)
    _found(db, account, order_id="O-1")
    _found(db, account, order_id="O-2")

    finding = _check_missed_orders(db)
    assert finding is not None
    assert finding.level == WARNING and finding.level != CRITICAL
    assert finding.count == 2
    # Штуки, а не только строки: по одной строке не видно, сколько единиц
    # продано дважды.
    assert "штук: 4" in finding.title, finding.title
    assert finding.link == "/report/rows/missed_orders"
    assert "missed_orders" in FULL_LISTS


def test_the_finding_and_its_list_count_the_same_thing(db):
    """Разойдись они — список показывал бы не то, что насчитала находка.

    Поэтому запрос вынесен в `_q_missed_orders` и его зовут ОБА, а не каждый
    свой. Тот же приём, что у остальных строчных находок.
    """
    from app.report import _check_missed_orders, _rows_missed_orders

    account = make_account(db)
    _product(db, account=account)
    for i in range(3):
        _found(db, account, order_id=f"O-{i}")
    # Эта за окном: в находку не идёт, значит и в списке её быть не должно.
    _found(db, account, order_id="O-old", age_days=10)

    finding = _check_missed_orders(db)
    rows = _rows_missed_orders(db)
    assert finding.count == len(rows) == 3, (finding.count, len(rows))
    assert not [r for r in rows if "O-old" in r], rows


def test_a_long_past_loss_does_not_hold_the_report_red(db):
    """Окно — три суточных прогона, и это не произвол.

    Один прогон заставил бы находку мигать по одному и тому же разовому сбою
    (сутки красная, сутки нет), а мигающую находку перестают читать. Месяц —
    разовая потеря от одной ночной недоступности площадки держала бы отчёт
    красным до следующего сезона.
    """
    from app.report import MISSED_ORDERS_WINDOW, _check_missed_orders

    account = make_account(db)
    _product(db, account=account)
    _found(db, account, order_id="O-old",
           age_days=MISSED_ORDERS_WINDOW.days + 1)

    assert _check_missed_orders(db) is None
    assert MISSED_ORDERS_WINDOW >= timedelta(days=2), (
        "окно короче двух прогонов — находка будет мигать")


def test_the_finding_is_in_the_report(db):
    """Проверка без читателя бесполезна так же, как находка без проверки."""
    from app.report import CHECKS, _check_missed_orders

    assert _check_missed_orders in CHECKS


# ---------------------------------------------------------------------------
# Расписание
# ---------------------------------------------------------------------------

def test_the_audit_waits_for_a_running_recalc():
    """Расчёт по кнопке делает то же и из той же ленты — вместе им нельзя.

    Идемпотентность по `ProcessedOrder` защищает от второго перемещения, но не
    от гонки за запись в SQLite: оба пути коммитят на заказ, и `database is
    locked` достался бы тому, кого человек ждёт на странице. Сверка суточная —
    она дождётся следующих суток.
    """
    import app.workers.scheduler as sched
    from app.models import RecalcJob, RecalcStatus

    called = {"n": 0}

    class Session:
        def query(self, *a, **kw):
            raise AssertionError("не должно дойти до кабинетов")
        def close(self):
            pass

    def fake_audit(*a, **kw):
        called["n"] += 1
        return {}

    real_session, real_audit = sched.SessionLocal, sched.audit_all
    real_active = __import__("app.recalc", fromlist=["active_job"]).active_job
    real_hb = sched._heartbeat
    notes = []
    try:
        sched.SessionLocal = Session
        sched.audit_all = fake_audit
        sched._heartbeat = lambda db, name, ok, err="": notes.append((name, ok, err))
        import app.recalc
        app.recalc.active_job = lambda db: RecalcJob(id=7, total=1,
                                                    status=RecalcStatus.running)
        sched.job_order_audit()
    finally:
        sched.SessionLocal, sched.audit_all = real_session, real_audit
        sched._heartbeat = real_hb
        __import__("app.recalc", fromlist=["active_job"]).active_job = real_active

    assert called["n"] == 0, "сверка пошла в ленты во время расчёта"
    # И пропуск НЕ молчит: отметка успешная, но с оговоркой — её показывает
    # «Диагностика». Молчаливый пропуск читался бы как «сверка прошла, потерь
    # нет».
    assert notes and notes[-1][0] == "order_audit" and notes[-1][1] is True
    assert "расчёт" in notes[-1][2], notes


def test_the_audit_cannot_rot_out_of_health():
    """Суточное задание без своей записи протухает через десять минут.

    `DEFAULT_EXPECTED_SECONDS` — 600, то есть мониторинг краснел бы через
    десять минут после прогона и держался красным остальные 23 часа с лишним.
    Так уже было с недельным импортом справочника, с часовым отчётом, а 21.09 —
    с бэкапом и чисткой сразу.
    """
    from app.routers.health import (EXPECTED_INTERVAL_SECONDS, REQUIRED_WORKERS,
                                    NON_WORKER_MARKERS)
    from app.workers.scheduler import ORDER_AUDIT_INTERVAL_HOURS

    assert "order_audit" in REQUIRED_WORKERS
    assert "order_audit" not in NON_WORKER_MARKERS
    interval = ORDER_AUDIT_INTERVAL_HOURS * 3600
    assert EXPECTED_INTERVAL_SECONDS["order_audit"] >= 2 * interval


def test_the_first_run_does_not_wait_for_a_whole_day():
    """Задание на `interval` отсчитывает первый запуск от момента ДОБАВЛЕНИЯ.

    Задания живут в памяти процесса и навешиваются заново при каждом старте
    воркера, а он перезапускается чаще раза в сутки. Ровно так суточная
    выгрузка каталога не запускалась НИ РАЗУ — снимок каталога Kit лежал
    пятидневной давности.
    """
    from app.workers.scheduler import (ORDER_AUDIT_FIRST_RUN_DELAY,
                                       ORDER_AUDIT_INTERVAL_HOURS)

    assert ORDER_AUDIT_FIRST_RUN_DELAY < timedelta(hours=ORDER_AUDIT_INTERVAL_HOURS)


# ---------------------------------------------------------------------------
# Галочки нет — заказ всё равно проводим, а пару НАЗЫВАЕМ
# ---------------------------------------------------------------------------

def test_an_unmanaged_pair_is_processed_and_named(db):
    """Галочки нет — заказ ПРОВОДИМ, и это изменилось 29.10 вместе с фактами.

    Пока жила вторая система, провести такую продажу значило бы списать то,
    что уже списал кто-то другой. Второй системы нет с конца сентября 2026:
    число на неподключённой карточке не обновляет НИКТО, товар при продаже
    уезжает со склада ЦС, а в 1С это не отражается — учёт завышается
    накопительно, по штуке на каждую продажу.

    Но провести — половина дела. Карточка продаст ещё, пока её устаревшее
    число не кончится, и решить это может только человек, поэтому по паре
    заводится строка. Проверяем ОБЕ половины: без первой остаток остался бы
    завышенным, без второй о карточке не узнал бы никто.
    """
    from app.models import AnomalyReason, AnomalyStatus, SyncAnomaly

    account = make_account(db)
    _product(db, stock=10, account=account, enabled=False)
    client = FeedClient([_order(qty=2)])

    stats = audit_account(db, client, account, _pending)

    assert stats["missed"] == 1 and stats["unmanaged"] == 1, stats
    assert stats["disabled"] == 0, "галочку сверка больше не спрашивает"
    # Половина первая: учёт поправлен — остаток списан, документ в 1С заведён.
    assert db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand == 8
    assert db.query(FtpTask).count() == 1
    processed = db.query(ProcessedOrder).one()
    assert processed.found_by == FOUND_BY_AUDIT
    # Половина вторая: про карточку сказано.
    row = db.query(SyncAnomaly).filter(
        SyncAnomaly.reason == AnomalyReason.order_on_unmanaged).first()
    assert row is not None
    assert row.order_id == "O-1" and row.account_id == account.id
    assert row.uid_1c == "u1"
    assert row.status == AnomalyStatus.new and row.is_test is False


def test_a_managed_pair_is_processed_without_an_anomaly(db):
    """Обратная проверка: по ОТМЕЧЕННОЙ паре строка не заводится.

    Без неё «заводим аномалию всегда» прошло бы как исправное, и «Аномалии»
    получили бы строку на каждый догнанный заказ — то есть сигнал про
    неуправляемую карточку утонул бы в обычной работе сверки.
    """
    from app.models import SyncAnomaly

    account = make_account(db)
    _product(db, stock=10, account=account, enabled=True)

    stats = audit_account(db, FeedClient([_order(qty=2)]), account, _pending)

    assert stats["missed"] == 1 and stats["unmanaged"] == 0, stats
    assert db.query(SyncAnomaly).count() == 0
    assert db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand == 8


def test_the_pair_is_reported_before_the_gate(db):
    """`process_new_order` сообщает пару САМ, а сверка её не переспрашивает.

    Спроси сверка `resolve_barcode` второй раз, незнакомый баркод получил бы
    второй счёт попыток в конфликте сопоставления — число перестало бы
    означать, сколько продаж по нему пропало. Проверяем следствием: отдаём
    решение о паре и uid прямо из `process_new_order`.
    """
    from app.workers.order_poller import process_new_order

    account = make_account(db)
    _product(db, stock=10, account=account, enabled=False)
    order = PlatformOrder(order_id="O-9", barcode="BC-1", quantity=1,
                          raw_status="new", order_date=date.today())

    result = process_new_order(db, order, account, _pending(account.platform),
                               is_test=False, order_date=date.today(),
                               respect_enabled=False,
                               found_by=FOUND_BY_AUDIT)
    db.commit()

    assert result["status"] == "processed"
    assert result["uid_1c"] == "u1"
    assert result["pair_enabled"] is False


def test_the_same_order_does_not_pile_up_day_after_day(db):
    """Сверка ходит по одному окну каждые сутки — строка обязана быть одна.

    Без этого один заказ дал бы тридцать аномалий, и число перестало бы
    означать масштаб: находка показывала бы «тридцать продаж» там, где продажа
    одна.

    Держится это `ProcessedOrder`, а НЕ проверкой внутри `_note_unmanaged`:
    заказ стал известен, `plan_account` второй раз его не отдаёт, а попади он
    всё равно — `process_new_order` ответит `already_processed`. Поэтому тест
    проверяет ЦЕПОЧКУ: второй прогон не проводит заказа (`missed == 0`), и
    строка остаётся одна. Мутация сторожа внутри `_note_unmanaged` этот тест не
    роняет, и это не пробел теста, а свойство сторожа — сказано у него в
    докстроке.
    """
    from app.models import SyncAnomaly

    account = make_account(db)
    _product(db, stock=10, account=account, enabled=False)
    client = FeedClient([_order()])

    first = audit_account(db, client, account, _pending)
    assert first["unmanaged"] == 1, first
    for _ in range(2):
        again = audit_account(db, client, account, _pending)
        assert again["missed"] == 0, "заказ проведён дважды"
        assert again["unmanaged"] == 0, again

    assert db.query(SyncAnomaly).count() == 1
    # И остаток списан РОВНО один раз: именно это и покупается тем, что хозяин
    # правила один. Сойдись счёт по аномалиям, а по остатку нет — вред был бы
    # ровно тот, от которого сверка и заведена.
    assert db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand == 9


def test_the_finding_is_critical_and_leads_to_its_list(db):
    """КРИТИЧНО — и это не разбавление критичного.

    У «потерянных заказов» сверка всё провела, осталась одна причина, и она
    сойдётся сама — поэтому там ВНИМАНИЕ. Здесь продажи идут ПРЯМО СЕЙЧАС по
    числу, которое никто не обновляет, то есть состояние
    самоподдерживающееся, и само оно не кончится, пока число не иссякнет.
    """
    from app.report import (CRITICAL, FULL_LISTS, WARNING, CHECKS,
                            _check_unmanaged_pairs, _rows_unmanaged_pairs)

    account = make_account(db)
    _product(db, stock=10, account=account, enabled=False)
    audit_account(db, FeedClient([_order(), _order(order_id="O-2")]),
                  account, _pending)

    assert _check_unmanaged_pairs in CHECKS
    finding = _check_unmanaged_pairs(db)
    assert finding is not None
    assert finding.level == CRITICAL and finding.level != WARNING
    assert finding.count == 2
    # Пар одна, продаж две: по одной строке не видно, сколько раз повторилось.
    assert "1 (продаж: 2)" in finding.title, finding.title
    assert finding.link == "/report/rows/unmanaged_pairs"
    assert "unmanaged_pairs" in FULL_LISTS
    # И находка со списком считают ОДНО И ТО ЖЕ.
    assert len(_rows_unmanaged_pairs(db)) == finding.count


def test_returning_the_tick_closes_the_row_by_itself(db):
    """Поставили галочку — пара под управлением, строка закрывается сама.

    Второй выход (снять карточку с продажи в кабинете площадки) по колонкам не
    спросить вовсе: нашего отзыва по такой паре не бывает — ноль мы отправляем
    только туда, куда отправляли непустой остаток. Выдумывать признак,
    которого нет, значило бы гасить строку на догадке, поэтому этот выход
    закрывает человек руками.
    """
    from app.anomalies import close_fixed_anomalies
    from app.models import AnomalyStatus, SyncAnomaly, SyncSetting as S

    account = make_account(db)
    _product(db, stock=10, account=account, enabled=False)
    audit_account(db, FeedClient([_order()]), account, _pending)
    assert db.query(SyncAnomaly).filter(
        SyncAnomaly.status == AnomalyStatus.new).count() == 1

    db.query(S).filter(S.uid_1c == "u1", S.account_id == account.id).first().enabled = True
    db.commit()
    close_fixed_anomalies(db)
    db.commit()

    assert db.query(SyncAnomaly).filter(
        SyncAnomaly.status == AnomalyStatus.new).count() == 0
