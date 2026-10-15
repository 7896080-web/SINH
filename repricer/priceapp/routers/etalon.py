"""Страница «Эталонные цены»: эталон WB по артикулу и что держит каждая площадка; смена эталона
одного артикула или файлом; настройки следования (`priceapp/etalon.py`)."""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from priceapp import audit, etalon, settings
from priceapp.database import get_db
from priceapp.deps import get_current_user, posted_form
from priceapp.excel import ExcelReadError, read_xlsx_rows, xlsx_response
from priceapp.flash import flash
from priceapp.models import User
from priceapp.pages import render
from priceapp.platforms import PLATFORMS

router = APIRouter()
ROWS_LIMIT = 300
IMPORT_LIMIT = 500
HEAD = ["Артикул 1С", "Название", "Эталон сейчас, ₽ (цена после скидки продавца)", "Новый эталон, ₽"]


def _int(raw) -> int | None:
    try:
        v = int(Decimal(str(raw).replace(" ", "").replace(",", ".")))
    except Exception:
        return None
    return v if v > 0 else None


def _back(**params) -> RedirectResponse:
    from urllib.parse import urlencode
    q = urlencode({k: v for k, v in params.items() if v})
    return RedirectResponse("/etalon" + (f"?{q}" if q else ""), status_code=303)


def _rows(db: Session, q: str, flt: str):
    ets = etalon.etalons(db)
    plan = etalon.follower_plan(db, ets)
    by_art: dict[str, dict[str, list]] = {}
    for c in plan:
        for art in c.articles:
            by_art.setdefault(art, {}).setdefault(c.platform, []).append(c)
    followers = [p for p in etalon.FOLLOWERS if etalon.shift(db, p) is not None]
    lim = etalon.max_step(db)
    counts = {p: {"diff": 0, "big": 0, "skip": 0, "same": 0} for p in followers}
    for c in plan:
        k = counts[c.platform]
        if c.skip:
            k["skip"] += 1
        elif c.follows:
            k["same"] += 1
        else:
            k["diff"] += 1
            if c.change_pct is not None and abs(c.change_pct) > lim:
                k["big"] += 1
    rows = []
    tol = etalon.tolerance(db)
    from priceapp.reference import below_floor_articles
    low = below_floor_articles(db) if flt == "belowfloor" else set()
    ql = q.strip().lower()
    for art in sorted(ets):
        et = ets[art]
        if not et.cards:
            continue
        if ql and ql not in art.lower() and ql not in (et.name or "").lower():
            continue
        fol = by_art.get(art, {})
        diff = any(not c.skip and not c.follows for cs in fol.values() for c in cs) or any(
            c.ref_sale and c.cur_sale and abs(c.cur_sale - c.ref_sale) > tol for c in et.cards)
        if flt == "diff" and not diff:
            continue
        if flt == "noetalon" and et.sale:
            continue
        if flt == "skip" and not any(c.skip for cs in fol.values() for c in cs):
            continue
        if flt == "belowfloor" and art not in low:
            continue
        rows.append({"et": et, "fol": fol, "diff": diff})
    return ets, rows, followers, counts


@router.get("/etalon")
def page(request: Request, q: str = "", flt: str = "", art: str = "", new: str = "",
         db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ets, rows, followers, counts = _rows(db, q, flt)
    preview = None
    sale = _int(new)
    if art and sale and art in ets:
        et = ets[art]
        hyp = dict(ets)
        hyp[art] = etalon.Etalon(art, sale, et.cards, et.name)
        preview = {"art": art, "sale": sale, "et": et, "wb": etalon.wb_plan(db, et, sale),
                   "fol": etalon.follower_plan(db, hyp, only={art})}
    elif art and new:
        flash(request, "Новый эталон — целое число рублей больше нуля.", "warn")
    lo, hi = etalon.corridor(db)
    return render(request, "etalon.html", user, "etalon", rows=rows[:ROWS_LIMIT], total=len(rows),
                  limit=ROWS_LIMIT, q=q, flt=flt, followers=followers, counts=counts, names=PLATFORMS,
                  shifts={p: settings.get(db, etalon.SHIFT_KEYS[p]) for p in etalon.FOLLOWERS},
                  lo=lo, hi=hi, auto=etalon.auto_enabled(db), tol=etalon.tolerance(db),
                  poll_at=settings.get(db, settings.WB_POLL_AT), wb_poll=_last_poll(db), max_step=etalon.max_step(db),
                  sale_days=settings.get(db, settings.ETALON_LAMODA_SALE_DAYS), preview=preview,
                  with_etalon=sum(1 for e in ets.values() if e.sale))


@router.post("/etalon/settings")
def save_settings(request: Request, form=Depends(posted_form), db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    changes = []
    for p in etalon.FOLLOWERS:
        raw = str(form.get(f"shift_{p}") or "").strip().replace(",", ".")
        if raw and (etalon._dec(raw) is None or not Decimal(0) <= etalon._dec(raw) < Decimal(100)):
            flash(request, f"{PLATFORMS[p]}: сдвиг — число от 0 до 99 или пусто.", "warn")
            return _back()
        if raw != settings.get(db, etalon.SHIFT_KEYS[p]):
            changes.append(f"{PLATFORMS[p]}: {settings.get(db, etalon.SHIFT_KEYS[p]) or '—'} -> {raw or '—'}")
            settings.put(db, etalon.SHIFT_KEYS[p], raw)
    lo, hi = _int(form.get("lo")), _int(form.get("hi"))
    if not lo or not hi or not 0 < lo <= hi < 100:
        flash(request, "Коридор скидки: от и до — целые проценты, «от» не больше «до».", "warn")
        return _back()
    for key, v in ((settings.ETALON_DISC_MIN, lo), (settings.ETALON_DISC_MAX, hi)):
        if str(v) != settings.get(db, key):
            changes.append(f"{key}: {settings.get(db, key)} -> {v}")
            settings.put(db, key, str(v))
    tol = str(form.get("tol") or "").strip()
    if tol and _int(tol) is None and tol != "0":
        flash(request, "Допуск — целое число рублей.", "warn")
        return _back()
    if tol and tol != settings.get(db, settings.ETALON_TOLERANCE_RUB):
        changes.append(f"допуск: {settings.get(db, settings.ETALON_TOLERANCE_RUB)} -> {tol} ₽")
        settings.put(db, settings.ETALON_TOLERANCE_RUB, tol)
    poll_at = str(form.get("poll_at") or "").strip()
    if poll_at != settings.get(db, settings.WB_POLL_AT):
        from priceapp.workers.jobs import parse_poll_at
        if poll_at and not parse_poll_at(poll_at):
            flash(request, "Время опроса WB — «чч:мм», несколько через запятую (например 00:05).", "warn")
            return _back()
        changes.append(f"опрос WB: {settings.get(db, settings.WB_POLL_AT) or '—'} -> {poll_at or '—'}")
        settings.put(db, settings.WB_POLL_AT, poll_at)
    auto = "1" if form.get("auto") == "1" else "0"
    if auto != settings.get(db, settings.ETALON_FOLLOW_AUTO):
        changes.append(f"автоследование: {'включено' if auto == '1' else 'выключено'}")
        settings.put(db, settings.ETALON_FOLLOW_AUTO, auto)
    if changes:
        audit.log(db, user.username, "etalon_settings", "", "; ".join(changes))
    db.commit()
    flash(request, "Сохранено: " + "; ".join(changes) if changes else "Ничего не изменилось.", "ok")
    return _back()


def _last_poll(db: Session) -> str:
    from priceapp.models import WorkerHeartbeat
    hb = db.get(WorkerHeartbeat, "wb_poll")
    at = hb.last_run_at if hb else None
    if not at:
        return ""
    from datetime import timezone
    return at.replace(tzinfo=timezone.utc).astimezone().strftime("%d.%m %H:%M")


@router.post("/etalon/poll-now")
def poll_now(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Опросить WB вне расписания: свежие цены, возврат эталона, следование площадок."""
    settings.put(db, settings.REF_POLL_NOW, "1")
    audit.log(db, user.username, "wb_poll_now", "", "опрос WB по кнопке")
    db.commit()
    flash(request, "Опрос WB начнётся в течение минуты: загрузятся свежие цены, где площадка или акция занизила "
                   "цену больше допустимого — вернётся эталон. Обновите страницу через 2–5 минут.", "ok")
    return _back()


@router.post("/etalon/set")
def set_one(request: Request, form=Depends(posted_form), db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    art, sale = str(form.get("art") or ""), _int(form.get("sale"))
    if not art or not sale:
        flash(request, "Не указан артикул или новый эталон.", "warn")
        return _back()
    report = etalon.set_etalon(db, art, sale, user.username)
    failed = any("не записан" in r.lower() for r in report)
    flash(request, (f"Эталон {art} НЕ изменён. " if failed else f"Эталон {art} -> {sale} ₽. ") + " · ".join(report),
          "warn" if failed or any("не отправлено" in r for r in report) else "ok")
    return _back(q=art)


@router.post("/etalon/follow-now")
def follow_now(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Привести все площадки к эталону — включая большой шаг (решение человека)."""
    cards = [c for c in etalon.follower_plan(db) if not c.skip and not c.follows]
    if not cards:
        flash(request, "Все площадки уже держат эталон — отправлять нечего.", "ok")
        return _back()
    res = etalon.send_followers(db, cards, actor=user.username)
    flash(request, "Приведено к эталону: " + "; ".join(f"{k}: {v}" for k, v in res.items()), "ok")
    return _back()


@router.get("/etalon/export")
def export(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ets = etalon.etalons(db)
    rows = [[art, e.name, e.sale, None] for art, e in sorted(ets.items()) if e.cards]
    return xlsx_response(HEAD, rows, "эталонные_цены.xlsx")


@router.post("/etalon/import")
def import_file(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    # обычный def: синхронная работа с базой в async-обработчике заморозила бы интерфейс
    try:
        data = read_xlsx_rows(file.file.read())
    except ExcelReadError as e:
        flash(request, str(e), "error")
        return _back()
    if data and HEAD[0] not in data[0]:
        flash(request, f"В файле нет колонки «{HEAD[0]}» — выгрузите файл этой страницей («Выгрузить в Excel»).", "error")
        return _back()
    ets = etalon.etalons(db)
    lim = float(etalon.max_step(db))
    todo, bad = [], []
    seen: set[str] = set()
    for r in data:
        art, new = str(r.get(HEAD[0]) or "").strip(), r.get(HEAD[3])
        if not art or new in (None, ""):
            continue                                  # пусто — без изменений
        sale = _int(new)
        if art in seen:
            bad.append(f"{art}: повтор строки — применена первая")
            continue
        seen.add(art)
        if sale is None:
            bad.append(f"{art}: «{new}» не число")
        elif art not in ets or not ets[art].cards:
            bad.append(f"{art}: на WB нет карточек")
        elif ets[art].sale and abs(Decimal(sale) / Decimal(ets[art].sale) - 1) * 100 > lim:
            # Опечатка ×10 в файле уходила бы сразу на WB и все площадки. Крупное — по одному на
            # странице, с предпросмотром того, что уйдёт (аудит 14.10).
            bad.append(f"{art}: {ets[art].sale} -> {sale} ₽ — изменение больше {lim:g}%, задайте на странице")
        elif sale != ets[art].sale:
            todo.append((art, sale))
    if len(todo) > IMPORT_LIMIT:
        flash(request, f"В файле {len(todo)} новых эталонов — больше {IMPORT_LIMIT} за раз. Разбейте файл.", "warn")
        return _back()
    done, refused = 0, []
    for art, sale in todo:
        rep = etalon.set_etalon(db, art, sale, user.username, snapshot=False)
        if any("не записан" in r.lower() for r in rep):
            refused.append(next(r for r in rep if "не записан" in r.lower()))
        else:
            done += 1
    from priceapp.reference import snapshot_if_dirty
    snapshot_if_dirty(db, f"эталон из файла: {done} артикулов")
    msg = f"Из файла: новый эталон у {done} артикулов, отправлено на WB и площадки-последователи."
    if refused or bad:
        msg += " Не применено: " + "; ".join((refused + bad)[:5]) + (" …" if len(refused) + len(bad) > 5 else "")
    flash(request, msg, "warn" if refused or bad else "ok")
    return _back()
