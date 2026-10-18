"""Сопоставление баркода из отчёта площадки с товаром 1С — и баркод для 1С.

1. **Товар 1С по баркоду отчёта** — по «Мэппингу» sync_admin (`syncdb.mapping`,
   только чтение): там уже применены все его правила, включая ручные привязки
   файлом. Своего мэппинга программа не ведёт — второй мэппинг однажды
   разошёлся бы с первым, и продажа ушла бы в 1С не тем товаром.
   Баркода в «Мэппинге» нет или база sync_admin недоступна — справочник
   баркодов 1С (`catalog.py`, команда BARCODE_DICT), если баркод там ровно у
   одного товара.
2. **Баркод, который уходит в 1С**, — тот, что прописан у этого товара В САМОЙ
   1С: обработка ищет номенклатуру по баркоду, и баркод площадки, которого в
   1С нет, она не найдёт. Если баркод отчёта у товара в 1С есть — он и уходит;
   иначе — баркод того же товара из 1С (наименьший, чтобы выбор не прыгал от
   отчёта к отчёту). «Прописан в 1С» — есть в справочнике BARCODE_DICT или
   пришёл в sync_admin из 1С (`syncdb.ONEC_SOURCES`).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from finapp import catalog, syncdb
from finapp.models import Barcode, Product
from finapp.timeutils import now_utc

_CHUNK = 500


@dataclass
class Resolution:
    uid_by_barcode: dict[str, str] = field(default_factory=dict)
    ambiguous: set[str] = field(default_factory=set)   # баркод у нескольких товаров справочника
    note: str = ""                                     # почему мэппинг sync_admin не прочитан


def resolve(db: Session, barcodes) -> Resolution:
    codes = sorted(set(b for b in barcodes if b))
    result = Resolution()
    try:
        sync_map = syncdb.mapping(codes)
    except syncdb.SyncDbError as e:
        sync_map = {}
        result.note = f"мэппинг Sync Admin не прочитан ({e}) — сопоставлено по справочнику 1С"
    own = catalog.owners(db, codes)
    for bc in codes:
        if bc in sync_map:
            result.uid_by_barcode[bc] = sync_map[bc][0]
        elif len(own.get(bc, ())) == 1:
            result.uid_by_barcode[bc] = next(iter(own[bc]))
        elif len(own.get(bc, ())) > 1:
            result.ambiguous.add(bc)
    _ensure_products(db, set(result.uid_by_barcode.values()))
    return result


def _ensure_products(db: Session, uids: set[str]) -> None:
    """Товар, найденный по мэппингу sync_admin, но ещё не пришедший в наш
    справочник, заводится по карточке sync_admin: на него ссылаются
    себестоимость и юнит-экономика."""
    ordered = sorted(uids)
    known: set[str] = set()
    for start in range(0, len(ordered), _CHUNK):
        known |= {u for (u,) in db.query(Product.uid_1c).filter(
            Product.uid_1c.in_(ordered[start:start + _CHUNK]))}
    missing = [u for u in ordered if u not in known]
    if not missing:
        return
    try:
        cards = syncdb.products(missing)
    except syncdb.SyncDbError:
        cards = {}
    for uid in missing:
        card = cards.get(uid, {})
        db.add(Product(uid_1c=uid, article=card.get("article", "")[:200],
                       name=card.get("name", "")[:500], size=card.get("size", "")[:100],
                       color=card.get("color", "")[:100], seen_at=now_utc()))
    db.flush()


def onec_barcodes(db: Session, uids) -> dict[str, set[str]]:
    """Товар 1С → баркоды, прописанные у него в 1С."""
    ordered = sorted(set(u for u in uids if u))
    out: dict[str, set[str]] = {}
    for start in range(0, len(ordered), _CHUNK):
        for bc, uid in db.query(Barcode.barcode, Barcode.uid_1c).filter(
                Barcode.uid_1c.in_(ordered[start:start + _CHUNK])):
            out.setdefault(uid, set()).add(bc)
    try:
        for uid, codes in syncdb.onec_barcodes(ordered).items():
            out.setdefault(uid, set()).update(codes)
    except syncdb.SyncDbError:
        pass
    return out


def barcode_for_1c(report_barcode: str, uid: str | None, known: dict[str, set[str]]) -> str | None:
    """Какой баркод положить в строку для 1С (None — у товара в 1С баркода нет)."""
    if not uid:
        return None
    codes = known.get(uid) or set()
    if report_barcode in codes:
        return report_barcode
    return min(codes) if codes else None
