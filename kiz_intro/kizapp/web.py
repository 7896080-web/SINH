"""Веб-часть «Ввода в оборот». Слушает только 127.0.0.1; входа в саму программу
нет — действия в ЧЗ защищены сертификатом ИП, а POST с чужой страницы (другой
порт, другой сайт) отсекается проверкой Origin.

ИП несколько (05.10.2026): у каждого свой вход в ЧЗ, свой справочник карточек
Нацкаталога и свои партии."""
from datetime import date, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from kizapp import chz, config, labels as L, service as S
from kizapp.db import Base, engine, get_db
from kizapp.models import Batch, Card, Doc, Journal, Org

HERE = Path(__file__).resolve().parent
app = FastAPI(title="Ввод в оборот")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))


def _local(d):
    return d.replace(tzinfo=timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M") if d else ""


templates.env.filters["local_time"] = _local


@app.on_event("startup")
def _startup():
    Base.metadata.create_all(bind=engine)


@app.middleware("http")
async def same_origin_only(request: Request, call_next):
    if request.method == "POST":
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") not in {f"http://127.0.0.1:{config.PORT}", f"http://localhost:{config.PORT}",
                                                 "http://testserver"}:
            return JSONResponse({"error": "запрос не с этой страницы"}, status_code=403)
    return await call_next(request)


def _go(url: str, msg: str = "", level: str = "ok") -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}msg={quote(msg)}&level={level}" if msg else url, status_code=303)


def _render(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, {
        "request": request, "msg": request.query_params.get("msg", ""),
        "level": request.query_params.get("level", "ok"), **ctx})


def _file(data: bytes, name: str, media: str) -> Response:
    return Response(data, media_type=media, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


# --- Главная ---------------------------------------------------------------------------------

@app.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    orgs = db.query(Org).order_by(Org.id).all()
    batches = db.query(Batch).order_by(Batch.id.desc()).all()
    names = {o.id: o.name for o in orgs}
    return _render(request, "index.html", orgs=[(o, S.token(o) is not None,
                                                db.query(Card).filter(Card.org_id == o.id).count()) for o in orgs],
                   batches=[(b, names.get(b.org_id, "?"), S.summary(db, b)) for b in batches],
                   status_ru=S.STATUS_RU, label={k: L.get(db, k) for k in L.DEFAULTS},
                   placeholders=L.PLACEHOLDERS,
                   journal=db.query(Journal).order_by(Journal.id.desc()).limit(15).all())


@app.post("/upload")
async def upload(file: UploadFile = File(...), org_id: int = Form(...), db: Session = Depends(get_db)):
    o = db.get(Org, org_id)
    if o is None:
        return _go("/", "Выберите ИП.", "error")
    data = await file.read(S.MAX_PDF + 1)
    try:
        b = S.load_pdf(db, o, data, file.filename or "")
    except S.KizError as e:
        db.rollback()
        return _go("/", f"Коды не загружены: {e}", "error")
    db.commit()
    note = S.refresh_cards(db, b)
    db.commit()
    return _go(f"/batch/{b.id}", f"Загружено кодов: {S.summary(db, b)['total']}."
               + (f" Карточки НК: {note}" if note else ""), "warn" if note else "ok")


@app.post("/labels/settings")
def labels_settings(title: str = Form(""), right: str = Form(""), bottom: str = Form(""),
                    module: str = Form("0.5"), db: Session = Depends(get_db)):
    problems = L.check_template(title + right + bottom)
    try:
        m = float(module.replace(",", "."))
        if not 0.3 <= m <= 1.0:
            raise ValueError
    except ValueError:
        problems.append("модуль — от 0,3 до 1 мм (203 dpi: 0,5; 300 dpi: 0,508)")
    if problems:
        return _go("/", "Не сохранено: " + "; ".join(problems), "error")
    for k, v in (("label_title", title), ("label_right", right), ("label_bottom", bottom), ("label_module", str(m))):
        L.put(db, k, v.strip())
    db.commit()
    return _go("/", "Шаблон этикетки сохранён.")


# --- ИП: реквизиты, вход, справочник НК --------------------------------------------------------

@app.post("/org")
def org_save(org_id: int = Form(0), name: str = Form(""), inn: str = Form(...), db: Session = Depends(get_db)):
    try:
        o = S.save_org(db, org_id or None, name, inn)
    except S.KizError as e:
        db.rollback()
        return _go("/", str(e), "error")
    S.log(db, "org", f"{o.name}, ИНН {o.inn}")
    db.commit()
    return _go("/", f"Сохранено: {o.name}.")


@app.get("/org/{org_id}/login")
def login_page(org_id: int, request: Request, db: Session = Depends(get_db)):
    o = db.get(Org, org_id)
    if o is None:
        return _go("/")
    return _render(request, "login.html", org=o, until=o.token_until if S.token(o) else None)


@app.post("/org/{org_id}/login/challenge")
def login_challenge(org_id: int):
    try:
        return chz.challenge()
    except chz.ChzError as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/org/{org_id}/login/token")
def login_token(org_id: int, uuid: str = Body(...), signature: str = Body(...), cert_inn: str = Body(""),
                db: Session = Depends(get_db)):
    o = db.get(Org, org_id)
    if o is None:
        return JSONResponse({"error": "организация не найдена"}, status_code=404)
    try:
        until = S.login(db, o, uuid, signature, cert_inn)
    except S.KizError as e:
        db.commit()
        return JSONResponse({"error": str(e)}, status_code=400)
    db.commit()
    return {"ok": True, "until": _local(until)}


@app.get("/org/{org_id}/cards")
def cards_page(org_id: int, request: Request, db: Session = Depends(get_db)):
    o = db.get(Org, org_id)
    if o is None:
        return _go("/")
    cards = db.query(Card).filter(Card.org_id == o.id).order_by(Card.gtin).all()
    return _render(request, "cards.html", org=o, cards=cards, token_ok=S.token(o) is not None, cert_ru=S.CERT_RU)


@app.post("/org/{org_id}/cards/refresh")
def cards_refresh(org_id: int, gtins: str = Form(""), db: Session = Depends(get_db)):
    """Перечитать справочник ИП из Нацкаталога: все его карточки и (если введены) новые GTIN."""
    o = db.get(Org, org_id)
    if o is None:
        return _go("/")
    new = [g.strip().zfill(14) for g in gtins.replace(",", " ").replace(";", " ").split() if g.strip().isdigit()]
    have = [g for (g,) in db.query(Card.gtin).filter(Card.org_id == o.id)]
    todo = sorted(set(have) | set(new))
    if not todo:
        return _go(f"/org/{org_id}/cards", "Справочник пуст — введите GTIN или загрузите партию.", "warn")
    note = S.fetch_cards(db, o, todo, force=True)
    S.log(db, "cards", f"{o.name}: запрошено карточек {len(todo)}" + (f" — {note}" if note else ""))
    db.commit()
    return _go(f"/org/{org_id}/cards", note or f"Справочник обновлён из Нацкаталога: {len(todo)} карточек.",
               "warn" if note else "ok")


# --- Партия ----------------------------------------------------------------------------------

@app.get("/batch/{batch_id}")
def batch_page(batch_id: int, request: Request, db: Session = Depends(get_db)):
    b = db.get(Batch, batch_id)
    if b is None:
        return _go("/")
    o = S.org_of(db, b)
    return _render(request, "batch.html", org=o, batch=b, s=S.summary(db, b), token_ok=S.token(o) is not None,
                   docs=db.query(Doc).filter(Doc.batch_id == b.id).order_by(Doc.id.desc()).all(),
                   status_ru=S.STATUS_RU, doc_ru=S.DOC_RU, cert_ru=S.CERT_RU)


@app.post("/batch/{batch_id}/refresh")
def batch_refresh(batch_id: int, db: Session = Depends(get_db)):
    b = db.get(Batch, batch_id)
    if b is None:
        return _go("/")
    note = S.refresh_cards(db, b, force=True) or S.refresh_statuses(db, b)
    db.commit()
    return _go(f"/batch/{batch_id}", note or "Карточки НК и статусы кодов обновлены.", "warn" if note else "ok")


@app.post("/batch/{batch_id}/date")
def batch_date(batch_id: int, production_date: str = Form(...), db: Session = Depends(get_db)):
    b = db.get(Batch, batch_id)
    if b is None:
        return _go("/")
    try:
        S.set_production_date(b, production_date)
    except S.KizError as e:
        return _go(f"/batch/{batch_id}", str(e), "error")
    db.commit()
    return _go(f"/batch/{batch_id}", "Дата производства сохранена.")


@app.post("/batch/{batch_id}/prepare")
def batch_prepare(batch_id: int, db: Session = Depends(get_db)):
    b = db.get(Batch, batch_id)
    if b is None:
        return JSONResponse({"error": "партия не найдена"}, status_code=404)
    try:
        d = S.prepare(db, b)
    except S.KizError as e:
        db.commit()
        return JSONResponse({"error": str(e)}, status_code=400)
    db.commit()
    return {"doc_id": d.id, "document": d.document, "count": d.codes_count, "summary": S.doc_summary(d)}


@app.post("/batch/{batch_id}/send")
def batch_send(batch_id: int, doc_id: int = Body(...), signature: str = Body(...), db: Session = Depends(get_db)):
    d = db.get(Doc, doc_id)
    if d is None or d.batch_id != batch_id:
        return JSONResponse({"error": "документ не найден"}, status_code=404)
    try:
        S.send(db, d, signature)
    except S.KizError as e:
        db.commit()
        return JSONResponse({"error": str(e)}, status_code=502)
    db.commit()
    return {"ok": True, "doc_id": d.chz_doc_id}


@app.post("/batch/{batch_id}/release")
def batch_release(batch_id: int, doc_id: int = Form(...), db: Session = Depends(get_db)):
    d = db.get(Doc, doc_id)
    if d is None or d.batch_id != batch_id:
        return _go(f"/batch/{batch_id}")
    try:
        S.release_unknown(db, d)
    except S.KizError as e:
        return _go(f"/batch/{batch_id}", str(e), "error")
    S.log(db, "release", f"партия #{batch_id}, документ #{d.id}")
    db.commit()
    return _go(f"/batch/{batch_id}", "Коды документа освобождены.")


@app.post("/batch/{batch_id}/labels")
def batch_labels(batch_id: int, db: Session = Depends(get_db)):
    b = db.get(Batch, batch_id)
    if b is None:
        return _go("/")
    try:
        codes = S.full_codes(db, b)
        pdf, warnings = L.build_pdf(db, codes, L.values_for(db, codes, S.org_of(db, b),
                                                             date.today().strftime("%d.%m.%Y"), b.id))
    except (S.KizError, L.LabelError) as e:
        return _go(f"/batch/{batch_id}", f"Этикетки не выданы: {e}", "error")
    S.log(db, "labels", f"партия #{b.id}: {len(codes)} этикеток; предупреждений {len(warnings)}")
    db.commit()
    return _file(pdf, f"Этикетки_партия_{b.id}_{len(codes)}шт.pdf", "application/pdf")


@app.post("/batch/{batch_id}/txt")
def batch_txt(batch_id: int, db: Session = Depends(get_db)):
    b = db.get(Batch, batch_id)
    if b is None:
        return _go("/")
    try:
        data = S.txt(db, b)
    except S.KizError as e:
        return _go(f"/batch/{batch_id}", f"Файл кодов не выдан: {e}", "error")
    db.commit()
    return _file(data, f"коды_партия_{b.id}.txt", "text/plain; charset=ascii")


@app.get("/health")
def health():
    return {"ok": True}
