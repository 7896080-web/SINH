"""Страница «Утверждение цен» — по кабинету: карточка × артикул 1С, ввод маржинальности, цена карточки,
прибыль, эталонная цена; Excel — выгрузка и загрузка той же книгой (`approval.py`)."""
import math
import urllib.parse
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from priceapp import approval, audit, reference, settings
from priceapp.database import data_version, get_db
from priceapp.deps import get_current_user, posted_form
from priceapp.flash import flash
from priceapp.models import Account, User
from priceapp.pages import render
from priceapp.platforms import PLATFORMS

router = APIRouter()
ROWS_LIMIT = 1500
_cache: dict = {}


def _accounts(db: Session) -> list[Account]:
    return list(db.query(Account).filter(Account.is_active.is_(True)).order_by(Account.platform, Account.name))


def _pick(accs, account_id: str) -> Account | None:
    if (account_id or "").isdigit():
        for a in accs:
            if a.id == int(account_id):
                return a
    return accs[0] if accs else None


def _account_from(db: Session, form) -> Account | None:
    raw = str(form.get("account_id") or "")
    return db.get(Account, int(raw)) if raw.isdigit() else None


def _fingerprint(db: Session, account: Account):
    keys = [*approval.ECON_DEFAULTS, approval.ads_key(account.id), settings.RATE_MODE, settings.RATE_MANUAL,
            settings.REF_DROP_PERCENT]
    return tuple(settings.get(db, k) for k in keys)


def computed(db: Session, account: Account) -> dict:
    """Расчёт кабинета; результат живёт, пока не сдвинулась версия данных и не поменялись параметры."""
    key = (account.id, data_version(), _fingerprint(db, account))
    hit = _cache.get(key)
    if hit is not None:
        return hit
    res = approval.compute(db, account)
    for k in [k for k in _cache if k[0] == account.id]:
        _cache.pop(k, None)
    _cache[key] = res
    return res


def _back(account_id, **params) -> RedirectResponse:
    q = urllib.parse.urlencode({"account_id": account_id, **{k: v for k, v in params.items() if v not in ("", None)}})
    return RedirectResponse(f"/approval?{q}", status_code=303)


FILTERS = {"": "все", "loss": "убыточные", "warn": "с предупреждением", "changes": "цена меняется",
           "no_econ": "нет данных по логистике", "multi": "в карточке несколько артикулов",
           "ref": "есть эталонная цена", "ref_bad": "площадка занизила (эталон)"}


def _num(raw: str):
    try:
        return float(str(raw).strip().replace(",", ".").replace(" ", "")) if str(raw).strip() else None
    except ValueError:
        return None


def _in(v, lo, hi) -> bool:
    """Значение попадает в [lo, hi]; граница не задана — не проверяется; значения нет — строка не проходит заданную границу."""
    if lo is None and hi is None:
        return True
    if v is None:
        return False
    return (lo is None or v >= lo) and (hi is None or v <= hi)


def _passes(r: dict, f: dict) -> bool:
    if f["cat"] and (r["category"] or "—") != f["cat"]:
        return False
    al = f["art"].strip().lower()
    if al and al not in r["art"].lower() and al not in str(r["wbart"]).lower() and al not in str(r["card"]):
        return False
    bc = f["barcode"].strip()
    if bc and not any(bc in b for b in r["barcodes"]):
        return False
    sz = f["size"].strip().lower()
    if sz and sz not in [x.lower() for x in r["sizes"]]:
        return False
    if not _in(float(r["sale"]) if r["sale"] else None, _num(f["pmin"]), _num(f["pmax"])):
        return False
    if not _in(r["ap"], _num(f["nmin"]), _num(f["nmax"])):
        return False
    if not _in(r["margin"], _num(f["mmin"]), _num(f["mmax"])):
        return False
    if not _in(r["ac"], _num(f["cmin"]), _num(f["cmax"])):
        return False
    flt = f["flt"]
    if flt == "loss":
        return r["apr"] is not None and r["apr"] < 0
    if flt == "warn":
        return bool(r["warn"])
    if flt == "changes":
        return bool(r["ap"] and r["sale"] and r["ap"] != r["sale"])
    if flt == "no_econ":
        return r["how"].startswith("нет данных")
    if flt == "multi":
        return r["narts"] >= 2
    if flt == "ref":
        return r["ref_price"] is not None
    if flt == "ref_bad":
        return r["ref_status"] not in ("", "в порядке")
    return True


def _totals(res: dict) -> dict:
    """Прибыль за период по всем строкам: по текущим ценам и по ценам карточек (при том же объёме)."""
    p = res["p"]
    now = new = 0.0
    for r in res["rows"]:
        if r["cost"] is None or r["k"] is None or not r["sale"]:
            continue
        unit = r["k"] - p["tax"] * r["w"] - p["ads"] - p["extra_ads"]
        base = float(r["sale"]) * unit - r["F"] - p["rent_unit"] - r["cost"]
        now += r["net"] * base
        new += r["net"] * (r["apr"] if r["apr"] is not None else base)
    return {"profit_now": now, "profit_new": new}


FILTER_KEYS = ("cat", "art", "barcode", "size", "pmin", "pmax", "nmin", "nmax", "mmin", "mmax", "cmin", "cmax", "flt")


@router.get("/approval")
def page(request: Request, account_id: str = Query(""), cat: str = Query(""), art: str = Query(""), barcode: str = Query(""),
         size: str = Query(""), pmin: str = Query(""), pmax: str = Query(""), nmin: str = Query(""), nmax: str = Query(""),
         mmin: str = Query(""), mmax: str = Query(""), cmin: str = Query(""), cmax: str = Query(""), flt: str = Query(""),
         db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    accs = _accounts(db)
    acc = _pick(accs, account_id)
    if acc is None:
        return render(request, "approval.html", user, "approval", accounts=[], acc=None)
    res = computed(db, acc)
    f = dict(cat=cat, art=art, barcode=barcode, size=size, pmin=pmin, pmax=pmax, nmin=nmin, nmax=nmax, mmin=mmin, mmax=mmax,
             cmin=cmin, cmax=cmax, flt=flt)
    out = [dict(r) for r in res["rows"] if _passes(r, f)]
    seen = set()
    for r in out:
        r["first_of_art"] = r["art"] not in seen
        seen.add(r["art"])
    qs = urllib.parse.urlencode({k: v for k, v in (("account_id", acc.id), *f.items()) if v})
    return render(request, "approval.html", user, "approval", accounts=accs, acc=acc, rows=out[:ROWS_LIMIT], total=len(out),
                  all_total=len(res["rows"]), cats=sorted({r["category"] or "—" for r in res["rows"]}), f=f,
                  filters=FILTERS, p=res["p"], rate=res["rate"], rule=res["rule"], tot=_totals(res), names=PLATFORMS,
                  drop=float(reference.drop_percent(db)), ref_enabled=reference.enabled(db), qs=qs, limit=ROWS_LIMIT)


@router.post("/approval/save")
def save(request: Request, form=Depends(posted_form), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    acc = _account_from(db, form)
    if acc is None:
        return RedirectResponse("/approval", status_code=303)
    current = {r["art"]: r["margin"] for r in computed(db, acc)["rows"]}
    values, errors = {}, []
    for k in form.keys():
        if not k.startswith("m:"):
            continue
        art = k[2:]
        raw = str(form.get(k) or "").strip().replace(",", ".").replace(" ", "")
        if raw == "":
            continue
        if raw == "-":
            if current.get(art) is not None:
                values[art] = None
            continue
        try:
            v = Decimal(raw)
        except InvalidOperation:
            errors.append(f"{art}: «{raw}» — не число")
            continue
        if current.get(art) is None or abs(float(v) - current[art]) > 1e-9:
            values[art] = v
    changed, errs = approval.set_margins(db, user, acc, values)
    errors += errs
    audit.log(db, user.username, "approval_save", acc.name, f"маржинальностей изменено: {changed}")
    db.commit()
    msg = f"{acc.name}: маржинальностей изменено — {changed}. На площадки ничего не отправлено."
    if errors:
        msg += f" Ошибок: {len(errors)} ({'; '.join(errors[:3])})."
    flash(request, msg, "warn" if errors else "ok")
    return _back(acc.id, **{k: form.get(k) for k in FILTER_KEYS})


@router.post("/approval/params")
def save_params(request: Request, form=Depends(posted_form), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    acc = _account_from(db, form)
    if acc is None:
        return RedirectResponse("/approval", status_code=303)
    errors = approval.save_params(db, acc, form)
    audit.log(db, user.username, "approval_params", acc.name,
              "параметры расчёта сохранены" + (f", ошибок {len(errors)}" if errors else ""))
    db.commit()
    if errors:
        flash(request, "Параметры сохранены не полностью: " + "; ".join(errors), "warn")
    else:
        flash(request, "Параметры расчёта сохранены.", "ok")
    return _back(acc.id)


@router.get("/approval/export")
def export(account_id: str = Query(""), platform_all: str = Query(""), db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    accs = _accounts(db)
    acc = _pick(accs, account_id)
    if acc is None:
        return RedirectResponse("/approval", status_code=303)
    chosen = [a for a in accs if a.platform == acc.platform] if platform_all == "1" else [acc]
    data = approval.build_workbook(db, chosen)
    name = "утверждение_цен_" + "_".join(approval.sheet_name(a) for a in chosen) + ".xlsx"
    disp = f"attachment; filename=\"export.xlsx\"; filename*=UTF-8''{urllib.parse.quote(name)}"
    return Response(content=data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": disp})


@router.post("/approval/import")
def import_file(request: Request, account_id: str = Query(""), file: UploadFile = File(...), db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    accs = _accounts(db)
    acc = _pick(accs, account_id)
    data = file.file.read()
    if not data:
        flash(request, "Файл пустой — выберите .xlsx, выгруженный этой страницей.", "warn")
        return _back(acc.id if acc else "")
    res = approval.import_workbook(db, user, data, accs)
    audit.log(db, user.username, "approval_import", acc.name if acc else "",
              f"листов {res['sheets']}, маржинальностей {res['changed']}, экономики строк {res['econ']}, ошибок {len(res['errors'])}")
    db.commit()
    msg = (f"Из файла: листов {res['sheets']}, маржинальностей изменено {res['changed']}, строк экономики загружено {res['econ']}"
           + (f", артикулов с разными значениями в строках {res['conflicts']} (взято большее)" if res.get("conflicts") else "")
           + ". На площадки ничего не отправлено.")
    if res["errors"]:
        msg += f" Ошибок: {len(res['errors'])} ({'; '.join(res['errors'][:3])})."
    flash(request, msg, "warn" if res["errors"] or not res["sheets"] else "ok")
    return _back(acc.id if acc else "")


@router.post("/approval/align")
def align(request: Request, form=Depends(posted_form), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Общие товары кабинетов одной площадки — одна цена продавца: по эталонному кабинету (с «Яворская» в
    названии, иначе первому), а если артикула в нём нет — по наибольшей цене из кабинетов."""
    acc = _account_from(db, form)
    if acc is None:
        return RedirectResponse("/approval", status_code=303)
    accs = [a for a in _accounts(db) if a.platform == acc.platform]
    master = next((a for a in accs if "Яворская" in a.name), accs[0])
    price: dict[str, dict] = {}
    for a in accs:
        for r in computed(db, a)["rows"]:
            if r["apa"] is None or r["margin"] is None:
                continue
            price.setdefault(r["art"], {}).setdefault(a.id, []).append(r)
    changed = 0
    for art, per in price.items():
        if len(per) < 2:
            continue
        target = max(x["apa"] for x in per[master.id]) if master.id in per else max(x["apa"] for xs in per.values() for x in xs)
        for a in accs:
            if a.id not in per or (a.id == master.id):
                continue
            row = max(per[a.id], key=lambda x: x["costmax"] or 0)
            m = math.floor(target * (1 - row["tar"]) / row["costmax"] * 10000) / 10000
            ch, _ = approval.set_margins(db, user, a, {art: Decimal(str(m))})
            changed += ch
    audit.log(db, user.username, "approval_align", acc.platform, f"выровнено общих товаров по {master.name}: изменено наценок {changed}")
    db.commit()
    flash(request, f"Общие товары выровнены по «{master.name}»: изменено наценок — {changed}. На площадки ничего не отправлено.", "ok")
    return _back(acc.id)
