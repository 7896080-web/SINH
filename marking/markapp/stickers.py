"""Стикеры коробов (ТЗ, 6.6) — генератор lamoda_stickers без изменений вёрстки.

Слово «КОМИССИЯ» или «АГЕНТ» — из той же схемы, что у УПД поставки
(`upd_service.effective_scheme`): стикеры и УПД не должны разойтись.
"""
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from lamoda_stickers import gen_stickers
from markapp.models import Supply
from markapp.timeutils import ru
from markapp.upd_service import effective_scheme

MAX_BOXES = 500


def filename(supply: Supply, boxes: int) -> str:
    return f"Маркировка_короба_Лемода_{supply.number}_{boxes}шт.xlsx"


def build(db: Session, supply: Supply, boxes: int, upd_date=None) -> tuple[bytes, str]:
    """Возвращает (xlsx, схема). Схема — по фактической дате УПД, если он
    выпущен, иначе по плановой."""
    if not 1 <= boxes <= MAX_BOXES:
        raise ValueError(f"число коробов — от 1 до {MAX_BOXES}")
    if supply.supply_date is None:
        raise ValueError("не указана дата поставки")
    scheme, _ = effective_scheme(db, supply, upd_date)
    sender = supply.organization.sticker_sender or f"Отправитель: {supply.organization.name}"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "s.xlsx"
        gen_stickers.build(boxes, supply.number, ru(supply.supply_date), out, scheme, sender=sender)
        return out.read_bytes(), scheme
