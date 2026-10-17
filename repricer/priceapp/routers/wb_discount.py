"""Страница «Порог скидки WB»: порог скидки от эталона по категориям WB и артикулам 1С — на все
кабинеты или на один; «отправить сейчас» цену «эталон − N%»; массовая правка по отбору; Excel;
история правок (`priceapp/wb_discount.py`).

Список — как в файле: общие артикулы (есть в нескольких кабинетах), затем каждая категория и её
артикулы. Новое значение у категории ставится на все её артикулы (`wd.set_category`)."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from priceapp import progress, wb_discount as wd
from priceapp.database import get_db
from priceapp.deps import get_current_user, posted_form
from priceapp.excel import ExcelReadError, bytes_response, read_xlsx_rows, xlsx_response
from priceapp.flash import flash
from priceapp.models import User
from priceapp.pages import render

router = APIRouter()
ROWS_LIMIT = 300
SEND_LIMIT = 500           # строк «отправить сейчас» за раз — файлом или галочкой «весь отбор»
GROUP = "group"
VIEWS = {GROUP: "Общие артикулы и категории с артикулами", wd.CATEGORY: "Только категории",
         wd.ARTICLE: "Только артикулы"}
FILTER_KEYS = ("acc", "by", "q", "cat", "flt")


def _params(src) -> dict:
    p = {k: str(src.get(k) or "").strip() for k in FILTER_KEYS}
    if p["by"] not in VIEWS:
        p["by"] = GROUP
    if p["flt"] not in wd.FILTERS:
        p["flt"] = ""
    return p


def _acc(db: Session, raw: str) -> int:
    try:
        i = int(raw or 0)
    except ValueError:
        return 0
    return i if any(a.id == i for a in wd.wb_accounts(db)) else 0


def _back(p: dict) -> RedirectResponse:
    q = urlencode({k: v for k, v in p.items() if v and not (k == "by" and v == GROUP)})
    return RedirectResponse("/wb-discount" + (f"?{q}" if q else ""), status_code=303)


def _view(db: Session, p: dict, all_cards=None):
    """(кабинет, строки списка Entry, все категории, артикулы по ключу) под отбор страницы."""
    acc = _acc(db, p["acc"])
    cats, arts = wd.table(db, acc, all_cards)
    if p["by"] == GROUP:
        entries = wd.grouped(cats, arts, p["q"], p["cat"], p["flt"])
    else:
        src = cats if p["by"] == wd.CATEGORY else arts
        entries = [wd.Entry("row", row=r, section=r.key if r.kind == wd.CATEGORY else (r.category or ""))
                   for r in src if wd.keep(r, p["q"], p["cat"], p["flt"])]
    return acc, entries, cats, {a.key: a for a in arts}


@router.get("/wb-discount")
def page(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from priceapp.routers.prices import saved_filters
    p = _params(request.query_params)
    acc, entries, cats, _ = _view(db, p)
    rows = [e for e in entries if e.kind == "row"]
    shown, n = [], 0
    for e in entries:                       # предел — по строкам, заголовки разделов не в счёт
        n += e.kind == "row"
        if n > ROWS_LIMIT:
            break
        shown.append(e)
    qs = urlencode({k: v for k, v in p.items() if v})
    return render(request, "wb_discount.html", user, "wb_discount", p=p, acc=acc, entries=shown,
                  total=len(rows), shown=min(len(rows), ROWS_LIMIT), views=VIEWS, filters=wd.FILTERS,
                  accounts=wd.wb_accounts(db), where=wd.account_name(db, acc),
                  categories=[c.key for c in cats if c.cards], default=wd.Thresholds(db).default,
                  history=wd.history(db), labels=wd.KIND_LABELS, fmt=wd.fmt, export_qs=qs,
                  current_url="/wb-discount" + (f"?{qs}" if qs else ""),
                  saved_filters=saved_filters(db, "/wb-discount"))


@router.post("/wb-discount/default")
def save_default(request: Request, form=Depends(posted_form), db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    p = _params(form)
    try:
        v = wd.parse_pct(form.get("value"))
    except (InvalidOperation, ValueError):
        v = None
    if v is None or wd.threshold_error(v):
        flash(request, "Порог на все товары — число больше 0 и не больше 90 (%).", "warn")
        return _back(p)
    changed = wd.set_default(db, v, user.username)
    db.commit()
    flash(request, f"Порог на все товары — {wd.fmt(v)}%. Начнёт действовать при ближайшем опросе WB." if changed
          else "Ничего не изменилось.", "ok")
    return _back(p)


def _send_many(db: Session, targets: list[tuple[str, str, Decimal, int]], actor: str, all_cards=None) -> dict:
    """(вид, ключ, «эталон −», кабинет). Категории — раньше артикулов: отправка артикула со своим
    порогом должна лечь последней."""
    all_cards = all_cards if all_cards is not None else wd.cards(db)
    targets = sorted(targets, key=lambda t: t[0] != wd.CATEGORY)
    progress.pin("Отправляю на WB «эталон − N%»", total=len(targets))
    sts = []
    for kind, key, pct, acc in targets:
        progress.stage(f"{wd.KIND_LABELS[kind].lower()} {key}")
        sts.append(wd.send_now(db, kind, key, pct, actor, all_cards=all_cards, account_id=acc))
        progress.advance()
    return wd.merge_reports(sts)


def _set(db: Session, kind: str, key: str, value, actor: str, acc: int, where: str, all_cards) -> tuple[bool, int]:
    """Порог строки: у категории — с каскадом на её артикулы."""
    if kind == wd.CATEGORY:
        return wd.set_category(db, key, value, actor, acc, where, all_cards)
    return wd.set_threshold(db, kind, key, value, actor, acc, where), 0


@router.post("/wb-discount/save")
@progress.tracked
def save_one(request: Request, form=Depends(posted_form), db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    """Строка таблицы: свой порог (пусто — снять) и, кнопкой «Отправить сейчас», «эталон − N%».
    У категории новое значение ставится и на все её артикулы."""
    p = _params(form)
    acc = _acc(db, p["acc"])
    kind, key = wd.parse_kind(form.get("kind")) or "", wd.parse_key(form.get("key"))
    if kind not in wd.KIND_LABELS or not key:
        flash(request, "Не указано, чей порог.", "warn")
        return _back(p)
    try:
        thr = wd.parse_pct(form.get("thr"))
    except (InvalidOperation, ValueError):
        flash(request, f"Порог «{form.get('thr')}» — не число.", "warn")
        return _back(p)
    if thr is not None and wd.threshold_error(thr):
        flash(request, f"{key}: {wd.threshold_error(thr)}.", "warn")
        return _back(p)
    where = wd.account_name(db, acc)
    all_cards = wd.cards(db)
    changed, cleared = _set(db, kind, key, thr, user.username, acc, where, all_cards)
    db.commit()
    text = (f"порог {wd.fmt(thr)}%" if thr is not None else "свой порог снят — действует общий") if changed \
        else "порог без изменений"
    if cleared:
        text += f"; у артикулов категории снято своих порогов: {cleared} — на них теперь порог категории"
    msg = [f"{wd.KIND_LABELS[kind]} {key} ({where}): {text}."]
    level = "ok"
    if form.get("action") == "send":
        try:
            now = wd.parse_pct(form.get("now"))
        except (InvalidOperation, ValueError):
            now = None
        if now is None or wd.now_error(now):
            flash(request, " ".join(msg) + " Не отправлено: «эталон −» — число от 0 до 90 (%).", "warn")
            return _back(p)
        st = _send_many(db, [(kind, key, now, acc)], user.username, all_cards)
        text, level = wd.report(f"{wd.KIND_LABELS[kind]} {key} ({where})", now, st)
        msg.append(text)
    flash(request, " ".join(msg), level)
    return _back(p)


@router.post("/wb-discount/bulk")
@progress.tracked
def bulk(request: Request, form=Depends(posted_form), db: Session = Depends(get_db),
         user: User = Depends(get_current_user)):
    """Массовая правка: отмеченные строки или весь отбор — свой порог, снять свой порог, «эталон −
    N%» сейчас. Где — кабинет из отбора (или все кабинеты WB). Артикул, чья категория тоже в
    правке, отдельно не правится — его покрывает категория."""
    p = _params(form)
    action = str(form.get("action") or "")
    all_cards = wd.cards(db)
    acc, entries, _, arts = _view(db, p, all_cards)
    if form.get("all_filtered") == "1":
        targets = [(e.row.kind, e.row.key) for e in entries if e.kind == "row"]
    else:
        targets = []
        for raw in form.getlist("sel"):
            k, _, key = str(raw).partition(":")
            kind = {"c": wd.CATEGORY, "a": wd.ARTICLE}.get(k)
            if kind and key and (kind, key) not in targets:
                targets.append((kind, key))
    if not targets:
        flash(request, "Ничего не отмечено — отметьте строки или «весь отбор».", "warn")
        return _back(p)
    targets = wd.covered(targets, arts)
    where = wd.account_name(db, acc)
    if action in ("set", "clear"):
        v = None
        if action == "set":
            try:
                v = wd.parse_pct(form.get("value"))
            except (InvalidOperation, ValueError):
                v = None
            if v is None or wd.threshold_error(v):
                flash(request, "Порог — число больше 0 и не больше 90 (%).", "warn")
                return _back(p)
        n = cleared = 0
        # категории — первыми: их каскад не должен снять порог, поставленный артикулу этой же правкой
        for kind, key in progress.each(sorted(targets, key=lambda t: t[0] != wd.CATEGORY), "Записываю порог",
                                       total=len(targets)):
            ch, cl = _set(db, kind, key, v, user.username, acc, where, all_cards)
            n += ch
            cleared += cl
        db.commit()
        what = f"порог {wd.fmt(v)}%" if v is not None else "свой порог снят — действует общий"
        flash(request, f"{where}: {what} — изменено строк {n} из {len(targets)}"
                       + (f"; у артикулов отмеченных категорий снято своих порогов: {cleared}" if cleared else "")
                       + ". Начнёт действовать при ближайшем опросе WB.", "ok")
        return _back(p)
    if action == "send":
        try:
            now = wd.parse_pct(form.get("now"))
        except (InvalidOperation, ValueError):
            now = None
        if now is None or wd.now_error(now):
            flash(request, "«Эталон −» — число от 0 до 90 (%); 0 — вернуть полный эталон.", "warn")
            return _back(p)
        if len(targets) > SEND_LIMIT:
            flash(request, f"Отмечено {len(targets)} строк — больше {SEND_LIMIT} за раз. Сузьте отбор.", "warn")
            return _back(p)
        st = _send_many(db, [(k, key, now, acc) for k, key in targets], user.username, all_cards)
        text, level = wd.report(f"{where}, строк {len(targets)}", now, st)
        flash(request, text, level)
        return _back(p)
    flash(request, "Неизвестное действие.", "warn")
    return _back(p)


@router.get("/wb-discount/export")
def export(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Файл для правки — всегда разделами, как на странице: общие артикулы, затем категории со
    своими артикулами. Отбор страницы (кабинет, поиск, категория, состояние) учитывается."""
    p = _params(request.query_params)
    p["by"] = GROUP
    acc, entries, _, _ = _view(db, p)
    return bytes_response(wd.export_xlsx(entries, wd.account_name(db, acc)), "порог_скидки_WB.xlsx")


@router.get("/wb-discount/history")
def history_export(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from priceapp.templating import local
    rows = [[local(h.at, "%d.%m.%Y %H:%M"), h.username, h.object, h.details] for h in wd.history(db, 20000)]
    return xlsx_response(wd.HISTORY_HEAD, rows, "порог_скидки_WB_история.xlsx")


@router.post("/wb-discount/import")
@progress.tracked
def import_file(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """Файл из «Экспорт в Excel»: правятся «Новый порог, %» (пусто — без изменений, «-» — снять свой)
    и «Отправить сейчас». Сначала категории (новое значение — на все их артикулы), затем артикулы:
    исключение, вписанное артикулу в том же файле, ложится поверх каскада категории."""
    p = _params(request.query_params)
    try:
        data = read_xlsx_rows(file.file.read())
    except ExcelReadError as e:
        flash(request, str(e), "error")
        return _back(p)
    if data and wd.COL_NEW not in data[0] and wd.OLD_FORMAT_COL in data[0]:
        flash(request, "Файл старого образца (порог в нём стоял заполненным) — выгрузите файл заново кнопкой "
                       "«Экспорт в Excel» и впишите изменения в колонку «Новый порог, %».", "error")
        return _back(p)
    if data and any(c not in data[0] for c in (wd.COL_KIND, wd.COL_KEY, wd.COL_NEW)):
        flash(request, f"В файле нет колонок «{wd.COL_KIND}», «{wd.COL_KEY}» и «{wd.COL_NEW}» — выгрузите файл "
                       "этой страницей («Экспорт в Excel»).", "error")
        return _back(p)
    accs = {a.name.casefold(): a for a in wd.wb_accounts(db)}
    all_cards = wd.cards(db)
    known_cats = {wd.norm(c.category): c.category for c in all_cards if c.category}
    known_arts = {a for c in all_cards for a in c.articles}
    thr, sends, bad = [], [], []
    seen: set = set()
    for n, r in enumerate(progress.each(data, "Читаю файл", total=len(data)), start=2):
        t_mode, t_val = wd.cell(r.get(wd.COL_NEW))
        n_mode, n_val = wd.cell(r.get(wd.COL_NOW))
        if t_mode == "skip" and n_mode == "skip":
            continue                                              # пусто — без изменений
        kind, key = wd.parse_kind(r.get(wd.COL_KIND)), wd.parse_key(r.get(wd.COL_KEY))
        if not kind or not key:
            bad.append(f"строка {n}: тип «Категория» или «Артикул» и что именно")
            continue
        acc_raw = str(r.get(wd.COL_WHERE) or "").strip()
        if not acc_raw or acc_raw.casefold() == wd.ALL_ACCOUNTS.casefold():
            acc = 0
        elif acc_raw.casefold() in accs:
            acc = accs[acc_raw.casefold()].id
        else:
            bad.append(f"строка {n}: кабинета «{acc_raw}» нет среди активных кабинетов WB")
            continue
        if kind == wd.CATEGORY:
            key = known_cats.get(wd.norm(key), key)
        exists = (key in known_arts) if kind == wd.ARTICLE else (wd.norm(key) in known_cats)
        ident = (kind, key if kind == wd.ARTICLE else wd.norm(key), acc)
        if ident in seen:
            bad.append(f"строка {n}: {key} повторяется — применена первая строка")
            continue
        seen.add(ident)
        if not exists and not (t_mode == "clear" and n_mode == "skip" and wd.get_rule(db, kind, key, acc)):
            bad.append(f"строка {n}: {key} — на WB нет карточек с эталоном")
            continue
        if t_mode == "bad" or (t_mode == "set" and wd.threshold_error(t_val)):
            bad.append(f"строка {n}: {key} — порог «{r.get(wd.COL_NEW)}»: больше 0 и не больше 90, «-» — снять")
            continue
        if n_mode in ("bad", "clear") or (n_mode == "set" and wd.now_error(n_val)):
            bad.append(f"строка {n}: {key} — «эталон −» «{r.get(wd.COL_NOW)}»: число от 0 до 90")
            continue
        if t_mode in ("set", "clear"):
            thr.append((kind, key, t_val, acc))
        if n_mode == "set":
            sends.append((kind, key, n_val, acc))
    if len(sends) > SEND_LIMIT:
        flash(request, f"В файле {len(sends)} строк «отправить сейчас» — больше {SEND_LIMIT} за раз. Разбейте файл.", "warn")
        return _back(p)
    n_cat = n_art = cleared = 0
    progress.pin("Записываю пороги", total=len(thr))
    for kind, key, v, acc in sorted(thr, key=lambda t: t[0] != wd.CATEGORY):
        ch, cl = _set(db, kind, key, v, user.username, acc, wd.account_name(db, acc), all_cards)
        progress.advance()
        if kind == wd.CATEGORY:
            n_cat += ch
        else:
            n_art += ch
        cleared += cl
    db.commit()
    msg = [f"Из файла: порог изменён у категорий — {n_cat}, у артикулов — {n_art}"
           + (f"; у артикулов этих категорий снято своих порогов: {cleared}" if cleared else "")]
    level = "ok"
    if sends:
        st = _send_many(db, sends, user.username, all_cards)
        text, level = wd.report(f"«Отправить сейчас» по {len(sends)} строкам", None, st)
        msg.append(text)
    if bad:
        level = "warn"
        msg.append("Не применено: " + "; ".join(bad[:5]) + (" …" if len(bad) > 5 else ""))
    flash(request, ". ".join(msg), level)
    return _back(p)
