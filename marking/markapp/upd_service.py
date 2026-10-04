"""УПД из выгрузки Lamoda «Поставки FBO» (ТЗ, 6.4–6.5 и разд. 8).

Сборка — ядро upd-constructor без изменений (`upd_constructor`): принятые
Lamoda УПД собирались ровно так, и сумма сходится с «Итого стоимость». Эта
обёртка добавляет то, чего у конструктора не было:
- сверку выгрузки с поставкой: те же артикулы, количества, цены и штрихкоды;
- реквизиты продавца из справочника организаций, а не из констант;
- схему договора, одну на поставку, с датой перехода из настроек.
"""
from __future__ import annotations

import io
import re
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from markapp import settings
from markapp.models import Organization, Supply
from markapp.timeutils import parse_ru, ru
from upd_constructor import config as C
from upd_constructor.builder import build_upd
from upd_constructor.calc import compute_line, compute_totals
from upd_constructor.check import check_file
from upd_constructor.lamoda_xlsx import read_supply

SCHEME_LABELS = {"auto": "авто — по дате УПД", "commission": "комиссия", "agency": "агентский договор"}


# --- Схема договора -------------------------------------------------------------

def agency_from(db: Session) -> date:
    return parse_ru(settings.get(db, settings.AGENCY_FROM))


def scheme_by_date(db: Session, d: date) -> str:
    """Правило Lamoda: документы по 30.09.2026 включительно — по-старому."""
    return "agency" if d >= agency_from(db) else "commission"


def effective_scheme(db: Session, supply: Supply, upd_date: date | None) -> tuple[str, str]:
    """(схема, предупреждение). Одна схема на поставку — и для УПД, и для стикеров.

    Стикеры нужны раньше УПД, поэтому до выпуска УПД считаем по плановой дате.
    Ручной выбор разрешён, но расхождение с датой называется вслух.
    """
    d = upd_date or supply.planned_upd_date or supply.supply_date
    by_date = scheme_by_date(db, d) if d else "commission"
    if supply.scheme_choice in ("commission", "agency"):
        chosen = supply.scheme_choice
        warn = "" if chosen == by_date else (
            f"схема «{SCHEME_LABELS[chosen]}» выбрана вручную, по дате {ru(d)} положена "
            f"«{SCHEME_LABELS[by_date]}»")
        return chosen, warn
    return by_date, ""


# --- Выгрузка «Поставки FBO» ----------------------------------------------------

@dataclass
class FboCheck:
    number: str | None = None
    date: str | None = None
    rows: int = 0
    units: Decimal = Decimal(0)
    total: Decimal = Decimal(0)
    problems: list[str] = field(default_factory=list)   # мешают выпуску УПД
    notes: list[str] = field(default_factory=list)      # стоит знать

    @property
    def ok(self) -> bool:
        return not self.problems


def read_fbo(data: bytes):
    return read_supply(io.BytesIO(data))


def check_fbo_against_supply(data: bytes, supply: Supply, db=None) -> FboCheck:
    """Выгрузка должна описывать ЭТУ поставку: иначе УПД уйдёт не с теми
    строками, а Lamoda отвергнет его по связке «артикул + код».

    Сверяются не только итоги по артикулу, но и КОДЫ: перепутанные между
    размерами коды дают те же итоги, но неверный УПД и — через пары из
    выгрузки — неверный справочник GTIN, по которому потом заказываются коды.
    """
    fbo = read_fbo(data)
    res = FboCheck(number=fbo.number, date=fbo.date, rows=len(fbo.rows))
    res.problems.extend(fbo.problems)
    if fbo.number and fbo.number != supply.number:
        res.problems.append(f"в выгрузке поставка {fbo.number}, а это поставка {supply.number}")
    fbo_by_sku: dict[str, dict] = defaultdict(lambda: {"qty": Decimal(0), "prices": set(), "eans": set()})
    for r in fbo.rows:
        rec = fbo_by_sku[r.name.strip()]
        rec["qty"] += r.qty
        if r.price is not None:
            rec["prices"].add(r.price.quantize(Decimal("0.01")))
            res.total += r.price * r.qty
        rec["eans"].add(r.gtin)
        res.units += r.qty
    ours: dict[str, dict] = defaultdict(lambda: {"qty": 0, "price": None, "ean": ""})
    for row in supply.rows:
        rec = ours[row.supplier_sku]
        rec["qty"] += row.qty
        rec["price"] = row.price
        rec["ean"] = row.ean
    diffs = []
    for sku in sorted(set(ours) | set(fbo_by_sku)):
        a, b = ours.get(sku), fbo_by_sku.get(sku)
        if a is None:
            diffs.append(f"{sku}: есть в выгрузке, нет в поставке")
            continue
        if b is None:
            diffs.append(f"{sku}: есть в поставке, нет в выгрузке")
            continue
        if Decimal(a["qty"]) != b["qty"]:
            diffs.append(f"{sku}: в поставке {a['qty']} шт, в выгрузке {b['qty']}")
        if a["price"] is not None and b["prices"] and b["prices"] != {a["price"]}:
            diffs.append(f"{sku}: цена в поставке {a['price']}, в выгрузке "
                         + ", ".join(str(p) for p in sorted(b["prices"])))
        if a["ean"] and b["eans"] != {a["ean"]}:
            diffs.append(f"{sku}: EAN в поставке {a['ean']}, в выгрузке " + ", ".join(sorted(b["eans"])))
    diffs.extend(_kiz_problems(fbo.rows, supply, db))
    if diffs:
        res.problems.append(f"выгрузка расходится с поставкой ({len(diffs)}): " + "; ".join(diffs[:5]))
        res._diffs = diffs  # полный список — для страницы
    return res


SHORT_KI = re.compile(r"^01(\d{14})21[\x21-\x7e]{13}$")


def _kiz_problems(rows, supply: Supply, db) -> list[str]:
    """Коды выгрузки: формат короткого КИ (ТЗ 7.4), один GTIN на артикул,
    согласие со справочником GTIN и — если коды заказаны программой — что
    каждый код закреплён за этой поставкой и этим артикулом."""
    from markapp.gtin import check_digit_ok
    out = []
    gtins: dict[str, set] = defaultdict(set)
    for n, r in enumerate(rows, 1):
        m = SHORT_KI.match(r.kiz or "")
        if not m or not check_digit_ok(m.group(1)):
            out.append(f"строка {n} ({r.name.strip()}): код «{(r.kiz or '')[:40]}» — не короткий КИ")
            continue
        gtins[r.name.strip()].add(m.group(1))
    for sku, gs in sorted(gtins.items()):
        if len(gs) > 1:
            out.append(f"{sku}: в выгрузке коды разных GTIN ({', '.join(sorted(gs))}) — коды перепутаны")
    if db is None:
        return out
    from markapp.models import GtinPair, MarkCode
    by_sku = {p.supplier_sku: p.gtin for p in db.query(GtinPair).all()}
    by_gtin = {g: s for s, g in by_sku.items()}
    for sku, gs in sorted(gtins.items()):
        for g in gs:
            if sku in by_sku and by_sku[sku] != g:
                out.append(f"{sku}: в выгрузке GTIN {g}, в справочнике {by_sku[sku]}")
            elif by_gtin.get(g, sku) != sku:
                out.append(f"{sku}: GTIN {g} в справочнике у артикула «{by_gtin[g]}»")
    ours = {c.cis: c.supplier_sku for c in db.query(MarkCode).filter(MarkCode.supply_id == supply.id).all()}
    if ours:
        for r in rows:
            sku = ours.get((r.kiz or "").strip())
            if sku is None:
                out.append(f"{r.name.strip()}: код {(r.kiz or '')[:31]} не закреплён за этой поставкой")
            elif sku != r.name.strip():
                out.append(f"{r.name.strip()}: код {r.kiz[:31]} заказан для «{sku}»")
    return out


# --- Сборка УПД ---------------------------------------------------------------

def seller_of(org: Organization) -> C.SellerIP:
    return C.SellerIP(surname=org.surname, firstname=org.firstname, patronymic=org.patronymic,
                      inn=org.inn, ogrnip=org.ogrnip, address=org.address, signer_role=org.signer_role)


@dataclass
class BuiltUpd:
    id_file: str
    xml: bytes
    scheme: str
    scheme_warning: str
    positions: int
    total_with_vat: Decimal
    findings: list


def build_for_supply(db: Session, supply: Supply, fbo_data: bytes, doc_date: date,
                     totals_mode: str = "rows") -> BuiltUpd:
    fbo = read_fbo(fbo_data)
    org = supply.organization
    rate = Decimal(org.vat_rate)
    lines = [compute_line(r.name, r.gtin, r.kiz, r.qty, r.price, rate, "gross") for r in fbo.rows]
    totals = compute_totals(lines, rate, totals_mode, "gross")
    scheme_name, warning = effective_scheme(db, supply, doc_date)
    scheme = C.SCHEMES[scheme_name]
    doc_date_s = ru(doc_date)
    supply_date_s = ru(supply.supply_date) or fbo.date or doc_date_s
    edo = C.EdoIds(receiver=C.EDO.receiver, sender=org.edo_sender_id or C.EDO.sender,
                   soft_name=C.EDO.soft_name)
    from upd_constructor.builder import make_id_file
    id_file = make_id_file(doc_date_s, edo)
    id_file, xml = build_upd(
        lines, totals, doc_number=supply.doc_number, doc_date=doc_date_s,
        ttn_number=supply.number, ttn_date=supply_date_s, transfer_date=supply_date_s,
        contract_number=org.contract_number or "б/н", rate_label=f"{org.vat_rate}%",
        seller=seller_of(org), id_file=id_file, scheme=scheme)
    findings = run_check(xml, scheme)
    return BuiltUpd(id_file=id_file, xml=xml, scheme=scheme_name, scheme_warning=warning,
                    positions=len(lines), total_with_vat=totals.uch, findings=findings)


def run_check(xml: bytes, scheme: C.Scheme | None = None) -> list:
    """Проверка конструктора работает с файлом — даём ей временный."""
    fd, path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(xml)
        return check_file(path, expected_scheme=scheme)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def findings_text(findings) -> str:
    return "\n".join(str(f) for f in findings if f.level != "INFO")


def has_errors(findings) -> bool:
    return any(f.level == "ERROR" for f in findings)
