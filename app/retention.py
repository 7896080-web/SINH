"""Сроки хранения истории: что удалять и почему именно это.

Аудит 21.09: прунинг в системе был ровно один — снимков остатков на дату. Всё
остальное росло неограниченно. `reconciliation_log` — 276 856 строк и около
сорока тысяч в сутки, то есть порядка четырнадцати миллионов за год.

Следствие не «кончится диск» — диска хватит. Следствие в том, что **час сверки
становится длиннее с каждым месяцем**, бэкап и `VACUUM` тяжелеют, а журнал, в
котором четырнадцать миллионов строк, перестаёт быть журналом: найти в нём
разбор конкретного дня уже нечем.

Три правила, каждое выстрадано:

**Удаляем только то, что описывает ПРОШЛОЕ состояние.** Строка сверки говорит,
каким остаток был в тот час; сегодняшний остаток лежит в `products`. Запись
очереди в терминальном статусе говорит, чем кончилась отправка, которая давно
кончилась. А вот запись в `pending` — это НЕ история, это незаконченное дело, и
её нельзя трогать никогда, сколько бы ей ни было лет.

**Порциями.** Одно `DELETE` на миллион строк — это длинная транзакция, то есть
ровно та беда, от которой мы только что вылечили сверку: на всё её время писать
в базу не может никто.

**Никогда не трогаем то, что ещё в работе.** Аномалия в статусе `new`, задание
1С в `pending`/`sent`/`timeout`, запись очереди в `pending` — всё это ждёт
человека или ответа, и «оно старое» не значит «оно не нужно».
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import (AnomalyStatus, AuditLog, DispatchQueueItem, DispatchStatus,
                        FtpTask, FtpTaskStatus, ReconciliationLog, SyncAnomaly,
                        TestLogEntry)
from app.timeutils import now_utc

logger = logging.getLogger("sync_worker")

# Тот же умолчательный путь, что у `scheduler.build_exchange`. Строкой, а не
# импортом: `retention` не должен тянуть за собой воркеры.
DEFAULT_ARCHIVE_DIR = r"C:\sync\archive"

# Сколько хранить. Числа не круглые ради красоты, а под конкретный разбор.
#
# Сверка — 90 суток: это квартал, дальше по ней не разбирают ничего, а объём у
# неё самый большой в системе.
RECONCILIATION_KEEP = timedelta(days=90)
# Очередь рассылки в терминальных статусах — 30 суток. Отчёт смотрит на
# последнюю запись пары и на сутки назад; месяц — с запасом на «вернёмся к
# этому после отпуска».
DISPATCH_KEEP = timedelta(days=30)
# Журнал действий человека — год. Это единственное место, где written кто и
# когда включил трансляцию или переподвязал баркод, и год — разумный минимум
# для вопроса «а кто это сделал».
AUDIT_KEEP = timedelta(days=365)
# Закрытые задания 1С — 180 суток. По ним сверяют, был ли документ, и полгода
# покрывает любой реальный разбор с бухгалтерией.
FTP_TASK_KEEP = timedelta(days=180)
# Разобранные аномалии — 90 суток.
ANOMALY_KEEP = timedelta(days=90)
# Журнал страницы «Тестирование» — 30 суток. Это отладочные записи.
TEST_LOG_KEEP = timedelta(days=30)

# Файлы обмена с 1С в каталоге архива — 60 суток. Это ЕДИНСТВЕННОЕ место, где
# чистка трогает диск, и до аудита 22.09 архив не чистил никто: `archive_result`
# делает `os.replace` в `dir_archive` и всё. Туда же ложится суточный
# `barcodes_*.txt` — на бою 154 232 строки, — и просят его ещё и через сорок
# секунд после КАЖДОГО старта воркера.
#
# Следствие отложенное и потому особенно неприятное: копии базы лежат на том же
# диске, и когда он кончится, разом откажут запись в SQLite, снятие копии и
# публикация файлов для 1С — последний рубеж исчезнет ровно тогда, когда он
# нужен. Шестьдесят суток — с запасом на любой разбор «что именно мы отправили
# в 1С в тот день».
EXCHANGE_ARCHIVE_KEEP = timedelta(days=60)

# По сколько строк за раз. См. правило про порции в заголовке модуля.
CHUNK = 500
# Сколько порций за один прогон. Потолок нужен на первый запуск, когда накопились
# миллионы: чистить их одним заходом значит занять базу надолго, а спешить некуда —
# задание ходит каждые сутки и дочистит.
MAX_CHUNKS = 200


def _purge(db: Session, model, condition, label: str) -> int:
    """Удалить порциями, коммитя каждую. Возвращает сколько удалено.

    Выбираем id отдельным запросом, а не `query.delete()` на всю выборку: так
    транзакция ограничена размером порции, а не количеством подходящих строк.
    """
    removed = 0
    for _ in range(MAX_CHUNKS):
        ids = [row[0] for row in
               db.query(model.id).filter(condition).limit(CHUNK).all()]
        if not ids:
            break
        db.query(model).filter(model.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        removed += len(ids)
    if removed:
        logger.info("хранение: %s — удалено %d", label, removed)
    return removed


def apply_retention(db: Session) -> dict:
    """Почистить историю по срокам. Возвращает {таблица: сколько удалено}."""
    now = now_utc()
    stats: dict[str, int] = {}

    stats["reconciliation_log"] = _purge(
        db, ReconciliationLog,
        ReconciliationLog.checked_at < now - RECONCILIATION_KEEP,
        "журнал сверки")

    # ТОЛЬКО `sent`. Запись в `pending` — незаконченная отправка: остаток у нас
    # уже списан, а на площадку не уехал; удалить её значит потерять единственный
    # след того, что площадка продаёт по старому числу. Сколько бы лет ей ни было.
    #
    # `error` тоже НЕ трогаем, и это правка аудита 21.09. Она выглядит
    # терминальной — попытки исчерпаны, рассылка её больше не возьмёт, — но
    # описывает она не прошлое, а НЕЗАКОНЧЕННОЕ ДЕЛО: остаток списан, число не
    # уехало, площадка продаёт то, чего нет. Через тридцать суток критичная
    # находка «Рассылка не доехала до площадки» просто исчезала из отчёта, хотя
    # расхождение никуда не девалось. Мёртвые отказы отсеивает сам отчёт
    # (`report._only_live_pairs` и «по паре позже была успешная отправка») — это
    # его работа, а не чистки.
    stats["dispatch_queue"] = _purge(
        db, DispatchQueueItem,
        (DispatchQueueItem.created_at < now - DISPATCH_KEEP)
        & (DispatchQueueItem.status == DispatchStatus.sent),
        "очередь рассылки")

    stats["audit_log"] = _purge(
        db, AuditLog, AuditLog.created_at < now - AUDIT_KEEP, "журнал действий")

    # `timeout` не трогаем НИКОГДА, сколько бы ему ни было: по нему неизвестно,
    # создан документ в 1С или нет, и его количество до сих пор считается «в
    # пути», то есть влияет на остаток прямо сейчас. Удалить такую строку значит
    # молча изменить остаток товара.
    stats["ftp_tasks"] = _purge(
        db, FtpTask,
        (FtpTask.created_at < now - FTP_TASK_KEEP)
        & FtpTask.status.in_([FtpTaskStatus.done, FtpTaskStatus.no_document]),
        "задания 1С")

    stats["sync_anomalies"] = _purge(
        db, SyncAnomaly,
        (SyncAnomaly.detected_at < now - ANOMALY_KEEP)
        & (SyncAnomaly.status == AnomalyStatus.resolved),
        "разобранные аномалии")

    stats["test_log"] = _purge(
        db, TestLogEntry, TestLogEntry.created_at < now - TEST_LOG_KEEP,
        "журнал тестирования")

    stats["exchange_archive"] = prune_exchange_archive()

    return stats


def prune_exchange_archive(directory: str | None = None,
                           keep: timedelta = EXCHANGE_ARCHIVE_KEEP) -> int:
    """Удалить старые файлы из каталога архива обмена с 1С.

    Только ФАЙЛЫ и только по возрасту: каталоги не трогаем вовсе, а ошибку на
    отдельном файле проглатываем и идём дальше — файл мог быть занят, и ронять
    из-за него всю чистку незачем. Каталога нет (Linux-разработка, где обмена
    не бывает) — тихо выходим.

    Возраст берём по времени изменения файла, а не по имени: имена у четырёх
    каналов разные, и разбирать каждое значило бы завести пятый способ ошибиться.
    """
    path = Path(directory or os.environ.get("SYNC_DIR_ARCHIVE", DEFAULT_ARCHIVE_DIR))
    if not path.is_dir():
        return 0
    cutoff = (now_utc() - keep).timestamp()
    removed = 0
    for entry in path.iterdir():
        try:
            if not entry.is_file() or entry.stat().st_mtime >= cutoff:
                continue
            entry.unlink()
            removed += 1
        except OSError:
            continue
    if removed:
        logger.info("хранение: архив обмена — удалено файлов %d", removed)
    return removed
