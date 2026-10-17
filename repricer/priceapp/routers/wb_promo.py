"""Страница «Акции WB»: календарь акций по кабинетам, наши карточки в обычных акциях — плановая
цена, «% от эталона для вхождения», порог, пометка «уложится в порог» / «вернётся эталон»; порог
«для акции» (до её конца) и «Добавить в акцию»; массовая правка, Excel, история
(`priceapp/wb_promo.py`).

Устроена как «Порог скидки WB»: отбор и Excel видны сразу, без выбора акции. Акция не выбрана —
в таблице и в файле все идущие и будущие обычные акции, каждая своим разделом."""
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
HISTORY = ("wb_promo_threshold", "wb_promo_threshold_back", "wb_promo_add", "wb_promo_leave_set", "wb_promo_leave")


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


def _view(db: Session, p: dict):
    """(кабинет, акции под отбором, строки Entry, категории для отбора, вне каталога, карточки)."""
    from priceapp import etalon
    acc = _acc(db, p["acc"])
    chosen = _promos(db, acc, int(p["promo"]) if p["promo"] else None)
    ets = etalon.etalons(db)
    pcards = wp.classify(db, chosen, wd.cards(db, ets))
    entries, cats, unknown = wp.sections(db, pcards, acc, p["by"], p["q"], p["cat"], p["flt"],
                                         {a: e.name for a, e in ets.items()})
    return acc, chosen, entries, cats, unknown, pcards


def _calendar(db: Session, promos: list[WbPromotion], pcards=None) -> list[dict]:
    """Список акций для страницы: одна строка на акцию (ID у WB общий), кабинеты и наши счётчики."""
    now = now_utc()
    counts: dict[int, dict] = {}
    for pc in (pcards if pcards is not None else wp.classify(db, promos)):
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
    acc, chosen, entries, cats, unknown, pcards = _view(db, p)
    in_scope = _promos(db, acc)
    calendar = _calendar(db, in_scope, pcards if not p["promo"] else None)
    rows = [e for e in entries if e.kind == "row"]
    shown, n = [], 0
    for e in entries:                       # предел — по строкам, заголовки разделов не в счёт
        n += e.kind == "row"
        if n > ROWS_LIMIT:
            break
        shown.append(e)
    fetched = max((x.fetched_at for x in db.query(WbPromotion)), default=None)
    qs = urlencode({k: v for k, v in p.items() if v})
    history = (db.query(AuditLog).filter(AuditLog.action.in_(HISTORY)).order_by(AuditLog.id.desc()).limit(50).all())
    promo = chosen[0] if p["promo"] and chosen else None
    return render(request, "wb_promo.html", user, "wb_promo", p=p, acc=acc, promo=promo, calendar=calendar,
                  promo_choices=[(c["id"], c["name"], c["type"]) for c in calendar], entries=shown,
                  total=len(rows), shown=min(len(rows), ROWS_LIMIT), unknown=unknown, views=VIEWS,
                  filters=wp.FILTERS, accounts=wd.wb_accounts(db), where=wd.account_name(db, acc),
                  categories=cats, fmt=wd.fmt, labels=wd.KIND_LABELS, types=wp.TYPE_LABELS,
                  has_regular=any(x.type == wp.REGULAR for x in in_scope),
                  fetched=fetched, beat=db.get(WorkerHeartbeat, "wb_promo"),
                  pending=settings.get(db, settings.WB_PROMO_REFRESH_NOW) == "1",
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
    """Строки правки: весь отбор или отмеченные. Артикул, чья категория в той же акции тоже в
    правке, отдельно не берём — его карточки уже в строке категории."""
    rows = [e.row for e in entries if e.kind == "row"]
    if form.get("all_filtered") != "1":
        sel = set(form.getlist("sel"))
        rows = [r for r in rows if r.sel in sel]
    cats = {(r.promo.promo_id, wd.norm(r.key)) for r in rows if r.kind == wd.CATEGORY}
    return [r for r in rows if not (r.kind == wd.ARTICLE and not r.shared and r.category
                                    and (r.promo.promo_id, wd.norm(r.category)) in cats)]


def _cards_of(rows) -> list:
    seen, out = set(), []
    for r in rows:
        for pc in r.cards:
            if id(pc) not in seen:
                seen.add(id(pc))
                out.append(pc)
    return out


def _fresh(db: Session, cards: list, leave_at=None) -> list:
    """Те же карточки, пересчитанные заново (после нового порога или со сроком участия) — по самим
    карточкам, а не по отбору страницы: при отборе «вернётся эталон» карточки, только что
    уложившиеся в новый порог, из отбора выпали бы, и добавлять стало бы нечего."""
    keys = {(pc.promo.account_id, pc.promo.promo_id, pc.item.nm_id) for pc in cards}
    promos = list({(pc.promo.account_id, pc.promo.promo_id): pc.promo for pc in cards}.values())
    return [pc for pc in wp.classify(db, promos, leave_at=leave_at)
            if (pc.promo.account_id, pc.promo.promo_id, pc.item.nm_id) in keys]


def _apply(request: Request, db: Session, p: dict, rows, action: str, raw_value, actor: str,
           raw_leave=None) -> RedirectResponse:
    """Общее для строки и массовой правки: порог «для акции» и срок участия (если вписаны) и/или
    «Добавить в акцию». Срок кончает и порог «для акции» этой акции."""
    try:
        leave_at = wp.parse_leave(raw_leave)
    except ValueError:
        flash(request, f"«Участвовать до» — дата: «{raw_leave}» не дата.", "warn")
        return _back(p)
    value = None
    if str(raw_value or "").strip():
        try:
            value = wd.parse_pct(raw_value)
        except (InvalidOperation, ValueError):
            value = None
        if value is None or wd.threshold_error(value):
            flash(request, "Новый порог для акции — число больше 0 и не больше 90 (%).", "warn")
            return _back(p)
    elif action == "set" and leave_at is None:
        flash(request, "Впишите новый порог для акции или срок участия.", "warn")
        return _back(p)
    cards = _cards_of(rows)
    if not cards:
        flash(request, "Ничего не отмечено — отметьте строки или «весь отбор».", "warn")
        return _back(p)
    msg, level = [], "ok"
    names = sorted({pc.promo.name for pc in cards})
    what = f"акция «{names[0]}»" if len(names) == 1 else f"акций {len(names)}"
    from priceapp.templating import local
    if value is not None:
        n = wp.set_threshold(db, cards, value, actor, leave_at)
        db.commit()
        msg.append(f"Порог для акции {wd.fmt(value)}% ({what}) — у артикулов изменено: {n}; действует до "
                   + (f"{local(leave_at)} (срок участия)" if leave_at else "конца акции") + ", потом вернётся прежний.")
    if leave_at is not None and action == "set":
        n, bad = wp.set_leave(db, cards, leave_at, actor)
        db.commit()
        msg.append(f"Срок участия до {local(leave_at)}: карточек {n} — потом полный эталон, и они выйдут из акции"
                   + (f"; не поставлено ({len(bad)}): " + "; ".join(bad[:5]) if bad else "") + ".")
        if bad or not n:
            level = "warn"
        if not n and not bad:
            msg.append("Срок ставится карточкам, что в акции или отправлены в неё, — таких в строках нет.")
    if action == "add":
        st = wp.add(db, _fresh(db, cards, leave_at), actor, leave_at=leave_at)
        text, level = wp.add_report(what[0].upper() + what[1:], st)
        msg.append(text)
        if leave_at is not None:
            n, bad = wp.set_leave(db, _fresh(db, cards), leave_at, actor)       # и тем, что уже были в акции
            db.commit()
            msg.append(f"Срок участия до {local(leave_at)}" + (f"; не поставлено: " + "; ".join(bad[:3]) if bad else "") + ".")
    flash(request, " ".join(msg), level)
    return _back(p)


@router.post("/wb-promo/save")
@progress.tracked
def save_one(request: Request, form=Depends(posted_form), db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    """Строка: «Сохранить порог» — порог для акции; «Добавить в акцию» — порог (если вписан) и
    добавление карточек строки, что укладываются в порог."""
    p = _params(form)
    _, _, entries, _, _, _ = _view(db, p)
    sel = str(form.get("sel") or "")
    rows = [e.row for e in entries if e.kind == "row" and e.row.sel == sel][:1]
    if not rows:
        flash(request, "Строка не найдена — обновите страницу.", "warn")
        return _back(p)
    return _apply(request, db, p, rows, "add" if form.get("action") == "add" else "set", form.get("thr"),
                  user.username, form.get("leave"))


@router.post("/wb-promo/bulk")
@progress.tracked
def bulk(request: Request, form=Depends(posted_form), db: Session = Depends(get_db),
         user: User = Depends(get_current_user)):
    p = _params(form)
    _, _, entries, _, _, _ = _view(db, p)
    return _apply(request, db, p, _targets(entries, form), "add" if form.get("action") == "add" else "set",
                  form.get("value"), user.username, form.get("leave"))


@router.get("/wb-promo/export")
def export(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Файл для правки — всегда разделами, как на странице; отбор страницы учитывается. Акция не
    выбрана — все идущие и будущие обычные акции, каждая своим разделом."""
    p = _params(request.query_params)
    p["by"] = GROUP
    _, chosen, entries, _, _, _ = _view(db, p)
    name = f"акция_WB_{p['promo']}.xlsx" if p["promo"] else "акции_WB.xlsx"
    return bytes_response(wp.export_xlsx(entries), name)


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
    """Файл из «Экспорт в Excel»: «Новый порог для акции, %», «Участвовать до (дата)» («-» — снять
    срок) и «Добавить в акцию» («да»); пусто — без изменений. Каждая строка — в акции из своей
    колонки «ID акции»; в одном файле может быть несколько акций. Кабинет — из отбора страницы."""
    p = _params(request.query_params)
    try:
        data = read_xlsx_rows(file.file.read())
    except ExcelReadError as e:
        flash(request, str(e), "error")
        return _back(p)
    if data and any(c not in data[0] for c in (wp.COL_KIND, wp.COL_KEY, wp.COL_PROMO, wp.COL_NEW, wp.COL_ADD,
                                                wp.COL_LEAVE)):
        flash(request, "В файле нет нужных колонок — выгрузите файл этой страницей («Экспорт в Excel»).", "error")
        return _back(p)
    todo: dict[str, list] = {}
    bad = []
    for n, r in enumerate(progress.each(data, "Читаю файл", total=len(data)), start=2):
        t_raw, add_raw = r.get(wp.COL_NEW), str(r.get(wp.COL_ADD) or "").strip().casefold()
        l_raw = r.get(wp.COL_LEAVE)
        if str(t_raw if t_raw is not None else "").strip() == "" and not add_raw and \
                str(l_raw if l_raw is not None else "").strip() == "":
            continue
        kind, key = wd.parse_kind(r.get(wp.COL_KIND)), wd.parse_key(r.get(wp.COL_KEY))
        pid = wd.parse_key(r.get(wp.COL_PROMO))
        if not kind or not key or not pid.isdigit():
            bad.append(f"строка {n}: нужны тип, категория или артикул и ID акции")
            continue
        if add_raw and add_raw not in wp.YES:
            bad.append(f"строка {n}: {key} — «Добавить в акцию»: «да» или пусто")
            continue
        leave = "keep"
        if str(l_raw if l_raw is not None else "").strip() == wd.CLEAR_CELL:
            leave = None
        elif str(l_raw if l_raw is not None else "").strip():
            try:
                leave = wp.parse_leave(l_raw)
            except ValueError:
                bad.append(f"строка {n}: {key} — «Участвовать до»: «{l_raw}» не дата")
                continue
        todo.setdefault(pid, []).append((n, kind, key, t_raw, bool(add_raw), leave))
    msgs, level = [], "ok"
    progress.pin("Применяю файл по акциям", total=len(todo))
    for pid, lines in todo.items():
        q = dict(p, promo=pid, by=GROUP, q="", cat="", flt="")
        _, chosen, entries, _, _, _ = _view(db, q)
        progress.advance()
        if not chosen:
            bad.append(f"акция {pid}: нет среди идущих и будущих — обновите календарь")
            continue
        by_sel = {e.row.sel: e.row for e in entries if e.kind == "row"}
        thr: dict[tuple, list] = {}
        adds, leaves = [], {}
        for n, kind, key, t_raw, want, leave in lines:
            row = by_sel.get(f"{pid}:{kind[0]}:{key}")
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
                thr.setdefault((v, leave if leave != "keep" else None), []).append(row)
            if want:
                adds.append((row, leave if leave != "keep" else None))
            if leave != "keep":
                leaves.setdefault(leave, []).append(row)
        changed = 0
        for (v, lv_at), rows in thr.items():
            changed += wp.set_threshold(db, _cards_of(rows), v, user.username, lv_at)
        db.commit()
        msgs.append(f"Акция «{chosen[0].name}»: порог для акции изменён у артикулов — {changed}")
        if adds:
            _, _, entries, _, _, _ = _view(db, q)
            by_sel = {e.row.sel: e.row for e in entries if e.kind == "row"}
            groups: dict = {}
            for row, lv_at in adds:
                if row.sel in by_sel:
                    groups.setdefault(lv_at, []).append(by_sel[row.sel])
            for lv_at, rows in groups.items():
                st = wp.add(db, _fresh(db, _cards_of(rows), lv_at), user.username, leave_at=lv_at)
                text, lv = wp.add_report("добавление", st)
                msgs.append(text)
                level = "warn" if lv == "warn" else level
        if leaves:
            _, _, entries, _, _, _ = _view(db, q)
            by_sel = {e.row.sel: e.row for e in entries if e.kind == "row"}
            n_leave, bad_leave = 0, []
            for lv_at, rows in leaves.items():
                k, b = wp.set_leave(db, _cards_of([by_sel[r.sel] for r in rows if r.sel in by_sel]), lv_at,
                                    user.username)
                n_leave, bad_leave = n_leave + k, bad_leave + b
            db.commit()
            msgs.append(f"срок участия изменён у карточек — {n_leave}")
            bad += bad_leave
    if bad:
        level = "warn"
        msgs.append("Не применено: " + "; ".join(bad[:5]) + (" …" if len(bad) > 5 else ""))
    flash(request, ". ".join(msgs) or "В файле нечего применять: колонки правки пустые.", level)
    return _back(p)
