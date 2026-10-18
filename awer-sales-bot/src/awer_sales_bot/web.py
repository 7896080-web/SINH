"""Админка (локальный веб): настройки месяца, ручные коэффициенты и планы, продавцы, итоги/ведомость.

Запуск: awer-bot web  →  http://127.0.0.1:8080  (логин admin, пароль из ADMIN_PASSWORD)
Все расчётные значения можно перекрыть вручную; пустое поле = расчётное значение.
"""
from __future__ import annotations

import html
import io
import os
import secrets
from datetime import date, timedelta

import openpyxl
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import Settings
from .db import DB
from .planning import service
from .planning.engine import month_add, rate_for, threshold_amounts

CSS = """
body{font-family:Arial,sans-serif;margin:24px;color:#1f2328;background:#fff}
h1{font-size:20px}h2{font-size:16px;margin-top:28px}
table{border-collapse:collapse;margin:8px 0}td,th{border:1px solid #d0d7de;padding:5px 8px;font-size:13px}
th{background:#eef2f7;text-align:left}td.n{text-align:right;white-space:nowrap}
input[type=text],input[type=number]{width:110px;padding:3px;background:#fffbe6;border:1px solid #c8b26b}
.calc{color:#57606a}.man{font-weight:bold;color:#9a3412}.ok{background:#e6f4ea}
.btn{padding:6px 14px;margin:6px 6px 6px 0;cursor:pointer}nav a{margin-right:14px}
.note{color:#57606a;font-size:12px}
"""


def _rub(x):
    return "" if x is None else f"{x:,.0f}".replace(",", " ")


def _num(s: str | None) -> float | None:
    if s is None or str(s).strip() == "":
        return None
    return float(str(s).replace(" ", "").replace("\xa0", "").replace(",", ".").rstrip("%"))


def create_app(cfg: Settings, db: DB) -> FastAPI:
    app = FastAPI(title="AWER — планы продавцов")
    security = HTTPBasic()
    password = os.environ.get("ADMIN_PASSWORD", "")

    def auth(c: HTTPBasicCredentials = Depends(security)):
        if not password:
            raise HTTPException(500, "Задайте ADMIN_PASSWORD в .env")
        if not (secrets.compare_digest(c.username, "admin") and secrets.compare_digest(c.password, password)):
            raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})

    def page(title: str, body: str) -> HTMLResponse:
        cur = date.today().strftime("%Y-%m")
        nav = (f'<nav><a href="/month/{cur}">Настройки месяца</a><a href="/sellers">Продавцы</a>'
               f'<a href="/month/{cur}/results">Итоги и ведомость</a></nav>')
        return HTMLResponse(f"<!doctype html><html lang=ru><head><meta charset=utf-8><title>{title}</title>"
                            f"<style>{CSS}</style></head><body>{nav}<h1>{title}</h1>{body}</body></html>")

    @app.get("/", dependencies=[Depends(auth)])
    def root():
        return RedirectResponse(f"/month/{date.today():%Y-%m}")

    # ------------------------------------------------------------------ месяц
    @app.get("/month/{month}", dependencies=[Depends(auth)])
    def month_page(month: str):
        mp = service.compute(db, cfg, month)
        p, sp = mp.params, mp.store
        ms = db.one("SELECT * FROM month_settings WHERE month=?", month)
        y, mo = map(int, month.split("-"))
        last = date(y + mo // 12, mo % 12 + 1, 1) - timedelta(days=1)
        today = min(date.today() - timedelta(days=1), last)
        f = lambda v, d=3: "" if v is None else f"{v:.{d}f}"  # noqa: E731
        prev, nxt = month_add(month, -1), month_add(month, 1)
        b = [f'<p><a href="/month/{prev}">← {prev}</a> · <a href="/month/{nxt}">{nxt} →</a></p>',
             f'<p class=note>План {"зафиксирован " + mp.frozen_at if mp.frozen_at else "НЕ зафиксирован"}. '
             'Ежедневные сообщения используют зафиксированный план. Жёлтые поля — ручная правка; пусто = расчёт.</p>',
             f'<form method=post action="/month/{month}/settings"><h2>Коэффициенты и ставки месяца</h2><table>',
             '<tr><th>Показатель</th><th>Расчёт</th><th>Вручную</th><th>Применяется</th></tr>',
             f'<tr><td>Коэффициент год к году (тренд, 3 мес. к прошлому году)</td><td class="n calc">{f(sp.trend_calc)}</td>'
             f'<td><input type=text name=trend_manual value="{f(mp.trend_manual) if mp.trend_manual is not None else ""}"></td>'
             f'<td class="n {"man" if mp.trend_manual is not None else ""}">{f(sp.trend)}</td></tr>',
             f'<tr><td>Шаг перевыполнения плана, %</td><td class="n calc">{cfg.params.step:.0%}</td>'
             f'<td><input type=text name=step value="{"" if not ms or ms["step"] is None else round(ms["step"]*100, 2)}"></td>'
             f'<td class=n>{p.step:.0%}</td></tr>',
             f'<tr><td>Надбавка к ставке за шаг, ₽</td><td class="n calc">{_rub(cfg.params.step_bonus)}</td>'
             f'<td><input type=text name=step_bonus value="{"" if not ms or ms["step_bonus"] is None else _rub(ms["step_bonus"])}"></td>'
             f'<td class=n>{_rub(p.step_bonus)}</td></tr>',
             f'<tr><td>Базовая ставка, ₽</td><td class="n calc">{_rub(cfg.params.base_rate)}</td>'
             f'<td><input type=text name=base_rate value="{"" if not ms or ms["base_rate"] is None else _rub(ms["base_rate"])}"></td>'
             f'<td class=n>{_rub(p.base_rate)}</td></tr>',
             f'<tr><td>Максимальная ставка (без админ. доплаты), ₽</td><td class="n calc">{_rub(cfg.params.max_rate)}</td>'
             f'<td><input type=text name=max_rate value="{"" if not ms or ms["max_rate"] is None else _rub(ms["max_rate"])}"></td>'
             f'<td class=n>{_rub(p.max_rate)}</td></tr>',
             f'<tr><td>Админ. доплата старшим, ₽ (фиксированная, схемой не меняется)</td><td class=n>{_rub(p.admin_bonus)}</td>'
             '<td class=note>config.yaml</td><td class=n>' + _rub(p.admin_bonus) + '</td></tr></table>',
             f'Комментарий: <input type=text style="width:420px" name=note value="{html.escape(ms["note"] or "") if ms else ""}">',
             '<br><button class=btn>Сохранить настройки</button></form>',
             '<h2>Магазин</h2><table>',
             f'<tr><td>Выторг этого месяца прошлого года</td><td class=n>{_rub(sp.last_year)}</td></tr>',
             f'<tr><td>Сезонность: месяц к предыдущему (прошлый год)</td><td class=n>{f(sp.seasonality, 2)}</td></tr>',
             f'<tr><td>План магазина = прошлый год × коэффициент</td><td class=n><b>{_rub(sp.plan)}</b></td></tr>',
             f'<tr><td>Средний месяц магазина за 3 последних</td><td class=n>{_rub(sp.recent_avg)}</td></tr>',
             f'<tr><td>Коэффициент месяца (план / средний месяц)</td><td class=n>{f(sp.coef, 3)}</td></tr>',
             f'<tr><td>План новичка (средний рядовой прошлого года × коэффициент)</td><td class=n>{_rub(sp.newcomer_plan)}</td></tr>',
             '</table>',
             f'<form method=post action="/month/{month}/plans"><h2>Личные планы (на полный месяц)</h2><table>',
             '<tr><th>Продавец</th><th>Роль</th><th>Уровень 3 мес.</th><th>План расчёт</th><th>План вручную</th>'
             f'<th>План итог</th><th>+{p.step:.0%} → ставка</th><th>Факт с 1-го</th><th>Выполн. (на смену)</th><th>Ставка сейчас</th></tr>']
        for r in mp.rows:
            st = service.status(db, cfg, r.seller_id, today) if today.strftime("%Y-%m") == month else None
            th = threshold_amounts(r.plan_final, p, 1)[1] if r.plan_final else None
            perf = st.performance if st else None
            b.append(
                f'<tr><td>{html.escape(r.name)}</td><td>{"админ" if r.role == "admin" else "продавец"}</td>'
                f'<td class=n>{_rub(r.level)}</td><td class="n calc">{_rub(r.plan_calc)}</td>'
                f'<td><input type=text name="p{r.seller_id}" value="{_rub(r.plan_manual)}"></td>'
                f'<td class="n {"man" if r.plan_manual is not None else ""}"><b>{_rub(r.plan_final)}</b></td>'
                f'<td class=n>{_rub(th[0]) + " → " + _rub(rate_for(1, r.role, p)) if th else ""}</td>'
                f'<td class=n>{_rub(st.sales) if st else ""}</td>'
                f'<td class="n {"ok" if perf and perf >= 1 + p.step else ""}">{"" if perf is None else f"{perf:.0%}"}</td>'
                f'<td class=n>{_rub(st.current_rate) if st else ""}</td></tr>')
        b += ['</table><button class=btn>Сохранить ручные планы</button></form>',
              f'<form method=post action="/month/{month}/freeze"><button class=btn>Зафиксировать план месяца</button>'
              '<span class=note> — перезаписывает зафиксированные планы; повторная фиксация в середине месяца меняет цели продавцам.</span></form>']
        return page(f"Настройки месяца {month}", "".join(b))

    @app.post("/month/{month}/settings", dependencies=[Depends(auth)])
    def save_settings(month: str, trend_manual: str = Form(""), step: str = Form(""), step_bonus: str = Form(""),
                      base_rate: str = Form(""), max_rate: str = Form(""), note: str = Form("")):
        st = _num(step)
        service.save_month_settings(db, month, trend_manual=_num(trend_manual), step=st / 100 if st else None,
                                    step_bonus=_num(step_bonus), base_rate=_num(base_rate), max_rate=_num(max_rate),
                                    note=note or None)
        return RedirectResponse(f"/month/{month}", status_code=303)

    @app.post("/month/{month}/plans", dependencies=[Depends(auth)])
    async def save_plans(month: str, request: Request):
        form = await request.form()
        for k, v in form.items():
            if k.startswith("p") and k[1:].isdigit():
                service.save_plan_override(db, month, int(k[1:]), _num(v))
        return RedirectResponse(f"/month/{month}", status_code=303)

    @app.post("/month/{month}/freeze", dependencies=[Depends(auth)])
    def freeze(month: str):
        service.freeze(db, cfg, month)
        return RedirectResponse(f"/month/{month}", status_code=303)

    # ------------------------------------------------------------------ итоги
    @app.get("/month/{month}/results", dependencies=[Depends(auth)])
    def results(month: str):
        rows = db.q("SELECT r.*, s.name, s.role FROM results r JOIN sellers s ON s.id=r.seller_id WHERE month=? "
                    "ORDER BY s.role DESC, s.name", month)
        b = [f'<form method=post action="/month/{month}/close"><button class=btn>Посчитать итоги {month}</button></form>',
             '<table><tr><th>Продавец</th><th>Продажи</th><th>Отработано</th><th>Выполнение</th><th>Шагов</th>'
             '<th>Ставка (полный мес.)</th></tr>']
        for r in rows:
            perf = "" if r["performance"] is None else format(r["performance"], ".0%")
            b.append(f'<tr><td>{html.escape(r["name"])}</td><td class=n>{_rub(r["sales"])}</td><td class=n>{r["fte"]:.2f}</td>'
                     f'<td class=n>{perf}</td>'
                     f'<td class=n>{r["steps"]}</td><td class=n>{_rub(r["rate"])}</td></tr>')
        b.append(f'</table><a href="/month/{month}/payroll.xlsx">Скачать ведомость (xlsx)</a>')
        return page(f"Итоги {month}", "".join(b))

    @app.post("/month/{month}/close", dependencies=[Depends(auth)])
    def close(month: str):
        service.close_month(db, cfg, month)
        return RedirectResponse(f"/month/{month}/results", status_code=303)

    @app.get("/month/{month}/payroll.xlsx", dependencies=[Depends(auth)])
    def payroll(month: str):
        p = service.month_params(db, cfg, month)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = month
        ws.append(["Продавец", "Роль", "Продажи, ₽", "Отработано (доля мес.)", "План (полный мес.)", "Выполнение",
                   "Ставка (полный мес.)", "Ставка к начислению", f"{p.sales_percent:.0%} от продаж", "Итого"])
        for r in db.q("SELECT r.*, s.name, s.role, pl.plan_full FROM results r JOIN sellers s ON s.id=r.seller_id "
                      "LEFT JOIN plans pl ON pl.month=r.month AND pl.seller_id=r.seller_id WHERE r.month=?", month):
            i = ws.max_row + 1
            ws.append([r["name"], r["role"], r["sales"], r["fte"], r["plan_full"], r["performance"], r["rate"],
                       f"=G{i}*MIN(1,D{i})", f"=C{i}*{p.sales_percent}", f"=H{i}+I{i}"])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 headers={"Content-Disposition": f"attachment; filename=payroll_{month}.xlsx"})

    # ------------------------------------------------------------------ приём выгрузки из 1С по HTTP
    @app.post("/api/upload")
    async def upload(request: Request):
        token = os.environ.get("UPLOAD_TOKEN", "")
        if not token or not secrets.compare_digest(request.headers.get("X-Token", ""), token):
            raise HTTPException(401, "bad token")
        from urllib.parse import unquote

        from .importers.onec_sales import import_file
        name = os.path.basename(unquote(request.headers.get("X-Filename", "upload.csv"))) or "upload.csv"
        inbox = cfg.onec.inbox
        inbox.mkdir(parents=True, exist_ok=True)
        path = inbox / name
        path.write_bytes(await request.body())
        rep = import_file(path, db, cfg)
        archive = cfg.onec.archive
        archive.mkdir(parents=True, exist_ok=True)
        path.replace(archive / name)
        return {"lines": rep.lines, "days": rep.days, "unknown_sellers": sorted(rep.unknown_sellers)}

    # ------------------------------------------------------------------ продавцы
    @app.get("/sellers", dependencies=[Depends(auth)])
    def sellers():
        b = ['<form method=post action="/sellers"><table><tr><th>Продавец (как в 1С)</th><th>Обращение</th><th>Роль</th>'
             '<th>Активен</th><th>MAX</th><th>Код привязки</th></tr>']
        for s in db.sellers(active_only=False):
            link = f'{cfg.max.bot_link}?start={s["invite_code"]}' if cfg.max.bot_link and s["invite_code"] else ""
            b.append(
                f'<tr><td>{html.escape(s["name"])}</td>'
                f'<td><input type=text name="fn{s["id"]}" value="{html.escape(s["first_name"] or "")}"></td>'
                f'<td><select name="role{s["id"]}"><option value=seller {"selected" if s["role"] == "seller" else ""}>продавец</option>'
                f'<option value=admin {"selected" if s["role"] == "admin" else ""}>админ (старший)</option></select></td>'
                f'<td><input type=checkbox name="act{s["id"]}" {"checked" if s["active"] else ""}></td>'
                f'<td>{"✅ " + str(s["max_user_id"]) if s["max_user_id"] else "—"}</td>'
                f'<td>{s["invite_code"] or ""} {("<br><span class=note>" + link + "</span>") if link else ""}'
                f' <button class=btn name=invite value="{s["id"]}">новый код</button></td></tr>')
        b.append('</table><button class=btn>Сохранить</button></form>')
        return page("Продавцы", "".join(b))

    @app.post("/sellers", dependencies=[Depends(auth)])
    async def sellers_save(request: Request):
        form = await request.form()
        if form.get("invite"):
            db.new_invite(int(form["invite"]))
        with db.tx() as c:
            for s in db.sellers(active_only=False):
                sid = s["id"]
                c.execute("UPDATE sellers SET first_name=?, role=?, active=? WHERE id=?",
                          (form.get(f"fn{sid}") or s["first_name"], form.get(f"role{sid}", s["role"]),
                           1 if form.get(f"act{sid}") else 0, sid))
        return RedirectResponse("/sellers", status_code=303)

    return app
