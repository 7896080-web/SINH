"""Отчёт о расхождениях: что он находит, чего не находит и что при этом говорит.

Главное, что здесь проверяется, — не «страница открывается», а два свойства, без
которых отчёт бесполезен:

  * он МОЛЧИТ, когда всё в порядке (отчёт, который всегда что-то показывает,
    перестают читать через неделю);
  * он называет СЛЕДСТВИЕ, а не факт — «12 заданий в статусе timeout» человек
    пролистывает, «остаток занижен, наружу уходит меньше, чем есть» нет.

И третье, менее очевидное: одна упавшая проверка не уносит весь отчёт. Отчёт,
который молчит из-за собственной ошибки, неотличим от отчёта, которому нечего
сказать, — а это ровно тот отказ, ради предотвращения которого он написан.
"""

from datetime import datetime, timedelta

from app.models import (
    AnomalyReason, Barcode, DispatchQueueItem, DispatchStatus, FtpTask,
    FtpTaskStatus, Platform, PlatformAccount, PlatformCatalogItem, Product,
    SyncAnomaly, SyncSetting, WorkerHeartbeat,
)
from app.report import CRITICAL, WARNING, collect_findings, summary_line
from app.timeutils import now_utc
from tests.factories import make_account


def _tick_all(db):
    """Отметить кабинет у всех пар, попавших в очередь.

    Находка «рассылка не доехала» — про пару, на которую мы ВОЗИМ. По
    неотмеченному кабинету, куда ни разу не отправляли непустой остаток, отказ
    расхождением не считается (`report._only_live_pairs`): площадка держит наше
    число, только если мы его туда посылали. Без этой отметки тест проверял бы
    сценарий, которого в его собственном описании нет.
    """
    db.flush()
    have = {(s.uid_1c, s.account_id) for s in db.query(SyncSetting).all()}
    for uid, account_id in {(r.uid_1c, r.account_id)
                            for r in db.query(DispatchQueueItem).all()}:
        if (uid, account_id) not in have:
            db.add(SyncSetting(uid_1c=uid, account_id=account_id, enabled=True))
    db.commit()


def _keys(findings) -> set[str]:
    return {f.key for f in findings}


def _by_key(findings, key):
    return next((f for f in findings if f.key == key), None)


# ------------------------------------------------------------------ тишина

def test_a_quiet_system_produces_no_findings(db):
    """Пустая база — расхождений нет. Отчёт, который всегда что-то показывает,
    ничем не отличается от отчёта, которого нет."""
    assert collect_findings(db) == []
    assert summary_line([]) == "расхождений нет"


def test_a_fresh_cabinet_with_a_fresh_catalog_is_quiet(db):
    account = make_account(db, name="ИП Яворская")
    db.add(PlatformCatalogItem(account_id=account.id, external_id="x1", barcode="111",
                               fetched_at=now_utc()))
    db.add(WorkerHeartbeat(worker_name="reconciliation_applied", last_run_at=now_utc()))
    db.commit()

    assert collect_findings(db) == []


# ------------------------------------------------------------- что находит

def test_dispatch_errors_are_critical_and_name_the_consequence(db):
    """Самое дорогое расхождение: у нас списано, на площадку не уехало."""
    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=5,
                             reason="order", status=DispatchStatus.error,
                             last_error="429 после пяти попыток"))
    db.commit()

    _tick_all(db)
    finding = _by_key(collect_findings(db), "dispatch_errors")

    assert finding is not None
    assert finding.level == CRITICAL
    assert "продаёт то, чего нет" in finding.consequence
    assert "429" in finding.details[0]


def test_a_platform_holding_more_than_we_sent_is_critical(db):
    """Найдено на бою 19.09: в кабинет писала вторая система и перетирала наши
    остатки. Направление решает срочность — БОЛЬШЕ нашего значит площадка
    продаёт то, чего нет."""
    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=5, sent_quantity=5,
        sent_sku="111", reason="order", status=DispatchStatus.sent,
        sent_at=now_utc(), verified_at=now_utc(), verified_quantity=12))
    db.commit()

    finding = _by_key(collect_findings(db), "platform_divergence")

    assert finding is not None
    assert finding.level == CRITICAL
    assert "продаёт то, чего нет" in finding.consequence
    assert "отправили 5, площадка держит 12" in finding.details[0]


def test_a_platform_holding_less_than_we_sent_is_a_warning(db):
    """Ровно сегодняшний случай: отправили 68, площадка держит 0. Теряются
    продажи, но не деньги покупателя — значит не критично."""
    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=68))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=68, sent_quantity=68,
        sent_sku="2004896744404", reason="broadcast_toggled",
        status=DispatchStatus.sent, sent_at=now_utc(),
        verified_at=now_utc(), verified_quantity=0))
    db.commit()

    assert _by_key(collect_findings(db), "platform_divergence").level == WARNING


def test_an_unverified_row_is_not_a_divergence(db):
    """Сверка не отработала (площадка молчит, sku она не знает, ещё не спрашивали)
    — это отсутствие проверки, а не расхождение."""
    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=5, sent_quantity=5,
        sent_sku="111", reason="order", status=DispatchStatus.sent,
        sent_at=now_utc(), verified_at=now_utc(), verified_quantity=None))
    db.commit()

    assert "platform_divergence" not in _keys(collect_findings(db))


def test_a_test_dispatch_error_is_not_a_discrepancy(db):
    """Симуляция со страницы «Тестирование» на площадку не уходила и остаток не
    двигала — в отчёте ей делать нечего. Это та же граница `is_test`, что и
    везде, и нарушить её здесь значит звать человека разбирать собственный тест."""
    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=5,
                             reason="order", status=DispatchStatus.error, is_test=True))
    db.commit()

    assert "dispatch_errors" not in _keys(collect_findings(db))


def test_broadcasting_without_a_recalc_is_critical(db):
    """Интерфейс такого не даёт — значит товар прошёл мимо него."""
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5,
                   broadcast_enabled=True))
    db.commit()

    finding = _by_key(collect_findings(db), "broadcast_without_recalc")

    assert finding is not None
    assert finding.level == CRITICAL
    assert "оверселл" in finding.consequence


def test_a_cabinet_killed_by_the_breaker_is_critical(db):
    account = make_account(db, name="Озон")
    account.is_active = False
    account.consecutive_failures = 5
    account.last_error = "401 Unauthorized"
    db.commit()

    finding = _by_key(collect_findings(db), "breaker_disabled")

    assert finding is not None
    assert finding.level == CRITICAL
    assert "не опрашиваются" in finding.consequence


def test_a_stale_catalog_is_found(db):
    """Ровно случай 19.09: снимок каталога Kit лежал пятидневной давности, а
    /health при этом был зелёный."""
    from app.report import CATALOG_STALE

    account = make_account(db, name="КИТ")
    db.add(PlatformCatalogItem(account_id=account.id, external_id="x1", barcode="111",
                               fetched_at=now_utc() - CATALOG_STALE - timedelta(days=1)))
    db.commit()

    finding = _by_key(collect_findings(db), "stale_catalog")

    assert finding is not None
    assert finding.level == WARNING
    assert "КИТ" in finding.details[0]


def test_a_cabinet_without_any_catalog_at_all_is_found(db):
    """Выгрузка не отработала НИ РАЗУ — строк нет вовсе. Это тот же дефект, что
    и протухший каталог, и пропустить его легче всего: пустая выборка выглядит
    как «проверять нечего»."""
    from app.report import CATALOG_STALE

    account = make_account(db, name="Старый кабинет")
    account.created_at = now_utc() - CATALOG_STALE - timedelta(days=1)
    db.commit()

    finding = _by_key(collect_findings(db), "stale_catalog")

    assert finding is not None
    assert "никогда" in finding.details[0]


def test_a_cabinet_added_a_minute_ago_is_not_blamed_for_an_empty_catalog(db):
    """Выгрузка каталога идёт через две минуты после старта и занимает время.
    Ругать только что заведённый кабинет значит приучить оператора пролистывать
    отчёт — а тогда он не заметит и настоящую находку."""
    make_account(db, name="Только что заведён")
    db.commit()

    assert "stale_catalog" not in _keys(collect_findings(db))


def test_an_old_pile_of_unmatched_barcodes_becomes_critical(db):
    """Заказ без сопоставленного баркода не рассосётся сам: документа в 1С нет и
    не будет. Старейшему неделя — это уже не «разберём на днях»."""
    from app.report import ANOMALY_OLD

    account = make_account(db)
    db.add(SyncAnomaly(uid_1c="u1", account_id=account.id,
                       reason=AnomalyReason.missing_barcode,
                       detected_at=now_utc() - ANOMALY_OLD - timedelta(days=1)))
    db.commit()

    finding = _by_key(collect_findings(db), "open_anomalies")

    assert finding is not None
    assert finding.level == CRITICAL


def test_orders_on_products_we_have_not_connected_are_not_a_discrepancy(db):
    """Пока идёт переход, часть каталога транслирует ещё старая система, и
    продажи по этим карточкам идут мимо нас В ПОРЯДКЕ ВЕЩЕЙ: товар у нас не
    отмечен, остаток по нему мы не рассылали и не списывали. Разбирать нечего,
    следствия нет — значит это не расхождение, а мера того, какая часть каталога
    ещё не переехала.

    Держать из-за них отчёт постоянно непустым нельзя: человек привыкнет его
    пролистывать и не заметит настоящую находку. С полным переходом строки
    исчезнут сами."""
    account = make_account(db)
    for i in range(50):
        db.add(SyncAnomaly(uid_1c="u1", account_id=account.id,
                           reason=AnomalyReason.order_on_disabled,
                           detected_at=now_utc() - timedelta(days=30)))
    db.commit()

    assert "open_anomalies" not in _keys(collect_findings(db))


def test_a_fresh_unmatched_barcode_is_only_a_warning(db):
    account = make_account(db)
    db.add(SyncAnomaly(uid_1c="u1", account_id=account.id,
                       reason=AnomalyReason.missing_barcode, detected_at=now_utc()))
    db.commit()

    assert _by_key(collect_findings(db), "open_anomalies").level == WARNING


def test_a_stale_reconciliation_is_found(db):
    """Метка `reconciliation_applied` — факт применения выгрузки, а не запуска
    задания. Разъехались эти два смысла на бою 17.09, и /health был зелёный."""
    from app.report import RECONCILIATION_STALE

    db.add(WorkerHeartbeat(worker_name="reconciliation_applied",
                           last_run_at=now_utc() - RECONCILIATION_STALE - timedelta(hours=1)))
    db.commit()

    finding = _by_key(collect_findings(db), "stale_reconciliation")

    assert finding is not None
    assert finding.level == WARNING


def test_negative_stock_is_reported(db):
    db.add(Product(uid_1c="u1", article="46 NAVY", name="Товар", stock_on_hand=-9))
    db.commit()

    finding = _by_key(collect_findings(db), "negative_stock")

    assert finding is not None
    assert "-9" in finding.details[0]


# ------------------------------------------- зависшие задания не двоятся

def test_a_task_waiting_for_a_human_is_not_also_counted_as_in_flight(db, monkeypatch):
    """Одна и та же строка не должна попасть и в «ждут разбора», и в «без
    ответа». Отчёт, который повторяется, читают невнимательно — а он ровно для
    того и написан, чтобы его читали внимательно."""
    import app.workers.ftp_channel as ftp

    monkeypatch.setattr(ftp, "repost_enabled", lambda: False)   # тогда timeout идёт в разбор

    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(FtpTask(command="CREATE_MOVEMENT", barcode="111", quantity=2, order_id="o1",
                   account_id=account.id, status=FtpTaskStatus.timeout,
                   created_at=now_utc() - timedelta(hours=5),
                   sent_at=now_utc() - timedelta(hours=5)))
    db.commit()

    findings = collect_findings(db)

    assert "tasks_needing_review" in _keys(findings)
    assert "stuck_1c_tasks" not in _keys(findings)


# --------------------------------------- отчёт не падает целиком из-за одной

def test_a_broken_check_does_not_silence_the_rest(db, monkeypatch):
    """Упавшая проверка превращается в собственную находку, а не в пустой отчёт.
    Молчание отчёта обязано означать «расхождений нет», и ничего другого."""
    import app.report as report

    def boom(db):
        raise RuntimeError("сломалась выборка")

    monkeypatch.setattr(report, "CHECKS", (boom, report._check_negative_stock))
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=-1))
    db.commit()

    findings = report.collect_findings(db)

    assert "boom_failed" in _keys(findings)
    assert "negative_stock" in _keys(findings)          # остальные отработали
    assert "сломалась выборка" in _by_key(findings, "boom_failed").details[0]


# --------------------------------------------------- порядок и строка лога

def test_critical_findings_come_first(db):
    account = make_account(db, name="Кабинет")
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=-1))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=5,
                             reason="order", status=DispatchStatus.error))
    db.commit()

    _tick_all(db)
    findings = collect_findings(db)

    assert findings[0].level == CRITICAL


def test_the_log_line_is_readable_without_opening_the_page(db):
    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=5,
                             reason="order", status=DispatchStatus.error))
    db.commit()

    _tick_all(db)
    line = summary_line(collect_findings(db))

    assert "критичных" in line and "dispatch_errors=1" in line


def test_every_finding_states_a_consequence(db):
    """Инвариант модуля: находка без следствия — это просто число, и человек её
    пролистает. Проверяем на всех проверках разом, чтобы новая не проехала."""
    account = make_account(db, name="Кабинет")
    account.is_active = False
    account.consecutive_failures = 5
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=-1,
                   broadcast_enabled=True))
    db.add(SyncAnomaly(uid_1c="u1", account_id=account.id,
                       reason=AnomalyReason.missing_barcode, detected_at=now_utc()))
    db.commit()

    findings = collect_findings(db)

    assert findings, "фикстура обязана дать хотя бы одну находку"
    for f in findings:
        assert f.consequence.strip(), f"{f.key}: нет следствия"
        assert f.title.strip(), f"{f.key}: нет заголовка"


# ------------------------------------------------------------------ страница

def test_the_report_page_opens_and_says_it_is_quiet(logged_in_client):
    r = logged_in_client.get("/report")

    assert r.status_code == 200
    assert "Расхождений нет" in r.text


def test_the_report_page_shows_a_finding_with_its_consequence(logged_in_client, web_db):
    account = PlatformAccount(platform=Platform.wb, name="ИП Яворская", warehouse_id="wh-1")
    web_db.add(account)
    web_db.commit()
    web_db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    web_db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=5,
                                 reason="order", status=DispatchStatus.error))
    # Кабинет отмечен: находка «рассылка не доехала» — про пару, на которую мы
    # возим (см. `_tick_all` выше и `report._only_live_pairs`).
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.commit()

    r = logged_in_client.get("/report")

    assert "Рассылка не доехала" in r.text
    assert "продаёт то, чего нет" in r.text


def test_the_diagnostics_page_links_to_the_report(logged_in_client):
    """Оператор ходит на «Диагностику». Если отчёт есть, а узнать о нём неоткуда,
    он ничем не лучше лога, в который никто не смотрит."""
    r = logged_in_client.get("/diagnostics")

    assert "/report" in r.text


def test_the_report_needs_a_login(client):
    """Страница показывает остатки, номера заказов и тексты ошибок площадок."""
    r = client.get("/report", follow_redirects=False)

    assert r.status_code in (302, 303, 307)


# ------------------------------------------- отчёт действительно собирается сам

def test_the_report_job_is_registered_in_the_scheduler(web_db):
    """Отчёт, который никто не запускает, — это просто страница.

    Проверяем и то, что задание есть, и то, что оно просит ПЕРВЫЙ прогон вскоре
    после старта, а не через час. `interval` отсчитывает первый запуск от момента
    добавления задания, а воркер перезапускается чаще — ровно так суточная
    выгрузка каталога не отработала ни разу (19.09, `job_catalog_poll`).
    """
    from datetime import timezone

    from app.workers.scheduler import build_scheduler

    # web_db, а не db: build_scheduler пишет отметку старта через SessionLocal,
    # то есть в движок самого приложения, а не в движок юнит-фикстуры.
    sched = build_scheduler()          # не запускаем — только состав заданий
    job = sched.get_job("discrepancy_report")

    assert job is not None
    assert (job.next_run_time - datetime.now(timezone.utc)).total_seconds() < 300


def test_the_report_worker_is_watched_by_health():
    """Если отчёт перестанет собираться, это не будет заметно никак — поэтому он
    перечислен среди обязательных воркеров, как и рабочие задания."""
    from app.routers.health import EXPECTED_INTERVAL_SECONDS, REQUIRED_WORKERS

    assert "discrepancy_report" in REQUIRED_WORKERS
    # Без своей записи об интервале он протухал бы по умолчанию через 10 минут и
    # держал /health красным между часовыми прогонами.
    assert EXPECTED_INTERVAL_SECONDS["discrepancy_report"] > 3600


# ------------------------------- отказ, перекрытый более поздней отправкой

def _kit_account(db):
    from app.models import Platform
    return make_account(db, Platform.kit, name="КИТ", warehouse_id="wh-1")


def test_a_missing_card_is_not_called_a_broken_dispatch(db):
    """Рассылка видит отсутствие карточки ДО запроса — по отсутствию
    идентификатора площадки. Следствие то же, что у неизвестного sku: продавать
    нечего, оверселла не будет, чинить надо мэппинг. Написать про такую позицию
    «площадка продаёт то, чего нет» значит отправить человека чинить связь."""
    account = _kit_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=5, reason="manual_resend_all",
        status=DispatchStatus.error,
        last_error="нет карточки в каталоге кабинета — остаток отправить не по чему, сначала мэппинг"))
    db.commit()

    _tick_all(db)
    findings = {f.key for f in collect_findings(db)}

    assert "unknown_sku" in findings
    assert "dispatch_errors" not in findings


def test_an_error_covered_by_a_later_send_is_not_a_discrepancy(db):
    """20.09 после починки Kit осталось 659 мёртвых записей от старого дефекта.
    Число по этим товарам потом доехало — но сами записи навсегда остались в
    `error`. Считать их расхождением значит держать отчёт красным вечно, а
    вечно красный отчёт оператор пролистывает не читая."""
    account = _kit_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=5, reason="order",
        status=DispatchStatus.error, last_error="не отправлено за 5 попыток: 400",
        created_at=now_utc() - timedelta(hours=3)))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=5, reason="manual_resend_all",
        status=DispatchStatus.sent, sent_at=now_utc() - timedelta(minutes=10),
        created_at=now_utc() - timedelta(minutes=12)))
    db.commit()

    assert "dispatch_errors" not in {f.key for f in collect_findings(db)}


def test_an_error_with_no_later_send_is_still_reported(db):
    """Перекрытие обязано быть ПОЗЖЕ отказа. Иначе достаточно одной старой
    удачной отправки, чтобы навсегда заглушить все будущие сбои по товару."""
    account = _kit_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=5, reason="manual_resend_all",
        status=DispatchStatus.sent, sent_at=now_utc() - timedelta(hours=5),
        created_at=now_utc() - timedelta(hours=5)))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=5, reason="order",
        status=DispatchStatus.error, last_error="не отправлено за 5 попыток: 500",
        created_at=now_utc() - timedelta(hours=1)))
    db.commit()

    assert "dispatch_errors" in {f.key for f in collect_findings(db)}


def test_a_send_to_another_cabinet_does_not_cover_the_error(db):
    """Пара — товар+кабинет. Удачная отправка на другой кабинет не говорит
    ничего о том, что лежит на этом."""
    from app.models import Platform

    kit = _kit_account(db)
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ", warehouse_id="wh-2")
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=kit.id, quantity=5, reason="order",
        status=DispatchStatus.error, last_error="не отправлено за 5 попыток: 500",
        created_at=now_utc() - timedelta(hours=2)))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=wb.id, quantity=5, reason="order",
        status=DispatchStatus.sent, sent_at=now_utc(), created_at=now_utc()))
    db.commit()

    _tick_all(db)
    assert "dispatch_errors" in {f.key for f in collect_findings(db)}


def test_an_error_superseded_by_a_newer_error_is_not_reported_twice(db):
    """Случай, которого не покрывало прежнее условие «перекрыто успехом»: у
    товара нет карточки в кабинете, успешной отправки не будет НИКОГДА, а старых
    отказов по нему накопилось два. Показывать надо текущее состояние пары —
    последнюю запись, — а не каждую историческую попытку.

    На бою 20.09 таких было сто: они вечно утверждали бы «площадка продаёт то,
    чего нет», хотя свежая запись по той же паре говорит совсем другое —
    «карточки нет, разбирайте мэппинг»."""
    account = _kit_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=5, reason="order",
        status=DispatchStatus.error, last_error="не отправлено за 5 попыток: 400",
        created_at=now_utc() - timedelta(hours=6)))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=5, reason="manual_resend_all",
        status=DispatchStatus.error,
        last_error="нет карточки в каталоге кабинета — остаток отправить не по чему",
        created_at=now_utc() - timedelta(minutes=5)))
    db.commit()

    _tick_all(db)
    findings = {f.key: f for f in collect_findings(db)}

    assert "dispatch_errors" not in findings, "старый отказ описывает прошлое пары"
    assert findings["unknown_sku"].count == 1, "текущее состояние пары — одна строка"


def test_the_newest_error_of_a_pair_is_always_reported(db):
    """Гашение не должно съедать пару целиком: последняя запись обязана
    остаться, иначе две записи одной секунды погасили бы друг друга."""
    account = _kit_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    moment = now_utc() - timedelta(minutes=5)
    for _ in range(2):
        db.add(DispatchQueueItem(
            uid_1c="u1", account_id=account.id, quantity=5, reason="order",
            status=DispatchStatus.error, last_error="не отправлено за 5 попыток: 500",
            created_at=moment))
    db.commit()

    _tick_all(db)
    finding = {f.key: f for f in collect_findings(db)}.get("dispatch_errors")

    assert finding is not None and finding.count == 1


# ---------------------- продажа площадки — не «наше число переписали»

def test_a_drop_explained_by_orders_is_not_a_divergence(db):
    """20.09 на бою обе «находки» были ровно этим: отправили 39 — площадка
    держит 38, отправили 29 — держит 28. Между отправкой и сверкой проходит
    полчаса, и площадка сама уменьшает остаток, когда товар покупают. Звать
    человека разбирать штатную продажу — верный способ отучить его читать
    отчёт."""
    from app.models import ProcessedOrder

    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=39))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=39, sent_quantity=39,
        sent_sku="2004896744503", reason="order", status=DispatchStatus.sent,
        sent_at=now_utc() - timedelta(minutes=30),
        verified_at=now_utc(), verified_quantity=38))
    db.add(ProcessedOrder(account_id=account.id, order_id="o-1", uid_1c="u1",
                          quantity=1, processed_at=now_utc() - timedelta(minutes=20)))
    db.commit()

    assert "platform_divergence" not in _keys(collect_findings(db))


def test_a_drop_bigger_than_the_orders_is_still_a_divergence(db):
    """Продажи объясняют падение ровно на своё количество. Всё, что сверх, —
    это уже чужая запись поверх нашей, и её надо показывать."""
    from app.models import ProcessedOrder

    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=39))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=39, sent_quantity=39,
        sent_sku="111", reason="order", status=DispatchStatus.sent,
        sent_at=now_utc() - timedelta(minutes=30),
        verified_at=now_utc(), verified_quantity=10))
    db.add(ProcessedOrder(account_id=account.id, order_id="o-1", uid_1c="u1",
                          quantity=1, processed_at=now_utc() - timedelta(minutes=20)))
    db.commit()

    assert "platform_divergence" in _keys(collect_findings(db))


def test_a_cancelled_order_does_not_explain_a_drop(db):
    """По отменённому заказу площадка остаток вернула — значит падением он не
    объясняется."""
    from app.models import OrderProcessStatus, ProcessedOrder

    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=39))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=39, sent_quantity=39,
        sent_sku="111", reason="order", status=DispatchStatus.sent,
        sent_at=now_utc() - timedelta(minutes=30),
        verified_at=now_utc(), verified_quantity=38))
    db.add(ProcessedOrder(account_id=account.id, order_id="o-1", uid_1c="u1",
                          quantity=1, status=OrderProcessStatus.cancelled,
                          processed_at=now_utc() - timedelta(minutes=20)))
    db.commit()

    assert "platform_divergence" in _keys(collect_findings(db))


def test_orders_on_another_cabinet_do_not_explain_a_drop(db):
    """Остаток уменьшает та площадка, где купили. Заказ соседнего кабинета про
    этот ничего не говорит."""
    from app.models import ProcessedOrder

    kit = make_account(db, Platform.kit, name="КИТ", warehouse_id="wh-2")
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=39))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=wb.id, quantity=39, sent_quantity=39,
        sent_sku="111", reason="order", status=DispatchStatus.sent,
        sent_at=now_utc() - timedelta(minutes=30),
        verified_at=now_utc(), verified_quantity=38))
    db.add(ProcessedOrder(account_id=kit.id, order_id="o-1", uid_1c="u1",
                          quantity=1, processed_at=now_utc() - timedelta(minutes=20)))
    db.commit()

    assert "platform_divergence" in _keys(collect_findings(db))


# ------------------------- расхождения сверки: свежие, а не архив

def test_a_stale_reconciliation_difference_is_not_reported(db):
    """20.09 на бою: 909 строк `needs_review` от 14–16.09, все неразрешённые.
    Разрешать их некому — сверка с тех пор применяет любое движение сама, и
    новых неразрешённых не появляется вовсе. Архив трёхдневной давности держал
    отчёт жёлтым круглосуточно."""
    from app.models import ReconciliationClassification, ReconciliationLog

    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(ReconciliationLog(
        uid_1c="u1", python_stock=-1, in_flight=0, expected_1c=-1, actual_1c=0,
        delta=1, classification=ReconciliationClassification.needs_review,
        resolved=False, checked_at=now_utc() - timedelta(days=4)))
    db.commit()

    assert "reconciliation_review" not in _keys(collect_findings(db))


def test_a_fresh_large_difference_is_reported_even_if_applied(db):
    """Сверка переписала остаток по 1С сама — но сама разница означает
    пересортицу на складе, и увидеть её надо. Привязка к `resolved` этот сигнал
    потеряла бы: свежие записи всегда применены."""
    from app.models import ReconciliationClassification, ReconciliationLog

    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(ReconciliationLog(
        uid_1c="u1", python_stock=5, in_flight=0, expected_1c=5, actual_1c=17,
        delta=12, classification=ReconciliationClassification.needs_review,
        resolved=True, checked_at=now_utc() - timedelta(hours=2)))
    db.commit()

    finding = _by_key(collect_findings(db), "reconciliation_review")

    assert finding is not None and finding.count == 1


# --------------------- закрытие старых расхождений сверки (кнопка)

def test_the_button_closes_only_old_reconciliation_rows(logged_in_client, web_db):
    """Закрывать можно ровно то, что отчёт уже не считает находкой. Свежие
    трогать нельзя: кнопка гасила бы сигнал вместо того, чтобы убрать архив."""
    from app.models import ReconciliationClassification, ReconciliationLog

    web_db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    old = ReconciliationLog(
        uid_1c="u1", python_stock=-1, in_flight=0, expected_1c=-1, actual_1c=0,
        delta=1, classification=ReconciliationClassification.needs_review,
        resolved=False, checked_at=now_utc() - timedelta(days=4))
    fresh = ReconciliationLog(
        uid_1c="u1", python_stock=5, in_flight=0, expected_1c=5, actual_1c=17,
        delta=12, classification=ReconciliationClassification.needs_review,
        resolved=False, checked_at=now_utc() - timedelta(hours=2))
    web_db.add_all([old, fresh])
    web_db.commit()

    r = logged_in_client.post("/diagnostics/close-old-reconciliation",
                              follow_redirects=False)

    assert r.status_code == 303
    web_db.refresh(old)
    web_db.refresh(fresh)
    assert old.resolved is True
    assert fresh.resolved is False, "свежее расхождение — это сигнал, а не архив"


def test_closing_old_rows_does_not_touch_stock(logged_in_client, web_db):
    """Команда правит журнальную пометку и больше ничего: остаток трогать —
    значит отправить на площадки число, которого никто не считал."""
    from app.models import ReconciliationClassification, ReconciliationLog

    web_db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    web_db.add(ReconciliationLog(
        uid_1c="u1", python_stock=-1, in_flight=0, expected_1c=-1, actual_1c=0,
        delta=1, classification=ReconciliationClassification.needs_review,
        resolved=False, checked_at=now_utc() - timedelta(days=4)))
    web_db.commit()

    logged_in_client.post("/diagnostics/close-old-reconciliation")

    product = web_db.query(Product).filter(Product.uid_1c == "u1").one()
    assert product.stock_on_hand == 5
    assert web_db.query(DispatchQueueItem).count() == 0


def test_closing_old_rows_is_written_to_the_journal(logged_in_client, web_db):
    from app.models import AuditLog, ReconciliationClassification, ReconciliationLog

    web_db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    web_db.add(ReconciliationLog(
        uid_1c="u1", python_stock=-1, in_flight=0, expected_1c=-1, actual_1c=0,
        delta=1, classification=ReconciliationClassification.needs_review,
        resolved=False, checked_at=now_utc() - timedelta(days=4)))
    web_db.commit()

    logged_in_client.post("/diagnostics/close-old-reconciliation")

    assert web_db.query(AuditLog).filter(
        AuditLog.action == "reconciliation_closed_old").count() == 1


def test_the_command_needs_a_login(client):
    r = client.post("/diagnostics/close-old-reconciliation", follow_redirects=False)

    assert r.status_code in (302, 303, 307)
    assert "/login" in r.headers.get("location", "")


# ------------------- находка обязана называть товар так, как его зовут люди

def test_a_missing_card_finding_names_the_article_and_the_cabinet(db):
    """20.09 на бою находка перечисляла внутренние идентификаторы вида
    `051ce509-a048-11ef-…`. В базе по ним всё находится, а человеку, который
    идёт с этим списком в кабинет площадки, они не говорят ничего."""
    account = _kit_account(db)
    db.add(Product(uid_1c="u1", article="TC26-2735", name="БлекВинил Куртка демисезон",
                   stock_on_hand=5))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=5, reason="manual_resend_all",
        status=DispatchStatus.error,
        last_error="нет карточки в каталоге кабинета — остаток отправить не по чему"))
    db.commit()

    _tick_all(db)
    finding = _by_key(collect_findings(db), "unknown_sku")

    assert "TC26-2735" in finding.details[0]
    assert "БлекВинил" in finding.details[0]
    assert "КИТ" in finding.details[0], "без кабинета непонятно, куда идти разбирать"
    assert "u1" not in finding.details[0]


def test_a_dispatch_error_finding_names_the_article_and_the_reason(db):
    """Та же беда была и здесь: uid плюс кусок технического текста.

    Счётчик попыток из строки убран намеренно (21.09). Эта находка берёт ТОЛЬКО
    записи в статусе `error`, то есть попытки по ним заведомо исчерпаны — «за 5
    попыток» не добавляет ничего, зато занимает место, на котором должен стоять
    ответ площадки. Ровно на этом оператор и споткнулся: причина обрывалась на
    адресе ручки, разобрать по ней было нечего."""
    account = _kit_account(db)
    db.add(Product(uid_1c="u1", article="D86321", name="Даунтлесс Куртка",
                   stock_on_hand=5))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=5, reason="order",
        status=DispatchStatus.error,
        last_error="не отправлено за 5 попыток: 500 склад недоступен"))
    db.commit()

    _tick_all(db)
    finding = _by_key(collect_findings(db), "dispatch_errors")

    assert "D86321" in finding.details[0]
    assert "склад недоступен" in finding.details[0], "ответ площадки тоже нужен"


def test_a_product_missing_from_the_catalogue_still_gets_a_line(db):
    """Товар мог быть удалён из номенклатуры, а запись очереди осталась. Строка
    обязана появиться всё равно — иначе находка насчитает больше, чем покажет."""
    account = _kit_account(db)
    db.add(DispatchQueueItem(
        uid_1c="u-нет", account_id=account.id, quantity=5, reason="order",
        status=DispatchStatus.error, last_error="не отправлено за 5 попыток: 500"))
    db.commit()

    _tick_all(db)
    finding = _by_key(collect_findings(db), "dispatch_errors")

    assert finding.details and "u-нет" in finding.details[0]


def test_the_sent_key_is_shown_when_it_is_known(db):
    """Для WB sku — это то, чем ищут карточку в кабинете: без него по артикулу
    искать дольше."""
    account = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=5, sent_sku="2000932279695",
        reason="order", status=DispatchStatus.error,
        last_error="площадка не знает этот sku на складе 1923790 (409 NotFound)"))
    db.commit()

    _tick_all(db)
    finding = _by_key(collect_findings(db), "unknown_sku")

    assert "2000932279695" in finding.details[0]


def test_the_reconciliation_finding_shows_what_diverged(db):
    """«Крупных расхождений: 14» без списка не отвечает на вопрос «что это».
    Пара чисел «у нас было / в 1С стало» сразу показывает, куда уехал склад."""
    from app.models import ReconciliationClassification, ReconciliationLog

    db.add(Product(uid_1c="u1", article="32481 (O)", name="МСЛ Рубашка К/р KAHVE",
                   stock_on_hand=5))
    db.add(ReconciliationLog(
        uid_1c="u1", python_stock=5, in_flight=0, expected_1c=5, actual_1c=17,
        delta=12, classification=ReconciliationClassification.needs_review,
        resolved=True, checked_at=now_utc() - timedelta(hours=2)))
    db.commit()

    finding = _by_key(collect_findings(db), "reconciliation_review")

    assert "32481 (O)" in finding.details[0]
    assert "5" in finding.details[0] and "17" in finding.details[0]
    assert "+12" in finding.details[0]


def test_the_diagnostics_counter_agrees_with_the_report(db):
    """20.09 на бою отчёт говорил «1 запись», а счётчик кабинета — «751»: он
    считал все строки в error за всё время. Две страницы, противоречащие друг
    другу, хуже одной неточной — верить перестают обеим."""
    from app.report import current_dispatch_errors

    account = _kit_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=5, reason="order",
        status=DispatchStatus.error, last_error="старое, уже неактуальное",
        created_at=now_utc() - timedelta(hours=6)))
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=5, reason="order",
        status=DispatchStatus.sent, sent_at=now_utc(), created_at=now_utc()))
    db.commit()

    assert current_dispatch_errors(db) == []
    assert current_dispatch_errors(db, account.id) == []
    assert "dispatch_errors" not in _keys(collect_findings(db))
