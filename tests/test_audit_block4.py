"""Находки аудита 22.09, блок «планировщик, отчёт, мониторинг, накат».

Общее у всех: система уже сломана, а показывает зелёное — либо потому, что
отметка об успехе ставится по факту «функция вернулась», либо потому, что
находка отчёта описывает не то, что произошло.
"""
import os
from datetime import timedelta

import pytest

from app import report
from app.models import (AuditLog, DispatchQueueItem, DispatchStatus, FtpTask,
                        FtpTaskStatus, MappingConflict, Platform, PlatformAccount,
                        Product, SyncSetting, WorkerHeartbeat)
from app.timeutils import now_utc
from tests.factories import make_account


# --------------------------------------------------------------------------
# 1. Справочник 1С заводит новый баркод — «актуализирован» обязан слететь
# --------------------------------------------------------------------------

def test_a_new_barcode_from_the_1c_dictionary_drops_the_recalc_mark(db):
    """Расчёт собирал заказы строго по ПРЕЖНЕМУ набору баркодов.

    Продажи по только что привязанному он заведомо не видел, а догнать их
    нечем: товар числится актуализированным, `catch_up_product` по нему не
    зовут, живой опрос старый заказ не принесёт. Наружу уходит остаток,
    завышенный ровно на эти продажи. Переподвязка, автопривязка по пулу и
    импорт «Мэппинга» это правило соблюдали; приём справочника — нет.
    """
    from app.workers.reconciliation import import_barcode_dict

    db.add(Product(uid_1c="u1", article="A-1", stock_on_hand=5,
                   recalc_done_at=now_utc(), recalc_account_ids="1"))
    db.commit()

    import_barcode_dict(db, [{"uid_1c": "u1", "barcode": "BC-NEW"}], full=True)

    product = db.query(Product).filter(Product.uid_1c == "u1").first()
    assert product.recalc_done_at is None
    # Пустая СТРОКА, а не NULL: NULL означал бы «расчёта не было никогда», и
    # ступень 2 лестницы перестала бы срабатывать вовсе.
    assert product.recalc_account_ids == ""


def test_a_product_without_a_recalc_is_left_alone_by_the_dictionary(db):
    """У товара без расчёта не трогаем НИЧЕГО.

    Выставить там пустую строку значит включить ступень 2 лестницы, которая для
    таких товаров обязана молчать: на отмеченный кабинет уехал бы ноль.
    """
    from app.workers.reconciliation import import_barcode_dict

    db.add(Product(uid_1c="u2", article="A-2", stock_on_hand=5))
    db.commit()

    import_barcode_dict(db, [{"uid_1c": "u2", "barcode": "BC-2"}], full=True)

    product = db.query(Product).filter(Product.uid_1c == "u2").first()
    assert product.recalc_done_at is None
    assert product.recalc_account_ids is None


def test_an_already_known_barcode_does_not_drop_the_mark(db):
    """Повторный прогон справочника ничего не заводит — значит и не снимает.

    Иначе четвертьчасовой импорт снимал бы «актуализирован» со всего каталога
    каждые пятнадцать минут, и трансляцию нельзя было бы включить никогда.
    """
    from app.models import Barcode
    from app.workers.reconciliation import import_barcode_dict

    db.add(Product(uid_1c="u3", article="A-3", stock_on_hand=5,
                   recalc_done_at=now_utc(), recalc_account_ids="1"))
    db.add(Barcode(barcode="BC-3", uid_1c="u3", source_platform="1c_dict"))
    db.commit()

    import_barcode_dict(db, [{"uid_1c": "u3", "barcode": "BC-3"}], full=True)

    assert db.query(Product).filter(Product.uid_1c == "u3").first().recalc_done_at


# --------------------------------------------------------------------------
# 2. Сбой одного кабинета сверки не уносит остальные
# --------------------------------------------------------------------------

def test_a_cabinet_whose_commit_fails_does_not_kill_the_rest_of_the_verification(db):
    """Без `db.rollback()` сессия остаётся деактивированной.

    Каждый следующий кабинет падал бы на первом же запросе с
    `PendingRollbackError`, то есть один сбойный кабинет уносил сверку по ВСЕМ.
    Существующий тест это не ловил: он роняет `build_client` ДО единой записи в
    базу, сессия остаётся здоровой.
    """
    from app.workers import verify_stock

    first = make_account(db, name="Первый")
    second = make_account(db, name="Второй")
    calls = []

    def fake_verify_account(session, client, account):
        calls.append(account.name)
        if account.name == "Первый":
            # Ровно то, что делает упавший коммит: сессия деактивирована.
            session.add(Product(uid_1c=None))       # нарушение NOT NULL
            try:
                session.commit()
            except Exception:
                pass
            assert not session.is_active
            raise RuntimeError("database is locked")
        return {"checked": 1, "match": 1, "diverged": 0, "unknown_sku": 0,
                "skipped": 0}

    original = verify_stock.verify_account
    verify_stock.verify_account = fake_verify_account
    try:
        total = verify_stock.verify_all(db, lambda s, i: object(), [first, second])
    finally:
        # Именно ВЕРНУТЬ, а не `del`: удаление снесло бы настоящую функцию из
        # модуля, и следующие тесты падали бы на импорте — модульное состояние
        # течёт между тестами, об этом в проекте уже спотыкались дважды.
        verify_stock.verify_account = original

    assert calls == ["Первый", "Второй"], "второй кабинет вообще не проверили"
    assert total["errors"] == 1
    assert total["checked"] == 1


# --------------------------------------------------------------------------
# 3. Зелёная отметка при непроведённых заказах — теперь с текстом и находкой
# --------------------------------------------------------------------------

def test_orders_that_were_not_processed_become_a_finding(db):
    """Откат по заказу уносит и `SyncAnomaly`, и `ProcessedOrder`.

    Персистентного следа не остаётся нигде, кроме строки лога до ротации, а
    `/health`, «Диагностика» и отчёт при этом зелены. Единица продана, у нас не
    списана, перемещения в 1С нет — остаток завышен и уезжает наружу.
    """
    db.add(WorkerHeartbeat(worker_name="poll_orders_account_1", last_run_at=now_utc(),
                           last_success=True,
                           last_error="заказов не проведено: 2 — IntegrityError"))
    db.commit()

    finding = report._check_orders_not_processed(db)
    assert finding is not None
    assert finding.level == report.CRITICAL
    assert "оверселл" in finding.consequence


def test_a_clean_order_poll_is_not_a_finding(db):
    """Разовый сбой стирается следующим циклом через 45 секунд.

    Находка — про УСТОЙЧИВЫЙ отказ, а не про мигание: отметка без текста её не
    даёт.
    """
    db.add(WorkerHeartbeat(worker_name="poll_orders_account_1", last_run_at=now_utc(),
                           last_success=True, last_error=None))
    db.commit()
    assert report._check_orders_not_processed(db) is None


def test_a_broken_verification_is_visible(db):
    """`job_verify_stock` ветку выбирал по `diverged`, а он ноль и когда не
    проверено НИЧЕГО: сорванный прогон выглядел как чистый."""
    db.add(WorkerHeartbeat(worker_name="verify_stock", last_run_at=now_utc(),
                           last_success=True,
                           last_error="сверка не прошла по кабинетам: 2"))
    db.commit()
    finding = report._check_verify_stock_broken(db)
    assert finding is not None
    assert "2" in finding.title


def test_a_truncated_catalogue_is_visible(db):
    """Признак вычислялся и уезжал в heartbeat, но показать его было некому:
    «Диагностика» рисует по кабинету только `poll_orders_account_*`."""
    db.add(WorkerHeartbeat(worker_name="catalog_poll_account_3", last_run_at=now_utc(),
                           last_success=True,
                           last_error="выгрузка каталога оборвана пределом страниц"))
    db.commit()
    finding = report._check_truncated_catalog(db)
    assert finding is not None
    assert "ключи" in finding.consequence or "ключ" in finding.consequence


# --------------------------------------------------------------------------
# 4. «Карточки нет» у Kit и Ozon — не «рассылка не доехала»
# --------------------------------------------------------------------------

def _pair(db, uid="u1", error="", card_missing=False):
    account = make_account(db)
    db.add(Product(uid_1c=uid, article="A-1", stock_on_hand=5,
                   broadcast_enabled=True))
    db.add(SyncSetting(uid_1c=uid, account_id=account.id, enabled=True))
    db.add(DispatchQueueItem(uid_1c=uid, account_id=account.id, quantity=5,
                             reason="order", status=DispatchStatus.error,
                             last_error=error, card_missing=card_missing,
                             created_at=now_utc()))
    db.commit()
    return account


@pytest.mark.parametrize("error", [
    "площадка не знает такой товар (variant_id 42) — карточки в этом кабинете нет",
    "карточка товара в архиве — остаток на неё не принимают",
    "Ozon: OFFER_NOT_FOUND: offer not found in the warehouse",
])
def test_a_missing_card_is_not_reported_as_a_broken_dispatch(db, error):
    """Ни один из этих текстов не совпадал с русскими подстроками отчёта.

    У Kit слова другие, у Ozon сообщение площадки по-английски. Все они
    становились КРИТИЧНОЙ находкой «остаток списан, площадка продаёт то, чего
    нет» — при том, что продавать там нечего вовсе. И навсегда: запись
    терминальная, успешной отправки по паре не будет, снять её нечем.
    """
    _pair(db, error=error, card_missing=True)

    assert report._q_dispatch_errors(db) == []
    assert len(report._q_unknown_sku(db)) == 1


def test_a_real_dispatch_failure_is_still_a_broken_dispatch(db):
    """Обратная сторона: сбой связи остаётся критичной находкой."""
    _pair(db, error="HTTPSConnectionPool: read timed out", card_missing=False)

    assert len(report._q_dispatch_errors(db)) == 1
    assert report._q_unknown_sku(db) == []


def test_old_rows_without_the_flag_are_still_recognised_by_text(db):
    """Записи, лежащие в базе с прежних времён, флага не имеют.

    Их добирает бэкфилл миграции, но правило оставлено и в запросе: потерять
    старую запись значило бы вернуть её в «рассылка не доехала».
    """
    _pair(db, error="площадка не знает этот sku на складе wh-1 (409 NotFound)",
          card_missing=False)

    assert report._q_dispatch_errors(db) == []
    assert len(report._q_unknown_sku(db)) == 1


# --------------------------------------------------------------------------
# 5. Конфликт от выгрузки каталога — не «непроведённая продажа»
# --------------------------------------------------------------------------

def test_a_conflict_from_the_catalogue_does_not_claim_sales(db):
    """`catalog_sync` заводит строку на карточку площадки, которой нет в 1С.

    Заказов по ней ноль, товара в 1С нет, остаток ничем не завышен. Находка
    брала ВСЕ строки без разбора, печатала их число как «заказов по ним N» и
    обещала «остаток завышен ровно на эти продажи» — через трое суток становясь
    КРИТИЧНОЙ и не гаснув: чистит их только появление баркода в 1С, а суточная
    выгрузка заводит новые.
    """
    account = make_account(db)
    db.add(MappingConflict(barcode="BC-1", account_id=account.id, attempts=0,
                           first_seen=now_utc() - timedelta(days=10)))
    db.commit()

    assert report._check_mapping_conflicts(db) is None
    other = report._check_catalog_cards_without_1c(db)
    assert other is not None
    assert other.level == report.WARNING
    assert "Заказов по ним ещё не было" in other.consequence


def test_a_conflict_from_a_real_order_is_still_critical(db):
    """`resolve_barcode` увеличивает счётчик на КАЖДОМ заказе."""
    account = make_account(db)
    db.add(MappingConflict(barcode="BC-2", account_id=account.id, attempts=3,
                           first_seen=now_utc() - timedelta(days=10)))
    db.commit()

    finding = report._check_mapping_conflicts(db)
    assert finding is not None
    assert finding.level == report.CRITICAL
    assert "заказов по ним 3" in finding.title
    assert report._check_catalog_cards_without_1c(db) is None


def test_the_catalogue_registers_a_conflict_with_zero_orders(db):
    """Единица здесь была прямой неправдой: отчёт складывает `attempts` и
    печатает сумму как «заказов по ним N», а заказов не было ни одного."""
    from app.workers.catalog_sync import load_platform_catalog
    from app.workers.platform_clients.base import CatalogItem

    account = make_account(db)

    class Client:
        last_truncated = False

        def get_catalog_items(self):
            return [CatalogItem(external_id="1:2", barcode="BC-X",
                                article="A-X", name="Неизвестный товар")]

    load_platform_catalog(db, Client(), account)

    conflict = db.query(MappingConflict).filter(
        MappingConflict.barcode == "BC-X").first()
    assert conflict is not None
    assert conflict.attempts == 0

    # И сразу следствие: находка про непроведённые продажи его не берёт.
    assert report._check_mapping_conflicts(db) is None
    assert report._check_catalog_cards_without_1c(db) is not None


# --------------------------------------------------------------------------
# 6. Кнопка «Сбросить счётчик сбоев» больше не гасит находку
# --------------------------------------------------------------------------

def test_resetting_failures_is_refused_for_a_breaker_disabled_cabinet(logged_in_client, web_db):
    """Кнопка гасила единственный сигнал, ничего не чиня.

    `_check_breaker_disabled` требует ОБОИХ признаков — выключен И счётчик
    ненулевой, — значит после нажатия критичная находка исчезала навсегда: у
    выключенного кабинета per-account задание снято, счётчик больше никто не
    увеличит, `/health` выключенные кабинеты пропускает намеренно.
    """
    account = PlatformAccount(platform=Platform.wb, name="Погашенный",
                              warehouse_id="wh", is_active=False,
                              consecutive_failures=5, last_error="401")
    web_db.add(account)
    web_db.commit()

    assert report._check_breaker_disabled(web_db) is not None

    logged_in_client.post(f"/diagnostics/accounts/{account.id}/reset-failures",
                          follow_redirects=True)

    web_db.expire_all()
    assert web_db.query(PlatformAccount).first().consecutive_failures == 5
    assert report._check_breaker_disabled(web_db) is not None


def test_resetting_failures_still_works_for_a_live_cabinet(logged_in_client, web_db):
    """Ради чего кнопка и заведена: оператор поправил ключи и не хочет ждать."""
    account = PlatformAccount(platform=Platform.wb, name="Живой", warehouse_id="wh",
                              is_active=True, consecutive_failures=3)
    web_db.add(account)
    web_db.commit()

    logged_in_client.post(f"/diagnostics/accounts/{account.id}/reset-failures",
                          follow_redirects=True)

    web_db.expire_all()
    assert web_db.query(PlatformAccount).first().consecutive_failures == 0


# --------------------------------------------------------------------------
# 7. Зависшая отмена описана своим знаком
# --------------------------------------------------------------------------

def test_a_stuck_cancellation_is_described_with_its_own_sign(db):
    """Открытое `CANCEL_MOVEMENT` считается «в пути» со знаком МИНУС.

    Остаток при этом ЗАВЫШЕН, наружу уходит больше, чем есть, — прямой
    оверселл, то есть противоположность создания. Находка давала одно следствие
    на всех, а `details` команду не называли вовсе.
    """
    account = make_account(db)
    db.add(FtpTask(command="CANCEL_MOVEMENT", order_id="O-1", barcode="BC-1",
                   quantity=2, account_id=account.id, status=FtpTaskStatus.timeout,
                   repost_count=0, created_at=now_utc() - timedelta(hours=5)))
    db.commit()

    finding = report._check_tasks_needing_review(db)
    assert finding is not None
    assert "завышен" in finding.consequence
    assert "CANCEL_MOVEMENT" in finding.details[0]


# --------------------------------------------------------------------------
# 8. У расхождения с площадкой появился полный список
# --------------------------------------------------------------------------

def test_the_divergence_finding_leads_to_a_page_that_exists(db):
    """Ссылка вела на `/diagnostics#accounts` — карточку со счётчиками очереди,
    где слов про сверку нет вовсе, а ключа в `FULL_LISTS` не было."""
    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A-1", stock_on_hand=5))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=9,
                             reason="order", status=DispatchStatus.sent,
                             sent_quantity=9, sent_sku="BC-1",
                             verified_at=now_utc(), verified_quantity=40,
                             sent_at=now_utc(), created_at=now_utc()))
    db.commit()

    finding = report._check_platform_divergence(db)
    assert finding is not None
    assert finding.link == "/report/rows/platform_divergence"
    assert "platform_divergence" in report.FULL_LISTS

    title, columns, rows_fn = report.FULL_LISTS["platform_divergence"]
    rows = rows_fn(db)
    assert len(rows) == 1
    assert len(rows[0]) == len(columns)
    assert "40" in rows[0]


# --------------------------------------------------------------------------
# 9. Архив обмена чистится
# --------------------------------------------------------------------------

def test_the_exchange_archive_is_pruned_by_age(tmp_path):
    """Архив не чистил НИКТО: `archive_result` делает `os.replace` и всё.

    Копии базы лежат на том же диске, и когда он кончится, разом откажут запись
    в SQLite, снятие копии и публикация файлов для 1С — последний рубеж
    исчезнет ровно тогда, когда нужен.
    """
    from app.retention import prune_exchange_archive

    old = tmp_path / "result_20260101.txt"
    fresh = tmp_path / "result_20260921.txt"
    old.write_text("x")
    fresh.write_text("y")
    ancient = (now_utc() - timedelta(days=90)).timestamp()
    os.utime(old, (ancient, ancient))

    removed = prune_exchange_archive(str(tmp_path))

    assert removed == 1
    assert not old.exists()
    assert fresh.exists()


def test_pruning_a_missing_archive_is_quiet(tmp_path):
    """На машине разработки каталога обмена нет вовсе."""
    from app.retention import prune_exchange_archive
    assert prune_exchange_archive(str(tmp_path / "нет-такого")) == 0


# --------------------------------------------------------------------------
# 10. Молчание площадки и «читать не умеем» — разные вещи
# --------------------------------------------------------------------------

class _Silent:
    """Клиент, который читать остатки УМЕЕТ, но площадка не ответила."""
    reads_stocks = True
    stock_key = "barcode"

    def get_stocks(self, warehouse_id, items):
        return None


class _DoesNotRead:
    """Ozon и Kit: метод не написан вовсе — вслепую такое не пишется."""
    reads_stocks = False
    stock_key = "barcode"

    def get_stocks(self, warehouse_id, items):
        return None


def _sent_row(db, account):
    db.add(Product(uid_1c="u1", article="A-1", stock_on_hand=5))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=5,
                             reason="order", status=DispatchStatus.sent,
                             sent_quantity=5, sent_sku="BC-1",
                             # Старше SETTLE_DELAY: свежую отправку сверка не
                             # берёт намеренно — площадке нужно время применить.
                             sent_at=now_utc() - timedelta(minutes=30),
                             created_at=now_utc() - timedelta(minutes=30)))
    db.commit()


def test_a_silent_platform_is_not_the_same_as_a_platform_we_cannot_read(db):
    """403 по снятой области, 404 по переименованной ручке и хронический 429
    дают `None`, а не исключение: `with_retry` 4xx кроме 429 не повторяет.

    Считая это тем же `skipped`, что и штатная тишина Ozon, мы получали зелёный
    heartbeat при неработающей сверке — а это единственная проверка, которая
    ловит перезапись наших остатков второй системой.
    """
    from app.workers.verify_stock import verify_account

    account = make_account(db)
    _sent_row(db, account)

    stats = verify_account(db, _Silent(), account)
    assert stats["silent"] == 1
    assert stats["skipped"] == 0


def test_a_platform_we_do_not_read_is_quiet(db):
    """Обратная сторона: у Ozon и Kit это штатно и навсегда, ругаться нельзя."""
    from app.workers.verify_stock import verify_account

    account = make_account(db)
    _sent_row(db, account)

    stats = verify_account(db, _DoesNotRead(), account)
    assert stats["skipped"] == 1
    assert stats["silent"] == 0


# --------------------------------------------------------------------------
# 11. «Диагностика» показывает отметку с оговоркой, а не зелёное «ок»
# --------------------------------------------------------------------------

def test_a_successful_heartbeat_with_a_problem_is_not_shown_as_ok(logged_in_client, web_db):
    """Успешная отметка с текстом рисовалась зелёным «ок», а текст не показывал
    НИКТО — ни страница, ни `/health` (там при `last_success=True` подставляется
    `None`). Признак вычислялся и терялся."""
    web_db.add(WorkerHeartbeat(worker_name="verify_stock", last_run_at=now_utc(),
                               last_success=True,
                               last_error="площадка не ответила на чтение остатков"))
    web_db.commit()

    page = logged_in_client.get("/diagnostics").text
    assert "с оговоркой" in page
    assert "площадка не ответила на чтение остатков" in page


def test_the_catalogue_poll_heartbeat_is_shown_per_cabinet(logged_in_client, web_db):
    """`catalog_poll_account_*` не рендерился нигде, хотя комментарий в
    `job_catalog_poll` утверждал обратное."""
    account = PlatformAccount(platform=Platform.wb, name="ИП ЯВОРСКАЯ",
                              warehouse_id="wh", is_active=True)
    web_db.add(account)
    web_db.commit()
    web_db.add(WorkerHeartbeat(worker_name=f"catalog_poll_account_{account.id}",
                               last_run_at=now_utc(), last_success=True,
                               last_error="выгрузка каталога оборвана пределом страниц"))
    web_db.commit()

    page = logged_in_client.get("/diagnostics").text
    assert "Выгрузка каталога" in page
    assert "оборвана пределом страниц" in page


# --------------------------------------------------------------------------
# 12. Восстановление из копии убирает спутников
# --------------------------------------------------------------------------

def test_restoring_removes_the_wal_companions(tmp_path, monkeypatch):
    """Процедура из четырёх команд не восстанавливала НИЧЕГО, и молча.

    NSSM при жёсткой остановке оставляет `-wal`/`-shm` со страницами ПРЕЖНЕЙ
    базы, SQLite при первом открытии накатывает их поверх подложенной копии.
    `integrity_check` при этом отвечает `ok`, размер правдоподобный — человек
    уверен, что откатился.
    """
    import sqlite3

    import scripts.restore_db as restore

    target = tmp_path / "sync_admin.db"
    source = tmp_path / "backup.db"
    for path in (target, source):
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE products (uid_1c TEXT)")
        con.commit()
        con.close()
    # Спутники, оставшиеся от жёстко убитой службы.
    for suffix in ("-wal", "-shm"):
        target.with_name(target.name + suffix).write_bytes(b"stale")

    monkeypatch.setattr(restore, "database_path", lambda *a, **k: target)
    monkeypatch.setattr(restore, "_services_running", lambda: [])
    monkeypatch.setattr(restore, "make_backup",
                        lambda *a, **k: type("R", (), {"ok": True, "path": "x"})())

    assert restore.main(["restore_db.py", str(source)]) == 0

    for suffix in ("-wal", "-shm"):
        assert not target.with_name(target.name + suffix).exists(), \
            f"спутник {suffix} остался рядом с восстановленной базой"
    kept = target.with_name(target.name + ".before-restore")
    assert kept.exists(), "прежняя база не отложена"
    assert kept.with_name(kept.name + "-wal").exists(), \
        "спутники унесены, а не удалены: в них последняя транзакция прежней базы"


def test_restoring_refuses_while_the_services_are_running(tmp_path, monkeypatch):
    """Живая служба держит базу открытой: замена файла под ней даёт мусор."""
    import scripts.restore_db as restore

    target = tmp_path / "sync_admin.db"
    source = tmp_path / "backup.db"
    target.write_bytes(b"")
    source.write_bytes(b"")
    monkeypatch.setattr(restore, "database_path", lambda *a, **k: target)
    monkeypatch.setattr(restore, "_services_running", lambda: ["sync_admin_web"])

    assert restore.main(["restore_db.py", str(source)]) == 1
