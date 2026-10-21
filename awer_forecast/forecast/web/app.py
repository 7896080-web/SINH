"""Веб-интерфейс: две рабочие страницы раздела 6 ТЗ + главная и справочник.

Запуск: `python -m forecast serve` (uvicorn). Вход — логин/пароль из `.env`
(FORECAST_LOGIN, FORECAST_PASSWORD_HASH), сессия в подписанной cookie.
"""
from __future__ import annotations

import logging
import secrets
import time
from datetime import date, datetime
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.engine import Engine
from starlette.middleware.sessions import SessionMiddleware

from forecast.catalog.excel_import import CatalogFileError, import_catalog
from forecast.config import Settings, get_settings
from forecast.db import make_engine
from forecast.db.schema import t_purchase_recommendations, t_shipment_recommendations
from forecast.jobs import (
    DAILY_STEPS, JOB_TITLES, JobAlreadyRunning, finish_job, run_daily, run_step, start_job,
)
from forecast.security import verify_password
from forecast.timeutil import MSK, as_aware_utc
from forecast.web import queries as q
from forecast.web.excel import build_workbook, xlsx_response

logger = logging.getLogger(__name__)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
PUBLIC_PATHS = {"/login", "/health"}


def _fmt_dt(value) -> str:
    if not value:
        return "—"
    if isinstance(value, datetime):
        return as_aware_utc(value).astimezone(MSK).strftime("%d.%m.%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return str(value)


def _fmt_num(value, digits: int = 2) -> str:
    if value is None:
        return "—"
    text = f"{float(value):.{digits}f}".rstrip("0").rstrip(".")
    return (text or "0").replace(".", ",")


TEMPLATES.env.filters["dt"] = _fmt_dt
TEMPLATES.env.filters["num"] = _fmt_num
TEMPLATES.env.globals["JOB_TITLES"] = JOB_TITLES


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = engine or make_engine(settings.database_url)
    secret = settings.session_secret
    if not secret:
        secret = secrets.token_urlsafe(32)
        logger.warning("FORECAST_SESSION_SECRET не задан — сессии сбросятся при перезапуске")
    app = FastAPI(title="AWER Forecast", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.engine = engine

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        if request.url.path not in PUBLIC_PATHS and not request.session.get("user"):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    # добавлен после require_login → выполняется раньше него и наполняет request.session
    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="forecast_session",
                       same_site="lax", max_age=12 * 3600, https_only=False)

    def render(request: Request, template: str, **context) -> HTMLResponse:
        flash = request.session.pop("flash", None)
        return TEMPLATES.TemplateResponse(request, template, {"user": request.session.get("user"),
                                                              "flash": flash, **context})

    def redirect(request: Request, url: str, message: str | None = None, kind: str = "ok") -> RedirectResponse:
        if message:
            request.session["flash"] = {"kind": kind, "text": message}
        return RedirectResponse(url, status_code=303)

    # ---------------- вход ----------------
    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        configured = bool(settings.password_hash)
        return render(request, "login.html", configured=configured)

    @app.post("/login")
    def login(request: Request, login: str = Form(...), password: str = Form(...)):
        if settings.password_hash and login == settings.login and verify_password(password, settings.password_hash):
            request.session.clear()
            request.session["user"] = login
            return RedirectResponse("/", status_code=303)
        time.sleep(1)   # подбор пароля перебором — медленнее
        return redirect(request, "/login", "Неверный логин или пароль", "error")

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ---------------- главная ----------------
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        with engine.connect() as conn:
            runs = q.last_runs(conn, list(DAILY_STEPS) + ["catalog_import"])
            stats = q.catalog_stats(conn)
            assemblies = q.recent_assemblies(conn)
        return render(request, "dashboard.html", runs=runs, stats=stats, assemblies=assemblies,
                      steps=DAILY_STEPS, planning=settings.planning, lamoda_ok=settings.lamoda_configured,
                      exchange_dirs=settings.onec_exchange_dirs, sales_dir=settings.onec_sales_dir)

    @app.post("/jobs/{job}")
    def run_job_now(request: Request, job: str, background: BackgroundTasks):
        user = request.session.get("user", "web")
        if job == "daily":
            background.add_task(run_daily, engine, settings, started_by=user)
            return redirect(request, "/", "Ежедневный цикл запущен — обновите страницу через минуту.")
        if job not in DAILY_STEPS:
            return redirect(request, "/", f"Нет такого задания: {job}", "error")

        def task():
            try:
                run_step(engine, settings, job, started_by=user)
            except JobAlreadyRunning:
                pass
        background.add_task(task)
        return redirect(request, "/", f"Запущено: {JOB_TITLES.get(job, job)}. Результат — в таблице ниже.")

    # ---------------- поставка на FBO ----------------
    @app.get("/fbo", response_class=HTMLResponse)
    def fbo_page(request: Request, all: int = 0, query: str = "", assembled: int | None = None):
        with engine.connect() as conn:
            batch_id = q.latest_batch(conn, t_shipment_recommendations)
            rows = q.fbo_rows(conn, batch_id=batch_id, show_all=bool(all), query=query)
            info = q.batch_info(conn, batch_id)
        confirmed = [r for r in rows if r.status == "confirmed" and (r.confirmed_qty or 0) > 0]
        return render(request, "fbo.html", rows=rows, info=info, show_all=bool(all), query=query,
                      confirmed_count=len(confirmed), confirmed_units=sum(r.confirmed_qty for r in confirmed),
                      assembled=assembled)

    @app.post("/fbo/save")
    async def fbo_save(request: Request):
        form = await request.form()
        values = {int(k[2:]): str(v) for k, v in form.items() if k.startswith("c_") and k[2:].isdigit()}
        changed, errors = q.save_confirmed(engine, values, request.session.get("user", "web"))
        back = "/fbo" + _keep_query(form)
        if errors:
            return redirect(request, back, "Не сохранено: " + "; ".join(errors[:5]), "error")
        return redirect(request, back, f"Сохранено строк: {changed}." if changed else "Изменений нет.")

    @app.post("/fbo/assemble")
    async def fbo_assemble(request: Request):
        # Кнопка в той же форме, что и поля «Подтверждено»: сначала сохраняем введённое,
        # чтобы не потерять цифры, вписанные без нажатия «Сохранить».
        form = await request.form()
        values = {int(k[2:]): str(v) for k, v in form.items() if k.startswith("c_") and k[2:].isdigit()}
        _, errors = q.save_confirmed(engine, values, request.session.get("user", "web"))
        if errors:
            return redirect(request, "/fbo" + _keep_query(form), "Не собрано: " + "; ".join(errors[:5]), "error")
        run_id, rows, units = q.assemble_shipment(engine, request.session.get("user", "web"))
        if run_id is None:
            return redirect(request, "/fbo", "Нет строк с заполненным «Подтверждено к поставке» — собирать нечего.",
                            "error")
        return redirect(request, f"/fbo?assembled={run_id}",
                        f"Поставка №{run_id} собрана: {rows} строк, {units} шт. Скачайте файл по кнопке.")

    @app.get("/fbo/export.xlsx")
    def fbo_export(all: int = 1, query: str = ""):
        with engine.connect() as conn:
            batch_id = q.latest_batch(conn, t_shipment_recommendations)
            rows = q.fbo_rows(conn, batch_id=batch_id, show_all=bool(all), query=query)
        content = build_workbook([("Поставка на FBO", q.FBO_COLUMNS, [r.excel() for r in rows])])
        return xlsx_response(content, f"рекомендации_поставка_FBO_{date.today():%Y-%m-%d}.xlsx")

    @app.get("/fbo/assembly/{assembly_id}.xlsx")
    def fbo_assembly_file(assembly_id: int):
        with engine.connect() as conn:
            rows = q.fbo_rows(conn, assembly_id=assembly_id)
        if not rows:
            return Response("Сборка не найдена", status_code=404)
        content = build_workbook([(f"Поставка {assembly_id}", q.FBO_COLUMNS, [r.excel() for r in rows])])
        return xlsx_response(content, f"поставка_FBO_{assembly_id}.xlsx")

    # ---------------- закупка ----------------
    @app.get("/purchase", response_class=HTMLResponse)
    def purchase_page(request: Request, query: str = ""):
        with engine.connect() as conn:
            batch_id = q.latest_batch(conn, t_purchase_recommendations)
            rows = q.purchase_rows(conn, statuses=("pending",), batch_id=batch_id, query=query) if batch_id else []
            no_reorder = (q.purchase_rows(conn, statuses=("no_reorder",), batch_id=batch_id, query=query)
                          if batch_id else [])
            ordered = q.purchase_rows(conn, statuses=("ordered",), query=query)
            info = q.batch_info(conn, batch_id)
        return render(request, "purchase.html", rows=rows, no_reorder=no_reorder, ordered=ordered, info=info,
                      query=query, total_units=sum(r.qty for r in rows))

    @app.post("/purchase/save")
    async def purchase_save(request: Request):
        form = await request.form()
        values = {int(k[2:]): str(v) for k, v in form.items() if k.startswith("o_") and k[2:].isdigit()}
        changed, errors = q.save_ordered(engine, values, request.session.get("user", "web"))
        if errors:
            return redirect(request, "/purchase", "Не сохранено: " + "; ".join(errors[:5]), "error")
        return redirect(request, "/purchase", f"Отмечено заказанным: {changed}." if changed else "Изменений нет.")

    @app.post("/purchase/{rec_id}/received")
    def purchase_received(request: Request, rec_id: int):
        q.mark_received(engine, rec_id)
        return redirect(request, "/purchase", "Отмечено полученным — больше не считается «в пути».")

    @app.get("/purchase/export.xlsx")
    def purchase_export():
        with engine.connect() as conn:
            batch_id = q.latest_batch(conn, t_purchase_recommendations)
            rows = q.purchase_rows(conn, statuses=("pending",), batch_id=batch_id) if batch_id else []
            no_reorder = q.purchase_rows(conn, statuses=("no_reorder",), batch_id=batch_id) if batch_id else []
            ordered = q.purchase_rows(conn, statuses=("ordered",))
        content = build_workbook([
            ("К закупке", q.PURCHASE_COLUMNS, [r.excel() for r in rows]),
            ("Без пополнения", q.PURCHASE_COLUMNS, [r.excel() for r in no_reorder]),
            ("Уже заказано", q.PURCHASE_COLUMNS + ["Заказано, шт", "Кто отметил"],
             [r.excel() + [r.ordered_qty, r.ordered_by] for r in ordered]),
        ])
        return xlsx_response(content, f"закупка_у_поставщика_{date.today():%Y-%m-%d}.xlsx")

    # ---------------- справочник ----------------
    @app.get("/catalog", response_class=HTMLResponse)
    def catalog_page(request: Request):
        with engine.connect() as conn:
            stats = q.catalog_stats(conn)
            runs = q.last_runs(conn, ["catalog_import", "lamoda_catalog", "lamoda_stocks", "onec_stock"])
            unmapped = q.unmapped_order_skus(conn)
            supplier_rows = q.suppliers(conn)
        return render(request, "catalog.html", stats=stats, runs=runs, unmapped=unmapped, suppliers=supplier_rows)

    @app.post("/catalog/import")
    async def catalog_import(request: Request, file: UploadFile = File(...)):
        content = await file.read()
        user = request.session.get("user", "web")
        try:
            run_id = start_job(engine, "catalog_import", user)
        except JobAlreadyRunning as exc:
            return redirect(request, "/catalog", str(exc), "error")
        try:
            with engine.begin() as conn:
                report = import_catalog(conn, content,
                                        default_supplier_lead_days=settings.planning.supplier_lead_days_default)
        except CatalogFileError as exc:
            finish_job(engine, run_id, "error", str(exc))
            return redirect(request, "/catalog", str(exc), "error")
        except Exception as exc:
            logger.exception("импорт карты упал")
            finish_job(engine, run_id, "error", f"{exc.__class__.__name__}: {exc}")
            return redirect(request, "/catalog", f"Ошибка импорта: {exc}", "error")
        details = report.as_dict()
        details["file"] = file.filename
        message = (f"{file.filename}: строк {report.rows_imported} из {report.rows_total}, моделей +{report.models_created}, "
                   f"размеров +{report.sizes_created}, баркодов +{report.barcodes_added}, "
                   f"конфликтов {len(report.conflicts)}, ошибок {len(report.errors)}")
        finish_job(engine, run_id, "ok", message, details)
        kind = "error" if report.conflicts or report.errors else "ok"
        return redirect(request, "/catalog", message, kind)

    @app.post("/suppliers/{supplier_id}")
    def supplier_update(request: Request, supplier_id: int, production_days: str = Form(""),
                        delivery_days: str = Form(""), moq: str = Form("")):
        errors = q.update_supplier(engine, supplier_id, production_days=production_days,
                                   delivery_days=delivery_days, moq=moq)
        if errors:
            return redirect(request, "/catalog", "Не сохранено: " + "; ".join(errors), "error")
        return redirect(request, "/catalog", "Поставщик сохранён. Новые сроки войдут в следующий пересчёт.")

    return app


def _keep_query(form) -> str:
    parts = []
    if form.get("all") == "1":
        parts.append("all=1")
    if form.get("query"):
        from urllib.parse import quote
        parts.append("query=" + quote(str(form.get("query"))))
    return ("?" + "&".join(parts)) if parts else ""
