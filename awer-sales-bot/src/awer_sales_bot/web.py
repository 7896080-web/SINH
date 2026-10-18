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

from . import botcfg, content, health
from . import tasks as tasks_mod
from . import crews as crews_mod
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
table.grid td.alien{background:#eceff3}table.grid td.swap{background:#fde68a}table.grid td.sh{font-size:11px;color:#57606a}
.health{font-size:12px;color:#57606a;margin:4px 0 0}.health.bad{color:#9a3412;font-weight:bold}
"""


def _rub(x):
    return "" if x is None else f"{x:,.0f}".replace(",", " ")


def _num(s: str | None) -> float | None:
    if s is None or str(s).strip() == "":
        return None
    return float(str(s).replace(" ", "").replace("\xa0", "").replace(",", ".").rstrip("%"))


def create_app(cfg: Settings, db: DB, runner=None, max_client=None) -> FastAPI:
    """runner — поток бота в службе (bot.Runner; None, если админка запущена отдельно: awer-bot web);
    max_client — фабрика клиента MAX по токену (тесты подменяют)."""
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
               f'<a href="/bot">Бот MAX</a><a href="/crews">Смены</a><a href="/learn">Обучение</a><a href="/tasks">Задания</a><a href="/schedule/{cur}">График работы</a><a href="/month/{cur}/results">Итоги и ведомость</a></nav>')
        return HTMLResponse(f"<!doctype html><html lang=ru><head><meta charset=utf-8><title>{title}</title>"
                            f"<style>{CSS}</style></head><body>{nav}{health.line(db, cfg)}<h1>{title}</h1>{body}</body></html>")

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

        from .importers.onec_sales import archive_name, import_file
        name = os.path.basename(unquote(request.headers.get("X-Filename", "upload.csv"))) or "upload.csv"
        inbox, archive = cfg.onec.inbox, cfg.onec.archive
        inbox.mkdir(parents=True, exist_ok=True)
        archive.mkdir(parents=True, exist_ok=True)
        path = inbox / f".upload-{secrets.token_hex(4)}-{name}"  # не под итоговым именем: import_inbox не заберёт
        path.write_bytes(await request.body())
        try:
            rep = import_file(path, db, cfg)
        except Exception as e:  # noqa: BLE001 — битый файл в archive/bad, ответ 1С с причиной (аудит В1)
            (archive / "bad").mkdir(exist_ok=True)
            path.replace(archive_name(archive / "bad", name))
            raise HTTPException(422, f"{name}: файл не распознан — {e}") from e
        path.replace(archive_name(archive, name))
        return {"lines": rep.lines, "days": rep.days, "unknown_sellers": sorted(rep.unknown_sellers)}

    # ------------------------------------------------------------------ бот MAX: токены, ссылки, владелец
    def _client(token: str):
        if max_client:
            return max_client(token)
        from .max_api import MaxClient
        return MaxClient(token, cfg.max.base_url, cfg.max.ca_file, timeout=15, pause=0.5)

    def _check(bot: str) -> str:
        """GET /me: имя бота или причина ошибки — в kv для страницы; ссылку на бота подставляем сами."""
        from .max_api import MaxAuthError
        tok = botcfg.token(bot)
        if not tok:
            return ""
        try:
            me = _client(tok).me()
        except MaxAuthError:
            res = "❌ MAX не принял токен — скопируйте его из business.max.ru заново"
        except Exception as e:  # noqa: BLE001
            res = f"❌ нет связи с MAX ({type(e).__name__})"
        else:
            nick = me.get("username") or ""
            if nick and not botcfg.link(cfg, bot):
                botcfg.set_link(db, cfg, bot, f"https://max.ru/{nick}")
            res = f"✅ на связи: {me.get('first_name') or me.get('name') or nick}" + (f" (@{nick})" if nick else "")
        db.set_kv(f"bot_check:{bot}", f"{res} — проверено {date.today():%d.%m}")
        return res

    @app.get("/bot", dependencies=[Depends(auth)])
    def bot_page(msg: str = ""):
        names = {"plan": "«План» — план и показатели", "training": "«Обучение» — уроки, тесты, база знаний"}
        cell = lambda f: "".join(f"<td>{f(b)}</td>" for b in names)  # noqa: E731
        b = [f'<p class=note>{html.escape(msg)}</p>' if msg else "",
             '<p class=note>Токен и ссылку на бота MAX выдаёт после модерации (business.max.ru → бот). Токен — как '
             'пароль: он хранится только в файле .env на этом компьютере, здесь видны последние 4 знака. Второй бот '
             'можно подключить позже — до этого уроки и тесты приходят в «План».</p>',
             '<h2>1. Боты</h2><form method=post action="/bot"><table>',
             '<tr><th></th>' + "".join(f"<th>{t}</th>" for t in names.values()) + '</tr>',
             '<tr><td>Токен</td>' + cell(lambda bt: f'<input type=password name="token_{bt}" autocomplete=off '
                                        f'style="width:260px" placeholder="вставьте токен"><br><span class=note>'
                                        f'{botcfg.mask(botcfg.token(bt))}</span>') + '</tr>',
             '<tr><td>Ссылка на бота</td>' + cell(lambda bt: f'<input type=text name="link_{bt}" style="width:260px" '
                                                 f'value="{html.escape(botcfg.link(cfg, bt))}" '
                                                 'placeholder="заполнится сама после проверки">') + '</tr>',
             '<tr><td>Связь с MAX</td>' + cell(lambda bt: html.escape(db.get_kv(f"bot_check:{bt}") or "—")) + '</tr>',
             '</table><button class=btn name=action value=save>Сохранить и проверить связь</button>']
        if runner and runner.running:
            b.append('<button class=btn name=action value=restart title="Нужно после смены токена">'
                     'Перезапустить службу</button><span class=note> — админка будет недоступна ~15 секунд</span>')
        b.append('</form>')

        own = botcfg.owners(db, cfg)
        b.append('<h2>2. Владелец</h2><p class=note>Владельцу каждое утро — сводка по всем продавцам, «Кого похвалить», '
                 'предупреждения о графике и выгрузке 1С. Продавцы этого не видят.</p>')
        if own:
            b.append('<form method=post action="/bot"><table><tr><th>Имя в MAX</th><th>user_id</th><th>Подключён</th>'
                     '<th></th></tr>')
            for o in own:
                b.append(f'<tr><td>{html.escape(o.get("name") or "—")}</td><td class=n>{o["id"]}</td>'
                         f'<td class=n>{_date((o.get("at") or "")[:10] or None)}</td><td><button class=btn name=action '
                         f'value="owner_del:{o["id"]}">Отключить</button></td></tr>')
            b.append('</table></form>')
        else:
            b.append('<p class=off>Владелец ещё не подключён — сводку получать некому.</p>')
        code = botcfg.owner_code(db)
        if code:
            lk = botcfg.link(cfg, "plan")
            if lk:
                b += [f'<div class=qr>{qr_svg(f"{lk}?start={code}")}</div>',
                      '<p>Наведите камеру телефона на QR-код и нажмите «Начать» в боте «План».</p>',
                      f'<p class=note>Ссылка: {html.escape(f"{lk}?start={code}")}</p>']
            b.append(f'<p>Или отправьте боту «План» код <b class=code>{code}</b>. Код одноразовый, действует сутки.</p>')
        else:
            b.append('<form method=post action="/bot"><button class=btn name=action value=owner_code>'
                     'Подключить владельца</button></form>')

        rows = [s for s in db.sellers() if s["role"] != "system"]
        plan = [s for s in rows if s["max_user_id"]]
        wait = [s for s in rows if not s["max_user_id"]]
        b.append('<h2>3. Продавцы</h2><p class=note>Каждый продавец подключается своим QR-кодом из карточки на странице '
                 '«Продавцы». Бот запоминает его аккаунт MAX и присылает только его личный план и показатели — чужих '
                 'цифр продавец не видит. Рядом с подключённым видно имя из его профиля MAX: проверьте, что подключился '
                 'тот самый человек.</p>')
        line = f'<p>Подключены к «Плану»: <b>{len(plan)} из {len(rows)}</b>'
        if botcfg.token("training"):
            line += f' · к «Обучению»: <b>{len([s for s in rows if s["training_user_id"]])} из {len(rows)}</b>'
        b.append(line + ' · <a href="/sellers">Продавцы →</a></p>')
        if wait:
            b.append('<p class=note>Ещё не подключены: ' + ", ".join(html.escape(s["name"]) for s in wait) + '</p>')
        return page("Бот MAX", "".join(b))

    @app.post("/bot", dependencies=[Depends(auth)])
    async def bot_save(request: Request):
        form = await request.form()
        action = str(form.get("action") or "")
        msg = []
        if action == "owner_code":
            botcfg.new_owner_code(db)
        elif action.startswith("owner_del:"):
            botcfg.remove_owner(db, cfg, int(action.split(":", 1)[1]))
            msg.append("Владелец отключён: сводка ему больше не приходит.")
        elif action == "restart" and runner:
            runner.restart()
            return HTMLResponse('<!doctype html><meta charset=utf-8><meta http-equiv=refresh content="20;url=/bot">'
                                '<p style="font-family:Arial">Служба перезапускается… Страница обновится сама через '
                                '20 секунд.</p>')
        elif action == "save":
            changed = False
            for bot in ("plan", "training"):
                tok = str(form.get(f"token_{bot}") or "").strip()
                if tok and tok != botcfg.token(bot):
                    try:
                        botcfg.set_token(cfg.env_path, bot, tok)
                        changed = True
                    except ValueError as e:
                        msg.append(f"{'«План»' if bot == 'plan' else '«Обучение»'}: {e}.")
                lk = str(form.get(f"link_{bot}") or "").strip()
                if lk != botcfg.link(cfg, bot):
                    if lk and not lk.startswith("https://max.ru/"):
                        msg.append("Ссылка на бота должна начинаться с https://max.ru/ — оставьте поле пустым, "
                                   "она заполнится сама.")
                    else:
                        botcfg.set_link(db, cfg, bot, lk)
                _check(bot)
            if runner and changed:
                msg.append("Бот запущен." if runner.start() else
                           "Новый токен заработает после перезапуска службы — кнопка «Перезапустить службу».")
            elif changed:
                msg.append("Сохранено. Перезапустите службу AwerSalesBot, чтобы бот начал работать с новым токеном.")
            else:
                msg.append("Сохранено.")
        return RedirectResponse(f"/bot?msg={quote(' '.join(msg))}" if msg else "/bot", status_code=303)

    # ------------------------------------------------------------------ смены и график работы
    def _short(store: str) -> str:
        return store[:1].upper()

    @app.get("/crews", dependencies=[Depends(auth)])
    def crews_page(msg: str = ""):
        crews_mod.ensure_crews(db, cfg)
        today = date.today()
        wed = crews_mod.wednesday(today)
        cs = crews_mod.crews(db)
        b = [f'<p class=note>{html.escape(msg)}</p>' if msg else "",
             '<p class=note>Смена — состав продавцов одного магазина. Каждую среду смены меняются магазинами. '
             'Магазин на эту неделю можно поправить — дальше бот чередует сам. Заболел кто-то и его заменили — '
             'это правится в «Графике работы» в конкретном дне, а не здесь.</p>',
             f'<form method=post action="/crews"><h2>Неделя со среды {wed:%d.%m.%Y}</h2><table>'
             '<tr><th>Смена</th><th>Магазин на этой неделе</th><th>Со следующей среды</th></tr>']
        for c in cs:
            now = crews_mod.crew_store(c, today, cfg.stores)
            nxt = crews_mod.crew_store(c, wed + timedelta(days=7), cfg.stores)
            opts = "".join(f'<option {"selected" if st == now else ""}>{html.escape(st)}</option>' for st in cfg.stores)
            b.append(f'<tr><td><input type=text name="name{c["id"]}" value="{html.escape(c["name"])}"></td>'
                     f'<td><select name="store{c["id"]}">{opts}</select></td><td>{html.escape(nxt)}</td></tr>')
        b.append('</table><h2>Состав</h2><table><tr><th>Продавец</th><th>Смена</th>'
                 '<th>Сильный продавец — ставить на сб и вс</th></tr>')
        for sl in db.sellers():
            opts = '<option value="">—</option>' + "".join(
                f'<option value="{c["id"]}" {"selected" if sl["crew_id"] == c["id"] else ""}>{html.escape(c["name"])}</option>'
                for c in cs)
            b.append(f'<tr><td>{html.escape(sl["name"])}</td><td><select name="crew{sl["id"]}">{opts}</select></td>'
                     f'<td><input type=checkbox name="key{sl["id"]}" {"checked" if sl["weekend_key"] else ""}></td></tr>')
        b.append('</table><button class=btn>Сохранить смены</button></form>'
                 f'<p class=note>Минимум в каждом магазине: в будни {cfg.staffing.weekday_min}, в субботу и воскресенье '
                 f'{cfg.staffing.weekend_min} продавца. Проверка — в «Графике работы».</p>')
        return page("Смены", "".join(b))

    @app.post("/crews", dependencies=[Depends(auth)])
    async def crews_save(request: Request):
        form = await request.form()
        wed = crews_mod.wednesday(date.today()).isoformat()
        with db.tx() as c:
            for cr in crews_mod.crews(db):
                name = " ".join(str(form.get(f"name{cr['id']}", cr["name"])).split()) or cr["name"]
                store = form.get(f"store{cr['id']}")
                store = store if store in cfg.stores else crews_mod.crew_store(cr, date.today(), cfg.stores)
                c.execute("UPDATE crews SET name=?, store=?, anchor=? WHERE id=?", (name, store, wed, cr["id"]))
            for sl in db.sellers():
                crew = form.get(f"crew{sl['id']}")
                c.execute("UPDATE sellers SET crew_id=?, weekend_key=? WHERE id=?",
                          (int(crew) if crew and str(crew).isdigit() else None,
                           1 if form.get(f"key{sl['id']}") else 0, sl["id"]))
        return RedirectResponse("/crews?msg=" + quote("Сохранено"), status_code=303)

    @app.get("/schedule/{month}", dependencies=[Depends(auth)])
    def schedule(month: str, msg: str = ""):
        """График по магазинам: в каждой таблице — кто работает в этом магазине; смены меняются магазинами по средам."""
        crews_mod.ensure_crews(db, cfg)
        e = html.escape
        y, mo = map(int, month.split("-"))
        n = service.days_in(month)
        dates = [date(y, mo, d) for d in range(1, n + 1)]
        saved = db.schedule_days(month)
        cs = crews_mod.crews(db)
        cnum = {c["id"]: i for i, c in enumerate(cs, 1)}
        sellers = sorted(db.sellers(), key=lambda s: (cnum.get(s["crew_id"], 99), s["name"]))
        own = {(s["id"], d.day): crews_mod.default_store(db, cfg, s["id"], d) for s in sellers for d in dates}
        where = {(sid, d): (st or own[(sid, d)] or cfg.stores[0]) for sid, ds in saved.items() for d, st in ds.items()}
        we = lambda d: " we" if d.weekday() >= 5 else ""  # noqa: E731
        probs = crews_mod.problems(db, cfg, dates[0], dates[-1])
        b = [f'<p><a href="/schedule/{month_add(month, -1)}">← {month_add(month, -1)}</a> · '
             f'<a href="/schedule/{month_add(month, 1)}">{month_add(month, 1)} →</a> · <a href="/crews">Смены</a></p>',
             f'<p class=note>{e(msg)}</p>' if msg else "",
             '<p class=note>Отдельная таблица на каждый магазин. Строка «смена» — какая смена в этом магазине: каждую '
             'среду смены меняются магазинами. Белые клетки — дни, когда смена продавца в этом магазине; серые — '
             'другой магазин: галочка там — выход на замену (выделяется цветом). Один продавец в один день — только '
             f'в одном магазине. Норма — {cfg.norm_shifts} рабочих дней. Отработанным считается день с продажами в 1С.</p>']
        if probs:
            b.append('<p class=off>Нарушены правила графика:</p><ul>' + "".join(f"<li>{e(x)}</li>" for x in probs) + "</ul>")
        b.append(f'<form method=post action="/schedule/{month}">')
        head = "".join(f'<th class="{we(d)}">{d.day}<br>{crews_mod.WD[d.weekday()]}</th>' for d in dates)
        for si, st in enumerate(cfg.stores):
            shifts = "".join(
                f'<td class="sh{we(d)}">' + ", ".join(f"С{cnum[c['id']]}" for c in cs
                                                      if crews_mod.crew_store(c, d, cfg.stores) == st) + "</td>"
                for d in dates)
            b.append(f'<h2>{e(st)}</h2><table class=grid><tr><th>Продавец</th>{head}</tr>'
                     f'<tr><td class=nm><span class=note>смена</span></td>{shifts}</tr>')
            for sl in sellers:
                sid = sl["id"]
                cells = []
                for d in dates:
                    mine = own[(sid, d.day)] == st
                    on = where.get((sid, d.day)) == st
                    cls = ("" if mine else "alien") + we(d) + (" swap" if on and not mine else "")
                    cells.append(f'<td class="{cls.strip()}"><input type=checkbox name="w{sid}_{d.day}_{si}" '
                                 f'data-s="{sid}" data-d="{d.day}" data-st="{si}" data-own="{int(mine)}" '
                                 f'data-we="{int(d.weekday() >= 5)}" {"checked" if on else ""}></td>')
                crew = f"С{cnum[sl['crew_id']]}" if sl["crew_id"] in cnum else "без смены"
                b.append(f'<tr><td class=nm>{e(sl["name"])} <span class=note>{crew}'
                         f'{" · сильный" if sl["weekend_key"] else ""}</span></td>{"".join(cells)}</tr>')
            cnt = "".join(f'<td id="n{si}_{d.day}" class="{we(d).strip()}" data-need="{crews_mod.minimum(cfg, d)}"></td>'
                          for d in dates)
            b.append(f'<tr><td class=nm><b>Продавцов</b> <span class=note>(нужно {cfg.staffing.weekday_min} в будни, '
                     f'{cfg.staffing.weekend_min} в сб и вс)</span></td>{cnt}</tr></table>')
        b.append('<h2>Итого по продавцам</h2><table><tr><th>Продавец</th><th>Смена</th><th>Рабочих дней</th>'
                 + "".join(f"<th>{e(st)}</th>" for st in cfg.stores) + '<th>Заполнить по схеме (с учётом ротации)</th></tr>')
        for sl in sellers:
            sid = sl["id"]
            crew = next((c["name"] for c in cs if c["id"] == sl["crew_id"]), "без смены")
            fill = (f'<select id="p{sid}"><option value="3,2">3/2</option><option value="2,2">2/2</option>'
                    f'<option value="5,2">5/2</option></select> с <input type=number id="k{sid}" value=1 min=1 max=7 '
                    f'style="width:42px">-го <button type=button class=btn onclick="fill({sid})">Заполнить</button>'
                    f'<button type=button class=btn onclick="weekends({sid})">+ сб и вс</button>'
                    f'<button type=button class=btn onclick="fill({sid},true)">Очистить</button>')
            b.append(f'<tr><td>{e(sl["name"])}{" · сильный" if sl["weekend_key"] else ""}</td><td>{e(crew)}</td>'
                     f'<td id="t{sid}" class=n></td>' + "".join(f'<td id="t{sid}_{si}" class=n></td>'
                                                              for si in range(len(cfg.stores))) + f'<td>{fill}</td></tr>')
        b.append(f'</table><button class=btn>Сохранить график</button></form>'
                 f'<script>const NORM={cfg.norm_shifts},NS={len(cfg.stores)};'
                 'const Q=s=>[...document.querySelectorAll(s)];'
                 'function count(){Q("[id^=n]").forEach(c=>{const [si,d]=c.id.slice(1).split("_");'
                 'const k=Q(`input[data-st="${si}"][data-d="${d}"]`).filter(x=>x.checked).length;'
                 'c.textContent=k;c.className=(c.className.replace(/ ?off/g,""))+(k<+c.dataset.need?" off":"");});'
                 'Q("[id^=t]").forEach(c=>{const p=c.id.slice(1).split("_");const sel=p.length>1?'
                 '`input[data-s="${p[0]}"][data-st="${p[1]}"]`:`input[data-s="${p[0]}"]`;'
                 'const k=Q(sel).filter(x=>x.checked).length;c.textContent=k;'
                 'if(p.length===1)c.className="n"+(k===NORM?"":" off");});}'
                 'function fill(s,clear){const [on,off]=document.getElementById("p"+s).value.split(",").map(Number);'
                 'const k=+document.getElementById("k"+s).value||1;'
                 'Q(`input[data-s="${s}"]`).forEach(x=>{const d=+x.dataset.d;'
                 'x.checked=!clear&&x.dataset.own==="1"&&d>=k&&((d-k)%(on+off))<on;mark(x);});count();}'
                 'function weekends(s){const busy=new Set(Q(`input[data-s="${s}"]`).filter(x=>x.checked).map(x=>x.dataset.d));'
                 'Q(`input[data-s="${s}"][data-we="1"][data-own="1"]`).forEach(x=>{if(!busy.has(x.dataset.d)){x.checked=true;mark(x);}});count();}'
                 'function mark(x){const td=x.parentElement;td.classList.toggle("swap",x.checked&&x.dataset.own!=="1");}'
                 'document.addEventListener("change",e=>{const x=e.target;if(!x.dataset||!x.dataset.s)return;'
                 'if(x.checked)Q(`input[data-s="${x.dataset.s}"][data-d="${x.dataset.d}"]`).forEach(y=>{if(y!==x){y.checked=false;mark(y);}});'
                 'mark(x);count();});count();</script>')
        return page(f"График работы {month}", "".join(b))

    @app.post("/schedule/{month}", dependencies=[Depends(auth)])
    async def schedule_save(month: str, request: Request):
        form = await request.form()
        n = service.days_in(month)
        y, mo = map(int, month.split("-"))
        for sl in db.sellers():
            days = {}
            for d in range(1, n + 1):
                picked = [st for si, st in enumerate(cfg.stores) if form.get(f"w{sl['id']}_{d}_{si}")]
                if picked:  # магазин храним, только если это не магазин его смены (замена)
                    dflt = crews_mod.default_store(db, cfg, sl["id"], date(y, mo, d))
                    st = dflt if dflt in picked else picked[0]
                    days[d] = None if st == dflt else st
            db.set_schedule(month, sl["id"], days)
        return RedirectResponse(f"/schedule/{month}?msg=" + quote("График сохранён"), status_code=303)

    # ------------------------------------------------------------------ обучение: правка контента
    def _learn_tabs(kind: str) -> str:
        return "<p>" + " · ".join(f'<b>{t}</b>' if k == kind else f'<a href="/learn?kind={k}">{t}</a>'
                                  for k, t in content.KINDS.items()) + "</p>"

    @app.get("/learn", dependencies=[Depends(auth)])
    def learn(kind: str = "lesson", msg: str = ""):
        if kind not in content.KINDS:
            raise HTTPException(404)
        rows = content.items(db, cfg, kind, active_only=False)
        b = [_learn_tabs(kind), f'<p class=note>{html.escape(msg)}</p>' if msg else "",
             '<p class=note>Тексты можно менять здесь — исправленное больше не перезаписывается стартовыми файлами. '
             'Черновики новых уроков, статей и тестов может написать Claude (скилл «Продажи в магазине»): вы проверяете '
             'и включаете. Выключенное не рассылается.</p>',
             f'<p><a class=btn href="/learn/new?kind={kind}">+ Добавить</a></p>'
             '<form method=post action="/learn/sync" style="display:inline"><button class=btn>Подтянуть новые тексты '
             'из файлов</button></form>',
             '<table><tr><th>Название</th><th>Тема / раздел</th><th>Включено</th><th>Отправлено / ответы</th>'
             '<th>Кем изменено</th></tr>']
        for x in rows:
            sent = db.one("SELECT COUNT(*) n FROM lessons_sent WHERE lesson_id=?",
                          x["id"] if kind != "onboarding" else f"onb-{x['data'].get('shift')}")["n"]
            q = db.one("SELECT COUNT(*) n, COALESCE(SUM(correct),0) ok FROM quiz_answers WHERE lesson_id=?", x["id"])
            stat = f"{sent}" + (f" · квиз {q['ok']} из {q['n']} верно" if q["n"] else "")
            if kind == "article":
                v = db.one("SELECT COUNT(*) n, COUNT(DISTINCT seller_id) p FROM kb_views WHERE article_id=?", x["id"])
                stat = f"открыли {v['n']} раз ({v['p']} чел.)"
            topic = (content.LESSON_TOPICS.get(x["topic"]) or content.ARTICLE_SECTIONS.get(x["topic"]) or
                     (f"рабочий день {x['data'].get('shift')}" if kind == "onboarding" else ""))
            b.append(f'<tr><td><a href="/learn/{quote(x["id"])}">{html.escape(x["title"])}</a></td>'
                     f'<td>{html.escape(topic or "")}</td><td>{"да" if x["active"] else "<span class=off>нет</span>"}'
                     f'</td><td class=n>{stat}</td><td>{"вы (админка)" if x["origin"] == "admin" else "стартовый файл"}'
                     f'</td></tr>')
        b.append("</table>")
        return page(f"Обучение: {content.KINDS[kind]}", "".join(b))

    @app.post("/learn/sync", dependencies=[Depends(auth)])
    def learn_sync():
        n = content.sync_from_files(db, cfg, force=True)
        return RedirectResponse("/learn?msg=" + quote(f"Из файлов добавлено или обновлено: {n}"), status_code=303)

    def _learn_form(x: dict, err: str = "") -> HTMLResponse:
        kind = x["kind"]
        e = html.escape
        d = x.get("data") or {}
        f = [_learn_tabs(kind), f'<p class=off>{e(err)}</p>' if err else "",
             f'<form method=post action="/learn/{quote(x["id"])}"><input type=hidden name=kind value="{kind}"><table>',
             f'<tr><td>Название</td><td><input type=text name=title style="width:520px" value="{e(x.get("title") or "")}"></td></tr>']
        if kind == "lesson":
            opts = "".join(f'<option value="{k}" {"selected" if x.get("topic") == k else ""}>{t}</option>'
                           for k, t in content.LESSON_TOPICS.items())
            f.append(f'<tr><td>Тема (по цели недели)</td><td><select name=topic>{opts}</select></td></tr>')
        if kind == "article":
            opts = "".join(f'<option value="{k}" {"selected" if x.get("topic") == k else ""}>{t}</option>'
                           for k, t in content.ARTICLE_SECTIONS.items())
            f.append(f'<tr><td>Раздел</td><td><select name=topic>{opts}</select></td></tr>')
        if kind == "onboarding":
            f.append(f'<tr><td>Рабочий день новичка №</td><td><input type=number name=shift min=1 max=60 '
                     f'value="{d.get("shift") or 1}"></td></tr>')
        if kind != "checklist":
            f.append(f'<tr><td>Текст</td><td><textarea name=body rows=12 cols=90>{e(x.get("body") or "")}</textarea>'
                     '<br><span class=note>Без символов * и _ (разметка MAX). Фразы для покупателя — в «ёлочках».</span></td></tr>')
        if kind in ("lesson", "onboarding"):
            f.append(f'<tr><td>Попробуйте сегодня</td><td><input type=text name=try_today style="width:520px" '
                     f'value="{e(x.get("try_today") or "")}"></td></tr>')
        if kind == "article":
            f.append(f'<tr><td>Слова для поиска</td><td><input type=text name=tags style="width:520px" '
                     f'value="{e(", ".join(d.get("tags") or []))}"></td></tr>')
        if kind == "test":
            f.append(f'<tr><td>Статьи перед тестом (id через запятую)</td><td><input type=text name=article_ids '
                     f'style="width:520px" value="{e(", ".join(d.get("article_ids") or []))}"></td></tr>')
        if kind in ("lesson", "test", "survey"):
            qs = d.get("quiz") if kind == "lesson" else d.get("questions")
            hint = ("? вопрос · - вариант · ~ свой ответ (можно ответить своими словами)" if kind == "survey" else
                    "? вопрос · + верный вариант · - неверный · = объяснение")
            f.append(f'<tr><td>{"Квиз после урока" if kind == "lesson" else "Вопросы"}</td><td>'
                     f'<textarea name=questions rows=14 cols=90>'
                     f'{e(x.get("questions_raw") or content.format_questions(qs or [], survey=kind == "survey"))}'
                     f'</textarea><br><span class=note>{hint}</span></td></tr>')
        if kind == "checklist":
            f.append(f'<tr><td>Пункты (по одному в строке)</td><td><textarea name=items rows=12 cols=90>'
                     f'{e(chr(10).join(d.get("items") or []))}</textarea></td></tr>')
        f.append(f'<tr><td>Включено</td><td><input type=checkbox name=active {"checked" if x.get("active", 1) else ""}>'
                 '</td></tr></table><button class=btn>Сохранить</button></form>'
                 f'<p class=note>id: {e(x["id"])} — не меняется (по нему бот помнит, что уже отправлял).</p>')
        return page(f"{content.KINDS[kind]}: {e(x.get('title') or 'новый')}", "".join(f))

    @app.get("/learn/new", dependencies=[Depends(auth)])
    def learn_new(kind: str = "lesson"):
        if kind not in content.KINDS:
            raise HTTPException(404)
        return _learn_form({"id": content.new_id(db, kind), "kind": kind, "active": 1,
                            "topic": "general" if kind == "lesson" else ("tkani" if kind == "article" else None)})

    @app.get("/learn/{cid}", dependencies=[Depends(auth)])
    def learn_edit(cid: str):
        x = content.get(db, cfg, cid)
        if not x:
            raise HTTPException(404)
        return _learn_form(x)

    @app.post("/learn/{cid}", dependencies=[Depends(auth)])
    async def learn_save(cid: str, request: Request):
        form = await request.form()
        kind = str(form.get("kind") or "")
        if kind not in content.KINDS:
            raise HTTPException(400)
        old = content.get(db, cfg, cid) or {}
        title = " ".join(str(form.get("title") or "").split())
        body = str(form.get("body") or "").replace("\r\n", "\n").strip() or None
        data = dict(old.get("data") or {})
        x = {"id": cid, "kind": kind, "title": title, "body": body, "topic": form.get("topic") or None,
             "try_today": str(form.get("try_today") or "").strip() or None, "active": 1 if form.get("active") else 0,
             "data": data}
        try:
            if not title:
                raise ValueError("Нужно название")
            if any(ch in (title + (body or "") + (x["try_today"] or "")) for ch in "*_"):
                raise ValueError("Символы * и _ ломают разметку MAX — уберите их")
            if kind in ("lesson", "test", "survey"):
                raw = str(form.get("questions") or "")
                x["questions_raw"] = raw
                qs = content.parse_questions(raw, survey=kind == "survey")
                data["quiz" if kind == "lesson" else "questions"] = qs
                if kind in ("test", "survey") and not qs:
                    raise ValueError("Нужен хотя бы один вопрос")
            if kind == "onboarding":
                data["shift"] = int(form.get("shift") or 1)
            if kind == "article":
                data["tags"] = [t.strip().lower() for t in str(form.get("tags") or "").split(",") if t.strip()]
            if kind == "test":
                data["article_ids"] = [t.strip() for t in str(form.get("article_ids") or "").split(",") if t.strip()]
            if kind == "checklist":
                items_ = [i.strip() for i in str(form.get("items") or "").splitlines() if i.strip()]
                if not items_:
                    raise ValueError("Нужен хотя бы один пункт")
                if any(len(i) > 120 for i in items_):
                    raise ValueError("Пункт длиннее 120 символов — кнопка не поместится")
                data["items"] = items_
        except ValueError as err:
            return _learn_form(x, str(err))
        content.save(db, cid, kind, title, body, x["try_today"], x["topic"], data, bool(x["active"]))
        return RedirectResponse(f"/learn?kind={kind}&msg=" + quote(f"Сохранено: {title}"), status_code=303)

    # ------------------------------------------------------------------ задания: тесты, опросы, чек-листы
    @app.get("/tasks", dependencies=[Depends(auth)])
    def tasks_page(msg: str = ""):
        crews_mod.ensure_crews(db, cfg)
        e = html.escape
        cs = db.q("SELECT * FROM campaigns ORDER BY active DESC, id DESC")
        quiet = tasks_mod.quiet_hours(db, date.today())
        b = [f'<p class=note>{e(msg)}</p>' if msg else "",
             '<p class=note>Тесты, опросы и чек-листы приходят в бот «Обучение» в тестовые часы — только тем, у кого '
             'сегодня рабочий день по графику. Тексты — на странице «Обучение». Рейтинг по результатам — '
             '<a href="/rating">информативный</a>, на ставку не влияет.</p>',
             '<h2>Рассылки</h2><table><tr><th>Что</th><th>Кому</th><th>Когда</th><th>Часы</th><th>Получили</th>'
             '<th>Выполнили</th><th>Включена</th></tr>']
        for c in cs:
            x = content.get(db, cfg, c["content_id"])
            st = db.one("SELECT COUNT(*) n, SUM(done_at IS NOT NULL) d FROM deliveries WHERE campaign_id=?", c["id"])
            a, z = tasks_mod.hours(cfg, c)
            when = tasks_mod.REPEATS[c["repeat"]] + (
                f" ({['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'][c['weekday']]})" if c["repeat"] == "weekly" and
                c["weekday"] is not None else "") + f", с {date.fromisoformat(c['start_day']):%d.%m}"
            b.append(f'<tr><td><a href="/tasks/{c["id"]}">{e(content.KINDS[x["kind"]][:-1] if x else "?")}: '
                     f'{e(x["title"] if x else c["content_id"])}</a></td><td>{e(tasks_mod.audience_label(db, c["audience"]))}'
                     f'</td><td>{e(when)}</td><td>{a:%H:%M}–{z:%H:%M}</td><td class=n>{st["n"]}</td>'
                     f'<td class=n>{st["d"] or 0}</td><td>{"да" if c["active"] else "нет"}</td></tr>')
        b.append("</table>")
        opts = "".join(f'<option value="{e(x["id"])}">{e(content.KINDS[x["kind"]])}: {e(x["title"])}</option>'
                       for k in ("test", "survey", "checklist") for x in content.items(db, cfg, k))
        aud = "".join(f'<option value="{k}">{v}</option>' for k, v in tasks_mod.AUDIENCES.items())
        aud += "".join(f'<option value="store:{e(st)}">магазин «{e(st)}» (кто сегодня там)</option>' for st in cfg.stores)
        aud += "".join(f'<option value="crew:{c["id"]}">смена «{e(c["name"])}»</option>' for c in crews_mod.crews(db))
        rep = "".join(f'<option value="{k}">{v}</option>' for k, v in tasks_mod.REPEATS.items())
        wd = "".join(f'<option value="{i}">{n}</option>' for i, n in enumerate(crews_mod.WD))
        a, z = tasks_mod.hours(cfg)
        hint = (f" Самые тихие часы по чекам 1С: {', '.join(f'{h}:00–{h + 1}:00' for h in quiet)}." if quiet else "")
        b += ['<h2>Новая рассылка</h2><form method=post action="/tasks"><table>',
              f'<tr><td>Что</td><td><select name=content_id>{opts}</select></td></tr>',
              f'<tr><td>Кому</td><td><select name=audience>{aud}</select></td></tr>',
              f'<tr><td>Когда</td><td><select name=repeat>{rep}</select> день недели (для «раз в неделю»): '
              f'<select name=weekday>{wd}</select></td></tr>',
              f'<tr><td>Начиная с</td><td><input type=date name=start_day value="{date.today().isoformat()}"></td></tr>',
              f'<tr><td>Тестовые часы</td><td>с <input type=time name=hour_from value="{a:%H:%M}"> до '
              f'<input type=time name=hour_to value="{z:%H:%M}"><span class=note>{hint}</span></td></tr>',
              '<tr><td>Только в рабочий день по графику</td><td><input type=checkbox name=only_working checked></td></tr>',
              '</table><button class=btn>Создать рассылку</button></form>']
        return page("Задания: тесты, опросы, чек-листы", "".join(b))

    @app.post("/tasks", dependencies=[Depends(auth)])
    def tasks_create(content_id: str = Form(...), audience: str = Form("all"), repeat: str = Form("once"),
                     weekday: int = Form(0), start_day: str = Form(""), hour_from: str = Form(""),
                     hour_to: str = Form(""), only_working: str = Form("")):
        x = content.get(db, cfg, content_id)
        if not x or x["kind"] not in ("test", "survey", "checklist") or repeat not in tasks_mod.REPEATS:
            raise HTTPException(400)
        with db.tx() as c:
            c.execute("INSERT INTO campaigns(content_id,audience,repeat,start_day,weekday,hour_from,hour_to,only_working,"
                      "active,created_at) VALUES(?,?,?,?,?,?,?,?,1,?)",
                      (content_id, audience, repeat, start_day or date.today().isoformat(), weekday,
                       hour_from or None, hour_to or None, 1 if only_working else 0, date.today().isoformat()))
        return RedirectResponse("/tasks?msg=" + quote(f"Рассылка создана: {x['title']}"), status_code=303)

    @app.get("/tasks/{cid}", dependencies=[Depends(auth)])
    def task_results(cid: int):
        e = html.escape
        c = db.one("SELECT * FROM campaigns WHERE id=?", cid)
        if not c:
            raise HTTPException(404)
        x = content.get(db, cfg, c["content_id"]) or {"title": c["content_id"], "kind": "test", "data": {}}
        qs = x["data"].get("questions") or [{"q": it} for it in x["data"].get("items") or []]
        b = [f'<p>{e(tasks_mod.audience_label(db, c["audience"]))} · {e(tasks_mod.REPEATS[c["repeat"]])} · '
             f'<a href="/learn/{quote(c["content_id"])}">текст</a></p>',
             f'<form method=post action="/tasks/{cid}/toggle"><button class=btn>'
             f'{"Выключить рассылку" if c["active"] else "Включить рассылку"}</button></form>',
             '<table><tr><th>Продавец</th><th>День</th><th>Выполнено</th><th>Результат</th>'
             + "".join(f'<th>{e(q["q"][:40])}</th>' for q in qs) + '</tr>']
        for d in db.q("SELECT d.*, s.name FROM deliveries d JOIN sellers s ON s.id=d.seller_id WHERE campaign_id=? "
                      "ORDER BY day DESC, s.name", cid):
            ans = {r["q"]: r for r in db.q("SELECT * FROM answers WHERE campaign_id=? AND seller_id=? AND day=?",
                                           cid, d["seller_id"], d["day"])}
            cells = []
            for i, q in enumerate(qs):
                r = ans.get(i)
                if r is None:
                    cells.append("<td></td>")
                elif x["kind"] == "test":
                    cells.append(f'<td>{"✅" if r["correct"] else "✗ " + e(q["options"][r["answer"]][:30])}</td>')
                elif x["kind"] == "checklist":
                    cells.append("<td>✅</td>")
                else:
                    cells.append(f'<td>{e((r["text"] or "")[:200])}</td>')
            score = "" if d["score"] is None else f'{d["score"]:.0%}'
            b.append(f'<tr><td>{e(d["name"])}</td><td>{date.fromisoformat(d["day"]):%d.%m}</td>'
                     f'<td>{"да" if d["done_at"] else "нет"}</td><td class=n>{score}</td>{"".join(cells)}</tr>')
        b.append("</table>")
        return page(f"{content.KINDS[x['kind']][:-1]}: {e(x['title'])}", "".join(b))

    @app.post("/tasks/{cid}/toggle", dependencies=[Depends(auth)])
    def task_toggle(cid: int):
        with db.tx() as c:
            c.execute("UPDATE campaigns SET active=1-active WHERE id=?", (cid,))
        return RedirectResponse(f"/tasks/{cid}", status_code=303)

    @app.get("/rating", dependencies=[Depends(auth)])
    def rating_page(month: str = ""):
        month = month or date.today().strftime("%Y-%m")
        pct_ = lambda v: "" if v is None else f"{v:.0%}"  # noqa: E731
        b = [f'<p><a href="/rating?month={month_add(month, -1)}">← {month_add(month, -1)}</a> · '
             f'<a href="/rating?month={month_add(month, 1)}">{month_add(month, 1)} →</a></p>',
             '<p class=note>Информативно, на ставку и ЗП не влияет (решение владельца). Итог — среднее из: тесты '
             '(доля верных), квизы после уроков, задания (выполнено из полученных). Продавцам не показывается.</p>',
             '<table><tr><th>#</th><th>Продавец</th><th>Итог</th><th>Тесты</th><th>Квизы</th><th>Задания</th></tr>']
        for i, r in enumerate(tasks_mod.rating(db, month), 1):
            b.append(f'<tr><td>{i}</td><td>{html.escape(r["name"])}</td><td class=n><b>{pct_(r["score"])}</b></td>'
                     f'<td class=n>{pct_(r["test"])}</td><td class=n>{pct_(r["quiz"])}</td>'
                     f'<td class=n>{r["done"]} из {r["sent"]}</td></tr>')
        b.append("</table>")
        return page(f"Рейтинг по обучению {month}", "".join(b))

    # ------------------------------------------------------------------ продавцы: подключение и увольнение
    def _date(d: str | None) -> str:
        return date.fromisoformat(d).strftime("%d.%m.%Y") if d else "—"

    def _role_select(s) -> str:
        return (f'<select name="role{s["id"]}"><option value=seller {"selected" if s["role"] == "seller" else ""}>'
                f'продавец</option><option value=admin {"selected" if s["role"] == "admin" else ""}>'
                'админ (старший)</option></select>')

    def _max_name(s) -> str:
        """Имя из профиля MAX при подключении — владелец сверяет, что подключился тот самый человек."""
        return f' <span class=note>(в MAX: {html.escape(s["max_name"])})</span>' if s["max_name"] else ""

    def _training_mark(s) -> str:
        if not cfg.training_token:
            return ""
        if s["training_user_id"]:
            return " · обучение ✅"
        return f' · <a href="/sellers/{s["id"]}/card">обучение — подключить</a>'

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
                         f'<td>✅ подключён{_max_name(s)}{_training_mark(s)} <button class=btn name=action value="invite:{s["id"]}" '
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
        back = '<p><a href="">Проверить, подключился ли</a> · <a href="/sellers">← к списку продавцов</a></p>'
        if s["max_user_id"] and cfg.training_token and not s["training_user_id"] and s["active"]:
            code = s["training_code"] or db.new_training_code(sid)
            b = [f'<p>✅ Бот «План» подключён. Осталось подключить бот «Обучение» — уроки, тесты и база знаний.</p>',
                 '<p class=note>Бот «План» уже прислал продавцу кнопку «Подключить обучение». Если кнопки нет — '
                 'покажите этот QR-код.</p>']
            if cfg.max_training.bot_link:
                link = f"{cfg.max_training.bot_link}?start={code}"
                b += [f'<div class=qr>{qr_svg(link)}</div>', f'<p class=note>Ссылка: {html.escape(link)}</p>']
            b += [f'<p>Или найдите бот «Обучение» в MAX и отправьте ему код <b class=code>{code}</b>.</p>', back]
            return page(f"Подключение к обучению: {html.escape(s['name'])}", "".join(b))
        if s["max_user_id"]:
            both = " и к боту «Обучение»" if cfg.training_token else ""
            body = (f'<p>✅ <b>{html.escape(s["name"])}</b> подключён к боту «План»{both}{_max_name(s)}. Утром после '
                    'следующего рабочего дня придёт первое сообщение.</p><p><a href="/sellers">← к списку продавцов</a></p>')
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
                  '<p class=note>Чтобы здесь был QR-код, подключите бота на странице <a href="/bot">«Бот MAX»</a>.</p>']
        if cfg.training_token:
            b.append('<p class=note>После подключения бот «План» пришлёт кнопку «Подключить обучение» — нажать её '
                     'и «Начать» во втором боте.</p>')
        b.append(back)
        return page(f"Подключение: {html.escape(s['name'])}", "".join(b))

    return app
