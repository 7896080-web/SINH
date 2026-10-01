"""Страница «Мэппинг»: артикулы Lamoda ↔ товары 1С по правилам sync_admin.

Правила и статусы — `markapp/mapping.py`. Страница только показывает и
выгружает: исправляют сопоставление в карточке Lamoda или в 1С, а не здесь —
привязка руками означала бы, что программа знает о товаре то, чего не знает
1С, а перемещение 1С делает по своему справочнику.
"""
import io
from urllib.parse import quote

import openpyxl
from openpyxl.cell import WriteOnlyCell
from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from markapp import audit, mapping as M, onec
from markapp.database import get_db
from markapp.deps import get_current_user
from markapp.flash import flash
from markapp.models import OnecTask, User
from markapp.pages import render
from markapp.timeutils import today_local

router = APIRouter()
PAGE_LIMIT = 500
MAX_UPLOAD = 60 * 1024 * 1024        # полный справочник 1С — ~150 тыс. строк, ~15 МБ


@router.get("/mapping")
def mapping_page(request: Request, status: str = "", q: str = "", db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    rows = M.build(db)
    chosen = M.select(rows, status, q)
    pending = (db.query(OnecTask).filter(OnecTask.command == "BARCODE_DICT",
                                         OnecTask.status.in_(("pending", "sent")))
               .order_by(OnecTask.id.desc()).first())
    return render(request, "mapping.html", user, "mapping", rows=chosen[:PAGE_LIMIT],
                  total=len(chosen), all_count=len(rows), counts=M.counts(rows),
                  labels=M.STATUS_LABELS, hints=M.STATUS_HINTS, status=status, q=q,
                  limit=PAGE_LIMIT, info=M.dictionary_info(db), pending=pending,
                  can_request=onec.epf_version(db) >= onec.MIN_VERSION["BARCODE_DICT"])


@router.get("/mapping/export")
def mapping_export(status: str = "", q: str = "", db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Весь отбор, а не первый экран: файл открывают, чтобы разобрать строки
    пачкой, и «моей строки нет» читалось бы как «всё сопоставлено» (урок sync_admin)."""
    chosen = M.select(M.build(db), status, q)
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet("Сопоставление")
    ws.append(["Размерный артикул Lamoda", "EAN", "Размер Lamoda", "Цвет Lamoda", "Статус",
               "ID товара 1С", "Артикул 1С", "Наименование 1С", "Размер 1С", "Цвет 1С",
               "Пул штрихкодов 1С", "Конфликт с", "GTIN", "НК: цвет", "НК: размер"])
    def text_cell(v):
        # Всё — текст. openpyxl превращает строку на «=» в ФОРМУЛУ: наименование
        # из 1С или артикул на «=» стал бы исполняемой формулой в файле, а
        # штрихкод-число потерял бы ведущий ноль.
        c = WriteOnlyCell(ws, value=v)
        c.data_type = "s"
        return c
    for r in chosen:
        ws.append([text_cell(v) for v in (
            r.supplier_sku, r.ean, r.lamoda_size, r.lamoda_color, r.label, r.item_id,
            r.onec_article, r.onec_name, r.onec_size, r.onec_color, ", ".join(r.pool),
            "; ".join(r.others), r.gtin, r.nk_color, r.nk_size)])
    buf = io.BytesIO()
    wb.save(buf)
    name = f"сопоставление_{today_local():%Y-%m-%d}.xlsx"
    return Response(buf.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@router.post("/mapping/request")
def mapping_request(request: Request, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    try:
        task = onec.enqueue_barcode_dict(db)
        audit.log(db, user.username, "onec_barcode_dict", task.order_id)
        db.commit()
        flash(request, "Запрос справочника отправлен в 1С. Полный справочник 1С выгружает несколько "
                       "минут; страница обновится, когда придёт ответ.", "ok")
    except onec.OnecError as e:
        db.rollback()
        flash(request, str(e), "error")
    return RedirectResponse("/mapping", status_code=303)


@router.post("/mapping/import")
async def mapping_import(request: Request, file: UploadFile = File(...),
                         db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    data = await file.read(MAX_UPLOAD + 1)
    try:
        if len(data) > MAX_UPLOAD:
            raise M.MappingError("файл больше 60 МБ")
        st = M.load_dictionary(db, M.parse_barcode_dict(M.decode_upload(data)),
                               f"файл {file.filename or ''}")
        audit.log(db, user.username, "onec_barcode_dict_file", file.filename or "",
                  f"строк {st['rows']}, SKU {st['items']}, неоднозначных штрихкодов {st['ambiguous']}")
        db.commit()
        flash(request, f"Справочник 1С загружен: строк {st['rows']}, SKU {st['items']}, "
                       f"неоднозначных штрихкодов {st['ambiguous']}.", "warn" if st["ambiguous"] else "ok")
    except M.MappingError as e:
        db.rollback()
        flash(request, f"Справочник не загружен: {e}", "error")
    return RedirectResponse("/mapping", status_code=303)
