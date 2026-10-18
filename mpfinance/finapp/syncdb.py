"""База sync_admin — ТОЛЬКО ЧТЕНИЕ: мэппинг баркодов и принятые заказы.

Открывается SQLite-соединением с `mode=ro`: запись в чужую базу невозможна на
уровне самого соединения, а не по договорённости. Схему sync_admin мы не
импортируем (это другая программа со своими миграциями) — читаем простым SQL:
- `barcodes` (barcode, uid_1c, source_platform) — «Мэппинг» sync_admin: какой
  товар 1С стоит за баркодом площадки, со всеми его правилами (справочник 1С,
  ручные привязки файлом, догадки по пулу размера). Повторять эти правила здесь
  значило бы вести второй мэппинг, который однажды разойдётся с первым;
- `products` (uid_1c, article, name, size, color);
- `platform_accounts` (id, platform, name) и `processed_orders` (account_id,
  order_id, status, processed_at, cancelled_at, quantity) — сверка заказов.

Если базы нет, она занята дольше таймаута или схема разошлась — программа
честно говорит «недоступна»: сопоставление идёт по справочнику 1С, сверка
заказов не делается, остальное работает.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from finapp import config

# Статусы `OrderProcessStatus` sync_admin (SQLAlchemy Enum хранит имя).
ACCEPTED = ("processed", "confirmed")
CANCELLED = "cancelled"
# Откуда баркод в «Мэппинге» sync_admin: эти два значения — баркод взят из 1С.
ONEC_SOURCES = ("1c_export", "1c_dict")
# Заказы страницы «Тестирование» sync_admin — не настоящие продажи.
TEST_PREFIX = "TEST-"
_CHUNK = 500


class SyncDbError(RuntimeError):
    pass


@dataclass
class SyncAccount:
    id: int
    platform: str
    name: str


@dataclass
class SyncOrder:
    order_id: str
    status: str
    processed_at: datetime | None
    cancelled_at: datetime | None
    quantity: int


def path() -> Path | None:
    raw = (config.SYNC_DB_PATH or "").strip()
    return Path(raw) if raw else None


def status_text() -> str:
    p = path()
    if p is None:
        return "не задана (MPFIN_SYNC_DB) — сверка с заказами выключена"
    if not p.exists():
        return f"файла {p} нет — сверка с заказами недоступна"
    return f"{p} (только чтение)"


@contextmanager
def _connect():
    p = path()
    if p is None:
        raise SyncDbError("база sync_admin не задана (MPFIN_SYNC_DB в .env)")
    if not p.exists():
        raise SyncDbError(f"нет файла базы sync_admin {p}")
    uri = "file:" + p.resolve().as_posix() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.Error as e:
        raise SyncDbError(f"база sync_admin не открылась: {e}") from e
    try:
        yield conn
    except sqlite3.Error as e:
        raise SyncDbError(f"чтение базы sync_admin: {e}") from e
    finally:
        conn.close()


def _moment(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _naive_utc(moment: datetime) -> datetime:
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment.replace(microsecond=0)


def accounts() -> list[SyncAccount]:
    with _connect() as conn:
        rows = conn.execute("SELECT id, platform, name FROM platform_accounts ORDER BY platform, name")
        return [SyncAccount(int(i), str(p), str(n)) for i, p, n in rows]


def orders_by_ids(account_id: int, order_ids: list[str]) -> dict[str, SyncOrder]:
    out: dict[str, SyncOrder] = {}
    ids = sorted(set(order_ids))
    with _connect() as conn:
        for start in range(0, len(ids), _CHUNK):
            chunk = ids[start:start + _CHUNK]
            marks = ",".join("?" * len(chunk))
            for oid, status, processed, cancelled, qty in conn.execute(
                    "SELECT order_id, status, processed_at, cancelled_at, quantity "
                    f"FROM processed_orders WHERE account_id = ? AND order_id IN ({marks})",
                    [account_id, *chunk]):
                out[oid] = SyncOrder(oid, str(status), _moment(processed), _moment(cancelled),
                                     int(qty or 0))
    return out


def accepted_between(account_id: int, start_utc: datetime, end_utc: datetime) -> list[SyncOrder]:
    """Принятые (не отменённые) заказы кабинета с `processed_at` в [start, end)
    — наивное UTC, как хранит sync_admin. Тестовые заказы не берём."""
    # SQLAlchemy пишет DateTime в SQLite строкой «ГГГГ-ММ-ДД ЧЧ:ММ:СС[.мкс]»,
    # сравнение — строковое; границы приводим к тому же виду.
    start_utc, end_utc = (_naive_utc(m) for m in (start_utc, end_utc))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT order_id, status, processed_at, cancelled_at, quantity FROM processed_orders "
            "WHERE account_id = ? AND status IN (?, ?) AND processed_at >= ? AND processed_at < ? "
            "AND order_id NOT LIKE ? ORDER BY processed_at",
            [account_id, *ACCEPTED, start_utc.isoformat(sep=" "), end_utc.isoformat(sep=" "),
             TEST_PREFIX + "%"])
        return [SyncOrder(oid, str(st), _moment(pr), _moment(ca), int(q or 0))
                for oid, st, pr, ca, q in rows]


def mapping(barcodes) -> dict[str, tuple[str, str]]:
    """Баркод → (uid_1c, откуда привязка) по «Мэппингу» sync_admin."""
    out: dict[str, tuple[str, str]] = {}
    codes = sorted(set(b for b in barcodes if b))
    with _connect() as conn:
        for start in range(0, len(codes), _CHUNK):
            chunk = codes[start:start + _CHUNK]
            marks = ",".join("?" * len(chunk))
            for bc, uid, source in conn.execute(
                    f"SELECT barcode, uid_1c, source_platform FROM barcodes WHERE barcode IN ({marks})",
                    chunk):
                out[str(bc)] = (str(uid), str(source or ""))
    return out


def onec_barcodes(uids) -> dict[str, set[str]]:
    """Товар 1С → его баркоды, пришедшие в sync_admin ИЗ 1С (а не с площадок)."""
    out: dict[str, set[str]] = {}
    ids = sorted(set(u for u in uids if u))
    with _connect() as conn:
        for start in range(0, len(ids), _CHUNK):
            chunk = ids[start:start + _CHUNK]
            marks = ",".join("?" * len(chunk))
            for uid, bc in conn.execute(
                    f"SELECT uid_1c, barcode FROM barcodes WHERE uid_1c IN ({marks}) "
                    f"AND source_platform IN (?, ?)", [*chunk, *ONEC_SOURCES]):
                out.setdefault(str(uid), set()).add(str(bc))
    return out


def products(uids) -> dict[str, dict]:
    out: dict[str, dict] = {}
    ids = sorted(set(u for u in uids if u))
    with _connect() as conn:
        for start in range(0, len(ids), _CHUNK):
            chunk = ids[start:start + _CHUNK]
            marks = ",".join("?" * len(chunk))
            for uid, article, name, size, color in conn.execute(
                    f"SELECT uid_1c, article, name, size, color FROM products WHERE uid_1c IN ({marks})",
                    chunk):
                out[str(uid)] = {"article": article or "", "name": name or "",
                                 "size": size or "", "color": color or ""}
    return out
