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
from urllib.parse import quote

import openpyxl
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import Settings
from .db import DB
from .planning import service
from .planning.engine import month_add, rate_for, threshold_amounts

def qr_svg(text: str) -> str:
    """QR-код ссылки на бота — SVG прямо в странице (без картинок и интернета)."""
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10)
    return img.to_string(encoding="unicode")


CSS = """
body{font-family:Arial,sans-serif;margin:24px;color:#1f2328;background:#fff}
h1{font-size:20px}h2{font-size:16px;margin-top:28px}
table{border-collapse:collapse;margin:8px 0}td,th{border:1px solid #d0d7de;padding:5px 8px;font-size:13px}
th{background:#eef2f7;text-align:left}td.n{text-align:right;white-space:nowrap}
input[type=text],input[type=number]{width:110px;padding:3px;background:#fffbe6;border:1px solid #c8b26b}
.calc{color:#57606a}.man{font-weight:bold;color:#9a3412}.ok{background:#e6f4ea}
.btn{padding:6px 14px;margin:6px 6px 6px 0;cursor:pointer}nav a{margin-right:14px}
.note{color:#57606a;font-size:12px}
.qr svg{width:260px;height:260px}.code{font-size:22px;letter-spacing:3px}
table.grid td,table.grid th{padding:2px 3px;text-align:center}table.grid td.nm{text-align:left;white-space:nowrap}
table.grid th.we,table.grid td.we{background:#f6f8fa}.off{color:#9a3412;font-weight:bold}
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
               f'<a href="/schedule/{cur}">График работы</a><a href="/month/{cur}/results">Итоги и ведомость</a></nav>')
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
             f'<th>План итог</th><th>+{p.step:.0%} → ставка</th><th>Факт с 1-го</th><th>Выполн. (за рабочий день)</th><th>Ставка сейчас</th></tr>']
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

    # ------------------------------------------------------------------ график смен
    @app.get("/schedule/{month}", dependencies=[Depends(auth)])
    def schedule(month: str, msg: str = ""):
        y, mo = map(int, month.split("-"))
        n = service.days_in(month)
        days = db.schedule_days(month)
        norm = cfg.norm_shifts
        wd = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        head = "".join(f'<th class="{"we" if date(y, mo, d).weekday() >= 5 else ""}">{d}<br>'
                       f'{wd[date(y, mo, d).weekday()]}</th>' for d in range(1, n + 1))
        b = [f'<p><a href="/schedule/{month_add(month, -1)}">← {month_add(month, -1)}</a> · '
             f'<a href="/schedule/{month_add(month, 1)}">{month_add(month, 1)} →</a></p>',
             f'<p class=note>{html.escape(msg)}</p>' if msg else "",
             f'<p class=note>Норма — {norm} рабочих дней в месяц. Отработанным считается день, в который у продавца есть '
             'продажи в 1С: если кто-то вышел на замену, бот учтёт это сам. График нужен, чтобы знать рабочие дни '
             'впереди — от них считается «до базы» в утреннем сообщении.</p>',
             f'<form method=post action="/schedule/{month}"><table class=grid>'
             f'<tr><th>Продавец</th>{head}<th>Дней</th><th>Заполнить по схеме</th></tr>']
        for s in db.sellers():
            sid, mine = s["id"], days.get(s["id"], set())
            cells = "".join(f'<td class="{"we" if date(y, mo, d).weekday() >= 5 else ""}"><input type=checkbox '
                            f'name="s{sid}_{d}" data-s="{sid}" data-d="{d}" {"checked" if d in mine else ""}></td>'
                            for d in range(1, n + 1))
            fill = (f'<select id="p{sid}"><option value="3,2">3/2</option><option value="2,2">2/2</option>'
                    f'<option value="5,2">5/2</option></select> с <input type=number id="k{sid}" value=1 min=1 max=7 '
                    f'style="width:42px">-го <button type=button class=btn onclick="fill({sid})">Заполнить</button>'
                    f'<button type=button class=btn onclick="fill({sid},true)">Очистить</button>')
            b.append(f'<tr><td class=nm>{html.escape(s["name"])}</td>{cells}'
                     f'<td id="c{sid}" class="{"" if len(mine) == norm else "off"}">{len(mine)}</td>'
                     f'<td class=nm>{fill}</td></tr>')
        b.append(f'</table><button class=btn>Сохранить график</button></form>'
                 f'<script>const N={n},NORM={norm};'
                 'function cnt(s){const c=[...document.querySelectorAll(`input[data-s="${s}"]`)].filter(x=>x.checked).length;'
                 'const e=document.getElementById("c"+s);e.textContent=c;e.className=c===NORM?"":"off";}'
                 'function fill(s,clear){const [on,off]=document.getElementById("p"+s).value.split(",").map(Number);'
                 'const k=+document.getElementById("k"+s).value||1;'
                 'document.querySelectorAll(`input[data-s="${s}"]`).forEach(x=>{const d=+x.dataset.d;'
                 'x.checked=!clear&&d>=k&&((d-k)%(on+off))<on;});cnt(s);}'
                 'document.addEventListener("change",e=>{if(e.target.dataset.s)cnt(e.target.dataset.s);});</script>')
        return page(f"График работы {month}", "".join(b))

    @app.post("/schedule/{month}", dependencies=[Depends(auth)])
    async def schedule_save(month: str, request: Request):
        form = await request.form()
        n = service.days_in(month)
        for s in db.sellers():
            db.set_schedule(month, s["id"], {d for d in range(1, n + 1) if form.get(f"s{s['id']}_{d}")})
        return RedirectResponse(f"/schedule/{month}?msg=" + quote("График сохранён"), status_code=303)

    # ------------------------------------------------------------------ продавцы: подключение и увольнение
    def _date(d: str | None) -> str:
        return date.fromisoformat(d).strftime("%d.%m.%Y") if d else "—"

    def _role_select(s) -> str:
        return (f'<select name="role{s["id"]}"><option value=seller {"selected" if s["role"] == "seller" else ""}>'
                f'продавец</option><option value=admin {"selected" if s["role"] == "admin" else ""}>'
                'админ (старший)</option></select>')

    def _fire_btn(s) -> str:
        msg = (f'Уволить {s["name"]}? Бот перестанет присылать сообщения и отвечать ему. '
               'Продажи этого месяца останутся в итогах и ведомости.')
        return (f'<button class=btn name=action value="fire:{s["id"]}" '
                f'onclick="return confirm({html.escape(repr(msg))})">Уволить</button>')

    @app.get("/sellers", dependencies=[Depends(auth)])
    def sellers(msg: str = ""):
        rows = db.sellers(active_only=False)
        waiting = [s for s in rows if s["active"] and not s["max_user_id"]]
        working = [s for s in rows if s["active"] and s["max_user_id"]]
        fired = [s for s in rows if not s["active"]]
        b = [f'<p class=note>{html.escape(msg)}</p>' if msg else "",
             '<p class=note>Новые продавцы появляются здесь сами, когда впервые встречаются в выгрузке 1С. '
             'Можно добавить заранее — тогда первый урок программы новичка придёт сразу после подключения к боту.</p>',
             '<h2>Добавить продавца</h2><form method=post action="/sellers/add"><table>',
             '<tr><td>Фамилия Имя — точно как в 1С</td><td><input type=text name=name style="width:260px" required></td></tr>',
             '<tr><td>Обращение в сообщениях</td><td><input type=text name=first_name style="width:260px" placeholder="по умолчанию — имя из 1С"></td></tr>',
             '<tr><td>Роль</td><td><select name=role><option value=seller>продавец</option>'
             '<option value=admin>админ (старший)</option></select></td></tr>',
             '<tr><td>Если в 1С записан по-другому</td><td><input type=text name=alias style="width:260px"></td></tr>',
             '</table><button class=btn>Добавить и выдать код подключения</button></form>',
             '<form method=post action="/sellers">',
             f'<h2>Ждут подключения к боту ({len(waiting)})</h2>']
        if waiting:
            b.append('<table><tr><th>Продавец (как в 1С)</th><th>Обращение</th><th>Роль</th><th>Первый рабочий день</th>'
                     '<th>Код</th><th></th></tr>')
            for s in waiting:
                first, _ = db.shift_range(s["id"])
                code = (f'<a href="/sellers/{s["id"]}/card">{s["invite_code"]} — карточка с QR</a>' if s["invite_code"]
                        else f'<button class=btn name=action value="invite:{s["id"]}">Выдать код</button>')
                b.append(f'<tr><td>{html.escape(s["name"])}</td>'
                         f'<td><input type=text name="fn{s["id"]}" value="{html.escape(s["first_name"] or "")}"></td>'
                         f'<td>{_role_select(s)}</td><td class=n>{_date(first or s["hired_at"])}</td>'
                         f'<td>{code}</td><td>{_fire_btn(s)}</td></tr>')
            b.append('</table>')
        else:
            b.append('<p class=note>Все работающие продавцы подключены.</p>')
        b.append(f'<h2>Работают ({len(working)})</h2>')
        if working:
            b.append('<table><tr><th>Продавец (как в 1С)</th><th>Обращение</th><th>Роль</th><th>Последний рабочий день</th>'
                     '<th>MAX</th><th></th></tr>')
            for s in working:
                _, last = db.shift_range(s["id"])
                b.append(f'<tr><td>{html.escape(s["name"])}</td>'
                         f'<td><input type=text name="fn{s["id"]}" value="{html.escape(s["first_name"] or "")}"></td>'
                         f'<td>{_role_select(s)}</td><td class=n>{_date(last)}</td>'
                         f'<td>✅ подключён <button class=btn name=action value="invite:{s["id"]}" '
                         'title="Если сменил телефон или аккаунт MAX">Новый код</button></td>'
                         f'<td>{_fire_btn(s)}</td></tr>')
            b.append('</table>')
        if waiting or working:
            b.append('<button class=btn>Сохранить обращение и роли</button>')
        b.append(f'<h2>Уволены ({len(fired)})</h2>')
        if fired:
            b.append('<table><tr><th>Продавец</th><th>Уволен</th><th>Последний рабочий день</th><th></th></tr>')
            for s in fired:
                _, last = db.shift_range(s["id"])
                b.append(f'<tr><td>{html.escape(s["name"])}</td><td class=n>{_date(s["fired_at"])}</td>'
                         f'<td class=n>{_date(last)}</td><td><button class=btn name=action '
                         f'value="restore:{s["id"]}">Вернуть на работу</button></td></tr>')
            b.append('</table>')
        b.append('</form><p class=note>Уволенный не получает сообщений, не участвует в планах и целях со следующего '
                 'дня; его продажи остаются в выторге магазина, а за текущий месяц — в итогах и ведомости.</p>')
        return page("Продавцы: подключение и увольнение", "".join(b))

    @app.post("/sellers", dependencies=[Depends(auth)])
    async def sellers_save(request: Request):
        form = await request.form()
        action = str(form.get("action") or "")
        if ":" in action:
            what, sid = action.split(":", 1)
            sid = int(sid)
            s = db.seller(sid)
            if not s:
                raise HTTPException(404)
            if what == "invite":
                db.new_invite(sid)
                return RedirectResponse(f"/sellers/{sid}/card", status_code=303)
            if what == "fire":
                db.fire(sid)
                return RedirectResponse(f"/sellers?msg={quote(s['name'] + ' уволен(а), бот отключён')}", status_code=303)
            if what == "restore":
                db.restore(sid)
                db.new_invite(sid)
                return RedirectResponse(f"/sellers/{sid}/card", status_code=303)
        with db.tx() as c:
            for s in db.sellers():
                sid = s["id"]
                role = form.get(f"role{sid}", s["role"])
                c.execute("UPDATE sellers SET first_name=?, role=? WHERE id=?",
                          (form.get(f"fn{sid}") or s["first_name"], role if role in ("seller", "admin") else s["role"],
                           sid))
        return RedirectResponse("/sellers?msg=" + quote("Сохранено"), status_code=303)

    @app.post("/sellers/add", dependencies=[Depends(auth)])
    def sellers_add(name: str = Form(...), first_name: str = Form(""), role: str = Form("seller"),
                    alias: str = Form("")):
        try:
            sid = db.add_seller(name, first_name.strip() or None, role if role in ("seller", "admin") else "seller")
        except ValueError as e:
            return RedirectResponse("/sellers?msg=" + quote(f"Не добавлен: {e}. Если он уволен — «Вернуть на работу»."),
                                    status_code=303)
        alias = " ".join(alias.split())
        if alias and not db.seller_id(alias, create=False):
            with db.tx() as c:
                c.execute("INSERT INTO seller_aliases(alias, seller_id) VALUES(?,?)", (alias, sid))
        db.new_invite(sid)
        return RedirectResponse(f"/sellers/{sid}/card", status_code=303)

    @app.get("/sellers/{sid}/card", dependencies=[Depends(auth)])
    def seller_card(sid: int):
        s = db.seller(sid)
        if not s:
            raise HTTPException(404)
        name = html.escape(s["first_name"] or s["name"])
        if s["max_user_id"]:
            body = (f'<p>✅ <b>{html.escape(s["name"])}</b> подключён к боту. Утром после следующего рабочего дня придёт первое '
                    'сообщение.</p><p><a href="/sellers">← к списку продавцов</a></p>')
            return page(f"Подключение: {html.escape(s['name'])}", body)
        if not s["active"] or not s["invite_code"]:
            return RedirectResponse("/sellers", status_code=303)
        code = s["invite_code"]
        b = [f'<p>Покажите эту страницу продавцу <b>{name}</b> или распечатайте её.</p>']
        if cfg.max.bot_link:
            link = f"{cfg.max.bot_link}?start={code}"
            b += [f'<div class=qr>{qr_svg(link)}</div>',
                  '<ol><li>Наведите камеру телефона на QR-код (или откройте ссылку ниже).</li>',
                  '<li>Откроется бот в MAX — нажмите «Начать».</li>',
                  f'<li>Бот ответит «Готово, {name}!» — подключение закончено.</li></ol>',
                  f'<p class=note>Ссылка: {html.escape(link)}</p>',
                  f'<p>Если QR-код не открывается — найдите бота в MAX и отправьте ему код <b class=code>{code}</b>.</p>']
        else:
            b += ['<ol><li>Откройте в MAX бота магазина.</li>',
                  f'<li>Отправьте ему код <b class=code>{code}</b>.</li>',
                  f'<li>Бот ответит «Готово, {name}!» — подключение закончено.</li></ol>',
                  '<p class=note>Чтобы здесь был QR-код, впишите ссылку на бота в config.yaml → max.bot_link.</p>']
        b.append('<p><a href="">Проверить, подключился ли</a> · <a href="/sellers">← к списку продавцов</a></p>')
        return page(f"Подключение: {html.escape(s['name'])}", "".join(b))

    return app
