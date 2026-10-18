"""Имена продавцов в 1С, ведомостях и боте: похожие записи — вероятно, один человек (другой порядок слов,
отчество, опечатка). Это только подсказка: объединяет владелец кнопкой на странице «Имена в 1С» (db.merge_sellers).
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from .db import DB

SIMILAR = 0.88  # «Абдулинанова Эльвина» ~ «Абдулиннанова Эльвина», но не «Петрова Ольга» ~ «Петрова Олеся»


def _words(name: str) -> set[str]:
    return {w.lower().replace("ё", "е") for w in name.split()}


def similar(a: str, b: str) -> bool:
    if re.sub(r"\D", "", a) != re.sub(r"\D", "", b):
        return False  # «Продавец01» и «Продавец02» — разные
    wa, wb = _words(a), _words(b)
    if len(wa & wb) >= 2 and (wa <= wb or wb <= wa):  # порядок слов, отчество
        return True
    return SequenceMatcher(None, " ".join(sorted(wa)), " ".join(sorted(wb))).ratio() >= SIMILAR


def _pair_key(a: int, b: int) -> str:
    return f"{min(a, b)}-{max(a, b)}"


def main_of(db: DB, a, b) -> tuple:
    """(основной, второй): основной — у кого последние продажи в 1С (его имя — как в 1С сейчас), затем — кто
    подключён к боту, затем — кто раньше заведён."""
    def rank(s):
        last = db.one("SELECT MAX(day) d FROM sales_lines WHERE seller_id=?", s["id"])["d"] or ""
        return (last, s["max_user_id"] is not None, -s["id"])
    return (a, b) if rank(a) >= rank(b) else (b, a)


def suspects(db: DB) -> list[tuple]:
    """[(основной, второй)] — похожие имена, кроме пар, отмеченных «это разные люди»."""
    rows = db.sellers(active_only=False)
    skip = set((db.get_kv("names_not_dup") or "").split(","))
    out = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if _pair_key(a["id"], b["id"]) not in skip and similar(a["name"], b["name"]):
                out.append(main_of(db, a, b))
    return out


def not_duplicate(db: DB, a: int, b: int) -> None:
    skip = [k for k in (db.get_kv("names_not_dup") or "").split(",") if k]
    db.set_kv("names_not_dup", ",".join(skip + [_pair_key(a, b)]))
