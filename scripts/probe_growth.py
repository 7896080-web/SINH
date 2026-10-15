"""Куда растёт база и на сколько хватит чистки — только чтение.

Запуск:

    C:\\sync_admin\\.venv\\Scripts\\python.exe C:\\sync_admin\\scripts\\probe_growth.py

Зачем. Все беды долгого срока службы в этой системе одного вида: она работает
дальше и просто перестаёт делать что-то одно, а увидеть это можно лишь по
следствию, то есть месяцами позже. Самая дорогая из них — у чистки есть ПОТОЛОК
(`retention.PER_RUN_LIMIT`, сто тысяч строк на таблицу за суточный прогон), и
если приход больше него, отставание копится каждые сутки и само не рассосётся
никогда. Запас сегодня есть, но он не в коде, а В ДАННЫХ: журнал сверки пишет
строку на каждый товар в снимке, то есть зависит от числа позиций С НЕНУЛЕВЫМ
ОСТАТКОМ, а не от каталога. Вырастет склад втрое — и запаса не станет.

Поэтому скрипт печатает не «всё хорошо», а ЧИСЛА: приход за сутки по каждой
таблице рядом с потолком, строки без срока хранения вообще, размеры на диске.
Решение по ним принимает человек.

Что скрипт НЕ делает. Он не авторитет по тому, что именно удалит чистка:
«старше срока» здесь считается ПО ДАТЕ, без оглядки на статус, а чистка берёт
только то, что описывает прошлое (`sent`, `done`, `resolved`), и никогда не
трогает незаконченное. То есть это оценка СВЕРХУ. Настоящее отставание называет
само задание — оговоркой успешной отметки, и она печатается рядом: расхождение
между своим счётом и её числом важнее любой отдельной строки.

Ничего не меняет и не удаляет.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import timedelta                                # noqa: E402
from pathlib import Path                                      # noqa: E402

from sqlalchemy import func                                   # noqa: E402
from sqlalchemy.exc import OperationalError                   # noqa: E402

from app import retention                                     # noqa: E402
from app.backup import backup_dir, database_path              # noqa: E402
from app.database import SessionLocal                         # noqa: E402
from app.models import (AnomalyStatus, Barcode, Product,      # noqa: E402
                        SyncAnomaly, WorkerHeartbeat)
from app.routers.anomalies import PAGE_LIMIT                  # noqa: E402
from app.timeutils import now_utc                             # noqa: E402

# Консоль боевого сервера пишет в cp1251. Свои строки держим в её пределах
# (закрыто тестом), но сюда печатаются и чужие данные — имена таблиц, текст
# оговорки, пути. Один символ, которого в cp1251 нет, уронил бы ВЕСЬ вывод.
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):      # перенаправленный вывод, старый Python
    pass

# Таблицы со сроком хранения: имя, модель, колонка времени, срок. Сроки берутся
# ИЗ `retention`, а не повторяются числами: разойдись они, скрипт отвечал бы про
# сроки, которых нет.
TIMED = [
    ("reconciliation_log", "журнал сверки", "checked_at",
     retention.RECONCILIATION_KEEP),
    ("dispatch_queue", "очередь рассылки", "created_at", retention.DISPATCH_KEEP),
    ("audit_log", "журнал действий", "created_at", retention.AUDIT_KEEP),
    ("ftp_tasks", "задания 1С", "created_at", retention.FTP_TASK_KEEP),
    ("sync_anomalies", "аномалии", "detected_at", retention.ANOMALY_KEEP),
    ("test_log_entries", "журнал тестирования", "created_at",
     retention.TEST_LOG_KEEP),
    ("stock_discrepancy_log", "история расхождений", "created_at",
     retention.DISCREPANCY_LOG_KEEP),
    ("stock_delta_documents", "применённые дельты 1С", "applied_at",
     retention.DELTA_DOC_KEEP),
    ("return_items", "возвраты", "status_changed_at", retention.RETURN_KEEP),
]


def _megabytes(size: int) -> str:
    return "%.1f МБ" % (size / 1024 / 1024)


def _table(db, name: str):
    """Модель по имени таблицы — чтобы считать одним запросом, без ORM-объектов."""
    for model in Product.registry.mappers:
        if model.class_.__tablename__ == name:
            return model.class_
    return None


class TableMissing(Exception):
    """Таблицы нет В БАЗЕ (а не в моделях) — см. `_count`."""


def _count(db, model, condition=None) -> int:
    """Сколько строк. Нет таблицы в базе — НАЗВАННЫЙ отказ, а не трассировка.

    Случай не выдуманный и ровно для этого скрипта: его запускают, когда что-то
    уже не так, а база под ним бывает копией старше схемы или базой, на которую
    не накатилась миграция. Сырая `OperationalError` из середины отчёта уносит
    весь остаток — и главное число («самый быстрый приход»), и размеры, и
    оговорку самого задания, — а читается как поломка скрипта: проверку
    отложат вовсе. То же правило, по которому `check_1c_module.py` называет
    выход, когда ему подсунули `.epf`.

    Сессию после этого чиним откатом: без него SQLAlchemy не даст сделать ни
    одного следующего запроса, и отчёт всё равно оборвался бы — только уже без
    объяснения.
    """
    try:
        query = db.query(func.count(model.id))
        if condition is not None:
            query = query.filter(condition)
        return query.scalar() or 0
    except OperationalError as e:
        db.rollback()
        raise TableMissing(str(e).split("\n")[0]) from e


def _timed(db) -> None:
    print("ТАБЛИЦЫ СО СРОКОМ ХРАНЕНИЯ")
    print("  потолок чистки: %d строк на таблицу за суточный прогон"
          % retention.PER_RUN_LIMIT)
    print("")
    print("  %-26s %10s %10s %10s  %s"
          % ("таблица", "всего", "за сутки", "ст. срока", "срок"))
    day = now_utc() - timedelta(days=1)
    worst = 0
    for name, human, column, keep in TIMED:
        model = _table(db, name)
        if model is None:
            print("  %-26s  ТАБЛИЦЫ НЕТ -- скрипт отстал от схемы" % human)
            continue
        stamp = getattr(model, column)
        try:
            total = _count(db, model)
            fresh = _count(db, model, stamp >= day)
            stale = _count(db, model, stamp < now_utc() - keep)
        except TableMissing as e:
            # Называем и идём дальше: остаток отчёта нужнее одной строки.
            print("  %-26s  ТАБЛИЦЫ НЕТ В БАЗЕ -- база не на head? копия "
                  "старше схемы? (%s)" % (human, e))
            continue
        worst = max(worst, fresh)
        print("  %-26s %10d %10d %10d  %d сут."
              % (human, total, fresh, stale, keep.days))
    print("")
    # Главное число всего скрипта.
    print("  самый быстрый приход: %d строк в сутки" % worst)
    if worst == 0:
        # Ноль прихода — это не «запас бесконечный», а «мерить пока нечего»:
        # свежая установка, остановленный воркер или пустая копия базы. Сказать
        # про запас «в 0.0 раза» значило бы напечатать бессмыслицу на месте
        # главного числа.
        print("  приход за сутки нулевой -- мерить нечего: свежая установка, "
              "остановленный")
        print("  воркер или копия базы вместо живой")
    elif worst >= retention.PER_RUN_LIMIT:
        print("  ОТСТАВАНИЕ КОПИТСЯ: приход больше потолка, чистка не догонит "
              "никогда")
    else:
        print("  запас до потолка: %d строк в сутки (приход может вырасти в %.1f "
              "раза)" % (retention.PER_RUN_LIMIT - worst,
                         retention.PER_RUN_LIMIT / worst))
    print("  «старше срока» считается ПО ДАТЕ, без оглядки на статус, — то есть")
    print("  это оценка СВЕРХУ: чистка не трогает незаконченное (`pending`,")
    print("  `timeout`, `new`) никогда, сколько бы ему ни было.")


# Таблицы, которые чистятся НЕ ПО СРОКУ. Называть их «без срока хранения» было
# бы неправдой: чистятся они по другому правилу, и человек, прочитавший такой
# список буквально, пошёл бы заводить им срок.
CLEANED_OTHERWISE = {
    "return_item_log": "вместе с самим возвратом",
    "return_batch_entries": "вместе с самим возвратом",
    "stock_date_snapshots": "по числу заявок, а не по сроку",
    "stock_date_rows": "вместе со своим снимком",
    "mapping_conflicts": "исчезает, когда баркод находится",
    "platform_catalog_items": "перезаписывается каждой выгрузкой каталога",
}


def _untimed(db) -> None:
    """Таблицы, у которых срока хранения нет вовсе.

    Печатаются НЕ ради тревоги: половина из них и не должна чиститься — товары,
    баркоды, настройки, ключи — это текущее состояние, а не история. Но таблица,
    у которой нет ни срока, ни другого правила, растёт молча, и разговор о ней
    начинается с числа. `processed_orders` тут главный: строка на каждый
    принятый заказ, навсегда.
    """
    print("")
    print("ТАБЛИЦЫ БЕЗ СРОКА ХРАНЕНИЯ (самые большие)")
    timed = {name for name, _h, _c, _k in TIMED}
    rows = []
    for mapper in Product.registry.mappers:
        model = mapper.class_
        name = model.__tablename__
        if name in timed or not hasattr(model, "id"):
            continue
        try:
            rows.append((_count(db, model), name))
        except TableMissing:
            # Той же причины, что и выше, и тем же ИМЕНЕМ: прежний `except
            # Exception` глушил бы заодно и настоящие ошибки.
            rows.append((-1, name))
    for count, name in sorted(rows, reverse=True)[:12]:
        note = CLEANED_OTHERWISE.get(name)
        shown = "ТАБЛИЦЫ НЕТ" if count < 0 else "%10d" % count
        print("  %-30s %10s%s" % (name, shown, "   (%s)" % note if note else ""))


def _anomalies(db) -> None:
    print("")
    print("АНОМАЛИИ (страница их разбора)")
    open_rows = _count(db, SyncAnomaly,
                       (SyncAnomaly.status == AnomalyStatus.new)
                       & SyncAnomaly.is_test.is_(False))
    pairs = db.query(SyncAnomaly.uid_1c, SyncAnomaly.account_id,
                     SyncAnomaly.reason).filter(
        SyncAnomaly.status == AnomalyStatus.new,
        SyncAnomaly.is_test.is_(False)).distinct().count()
    print("  открытых: %d, строк на странице (сгруппировано): %d" % (open_rows, pairs))
    print("  предел показа: %d" % PAGE_LIMIT)
    if pairs > PAGE_LIMIT:
        print("  список обрезан -- страница об этом говорит, выгрузка берёт весь "
              "отбор")
    print("  закрываются сами, когда причина исчезла; в статусе `new` их не "
          "чистит никто")
    print("  намеренно -- это незаконченное дело, а не история.")


def _disk(db) -> None:
    print("")
    print("НА ДИСКЕ")
    # Версия SQLite — не любопытство. От неё зависит, можно ли писать запросы с
    # оконными функциями (нужна 3.25) и пользуется ли SQLAlchemy `RETURNING`
    # (3.35). На машине разработки она своя, и решать по ней за боевую нельзя.
    print("  SQLite: %s (оконные функции с 3.25, RETURNING с 3.35)"
          % sqlite3.sqlite_version)
    base = database_path()
    if base is None:
        print("  база не SQLite -- размеров тут не считаем")
    else:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(base) + suffix)
            if path.exists():
                total += path.stat().st_size
                print("  %-28s %s" % (path.name, _megabytes(path.stat().st_size)))
        print("  база целиком: %s" % _megabytes(total))
        print("  файл НЕ сжимается сам: удалённые чисткой страницы")
        print("  переиспользуются, но размер назад не идёт -- только `VACUUM`,")
        print("  а он требует вдвое места и остановки служб.")
    copies = backup_dir()
    if copies.exists():
        sizes = [p.stat().st_size for p in copies.glob("*.db")]
        print("  копий в %s: %d на %s"
              % (copies, len(sizes), _megabytes(sum(sizes))))


def _job(db) -> None:
    """Что сказало само задание чистки. Его число главнее нашего счёта."""
    print("")
    print("ЧТО СКАЗАЛА САМА ЧИСТКА")
    row = db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == "retention").first()
    if row is None:
        print("  задание не отрабатывало ни разу (свежая установка или воркер "
              "только что поднялся)")
        return
    print("  последний прогон: %s" % row.last_run_at)
    print("  успешно: %s" % ("да" if row.last_success else "НЕТ"))
    if row.last_error:
        print("  оговорка: %s" % row.last_error)
    else:
        print("  оговорок нет")


def main() -> int:
    db = SessionLocal()
    try:
        _timed(db)
        _untimed(db)
        _anomalies(db)
        _disk(db)
        _job(db)
    finally:
        db.close()
    print("")
    print("Пустые числа тут НЕ значат «всё хорошо»: значат «пока в пределах")
    print("срока». Смотреть надо на приход за сутки рядом с потолком.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
