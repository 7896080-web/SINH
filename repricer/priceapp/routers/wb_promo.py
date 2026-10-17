"""Страница «Акции WB»: календарь акций по кабинетам, наши карточки в обычных акциях — плановая
цена, «% от эталона для вхождения», порог, пометка «уложится в порог» / «вернётся эталон»; порог
«для акции» (до её конца) и «Добавить в акцию»; массовая правка, Excel, история
(`priceapp/wb_promo.py`)."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from priceapp import audit, progress, settings, wb_discount as wd, wb_promo as wp
from priceapp.database import get_db
from priceapp.deps import get_current_user, posted_form
from priceapp.excel import ExcelReadError, bytes_response, read_xlsx_rows, xlsx_response
from priceapp.flash import flash
from priceapp.models import User, WbPromotion
from priceapp.pages import render
from priceapp.timeutils import now_utc

router = APIRouter()
ROWS_LIMIT = 300
GROUP = "group"
VIEWS = {GROUP: "Общие артикулы и категории с артикулами", wd.CATEGORY: "Только категории",
         wd.ARTICLE: "Только артикулы"}
FILTER_KEYS = ("acc", "promo", "by", "q", "cat", "flt")
HISTORY = ("wb_promo_threshold", "wb_promo_threshold_back", "wb_promo_add")


def _params(src) -> dict:
    p = {k: str(src.get(k) or "").strip() for k in FILTER_KEYS}
    if p["by"] not in VIEWS:
        p["by"] = GROUP
    if p["flt"] not in wp.FILTERS:
        p["flt"] = ""
    if not p["promo"].isdigit():
        p["promo"] = ""
    return p


def _acc(db: Session, raw: str) -> int:
    try:
        i = int(raw or 0)
    except ValueError:
        return 0
    return i if any(a.id == i for a in wd.wb_accounts(db)) else 0


def _back(p: dict) -> RedirectResponse:
    q = urlencode({k: v for k, v in p.items() if v and not (k == "by" and v == GROUP)})
    return RedirectResponse("/wb-promo" + (f"?{q}" if q else ""), status_code=303)


def _promos(db: Session, acc: int, promo_id: int | None = None) -> list[WbPromotion]:
    """Идущие и будущие акции (закончившиеся — нет) кабинета или всех кабинетов."""
    now = now_utc()
    q = db.query(WbPromotion).filter((WbPromotion.end_at.is_(None)) | (WbPromotion.end_at >= now))
    if acc:
        q = q.filter(WbPromotion.account_id == acc)
    if promo_id is not None:
        q = q.filter(WbPromotion.promo_id == promo_id)
    return q.order_by(WbPromotion.start_at, WbPromotion.name).all()


def _view(db: Session, p: dict, all_cards=None):
    """(кабинет, акции, выбранная акция, строки Entry, категории, артикулы по ключу, вне каталога, карточки)."""
    acc = _acc(db, p["acc"])
    chosen = _promos(db, acc, int(p["promo"])) if p["promo"] else []
    pcards = wp.classify(db, chosen, all_cards) if chosen else []
    cats, arts, unknown = wp.rows(db, pcards, acc)
    if p["by"] == GROUP:
        entries = wp.grouped(cats, arts, p["q"], p["cat"], p["flt"])
    else:
        src = cats if p["by"] == wd.CATEGORY else arts
        entries = [wd.Entry("row", row=r, section=r.key if r.kind == wd.CATEGORY else (r.category or ""))
                   for r in src if wp.keep(r, p["q"], p["cat"], p["flt"])]
    return acc, chosen, entries, cats, {a.key: a for a in arts}, unknown, pcards


def _calendar(db: Session, promos: list[WbPromotion]) -> list[dict]:
    """Список акций для страницы: одна строка на акцию (ID у WB общий), кабинеты и наши счётчики."""
    now = now_utc()
    counts: dict[int, dict] = {}
    for pc in wp.classify(db, promos):
        k = counts.setdefault(pc.promo.promo_id, {"ours": 0, "in": 0, wp.FITS: 0, wp.RETURNS: 0, wp.FLOOR: 0})
        if pc.card is None:
            continue
        k["ours"] += 1
        k["in"] += pc.item.in_action
        if pc.status in k:
            k[pc.status] += 1
    from priceapp.models import Account
    names = {a.id: a.name for a in db.query(Account)}
    out: dict[int, dict] = {}
    for p in promos:
        row = out.setdefault(p.promo_id, {"id": p.promo_id, "name": p.name, "type": p.type, "start": p.start_at,
                                          "end": p.end_at, "accounts": [], "exceptions": 0, "note": "",
                                          "counts": counts.get(p.promo_id, {})})
        row["accounts"].append(names.get(p.account_id, p.account_id))
        row["exceptions"] += p.exceptions or 0
        if p.items_note:
            row["note"] = p.items_note
        if p.start_at and p.start_at > now:
            days = (p.start_at - now).days
            row["state"] = "начнётся " + ("в ближайшие сутки" if days < 1 else f"через {days} дн.")
        else:
            row["state"] = "идёт"
    return list(out.values())


@router.get("/wb-promo")
def page(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from priceapp.models import AuditLog, WorkerHeartbeat
    from priceapp.routers.prices import saved_filters
    p = _params(request.query_params)
    all_cards = wd.cards(db)
    acc, chosen, entries, cats, _, unknown, pcards = _view(db, p, all_cards)
    calendar = _calendar(db, _promos(db, acc))
    rows = [e for e in entries if e.kind == "row"]
    shown, n = [], 0
    for e in entries:
        n += e.kind == "row"
        if n > ROWS_LIMIT:
            break
        shown.append(e)
    fetched = max((x.fetched_at for x in db.query(WbPromotion)), default=None)
    hb = db.get(WorkerHeartbeat, "wb_promo")
    qs = urlencode({k: v for k, v in p.items() if v})
    history = (db.query(AuditLog).filter(AuditLog.action.in_(HISTORY)).order_by(AuditLog.id.desc()).limit(50).all())
    return render(request, "wb_promo.html", user, "wb_promo", p=p, acc=acc, promo=chosen[0] if chosen else None,
                  promo_accounts=sorted({x.account_id for x in chosen}), calendar=calendar, entries=shown,
                  total=len(rows), shown=min(len(rows), ROWS_LIMIT), unknown=unknown, views=VIEWS,
                  filters=wp.FILTERS, accounts=wd.wb_accounts(db), where=wd.account_name(db, acc),
                  categories=[c.key for c in cats], fmt=wd.fmt, labels=wd.KIND_LABELS, types=wp.TYPE_LABELS,
                  fetched=fetched, beat=hb, pending=settings.get(db, settings.WB_PROMO_REFRESH_NOW) == "1",
                  history=history, export_qs=qs, current_url="/wb-promo" + (f"?{qs}" if qs else ""),
                  saved_filters=saved_filters(db, "/wb-promo"), statuses=wp.STATUS_LABELS,
                  n_cards=len([pc for pc in pcards if pc.card is not None]))


@router.post("/wb-promo/refresh")
def refresh_now(request: Request, form=Depends(posted_form), db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    settings.put(db, settings.WB_PROMO_REFRESH_NOW, "1")
    audit.log(db, user.username, "wb_promo_refresh", "", "обновление календаря акций по кнопке")
    db.commit()
    flash(request, "Календарь акций обновится в течение пары минут (по каждому кабинету — список акций и товары "
                   "обычных акций). Обновите страницу чуть позже.", "ok")
    return _back(_params(form))


def _targets(entries, form) -> list:
    """Строки правки: весь отбор или отмеченные. Артикул, чья категория тоже в правке, отдельно не
    берём — его карточки уже в строке категории."""
    rows = [e.row for e in entries if e.kind == "row"]
    if form.get("all_filtered") != "1":
        sel = set(form.getlist("sel"))
        rows = [r for r in rows if r.sel in sel]
    cats = {wd.norm(r.key) for r in rows if r.kind == wd.CATEGORY}
    return [r for r in rows if not (r.kind == wd.ARTICLE and not r.shared and r.category and wd.norm(r.category) in cats)]


def _cards_of(rows) -> list:
    seen, out = set(), []
    for r in rows:
        for pc in r.cards:
            if id(pc) not in seen:
                seen.add(id(pc))
                out.append(pc)
    return out


def _apply(request: Request, db: Session, p: dict, rows, action: str, raw_value, actor: str) -> RedirectResponse:
    """Общее для строки и массовой правки: порог «для акции» (если вписан) и/или «Добавить в акцию»."""
    value = None
    if str(raw_value or "").strip():
        try:
            value = wd.parse_pct(raw_value)
        except (InvalidOperation, ValueError):
            value = None
        if value is None or wd.threshold_error(value):
            flash(request, "Новый порог для акции — число больше 0 и не больше 90 (%).", "warn")
            return _back(p)
    elif action == "set":
        flash(request, "Впишите новый порог для акции.", "warn")
        return _back(p)
    cards = _cards_of(rows)
    if not cards:
        flash(request, "Ничего не отмечено — отметьте строки или «весь отбор».", "warn")
        return _back(p)
    msg, level = [], "ok"
    if value is not None:
        n = wp.set_threshold(db, cards, value, actor)
        db.commit()
        promo = cards[0].promo
        from priceapp.templating import local
        msg.append(f"Порог для акции {wd.fmt(value)}% — у артикулов изменено: {n}; действует до "
                   f"{local(promo.end_at)}, потом вернётся прежний.")
    if action == "add":
        # Пересчёт после нового порога — по самим карточкам, а не по отбору страницы: при отборе
        # «вернётся эталон» карточки, только что уложившиеся в новый порог, из отбора выпали бы, и
        # добавлять стало бы нечего.
        keys = {(pc.promo.account_id, pc.promo.promo_id, pc.item.nm_id) for pc in cards}
        promos = list({(pc.promo.account_id, pc.promo.promo_id): pc.promo for pc in cards}.values())
        fresh = [pc for pc in wp.classify(db, promos)
                 if (pc.promo.account_id, pc.promo.promo_id, pc.item.nm_id) in keys]
        st = wp.add(db, fresh, actor)
        text, level = wp.add_report(f"Акция «{cards[0].promo.name}»", st)
        msg.append(text)
    flash(request, " ".join(msg), level)
    return _back(p)


@router.post("/wb-promo/save")
@progress.tracked
def save_one(request: Request, form=Depends(posted_form), db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    """Строка: «Сохранить порог» — порог для акции; «Добавить в акцию» — порог (если вписан) и
    добавление карточек строки, что укладываются в порог."""
    p = _params(form)
    _, chosen, entries, _, _, _, _ = _view(db, p)
    sel = str(form.get("sel") or "")
    rows = [e.row for e in entries if e.kind == "row" and e.row.sel == sel][:1]
    if not chosen or not rows:
        flash(request, "Строка не найдена — обновите страницу.", "warn")
        return _back(p)
    return _apply(request, db, p, rows, "add" if form.get("action") == "add" else "set", form.get("thr"),
                  user.username)


@router.post("/wb-promo/bulk")
@progress.tracked
def bulk(request: Request, form=Depends(posted_form), db: Session = Depends(get_db),
         user: User = Depends(get_current_user)):
    p = _params(form)
    _, chosen, entries, _, _, _, _ = _view(db, p)
    if not chosen:
        flash(request, "Сначала выберите акцию.", "warn")
        return _back(p)
    rows = _targets(entries, form)
    return _apply(request, db, p, rows, "add" if form.get("action") == "add" else "set", form.get("value"),
                  user.username)


@router.get("/wb-promo/export")
def export(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p = _params(request.query_params)
    p["by"] = GROUP
    _, chosen, entries, _, _, _, _ = _view(db, p)
    if not chosen:
        flash(request, "Сначала выберите акцию.", "warn")
        return _back(p)
    return bytes_response(wp.export_xlsx(entries, chosen[0]), f"акция_WB_{chosen[0].promo_id}.xlsx")


@router.get("/wb-promo/history")
def history_export(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from priceapp.models import AuditLog
    from priceapp.templating import local
    rows = [[local(h.at, "%d.%m.%Y %H:%M"), h.username, h.object, h.details]
            for h in db.query(AuditLog).filter(AuditLog.action.in_(HISTORY)).order_by(AuditLog.id.desc()).limit(20000)]
    return xlsx_response(wd.HISTORY_HEAD, rows, "акции_WB_история.xlsx")


@router.post("/wb-promo/import")
@progress.tracked
def import_file(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """Файл из «Экспорт в Excel»: «Новый порог для акции, %» (пусто — без изменений) и «Добавить в
    акцию» («да»). Строки — той акции, что в колонке «ID акции»; отбор кабинета — как на странице."""
    p = _params(request.query_params)
    try:
        data = read_xlsx_rows(file.file.read())
    except ExcelReadError as e:
        flash(request, str(e), "error")
        return _back(p)
    if data and any(c not in data[0] for c in (wp.COL_KIND, wp.COL_KEY, wp.COL_PROMO, wp.COL_NEW, wp.COL_ADD)):
        flash(request, "В файле нет нужных колонок — выгрузите файл этой страницей («Экспорт в Excel»).", "error")
        return _back(p)
    todo: dict[str, list] = {}
    bad = []
    for n, r in enumerate(data, start=2):
        t_raw, add_raw = r.get(wp.COL_NEW), str(r.get(wp.COL_ADD) or "").strip().casefold()
        if str(t_raw if t_raw is not None else "").strip() == "" and not add_raw:
            continue
        kind, key = wd.parse_kind(r.get(wp.COL_KIND)), wd.parse_key(r.get(wp.COL_KEY))
        pid = wd.parse_key(r.get(wp.COL_PROMO))
        if not kind or not key or not pid.isdigit():
            bad.append(f"строка {n}: нужны тип, категория или артикул и ID акции")
            continue
        if add_raw and add_raw not in wp.YES:
            bad.append(f"строка {n}: {key} — «Добавить в акцию»: «да» или пусто")
            continue
        todo.setdefault(pid, []).append((n, kind, key, t_raw, bool(add_raw)))
    msgs, level = [], "ok"
    for pid, lines in todo.items():
        q = dict(p, promo=pid, by=GROUP, q="", cat="", flt="")
        _, chosen, entries, _, _, _, _ = _view(db, q)
        if not chosen:
            bad.append(f"акция {pid}: нет среди идущих и будущих — обновите календарь")
            continue
        by_sel = {e.row.sel: e.row for e in entries if e.kind == "row"}
        thr: dict[Decimal, list] = {}
        adds = []
        for n, kind, key, t_raw, want in lines:
            row = by_sel.get(f"{kind[0]}:{key}")
            if row is None:
                bad.append(f"строка {n}: {key} — в акции {pid} таких карточек нет")
                continue
            if str(t_raw if t_raw is not None else "").strip():
                try:
                    v = wd.parse_pct(t_raw)
                except (InvalidOperation, ValueError):
                    v = None
                if v is None or wd.threshold_error(v):
                    bad.append(f"строка {n}: {key} — порог «{t_raw}»: больше 0 и не больше 90")
                    continue
                thr.setdefault(v, []).append(row)
            if want:
                adds.append(row)
        changed = 0
        for v, rows in thr.items():
            changed += wp.set_threshold(db, _cards_of(rows), v, user.username)
        db.commit()
        msgs.append(f"Акция «{chosen[0].name}»: порог для акции изменён у артикулов — {changed}")
        if adds:
            _, _, entries, _, _, _, _ = _view(db, q)
            by_sel = {e.row.sel: e.row for e in entries if e.kind == "row"}
            st = wp.add(db, _cards_of([by_sel[r.sel] for r in adds if r.sel in by_sel]), user.username)
            text, lv = wp.add_report("добавление", st)
            msgs.append(text)
            level = "warn" if lv == "warn" else level
    if bad:
        level = "warn"
        msgs.append("Не применено: " + "; ".join(bad[:5]) + (" …" if len(bad) > 5 else ""))
    flash(request, ". ".join(msgs) or "В файле нечего применять: колонки правки пустые.", level)
    return _back(p)
