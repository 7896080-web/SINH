"""Чужой остаток по баркодам обязан дойти до ЧЕЛОВЕКА, а не только до лога.

06.10 на бою по `2617 C24-2317CQ BRICK RED`: в 1С по размеру 52 лежало 11, по
соседнему 15 — и наружу каждый час уезжало 15. Сверка это ЗНАЛА: несколько
штрихкодов одного SKU держат один физический остаток, разные числа означают
чужой баркод, и `run_reconciliation` считает такие товары в
`stats["barcode_conflicts"]`. Знала и молчала: счётчик уходил одной строкой
WARNING в лог воркера, а лог читают, когда уже что-то случилось.

Это ровно тот класс, который в проекте чинят с весны — писатель без читателя, —
и правило против него записано общим: задание, вернувшее ненулевой счётчик
непроведённой работы, пишет текст в `last_error` УСПЕШНОГО heartbeat, и у текста
обязан быть читатель. У обрезанного снимка читатель был, у разошедшихся
баркодов — нет.

Проверяем ПО СЛЕДСТВИЮ: текст в отметке и находка в отчёте. Проверка «функция
вызвана» молчала бы при оборванной цепочке ровно так же, как исправная.
"""
from app.models import Barcode, Product, WorkerHeartbeat
from app.report import CRITICAL, collect_findings
from app.timeutils import now_utc
from app.workers import scheduler
from app.workers.scheduler import RECONCILIATION_APPLIED


def _mark(db, text: str):
    db.add(WorkerHeartbeat(worker_name=RECONCILIATION_APPLIED,
                           last_run_at=now_utc(), last_success=True,
                           last_error=text))
    db.commit()


def _findings(db):
    return {f.key: f for f in collect_findings(db)}


def test_the_conflict_becomes_a_finding(db):
    """Главное следствие: человек видит это в отчёте, а не в логе."""
    _mark(db, "у 3 товаров 1С отдала РАЗНЫЕ остатки по баркодам одного товара "
              "— наружу уходит чужое число")

    found = _findings(db)

    assert "barcode_conflicts" in found, (
        "сверка насчитала конфликты, а отчёт промолчал")
    finding = found["barcode_conflicts"]
    assert "3" in finding.title, finding.title
    assert finding.level == CRITICAL, (
        "следствие — оверселл на живой карточке прямо сейчас")
    assert "Мэппинг" in finding.consequence, (
        "находка обязана говорить, ЧЕМ это чинится\n" + finding.consequence)


def test_a_clean_run_says_nothing(db):
    """Молчание на исправной системе — обязательное свойство отчёта."""
    _mark(db, "")

    assert "barcode_conflicts" not in _findings(db)


def test_two_notes_do_not_crowd_each_other_out(db):
    """Две беды сразу — две находки, и каждая со СВОИМ текстом.

    Поле одно, оговорки складываются через «; ». Возьми находка всё поле
    целиком, в заголовке про обрезанный снимок стояла бы ещё и фраза про
    баркоды — две разные беды слиплись бы в одну, и вторую человек не искал бы
    вовсе.
    """
    _mark(db, "снимок покрыл 10 позиций — обнуление распроданного отключено; "
              "у 4 товаров 1С отдала РАЗНЫЕ остатки по баркодам одного товара "
              "— наружу уходит чужое число")

    found = _findings(db)

    assert "suspicious_snapshot" in found and "barcode_conflicts" in found, found
    assert "РАЗНЫЕ остатки" not in found["suspicious_snapshot"].title, (
        "в находку про снимок уехал чужой текст\n"
        + found["suspicious_snapshot"].title)
    assert "обнуление" not in found["barcode_conflicts"].title, (
        "в находку про баркоды уехал чужой текст\n"
        + found["barcode_conflicts"].title)


def test_the_job_itself_writes_the_note(monkeypatch, db):
    """БОЕВЫМ путём: настоящая сверка на настоящем конфликте баркодов.

    Проверки выше начинаются с готового текста в отметке, то есть молчали бы,
    забудь кто-нибудь положить туда счётчик, — а именно это и было сломано:
    число считалось и уходило в лог. Поэтому здесь задание прогоняется целиком,
    со СВОЕЙ `run_reconciliation`, и смотрим мы на отметку, которую оно оставило.

    Данные — боевой случай 06.10: у строки два баркода, 1С отдала по ним 11 и
    15. Максимум возьмёт 15 — остаток соседнего размера.
    """
    db.add(Product(uid_1c="u-52", article="2617 C24-2317CQ", size="52",
                   color="BRICK RED", name="Куртка", stock_on_hand=11))
    db.add(Barcode(barcode="2000000000011", uid_1c="u-52"))
    db.add(Barcode(barcode="2000000000015", uid_1c="u-52"))
    db.commit()

    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
    monkeypatch.setattr(scheduler, "_build_ftp_exchange", lambda: object())
    monkeypatch.setattr(scheduler, "fetch_stock_export_snapshot",
                        lambda exchange, not_older_than=None: ([
                            {"barcodes": ["2000000000011"], "quantity": 11},
                            {"barcodes": ["2000000000015"], "quantity": 15},
                        ], now_utc()))
    monkeypatch.setattr(scheduler, "import_product_master",
                        lambda db_, rows: {"created": 0})

    scheduler.job_reconciliation()

    mark = db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == RECONCILIATION_APPLIED).first()
    assert mark is not None and mark.last_error, (
        "сверка увидела разные остатки по баркодам и ничего не сказала наверх")
    assert "РАЗНЫЕ остатки" in mark.last_error, mark.last_error

    assert "barcode_conflicts" in _findings(db), (
        "текст в отметке есть, а читателя у него нет — ровно тот дефект")
