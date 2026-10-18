"""Стартовая история из ведомостей ЗП / авансов (отчёт 1С «Продажи» с колонками ставки и %).

Каждый файл = полмесяца (аванс: 1–15, ЗП: 16–конец). Строки продавцов — верхний уровень
группировки: «Фамилия Имя» и «Фамилия Имя МСЛ» (вторая точка). Из файла берутся:
  * продажи — колонка B (если пусто — C);
  * ставка — колонка с заголовком «Ставка» — по ней оценивается отработанное:
    полная ставка за полмесяца = 0,5 месяца (админам всегда 0,5).
Если за месяц найден только один файл-половина, доля месяца в данных = 0,5.

Использование: awer-bot import-history <папка> [--admins "Фамилия Имя" ...]
Проверено на архиве ведомостей июль 2025 – сентябрь 2026. При дублях периода
(«ЗП май 2026» и «ЗП май 2026 полн.») берётся файл с «полн» в имени, иначе самый поздний.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import openpyxl

from ..db import DB

NAME = re.compile(r"^([А-ЯЁ][а-яё]+(?: [А-ЯЁ][а-яё]+)?)( МСЛ)?\s*$")
PERIOD = re.compile(r"(\d\d)\.(\d\d)\.(\d{4})\s*-\s*(\d\d)\.(\d\d)\.(\d{4})")
STOP = {"Итого", "Продавец", "Номенклатура", "Упаковка", "Ремень", "Джинсы", "Пиджак", "Платье", "Брюки", "Джемпер",
        "Свитшот", "Кофта", "Галстук", "Пальто", "Жилет", "Шорты", "Рубашка", "Куртка", "Костюм", "Гольф", "Свитер",
        "Водолазка", "Толстовка", "Футболка", "Пуховик", "Парка", "Бомбер", "Худи", "Поло", "Носки", "Сумка"}


def _period(rows) -> tuple[str, int] | None:
    for r in rows[:8]:
        for v in r:
            if isinstance(v, str):
                m = PERIOD.search(v)
                if m:
                    d1, mo, y = int(m.group(1)), m.group(2), m.group(3)
                    return f"{y}-{mo}", (1 if d1 == 1 else 2)
    return None


def _period_from_name(name: str) -> tuple[str, int] | None:
    months = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8,
              "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}
    low = name.lower()
    y = re.search(r"20\d\d", low)
    if not y:
        return None
    for k, v in sorted(months.items(), key=lambda kv: -len(kv[0])):
        if re.search(r"\b" + k, low):
            half = 1 if low.startswith("аванс") else 2
            return f"{y.group(0)}-{v:02d}", half
    return None


def parse_file(path: Path) -> tuple[tuple[str, int] | None, list[dict]]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    per = _period(rows) or _period_from_name(path.stem)
    cand = []
    for i, r in enumerate(rows, 1):
        a = r[0] if r else None
        if not isinstance(a, str):
            continue
        m = NAME.match(a.strip())
        if not m or m.group(1) in STOP or a.strip() in STOP:
            continue
        if not any(isinstance(v, (int, float)) for v in r[1:3]):
            continue
        cand.append((i, ws.row_dimensions[i].outline_level, m.group(1), bool(m.group(2)), r))
    if not cand:
        return per, []
    lv = min(c[1] for c in cand)
    cand = [c for c in cand if c[1] == lv]
    first = cand[0][0]
    sc = None
    for r in rows[: first - 1]:
        for j, v in enumerate(r):
            if isinstance(v, str) and v.strip().lower() == "ставка" and sc is None:
                sc = j
    out = []
    for i, _, name, msl, r in cand:
        sales = r[1] if isinstance(r[1], (int, float)) else (r[2] if isinstance(r[2], (int, float)) else 0)
        st = r[sc] if sc is not None and isinstance(r[sc], (int, float)) else None
        out.append(dict(name=name, msl=msl, sales=float(sales), stavka=st))
    return per, out


def import_history(folder: str | Path, db: DB, admins: list[str], full_half_rate: dict[str, float] | None = None,
                   skip_prefixes: tuple[str, ...] = ("Стажер",)) -> dict[str, int]:
    """full_half_rate: {'2026-07': 12500} — полная ставка за полмесяца по обеим точкам с этого месяца
    (по умолчанию 10 000 до июня 2026 и 12 500 с июля 2026 — как в ведомостях AWER)."""
    rates = full_half_rate or {"0000-00": 10000, "2026-07": 12500}
    chosen: dict[tuple[str, int], Path] = {}
    for f in sorted(Path(folder).rglob("*.xlsx")):
        if f.name.startswith("~$") or "пересчет" in f.name.lower():
            continue
        try:
            per, _ = parse_file(f)
        except Exception:  # noqa: BLE001
            continue
        if not per:
            continue
        old = chosen.get(per)
        if old is None or ("полн" in f.name.lower() and "полн" not in old.name.lower()) or (
                ("полн" in f.name.lower()) == ("полн" in old.name.lower()) and f.stat().st_mtime > old.stat().st_mtime):
            chosen[per] = f
    sales = defaultdict(float)
    fte = defaultdict(float)
    halves = defaultdict(set)
    for (month, half), f in sorted(chosen.items()):
        _, recs = parse_file(f)
        rate = [v for k, v in sorted(rates.items()) if k <= month][-1]
        per_half = defaultdict(float)
        for r in recs:
            if r["name"].startswith(skip_prefixes):
                continue
            sales[(month, r["name"])] += r["sales"]
            per_half[r["name"]] += r["stavka"] or 0
        for name, st in per_half.items():
            fte[(month, name)] += 0.5 if name in admins else 0.5 * min(1.0, st / rate)
        halves[month].add(half)
    with db.tx() as c:
        for (month, name), s in sales.items():
            sid = db.seller_id(name)
            c.execute("INSERT INTO monthly_history(month,seller_id,sales,fte,source) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(month,seller_id) DO UPDATE SET sales=excluded.sales, fte=excluded.fte",
                      (month, sid, round(s, 2), round(fte[(month, name)], 2), "ведомости ЗП"))
        for month, hs in halves.items():
            c.execute("INSERT INTO month_coverage(month,coverage) VALUES(?,?) "
                      "ON CONFLICT(month) DO UPDATE SET coverage=excluded.coverage", (month, 1.0 if len(hs) == 2 else 0.5))
        for a in admins:
            c.execute("UPDATE sellers SET role='admin' WHERE name=?", (a,))
    return {"files": len(chosen), "rows": len(sales), "months": len(halves)}
