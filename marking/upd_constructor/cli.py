import argparse, sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from . import config as C
from .calc import compute_line, compute_totals
from .lamoda_xlsx import read_supply
from .builder import build_upd
from .check import check_file


def cmd_build(a):
    sup = read_supply(a.xlsx)
    for p in sup.problems:
        print("ПРОБЛЕМА:", p, file=sys.stderr)
    if sup.problems and not a.force:
        print("Есть проблемы во входных данных — исправьте или добавьте --force", file=sys.stderr)
        return 2
    rate = Decimal(a.vat)
    lines = [compute_line(r.name, r.gtin, r.kiz, r.qty, r.price, rate, a.price_mode) for r in sup.rows]
    totals = compute_totals(lines, rate, a.totals, a.price_mode)
    today = date.today().strftime("%d.%m.%Y")
    doc_number = a.doc_number or sup.number
    if not doc_number:
        print("Нет номера документа: задайте --doc-number", file=sys.stderr); return 2
    doc_date = a.doc_date or today
    scheme = C.scheme_for(doc_date) if a.scheme == "auto" else C.SCHEMES[a.scheme]
    if a.vid_oper:
        scheme = C.Scheme(scheme.name, a.vid_oper, scheme.basis_name)
    if scheme.name != C.scheme_for(doc_date).name:
        print(f"ВНИМАНИЕ: схема «{scheme.name}» задана вручную, по дате УПД {doc_date} положена "
              f"«{C.scheme_for(doc_date).name}»", file=sys.stderr)
    id_file, data = build_upd(
        lines, totals, doc_number=doc_number, doc_date=doc_date,
        ttn_number=a.ttn_number or sup.number or doc_number, ttn_date=a.ttn_date or sup.date or doc_date,
        transfer_date=a.transfer_date or sup.date or doc_date, contract_number=a.contract_number,
        contract_date=a.contract_date, rate_label=f"{a.vat}%", scheme=scheme)
    out = Path(a.out or ".")
    out.mkdir(parents=True, exist_ok=True)
    # Имя файла = номер документа (Диадок принимает, подтверждено). ИдФайл внутри XML — по образцу эталонов.
    path = out / (f"{id_file}.xml" if a.name_by_idfile else f"{doc_number}.xml")
    path.write_bytes(data)
    print(f"Схема: {scheme.name} (ВидОпер={scheme.vid_oper!r}, основание={scheme.basis_name!r})")
    print(f"Записан {path}\n  позиций {len(lines)}, кол-во {totals.qty}, без НДС {totals.bez}, НДС {totals.nal}, с НДС {totals.uch}")
    findings = check_file(path, expected_scheme=scheme)
    for f in findings:
        if f.level != "INFO":
            print(f)
    return 1 if any(f.level == "ERROR" for f in findings) else 0


def cmd_check(a):
    rc = 0
    for p in a.files:
        print(f"== {p}")
        for f in check_file(p):
            print(" ", f)
            rc |= f.level == "ERROR"
    return int(rc)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="upd_constructor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="собрать УПД из выгрузки Lamoda «Поставки FBO» (.xlsx)")
    b.add_argument("xlsx")
    b.add_argument("-o", "--out")
    b.add_argument("--doc-number"); b.add_argument("--doc-date")
    b.add_argument("--ttn-number"); b.add_argument("--ttn-date"); b.add_argument("--transfer-date")
    b.add_argument("--contract-number", default="б/н"); b.add_argument("--contract-date")
    b.add_argument("--vat", default="5", help="ставка НДС, %% (по умолчанию 5)")
    b.add_argument("--price-mode", choices=["gross", "net"], default="gross")
    b.add_argument("--totals", choices=["rows", "reference"], default="rows")
    b.add_argument("--scheme", choices=["auto", "commission", "agency"], default="auto",
                   help="схема договора; auto — по дате УПД: по 30.09.2026 комиссия, с 01.10.2026 агентский")
    b.add_argument("--vid-oper", help="переопределить ВидОпер (с 01.10.2026 без слова «комис»)")
    b.add_argument("--name-by-idfile", action="store_true",
                   help="назвать файл по ИдФайл (ON_NSCHFDOPPR_…) вместо <номер документа>.xml")
    b.add_argument("--force", action="store_true")
    b.set_defaults(fn=cmd_build)
    c = sub.add_parser("check", help="проверить готовый УПД XML")
    c.add_argument("files", nargs="+")
    c.set_defaults(fn=cmd_check)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
