"""Потолок чистки, о который упёрлись, обязан выходить наверх.

У чистки истории есть предел: `CHUNK` × `MAX_CHUNKS` = 100 000 строк на таблицу
за суточный прогон. Предел правильный — первый запуск на миллионах строк не
должен занять базу надолго, — но до этой правки он был НЕВИДИМ. Задание удаляло
ровно сто тысяч, писало в журнал «удалено 100 000», ставило зелёную отметку и
честно считалось отработавшим: своё дело оно сделало ровно столько, сколько
обещало. Если приходит больше ста тысяч в сутки, отставание копится каждые сутки
и само не рассосётся никогда.

Запас сегодня есть, и он не в коде, а в данных: журнал сверки пишет строку на
каждый товар в снимке — около полутора тысяч в час, сорока тысяч в сутки. Это
число товаров С НЕНУЛЕВЫМ ОСТАТКОМ, то есть растёт со складом, а не с каталогом:
втрое больше позиций в наличии — и потолок становится границей.

Второй случай попроще и тоже живой: бэкап несколько суток не снимался, чистка
все эти сутки законно отказывалась (копии нет — удалять безвозвратно нечем), и
после починки у неё долг, который разгребается по сто тысяч в сутки при сорока
тысячах нового прихода.
"""

from datetime import timedelta

import pytest

from app import retention
from app.models import (DispatchQueueItem, DispatchStatus, Platform,
                        ReconciliationClassification, ReconciliationLog,
                        ReturnItem, ReturnItemLog, ReturnStatus, WorkerHeartbeat)
from app.report import WARNING, _check_retention_backlog
from app.retention import PER_RUN_LIMIT, apply_retention, backlog_note
from app.timeutils import now_utc
from app.workers import scheduler


def _old(days: int):
    return now_utc() - timedelta(days=days)


def _recon(db, days: int, uid="u1"):
    db.add(ReconciliationLog(uid_1c=uid, checked_at=_old(days), python_stock=1,
                             in_flight=0, expected_1c=1, actual_1c=1, delta=0,
                             classification=ReconciliationClassification.normal))


def _queue(db, days: int, status=DispatchStatus.sent):
    db.add(DispatchQueueItem(uid_1c="u1", account_id=1, quantity=5, status=status,
                             reason="test", is_test=False, created_at=_old(days)))


@pytest.fixture()
def tiny_cap(monkeypatch):
    """Уменьшить потолок, не разводя его с самим собой.

    Три константы подменяются ВМЕСТЕ намеренно: `PER_RUN_LIMIT` — это и есть
    `CHUNK` × `MAX_CHUNKS`, и подмени мы только две, `_purge` сравнивал бы
    удалённое с боевыми ста тысячами и остаток не посчитал бы никогда. Что
    они обязаны сходиться, закреплено отдельным тестом ниже.
    """
    def apply(chunk: int, chunks: int):
        monkeypatch.setattr(retention, "CHUNK", chunk)
        monkeypatch.setattr(retention, "MAX_CHUNKS", chunks)
        monkeypatch.setattr(retention, "PER_RUN_LIMIT", chunk * chunks)
    return apply


def test_the_cap_constant_matches_its_parts():
    """Потолок считается ОТ своих слагаемых, а не числом рядом с ними.

    Разойдись они, `_purge` сравнивал бы удалённое не с тем числом: остаток
    не посчитался бы ни разу, и находка молчала бы ровно так же, как на
    исправной системе."""
    assert PER_RUN_LIMIT == retention.CHUNK * retention.MAX_CHUNKS


# ------------------------------------------------- упёрлись и сказали об этом

def test_hitting_the_cap_is_reported_with_a_number(db, tiny_cap):
    tiny_cap(chunk=2, chunks=1)
    for i in range(5):
        _recon(db, 200, uid=f"u{i}")
    db.commit()

    backlog: dict[str, int] = {}
    stats = apply_retention(db, backlog)

    assert stats["reconciliation_log"] == 2, "удалили не ровно потолок"
    assert backlog == {"журнал сверки": 3}, (
        "упёрлись в потолок и промолчали — ровно тот дефект, из-за которого "
        "отставание копилось бы навсегда при зелёной отметке")
    note = backlog_note(backlog)
    assert "3" in note and "журнал сверки" in note
    assert retention.BACKLOG_MARK in note


def test_a_run_that_cleaned_everything_leaves_no_note(db, tiny_cap):
    """Оговорка на исправном прогоне приучит её не читать.

    Строк меньше потолка — чистка вычерпала всё, и сказать ей нечего."""
    tiny_cap(chunk=5, chunks=2)
    _recon(db, 200)
    db.commit()

    backlog: dict[str, int] = {}
    apply_retention(db, backlog)

    assert backlog == {}
    assert backlog_note(backlog) == ""


def test_exactly_the_cap_and_nothing_left_is_not_a_backlog(db, tiny_cap):
    """Граница: удалили ровно потолок, а больше просроченного и не было.

    Число удалённых тут такое же, как при настоящем отставании, — отличает их
    только счёт остатка. Без него находка горела бы у каждой чистки, которой
    случайно досталось ровно сто тысяч строк."""
    tiny_cap(chunk=2, chunks=1)
    _recon(db, 200, uid="a")
    _recon(db, 200, uid="b")
    db.commit()

    backlog: dict[str, int] = {}
    assert apply_retention(db, backlog)["reconciliation_log"] == 2
    assert backlog == {}


def test_the_leftover_is_counted_by_the_SAME_condition_that_deletes(db, tiny_cap):
    """Остаток считается тем же условием, которым удаляли, — и это главное.

    У очереди рассылки срок хранения стоит на `sent`: запись в `pending` — это
    НЕЗАКОНЧЕННОЕ дело (остаток списан, число не уехало), и чистка её не берёт
    никогда. Считай остаток условием пошире — «старше тридцати суток», — и
    находка обещала бы отставание на строках, которые чистка и не собиралась
    удалять."""
    tiny_cap(chunk=2, chunks=1)
    for _ in range(3):
        _queue(db, 60, status=DispatchStatus.sent)
    for _ in range(5):
        _queue(db, 60, status=DispatchStatus.pending)
    db.commit()

    backlog: dict[str, int] = {}
    apply_retention(db, backlog)

    assert backlog == {"очередь рассылки": 1}, (
        "в остаток попали незаконченные отправки — их чистка не удаляет вовсе")


def test_a_row_that_became_purgeable_mid_run_is_not_a_backlog(db, tiny_cap,
                                                              monkeypatch):
    """Остаток считается ТОЛЬКО когда потолок действительно упёрся.

    Случай живой: запись очереди старше тридцати суток лежит в `pending`
    (незаконченная отправка — чистка её не берёт никогда), и ровно в эти секунды
    рассылка доводит её до `sent`. Чистка свою таблицу уже прошла, строка
    просрочена, потолок тут ни при чём. Считай мы остаток всегда — оговорка
    «не успеваем» выходила бы из-за одной такой строки, а оговорка на исправном
    прогоне приучает её не читать."""
    tiny_cap(chunk=2, chunks=1)
    _queue(db, 60, status=DispatchStatus.sent)
    late = DispatchQueueItem(uid_1c="u2", account_id=1, quantity=5,
                             status=DispatchStatus.pending, reason="test",
                             is_test=False, created_at=_old(60))
    db.add(late)
    db.commit()

    real_commit = db.commit
    flipped: list[int] = []

    def commit_and_flip():
        real_commit()
        if not flipped:                     # ровно один раз, посреди прогона
            flipped.append(1)
            late.status = DispatchStatus.sent
            real_commit()

    monkeypatch.setattr(db, "commit", commit_and_flip)

    backlog: dict[str, int] = {}
    apply_retention(db, backlog)

    assert flipped == [1], "подмена не сработала — тест проверяет не то, что думает"
    assert backlog == {}, (
        "в остаток попала строка, ставшая просроченной уже после прохода: "
        "потолок не упирался, удалено меньше него")


# ------------------------------------------- потолок не должен осиротить детей

def test_a_return_is_not_deleted_while_its_history_is_not_purged(db, tiny_cap):
    """Родителя не удаляем, пока дети вычерпаны не до конца.

    Потолок стоит на КАЖДОЙ таблице отдельно, а история растёт быстрее самих
    вещей — несколько строк на вещь. Упрись история в потолок, а вещи удались в
    том же прогоне, и подзапрос `doomed` в следующем прогоне остатка детей уже
    не найдёт: вещи нет, условие пустое, строки истории осиротели бы НАВСЕГДА —
    ровно против этого они и чистятся первыми. Поймано чтением своего же диффа:
    счёт остатка сделал потолок видимым, а дефект был и до него."""
    tiny_cap(chunk=2, chunks=1)
    for i in range(3):
        item = ReturnItem(barcode=f"b{i}", platform=Platform.wb,
                          status=ReturnStatus.back_to_sale,
                          status_changed_at=_old(400))
        db.add(item)
        db.flush()
        for _ in range(2):
            db.add(ReturnItemLog(return_id=item.id, at=_old(400),
                                 to_status=ReturnStatus.back_to_sale))
    db.commit()

    backlog: dict[str, int] = {}
    stats = apply_retention(db, backlog)

    assert stats["return_item_log"] == 2, "история чистится первой и порциями"
    assert stats["return_items"] == 0, (
        "вещь удалили при недочищенной истории — её строки осиротели навсегда")
    assert db.query(ReturnItem).count() == 3
    assert backlog.get("история возвратов") == 4, (
        "про недочищенных детей обязана говорить оговорка, иначе пропуск молчит")


def test_a_return_is_deleted_when_its_history_ended_exactly_at_the_cap(db,
                                                                       tiny_cap):
    """Граница: детей было РОВНО столько, сколько чистка берёт за прогон.

    Удалено ровно сто тысяч — и это может значить как «осталось ещё», так и
    «больше не было». Считай мы по числу удалённых, родители ждали бы целые
    сутки без всякой причины. Вопрос задаётся правильный: остались ли дети."""
    tiny_cap(chunk=2, chunks=1)
    item = ReturnItem(barcode="b", platform=Platform.wb,
                      status=ReturnStatus.back_to_sale,
                      status_changed_at=_old(400))
    db.add(item)
    db.flush()
    for _ in range(2):                      # ровно потолок
        db.add(ReturnItemLog(return_id=item.id, at=_old(400),
                             to_status=ReturnStatus.back_to_sale))
    db.commit()

    stats = apply_retention(db)

    assert stats["return_item_log"] == 2
    assert stats["return_items"] == 1, (
        "детей не осталось, а родителя всё равно отложили на сутки")
    assert db.query(ReturnItem).count() == 0


def test_the_returns_catch_up_on_later_runs(db, tiny_cap):
    """И это не тупик: следующий прогон берёт следующую порцию и доходит сам."""
    tiny_cap(chunk=2, chunks=1)
    item = ReturnItem(barcode="b", platform=Platform.wb,
                      status=ReturnStatus.back_to_sale,
                      status_changed_at=_old(400))
    db.add(item)
    db.flush()
    for _ in range(3):
        db.add(ReturnItemLog(return_id=item.id, at=_old(400),
                             to_status=ReturnStatus.back_to_sale))
    db.commit()

    for _ in range(5):                      # с запасом: порция по две строки
        apply_retention(db)

    assert db.query(ReturnItemLog).count() == 0
    assert db.query(ReturnItem).count() == 0, (
        "вещи не удалились даже после того, как история вычерпалась")


# --------------------------------------------------------------- у текста читатель

def test_the_job_puts_the_backlog_into_a_successful_heartbeat(monkeypatch, db):
    """Успех, а не ошибка: `/health` про живость, расхождение разбирает отчёт.

    Задание отработало и своё дело сделало — ровно столько, сколько обещало."""
    def fake(db_, backlog=None):
        if backlog is not None:
            backlog["журнал сверки"] = 7
        return {"reconciliation_log": PER_RUN_LIMIT}

    monkeypatch.setattr(scheduler, "apply_retention", fake)
    monkeypatch.setattr(scheduler, "last_backup", lambda: (now_utc(), 3))
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    scheduler.job_retention()

    row = db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == "retention").first()
    assert row.last_success is True
    assert retention.BACKLOG_MARK in (row.last_error or ""), (
        "задание упёрлось в потолок и не сказало об этом наверх")
    assert "7" in row.last_error


def test_the_report_reads_the_note(db):
    db.add(WorkerHeartbeat(worker_name="retention", last_run_at=now_utc(),
                           last_success=True,
                           last_error=backlog_note({"журнал сверки": 120000})))
    db.flush()

    finding = _check_retention_backlog(db)
    assert finding is not None, "оговорка без читателя — тот же дефект"
    assert finding.level == WARNING, (
        "деньги прямо сейчас не теряются; разбавь мы критичное отложенным, "
        "уведомления обесценились бы целиком — они будят только на красном")
    assert "120000" in finding.title
    assert finding.consequence, "находка обязана называть следствие, а не факт"


def test_the_report_stays_quiet_on_a_clean_run(db):
    db.add(WorkerHeartbeat(worker_name="retention", last_run_at=now_utc(),
                           last_success=True, last_error=""))
    db.flush()
    assert _check_retention_backlog(db) is None


def test_the_report_does_not_fire_on_someone_elses_note(db):
    """В поле лежит оговорка про отсутствующую копию — это ДРУГАЯ беда, и о ней
    уже говорит `_check_backup_missing`. Взяв поле целиком, находка объявила бы
    отставание чистки там, где чистка просто не начиналась."""
    db.add(WorkerHeartbeat(
        worker_name="retention", last_run_at=now_utc(), last_success=True,
        last_error="чистка не начата: нет свежей копии базы (копий нет вовсе)"))
    db.flush()
    assert _check_retention_backlog(db) is None


def test_the_report_takes_only_its_own_part_of_the_field(db):
    """Две оговорки складываются через «; », и каждая находка берёт свою.

    Сойдись они в одном заголовке, две разные беды слиплись бы в одну, и вторую
    человек не искал бы вовсе."""
    db.add(WorkerHeartbeat(
        worker_name="retention", last_run_at=now_utc(), last_success=True,
        last_error="что-то ещё про другое; "
                   + backlog_note({"журнал сверки": 99})))
    db.flush()

    finding = _check_retention_backlog(db)
    assert finding is not None
    assert "что-то ещё про другое" not in finding.title
    assert "99" in finding.title


def test_the_check_is_wired_into_the_report():
    """Проверка, которой нет в списке, — проверка без читателя."""
    from app.report import CHECKS
    assert _check_retention_backlog in CHECKS
