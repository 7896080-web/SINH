from datetime import date
from decimal import Decimal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from markapp import audit, onec, settings, stickers, supplies as S, upd_service as U
from markapp.database import get_db
from markapp.deps import get_current_user
from markapp.flash import flash
from markapp.models import (EDITABLE_STATUSES, FboUpload, OnecTask, Organization, Supply,
                            SupplyStatus, UpdDocument, User)
from markapp.pages import render
from markapp.timeutils import parse_ru, ru, today_local

router = APIRouter()
MAX_UPLOAD = 20 * 1024 * 1024


def _back(supply_id: int) -> RedirectResponse:
    return RedirectResponse(f"/supplies/{supply_id}", status_code=303)


def _get(db: Session, supply_id: int) -> Supply:
    supply = db.get(Supply, supply_id)
    if supply is None:
        raise S.SupplyError("поставка не найдена")
    return supply


def _date_or_none(text: str) -> date | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text) if "-" in text else parse_ru(text)
    except ValueError:
        raise S.SupplyError(f"дата «{text}» не разобрана — ДД.ММ.ГГГГ")


async def _read(upload: UploadFile) -> bytes:
    data = await upload.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise S.SupplyError("файл больше 20 МБ")
    return data


@router.get("/supplies")
def supply_list(request: Request, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    items = db.query(Supply).order_by(Supply.id.desc()).limit(200).all()
    return render(request, "supplies.html", user, "supplies", supplies=items,
                  totals={s.id: S.totals(s) for s in items})


@router.get("/supplies/new")
def supply_new(request: Request, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    return render(request, "supply_new.html", user, "supplies",
                  next_number=S.next_number(db), org=settings.lamoda_org(db))


@router.post("/supplies/new")
async def supply_create(request: Request, file: UploadFile = File(...),
                        number: str = Form(""), doc_number: str = Form(""),
                        supply_date: str = Form(""), db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    org = settings.lamoda_org(db)
    if org is None:
        flash(request, "Не выбран ИП для Lamoda — укажите его на странице «Организации».", "error")
        return RedirectResponse("/supplies/new", status_code=303)
    try:
        parsed = S.parse_input(await _read(file))
        number = (number or parsed.number or S.next_number(db)).strip()
        doc_number = (doc_number or number).strip()
        d = _date_or_none(supply_date) or parsed.supply_date
        supply = S.create_supply(db, parsed, organization_id=org.id, number=number,
                                 doc_number=doc_number, supply_date=d,
                                 filename=file.filename or "", username=user.username)
        audit.log(db, user.username, "supply_created", f"поставка {number}",
                  f"{len(supply.rows)} строк, файл {file.filename}")
        db.commit()
    except S.SupplyError as e:
        db.rollback()
        flash(request, f"Поставка не создана: {e}", "error")
        return RedirectResponse("/supplies/new", status_code=303)
    flash(request, f"Поставка {supply.number} создана: {len(supply.rows)} строк.", "ok")
    return _back(supply.id)


@router.get("/supplies/{supply_id}")
def supply_detail(supply_id: int, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    supply = db.get(Supply, supply_id)
    if supply is None:
        return RedirectResponse("/supplies", status_code=303)
    tasks = (db.query(OnecTask).filter(OnecTask.supply_id == supply.id)
             .order_by(OnecTask.id.desc()).all())
    fbo = (db.query(FboUpload).filter(FboUpload.supply_id == supply.id)
           .order_by(FboUpload.id.desc()).first())
    upd = (db.query(UpdDocument).filter(UpdDocument.supply_id == supply.id)
           .order_by(UpdDocument.id.desc()).first())
    scheme, scheme_warn = U.effective_scheme(db, supply, upd.doc_date if upd else None)
    sticker_warn = ""
    if upd is not None:
        planned, _ = U.effective_scheme(db, supply, None)
        if planned != upd.scheme:
            sticker_warn = (f"УПД выпущен по схеме «{U.SCHEME_LABELS[upd.scheme]}», а по плановой дате "
                            f"стикеры печатались бы как «{U.SCHEME_LABELS[planned]}». Перепечатайте стикеры.")
    pending = [t for t in tasks if t.status in ("pending", "sent")]
    return render(request, "supply.html", user, "supplies", supply=supply, tasks=tasks,
                  totals=S.totals(supply), problems=S.blocking_problems(supply),
                  editable=supply.status in [s.value for s in EDITABLE_STATUSES],
                  epf_ready=onec.epf_ready(db), fbo=fbo, upd=upd, scheme=scheme,
                  scheme_warn=scheme_warn, sticker_warn=sticker_warn,
                  scheme_labels=U.SCHEME_LABELS, pending=pending, today=today_local(),
                  organizations=db.query(Organization).filter(Organization.is_active.is_(True)).all())


@router.post("/supplies/{supply_id}/header")
def supply_header(supply_id: int, request: Request, number: str = Form(...),
                  doc_number: str = Form(...), supply_date: str = Form(""),
                  planned_upd_date: str = Form(""), scheme_choice: str = Form("auto"),
                  scheme_reason: str = Form(""), db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    try:
        supply = _get(db, supply_id)
        number, doc_number = number.strip(), doc_number.strip()
        editable = supply.status in [s.value for s in EDITABLE_STATUSES]
        if not editable and (number != supply.number or doc_number != supply.doc_number
                             or _date_or_none(supply_date) != supply.supply_date):
            raise S.SupplyError("номер и дата поставки после перемещения не меняются")
        if db.query(UpdDocument).filter(UpdDocument.supply_id == supply.id).first() \
                and doc_number != supply.doc_number:
            raise S.SupplyError("УПД уже выпущен — номер документа не меняется")
        errs = S.validate_numbers(db, number, doc_number, supply.id)
        if errs:
            raise S.SupplyError("; ".join(errs))
        if scheme_choice not in U.SCHEME_LABELS:
            raise S.SupplyError("неизвестная схема")
        if scheme_choice != "auto" and scheme_choice != supply.scheme_choice and not scheme_reason.strip():
            raise S.SupplyError("ручной выбор схемы договора требует причины")
        if editable and number != supply.number:
            S._remember_number(db, number)
        supply.number, supply.doc_number = number, doc_number
        supply.supply_date = _date_or_none(supply_date)
        supply.planned_upd_date = _date_or_none(planned_upd_date) or supply.supply_date
        if scheme_choice != supply.scheme_choice:
            audit.log(db, user.username, "scheme_changed", f"поставка {supply.number}",
                      f"{supply.scheme_choice} → {scheme_choice}; причина: {scheme_reason.strip()}")
            supply.scheme_choice = scheme_choice
            supply.scheme_reason = scheme_reason.strip()
        db.commit()
        flash(request, "Сохранено.", "ok")
    except S.SupplyError as e:
        db.rollback()
        flash(request, str(e), "error")
    return _back(supply_id)


@router.post("/supplies/{supply_id}/rows/{row_id}")
def supply_row_qty(supply_id: int, row_id: int, request: Request, qty: int = Form(...),
                   db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        supply = _get(db, supply_id)
        S.update_row_qty(supply, row_id, qty)
        db.commit()
        flash(request, "Строка изменена; поставка вернулась в черновик — проверьте остаток в 1С заново.", "ok")
    except S.SupplyError as e:
        db.rollback()
        flash(request, str(e), "error")
    return _back(supply_id)


@router.post("/supplies/{supply_id}/refresh")
def supply_refresh(supply_id: int, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    try:
        supply = _get(db, supply_id)
        S.refresh_from_catalog(db, supply)
        db.commit()
        flash(request, "Штрихкоды и цены перечитаны из справочника «Одежда полный».", "ok")
    except S.SupplyError as e:
        db.rollback()
        flash(request, str(e), "error")
    return _back(supply_id)


@router.post("/supplies/{supply_id}/delete")
def supply_delete(supply_id: int, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    try:
        supply = _get(db, supply_id)
        S.ensure_editable(supply)
        if db.query(OnecTask).filter(OnecTask.supply_id == supply.id,
                                     OnecTask.command == "SUPPLY_MOVEMENT").first():
            raise S.SupplyError("по поставке уже отправлялось перемещение — удалять нельзя")
        db.query(OnecTask).filter(OnecTask.supply_id == supply.id).delete()
        audit.log(db, user.username, "supply_deleted", f"поставка {supply.number}")
        db.delete(supply)
        db.commit()
        flash(request, f"Черновик поставки {supply.number} удалён. Номер больше не выдаётся.", "ok")
        return RedirectResponse("/supplies", status_code=303)
    except S.SupplyError as e:
        db.rollback()
        flash(request, str(e), "error")
        return _back(supply_id)


@router.post("/supplies/{supply_id}/check")
def supply_check(supply_id: int, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    try:
        supply = _get(db, supply_id)
        S.ensure_editable(supply)
        problems = S.blocking_problems(supply)
        if problems:
            raise S.SupplyError("; ".join(problems))
        if not onec.epf_ready(db):
            raise S.SupplyError("обработка 1С ещё не обновлена под маркировку (нет ответа на PING) — "
                                "см. «Диагностика»")
        task = onec.enqueue_check(db, supply)
        audit.log(db, user.username, "onec_check", f"поставка {supply.number}", task.order_id)
        db.commit()
        flash(request, "Проверка остатка отправлена в 1С. Ответ придёт через минуту-две — обновите страницу.", "ok")
    except (S.SupplyError, onec.OnecError) as e:
        db.rollback()
        flash(request, str(e), "error")
    return _back(supply_id)


@router.post("/supplies/{supply_id}/move")
def supply_move(supply_id: int, request: Request, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    try:
        supply = _get(db, supply_id)
        if supply.status != SupplyStatus.checked.value:
            raise S.SupplyError("переместить можно только после успешной проверки остатка в 1С")
        if not onec.epf_ready(db):
            raise S.SupplyError("обработка 1С не обновлена под маркировку")
        busy = (db.query(OnecTask).filter(OnecTask.supply_id == supply.id,
                                          OnecTask.command == "SUPPLY_MOVEMENT",
                                          OnecTask.status.in_(("pending", "sent"))).first())
        if busy:
            raise S.SupplyError("перемещение уже отправлено, ждём ответа 1С")
        task = onec.enqueue_movement(db, supply)
        audit.log(db, user.username, "onec_movement", f"поставка {supply.number}", task.order_id)
        db.commit()
        flash(request, "Перемещение отправлено в 1С одним документом. Ответ — через минуту-две.", "ok")
    except (S.SupplyError, onec.OnecError) as e:
        db.rollback()
        flash(request, str(e), "error")
    return _back(supply_id)


@router.post("/supplies/{supply_id}/retry/{task_id}")
def supply_retry(supply_id: int, task_id: int, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """Повтор зависшего перемещения. Безопасен: 1С находит уже проведённый
    документ по order_id и возвращает его номер, второго не создаёт."""
    try:
        supply = _get(db, supply_id)
        task = db.get(OnecTask, task_id)
        if task is None or task.supply_id != supply.id or task.command != "SUPPLY_MOVEMENT" \
                or task.status != "timeout":
            raise S.SupplyError("повторить можно только зависшее перемещение")
        new = onec.enqueue_movement(db, supply)
        audit.log(db, user.username, "onec_movement_retry", f"поставка {supply.number}",
                  f"после задания {task.id}: {new.order_id}")
        db.commit()
        flash(request, "Перемещение отправлено повторно. Если документ уже есть, 1С вернёт его номер.", "ok")
    except (S.SupplyError, onec.OnecError) as e:
        db.rollback()
        flash(request, str(e), "error")
    return _back(supply_id)


@router.post("/supplies/{supply_id}/fbo")
async def supply_fbo(supply_id: int, request: Request, file: UploadFile = File(...),
                     db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        supply = _get(db, supply_id)
        if supply.status not in (SupplyStatus.moved.value, SupplyStatus.upd_issued.value):
            raise S.SupplyError("выгрузку «Поставки FBO» загружают после перемещения в 1С")
        data = await _read(file)
        try:
            res = U.check_fbo_against_supply(data, supply)
        except Exception as e:
            raise S.SupplyError(f"выгрузка не разобрана: {e}")
        report = "\n".join(res.problems + getattr(res, "_diffs", []) + res.notes)
        db.add(FboUpload(supply_id=supply.id, filename=file.filename or "", content=data,
                         ok=res.ok, report=report, username=user.username))
        db.commit()
        if res.ok:
            flash(request, f"Выгрузка сверена с поставкой: {res.rows} строк, {res.units} шт. Можно выпускать УПД.", "ok")
        else:
            flash(request, "Выгрузка НЕ сходится с поставкой: " + "; ".join(res.problems), "error")
    except S.SupplyError as e:
        db.rollback()
        flash(request, str(e), "error")
    return _back(supply_id)


@router.post("/supplies/{supply_id}/upd")
def supply_upd(supply_id: int, request: Request, doc_date: str = Form(...),
               totals_mode: str = Form("rows"), force: str = Form(""),
               db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        supply = _get(db, supply_id)
        if supply.status not in (SupplyStatus.moved.value, SupplyStatus.upd_issued.value):
            raise S.SupplyError("УПД выпускается после перемещения в 1С")
        fbo = (db.query(FboUpload).filter(FboUpload.supply_id == supply.id)
               .order_by(FboUpload.id.desc()).first())
        if fbo is None or not fbo.ok:
            raise S.SupplyError("нет сверенной выгрузки «Поставки FBO»")
        if totals_mode not in ("rows", "reference"):
            raise S.SupplyError("режим итогов: rows или reference")
        d = _date_or_none(doc_date)
        if d is None:
            raise S.SupplyError("укажите дату УПД")
        built = U.build_for_supply(db, supply, fbo.content, d, totals_mode)
        errors = U.has_errors(built.findings)
        if errors and not force:
            raise S.SupplyError("проверка УПД нашла ошибки, документ не выпущен: "
                                + U.findings_text(built.findings)[:300])
        old = db.query(UpdDocument).filter(UpdDocument.supply_id == supply.id).all()
        for o in old:
            audit.log(db, user.username, "upd_replaced", f"УПД {o.doc_number}",
                      f"прежний от {ru(o.doc_date)} заменён")
            db.delete(o)
        db.flush()
        doc = UpdDocument(supply_id=supply.id, fbo_upload_id=fbo.id, doc_number=supply.doc_number,
                          doc_date=d, scheme=built.scheme,
                          scheme_manual=supply.scheme_choice != "auto",
                          scheme_reason=supply.scheme_reason, id_file=built.id_file, xml=built.xml,
                          positions=built.positions, total_with_vat=built.total_with_vat,
                          check_report=U.findings_text(built.findings), has_errors=errors,
                          username=user.username)
        db.add(doc)
        supply.status = SupplyStatus.upd_issued.value
        audit.log(db, user.username, "upd_issued", f"УПД {supply.doc_number}",
                  f"от {ru(d)}, схема {built.scheme}, {built.positions} поз., {built.total_with_vat}"
                  + (" — ВЫПУЩЕН С ОШИБКАМИ ПРОВЕРКИ" if errors else ""))
        db.commit()
        msg = f"УПД {supply.doc_number} выпущен: {built.positions} поз., с НДС {built.total_with_vat}."
        if built.scheme_warning:
            msg += " Внимание: " + built.scheme_warning
        flash(request, msg, "warn" if (built.scheme_warning or errors) else "ok")
    except S.SupplyError as e:
        db.rollback()
        flash(request, str(e), "error")
    return _back(supply_id)


def _attachment(data: bytes, name: str, media: str) -> Response:
    return Response(data, media_type=media, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@router.get("/supplies/{supply_id}/upd.xml")
def supply_upd_download(supply_id: int, db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    doc = (db.query(UpdDocument).filter(UpdDocument.supply_id == supply_id)
           .order_by(UpdDocument.id.desc()).first())
    if doc is None:
        return RedirectResponse(f"/supplies/{supply_id}", status_code=303)
    return _attachment(doc.xml, f"{doc.doc_number}.xml", "application/xml")


@router.post("/supplies/{supply_id}/stickers")
def supply_stickers(supply_id: int, request: Request, boxes: int = Form(...),
                    db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        supply = _get(db, supply_id)
        upd = (db.query(UpdDocument).filter(UpdDocument.supply_id == supply.id)
               .order_by(UpdDocument.id.desc()).first())
        data, scheme = stickers.build(db, supply, boxes, upd.doc_date if upd else None)
        audit.log(db, user.username, "stickers", f"поставка {supply.number}", f"{boxes} коробов, {scheme}")
        db.commit()
        return _attachment(data, stickers.filename(supply, boxes),
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except (S.SupplyError, ValueError) as e:
        db.rollback()
        flash(request, str(e), "error")
        return _back(supply_id)
