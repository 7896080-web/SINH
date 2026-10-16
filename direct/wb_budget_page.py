# -*- coding: utf-8 -*-
"""
Страница «Бюджеты WB»: недельные потолки пополнений, пополнения в сутки и дневные лимиты кампаний — с номером,
названием и артикулами каждой кампании (запрос пользователя 16.10.2026: «страница корректировки недельных бюджетов
по кампаниям; для идентификации помимо id — артикулы, которые включены в кампанию»).

Как работает:
  • запускает маленький веб-сервер только для этого компьютера (127.0.0.1, случайный порт, одноразовый ключ в адресе —
    снаружи и без ключа страницу не открыть) и открывает её в браузере;
  • кампании и их артикулы — из WB (только чтение: список кампаний), артикулы продавца и названия товаров —
    из wb_history.db, пополнения за 7 дней — из wb_bids_state.json;
  • править можно кампании под управлением автопилота (RULES в wb_bids.py, с «*» — все кампании кабинета);
    остальные показаны для справки;
  • «Проверить» — план: что было → что станет и предупреждения (ничего не меняет); «Сохранить» — запись в
    wb_budget_overrides.json (тот же файл, что у wb_budget.py, поверх RULES) с резервной копией в wb_budget_backups;
    «Отменить последнее сохранение» — вернуть файл как до последней записи (своей или wb_budget.py), сразу.
  • Кампании, поправленные здесь, wb_budget.py больше не меняет (ручное решение важнее автоматики).
Когда действует: снижение потолков пополнений — сразу (часовая проверка берёт меньшее); повышение — с ближайшего
запуска автопилота (8:00, 13:00, 18:00); дневной лимит кампаний за клики WB получит при ближайшем запуске автопилота;
свой лимит (показы; клики ниже 1 000 ₽) — частая проверка в течение 5 минут. Деньги здесь не переводятся.

Запуск:  python wb_budget_page.py                (на сервере — ярлык «Бюджеты WB» на рабочем столе)
         python wb_budget_page.py --browser "C:\\путь\\к\\браузеру.exe"
Страница сама закрывается через 30 минут без действий или вскоре после закрытия вкладки.
Нужна категория токена «Продвижение» (только чтение списка кампаний). Журнал записей — wb_budget_page_log.txt.
"""
import datetime as dt
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import wb_bids as B

# ---------------- НАСТРОЙКИ ----------------
IDLE_MIN = 30                      # без запросов столько минут — страница закрывается
FIELDS = ("daily_limit", "topup_day_cap", "topup_week_cap")      # то же, что правит wb_budget.py (B.BUDGET_KEYS)
MAX_VALUE = 300000                 # ₽ — больше в одно поле не принимаю (защита от лишнего нуля)
EXPERIMENT = {"Ребрик": "2026-10-27", "Караман": "2026-10-27"}   # предупреждать: деньги влияют на сравнение
# -------------------------------------------
HERE = B.HERE
LOCK = HERE / "wb_budget_page.running"
LOG = HERE / "wb_budget_page_log.txt"
BACKUPS = HERE / "wb_budget_backups"           # те же копии, что у wb_budget.py: откат общий
OVERRIDES = B.BUDGET_OVERRIDES
LABEL = {"daily_limit": "дневной лимит", "topup_day_cap": "пополнения в сутки", "topup_week_cap": "недельный потолок",
         "_week_budget": "недельный бюджет кабинета"}
PAY = {"cpm": "показы", "cpc": "клики"}
STATUS = {9: "работает", 11: "пауза", 4: "готова к запуску"}
_rules_lock = threading.Lock()
_write_lock = threading.Lock()


def ddmm(iso):
    return f"{iso[8:10]}.{iso[5:7]}" if iso and len(iso) >= 10 else (iso or "")


def rub(v):
    return "—" if v is None else B.fm_rub(v)


def load_overrides():
    try:
        return json.loads(OVERRIDES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"items": {}, "raised": {}}


def rules_now():
    """RULES с поправками из файла — как их увидит следующий запуск автопилота и проверок."""
    rules = json.loads(json.dumps(B.RULES_BASE))
    B.apply_budget_overrides(rules)
    return rules


def rule_of(rules, cab, cid, camps):
    """B.live_rule по заданным правилам (B.RULES подменяется на время вызова)."""
    with _rules_lock:
        saved = B.RULES
        B.RULES = rules
        try:
            return B.live_rule(cab, cid, camps)
        finally:
            B.RULES = saved


def products(cab):
    """{nm: (артикул продавца, название)} из wb_history.db (только чтение)."""
    if not B.HISTORY_DB.exists():
        return {}
    try:
        con = sqlite3.connect(B.HISTORY_DB.as_uri() + "?mode=ro", uri=True, timeout=5)
        rows = con.execute("SELECT nm, vendor, title FROM products WHERE cab=?", (cab,)).fetchall()
        con.close()
        return {int(n): (v or "", t or "") for n, v, t in rows}
    except sqlite3.Error:
        return {}


def flags(state, cid):
    out = []
    if B.limit_paused(state, cid):
        out.append("пауза по дневному лимиту")
    if B.pace_paused(state, cid):
        out.append("пауза по графику расхода")
    if B.on_hold(cid):
        out.append("в стоп-листе wb_hold.txt")
    return out


def collect():
    """Всё для страницы: кабинеты, кампании с артикулами, текущие и «по правилам» значения, пополнения за 7 дней."""
    rules = rules_now()
    ov = load_overrides()
    state = B.load_state()
    cabs = {k: v for k, v in B.load_tokens().items() if k in B.RULES_BASE}
    res, threads = {}, []

    def one(cab, token):
        try:
            res[cab] = ("ok", B.get_campaigns(token))
        except B.ApiError as e:
            res[cab] = ("err", str(e))

    for cab, token in cabs.items():
        t = threading.Thread(target=one, args=(cab, token), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(120)
    out = []
    for cab in B.RULES_BASE:
        if cab not in cabs:
            continue
        kind, camps = res.get(cab, ("err", "WB не ответил за 2 минуты"))
        c = {"cab": cab, "error": camps if kind == "err" else None, "camps": [], "other": [],
             "experiment": EXPERIMENT.get(cab) if EXPERIMENT.get(cab, "") >= dt.date.today().isoformat() else None}
        wb_ = (rules.get(cab) or {}).get("_week_budget")
        c["week_budget"] = {"cur": wb_, "base": (B.RULES_BASE.get(cab) or {}).get("_week_budget"),
                            "topped": B.topped_cabinet(state, cab, list(camps) if kind == "ok" else [])}
        if kind == "err":
            out.append(c)
            continue
        prod = products(cab)
        manual = ov.get("manual") or {}
        raised = ov.get("raised") or {}
        for cid, a in camps.items():
            st = a.get("settings") or {}
            nms = [s.get("nm_id") for s in a.get("nm_settings") or [] if s.get("nm_id")]
            row = {"id": cid, "name": st.get("name") or str(cid), "pay": PAY.get(st.get("payment_type"), "?"),
                   "status": STATUS.get(a.get("_status"), str(a.get("_status"))), "active": a.get("_status") == 9,
                   "flags": flags(state, cid),
                   "nms": [{"nm": n, "vendor": (prod.get(n) or ("", ""))[0], "title": (prod.get(n) or ("", ""))[1]}
                           for n in nms]}
            r = rule_of(rules, cab, cid, camps)
            if r is None:
                c["other"].append(row)
                continue
            base = rule_of(B.RULES_BASE, cab, cid, camps) or {}
            row.update({"cur": {k: r.get(k) for k in FIELDS}, "base": {k: base.get(k) for k in FIELDS},
                        "topup": bool(r.get("topup_below")), "topped": B.topped_week(state, cid),
                        "manual": (manual.get(str(cid)) or {}).get("date"),
                        "raised": (raised.get(str(cid)) or {}).get("date"),
                        "until": ((B.RULES_BASE.get(cab) or {}).get(str(cid)) or {}).get("until")})
            c["camps"].append(row)
        c["camps"].sort(key=lambda x: (not x["active"], -(x["cur"].get("topup_week_cap") or 0) if x["topup"] else 0,
                                       x["id"]))
        c["other"].sort(key=lambda x: (not x["active"], x["id"]))
        out.append(c)
    return {"at": dt.datetime.now().strftime("%d.%m.%Y %H:%M"), "cabinets": out,
            "overrides_at": ov.get("at"), "last_backup": last_backup_name()}


def last_backup_name():
    files = sorted(BACKUPS.glob("overrides_*.json")) if BACKUPS.exists() else []
    return files[-1].name if files else None


def plan(data, changes):
    """Проверка правок: (строки плана, предупреждения, ошибки, нормализованные правки)."""
    by_cab = {c["cab"]: c for c in data["cabinets"]}
    lines, warns, errors, norm = [], [], [], []
    for ch in changes:
        cab, cid, field = ch.get("cab"), str(ch.get("id")), ch.get("field")
        c = by_cab.get(cab)
        try:
            v = int(round(float(str(ch.get("value")).replace(" ", "").replace(",", "."))))
        except (TypeError, ValueError):
            errors.append(f"{cab} {cid}: «{ch.get('value')}» — не число")
            continue
        if c is None or c.get("error"):
            errors.append(f"{cab}: кабинета нет или он не загрузился — правку не принимаю")
            continue
        if v < 0 or v > MAX_VALUE:
            errors.append(f"{cab} {cid}: {v} ₽ — вне 0…{rub(MAX_VALUE)} ₽")
            continue
        if cid == "_week_budget":
            cur = c["week_budget"]["cur"]
            if cur == v:
                continue
            if not cur:
                errors.append(f"{cab}: недельного бюджета кабинета в правилах нет — его заводят в RULES, не здесь")
                continue
            norm.append({"cab": cab, "id": "_week_budget", "field": "_week_budget", "value": v})
            lines.append(f"{cab} · недельный бюджет кабинета: {rub(cur)} → {rub(v)} ₽")
            continue
        row = next((x for x in c["camps"] if str(x["id"]) == cid), None)
        if row is None:
            errors.append(f"{cab} {cid}: кампания не под управлением автопилота — не меняю (иначе автопилот начал бы ею "
                          "управлять)")
            continue
        if field not in FIELDS:
            errors.append(f"{cab} {cid}: поле «{field}» здесь не меняется")
            continue
        cur = row["cur"].get(field)
        if cur == v:
            continue
        if field == "daily_limit" and v < 100:
            errors.append(f"«{row['name']}» {cid}: дневной лимит {v} ₽ — меньше 100 ₽ не ставлю")
            continue
        if field != "daily_limit" and not row["topup"]:
            errors.append(f"«{row['name']}» {cid}: автопополнения у кампании нет — потолки пополнений не действуют")
            continue
        norm.append({"cab": cab, "id": row["id"], "field": field, "value": v})
        arts = ", ".join(x["vendor"] or str(x["nm"]) for x in row["nms"][:6]) + (
            f" и ещё {len(row['nms']) - 6}" if len(row["nms"]) > 6 else "")
        lines.append(f"{cab} · «{row['name']}» {row['id']} ({row['pay']}; {arts or 'без товаров'}): {LABEL[field]} "
                     f"{rub(cur)} → {rub(v)} ₽")
    # предупреждения — по значениям «после»
    after = {}
    for n in norm:
        after.setdefault((n["cab"], str(n["id"])), {})[n["field"]] = n["value"]
    for c in data["cabinets"]:
        if c.get("error"):
            continue
        touched = any(k[0] == c["cab"] for k in after)
        if not touched:
            continue
        if c.get("experiment"):
            warns.append(f"{c['cab']}: эксперимент до {ddmm(c['experiment'])} — изменение денег скажется на сравнении итогов")
        week_sum = 0
        for row in c["camps"]:
            a = {**row["cur"], **after.get((c["cab"], str(row["id"])), {})}
            if row["topup"] and row["active"]:
                week_sum += a.get("topup_week_cap") or 0
            if (c["cab"], str(row["id"])) not in after:
                continue
            nm = f"«{row['name']}» {row['id']}"
            wk, dl = a.get("topup_week_cap"), a.get("daily_limit")
            if row["topup"] and wk and wk % B.MIN_DEPOSIT:
                warns.append(f"{nm}: недельный потолок {rub(wk)} ₽ не кратен 1 000 — WB не пополняет меньше чем на 1 000 ₽, "
                             f"последние {rub(wk % B.MIN_DEPOSIT)} ₽ недели не используются")
            if row["topup"] and wk is not None and dl and wk < dl * 7:
                warns.append(f"{nm}: недельный потолок {rub(wk)} ₽ меньше 7 дневных лимитов ({rub(dl * 7)} ₽) — к концу "
                             "недели кампания может встать без денег")
            for k, v in after[(c["cab"], str(row["id"]))].items():
                b = row["base"].get(k)
                if b and v > b * 3:
                    warns.append(f"{nm}: {LABEL[k]} {rub(v)} ₽ — больше чем втрое против правил ({rub(b)} ₽); проверьте, "
                                 "не лишний ли ноль")
            if row["pay"] == "клики" and dl and dl < B.MIN_DEPOSIT and "daily_limit" in after[(c["cab"], str(row["id"]))]:
                warns.append(f"{nm}: у WB дневной лимит не меньше 1 000 ₽ — {rub(dl)} ₽ держит наша частая проверка паузой")
            if row.get("until"):
                warns.append(f"{nm}: правило кампании действует до {ddmm(row['until'])} — после этой даты поправка перестанет "
                             "действовать (кампания перейдёт на общие правила кабинета)")
        cab_wb = after.get((c["cab"], "_week_budget"), {}).get("_week_budget", c["week_budget"]["cur"])
        if cab_wb and week_sum > cab_wb:
            warns.append(f"{c['cab']}: сумма недельных потолков работающих кампаний {rub(week_sum)} ₽ больше недельного "
                         f"бюджета кабинета {rub(cab_wb)} ₽ — пополнения упрутся в бюджет кабинета")
    return lines, warns, errors, norm


def digest(norm):
    return hashlib.sha256(json.dumps(norm, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def apply_changes(data, norm):
    ov = load_overrides()
    BACKUPS.mkdir(exist_ok=True)
    bk = BACKUPS / f"overrides_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    bk.write_text(json.dumps(ov, ensure_ascii=False, indent=1), encoding="utf-8")
    by_cab = {c["cab"]: c for c in data["cabinets"]}
    items = ov.setdefault("items", {})
    manual = ov.setdefault("manual", {})
    today = dt.date.today().isoformat()
    for n in norm:
        cab, cid, field, v = n["cab"], str(n["id"]), n["field"], n["value"]
        it = items.setdefault(cab, {})
        if cid == "_week_budget":
            if v == by_cab[cab]["week_budget"]["base"]:
                it.pop("_week_budget", None)
                manual.pop("_week_budget:" + cab, None)
            else:
                it["_week_budget"] = v
                manual["_week_budget:" + cab] = {"date": today, "cabinet": cab}
            if not it:
                items.pop(cab, None)
            continue
        row = next(x for x in by_cab[cab]["camps"] if str(x["id"]) == cid)
        e = it.setdefault(cid, {})
        if v == row["base"].get(field):
            e.pop(field, None)              # как в правилах — поправка больше не нужна
        else:
            e[field] = v
        if not e:
            it.pop(cid, None)
            manual.pop(cid, None)
        else:
            manual[cid] = {"date": today, "cabinet": cab}
        (ov.get("raised") or {}).pop(cid, None)   # ручное решение важнее: wb_budget.py эту кампанию больше не трогает
        if not it:
            items.pop(cab, None)
    ov["at"] = dt.datetime.now().isoformat(timespec="seconds")
    tmp = OVERRIDES.with_suffix(".tmp")
    tmp.write_text(json.dumps(ov, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, OVERRIDES)
    return bk.name


def undo_last():
    files = sorted(BACKUPS.glob("overrides_*.json")) if BACKUPS.exists() else []
    if not files:
        return None
    prev = json.loads(files[-1].read_text(encoding="utf-8"))
    tmp = OVERRIDES.with_suffix(".tmp")
    tmp.write_text(json.dumps(prev, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, OVERRIDES)
    files[-1].rename(files[-1].with_suffix(".rolled_back"))
    return files[-1].name


def write_log(lines):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("\n".join([f"{dt.datetime.now():%d.%m.%Y %H:%M}"] + [f"  {x}" for x in lines]) + "\n\n")
    except OSError:
        pass


class App:
    def __init__(self):
        self.token = secrets.token_urlsafe(16)
        self.last = time.time()
        self.bye_at = None
        self.data = None
        self.server = None


APP = App()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _ok(self):
        u = urlparse(self.path)
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost") or (parse_qs(u.query).get("k") or [""])[0] != APP.token:
            self.send_response(403)
            self.end_headers()
            self.wfile.write("Нет доступа: откройте страницу ярлыком «Бюджеты WB».".encode("utf-8"))
            return None
        APP.last = time.time()
        APP.bye_at = None
        return u.path

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body if isinstance(body, bytes) else (json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
                                                  if not isinstance(body, str) else body.encode("utf-8"))
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self._ok()
        if path is None:
            return
        if path == "/":
            self._send(200, PAGE.replace("__TOKEN__", APP.token), "text/html; charset=utf-8")
        elif path == "/data":
            try:
                APP.data = collect()
                self._send(200, APP.data)
            except Exception as e:      # noqa: BLE001 — показать на странице, а не уронить сервер
                self._send(500, {"error": f"{type(e).__name__}: {e}"})
        elif path == "/ping":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "нет такой страницы"})

    def do_POST(self):
        path = self._ok()
        if path is None:
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0) or 0) or b"{}")
        except ValueError:
            body = {}
        if path == "/bye":
            APP.bye_at = time.time()
            self._send(200, {"ok": True})
            return
        if path in ("/preview", "/save"):
            if APP.data is None:
                APP.data = collect()
            lines, warns, errors, norm = plan(APP.data, body.get("changes") or [])
            if path == "/preview" or errors:
                self._send(200, {"lines": lines, "warnings": warns, "errors": errors, "digest": digest(norm)})
                return
            if body.get("digest") != digest(norm):
                self._send(200, {"errors": ["Правки изменились после проверки — нажмите «Проверить» ещё раз."]})
                return
            if not norm:
                self._send(200, {"errors": ["Менять нечего."]})
                return
            with _write_lock:
                name = apply_changes(APP.data, norm)
            write_log(["ЗАПИСАНО (страница «Бюджеты WB»), резервная копия " + name] + lines + [f"! {w}" for w in warns])
            APP.data = None
            self._send(200, {"saved": len(norm), "backup": name})
            return
        if path == "/undo":
            with _write_lock:
                name = undo_last()
            write_log([f"ОТМЕНА: файл поправок возвращён как до {name}" if name else "ОТМЕНА: копий нет"])
            APP.data = None
            self._send(200, {"undone": name})
            return
        self._send(404, {"error": "нет такой страницы"})


def watchdog():
    while True:
        time.sleep(5)
        idle = time.time() - APP.last
        if idle > IDLE_MIN * 60 or (APP.bye_at and time.time() - APP.bye_at > 15):
            APP.server.shutdown()
            return


def open_browser(url):
    br = sys.argv[sys.argv.index("--browser") + 1] if "--browser" in sys.argv[:-1] else None
    if br and Path(br).exists():
        subprocess.Popen([br, url])
    else:
        webbrowser.open(url)


def main():
    if sys.stdout is None:                      # запуск ярлыком через pythonw.exe — консоли нет
        sys.stdout = sys.stderr = open(HERE / "wb_budget_page_out.txt", "a", encoding="utf-8")
    # уже открыта — показать ту же страницу, второй сервер не поднимать
    try:
        old = json.loads(LOCK.read_text(encoding="utf-8"))
        url = f"http://127.0.0.1:{old['port']}/?k={old['token']}"
        with urllib.request.urlopen(f"http://127.0.0.1:{old['port']}/ping?k={old['token']}", timeout=2) as r:
            if r.status == 200:
                open_browser(url)
                return
    except (OSError, ValueError, KeyError):
        pass
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv[:-1] else 0
    APP.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    port = APP.server.server_address[1]
    LOCK.write_text(json.dumps({"pid": os.getpid(), "port": port, "token": APP.token}), encoding="utf-8")
    url = f"http://127.0.0.1:{port}/?k={APP.token}"
    print(f"{dt.datetime.now():%d.%m.%Y %H:%M} — страница «Бюджеты WB»: {url.split('?')[0]}", flush=True)
    threading.Thread(target=watchdog, daemon=True).start()
    if "--no-open" not in sys.argv:
        open_browser(url)
    try:
        APP.server.serve_forever()
    finally:
        try:
            if json.loads(LOCK.read_text(encoding="utf-8")).get("token") == APP.token:
                LOCK.unlink()
        except (OSError, ValueError):
            pass


PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Бюджеты WB</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--mute:#6b7280;--line:#e3e6eb;--acc:#5b3fd0;--accink:#fff;--accbg:#efeafe;
--warn:#9a5b00;--warnbg:#fff4e0;--err:#b42318;--errbg:#fdecea;--ok:#0f7a3d;--okbg:#e7f6ed;--chg:#fff7d6}
@media (prefers-color-scheme:dark){:root{--bg:#14161b;--card:#1d2027;--ink:#e8eaee;--mute:#9aa1ad;--line:#2d313a;
--acc:#a78bfa;--accink:#14161b;--accbg:#2a2440;--warn:#f0b35a;--warnbg:#33281a;--err:#f28b82;--errbg:#3a1f1d;--ok:#6fd39b;--okbg:#17301f;--chg:#3a3418}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{padding:16px 20px 8px}h1{margin:0 0 4px;font-size:20px}.sub{color:var(--mute);font-size:13px}
.tools{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:8px 20px}
input[type=search]{padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);min-width:420px;max-width:100%}
section{margin:12px 20px;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.cabhead{display:flex;gap:16px;align-items:center;flex-wrap:wrap;padding:12px 14px;border-bottom:1px solid var(--line)}
.cabhead h2{margin:0;font-size:17px}.pill{font-size:12px;padding:2px 8px;border-radius:999px;background:var(--accbg);color:var(--acc)}
.pill.w{background:var(--warnbg);color:var(--warn)}.pill.g{background:var(--line);color:var(--mute)}
table{width:100%;border-collapse:collapse}th,td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}
th{font-size:12px;color:var(--mute);font-weight:600;background:var(--card);position:sticky;top:0}
th.num,td.num{text-align:right;white-space:nowrap}th.wk,td.wk{background:var(--accbg)}
.cid{font-weight:700}.name{color:var(--mute);font-size:12px}.badges{margin-top:3px;display:flex;gap:4px;flex-wrap:wrap}
.arts{display:flex;flex-wrap:wrap;gap:4px;max-width:520px}.art{font-size:12px;padding:1px 6px;border:1px solid var(--line);border-radius:6px;white-space:nowrap}
.art b{font-weight:600}.art span{color:var(--mute)}
input.v{width:96px;padding:5px 7px;text-align:right;border:1px solid var(--line);border-radius:7px;background:var(--card);color:var(--ink);font:inherit}
input.v.changed{background:var(--chg);border-color:var(--warn)}.hint{font-size:11px;color:var(--mute)}
details{padding:8px 14px}summary{cursor:pointer;color:var(--mute)}
.bar{position:sticky;bottom:0;background:var(--card);border-top:1px solid var(--line);padding:10px 20px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
button{padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);font:inherit;cursor:pointer}
button.p{background:var(--acc);border-color:var(--acc);color:var(--accink)}button:disabled{opacity:.45;cursor:default}
#plan{margin:12px 20px}.box{border-radius:10px;padding:10px 14px;margin:8px 0}.box.e{background:var(--errbg);color:var(--err)}
.box.w{background:var(--warnbg);color:var(--warn)}.box.o{background:var(--okbg);color:var(--ok)}.box.i{background:var(--card);border:1px solid var(--line)}
.box ul{margin:4px 0;padding-left:18px}.err{color:var(--err);padding:12px 14px}.muted{color:var(--mute)}
</style></head><body>
<header><h1>Бюджеты WB — недельные потолки и лимиты кампаний</h1>
<div class="sub" id="sub">Загружаю кампании из WB…</div></header>
<div class="tools"><input type="search" id="q" placeholder="Найти: номер кампании, артикул, название…">
<label><input type="checkbox" id="act"> только работающие</label><span class="muted" id="cnt"></span></div>
<div id="cabs"></div><div id="plan"></div>
<div class="bar"><button id="chk" class="p" disabled>Проверить изменения</button><button id="save" disabled>Сохранить</button>
<button id="reset" disabled>Сбросить правки</button><span style="flex:1"></span><button id="undo">Отменить последнее сохранение</button></div>
<script>
const K="__TOKEN__", FIELDS=["daily_limit","topup_day_cap","topup_week_cap"];
const LBL={daily_limit:"Дневной лимит",topup_day_cap:"Пополнений в сутки",topup_week_cap:"Недельный потолок"};
let DATA=null, lastDigest=null;
const fmt=v=>v==null?"—":Math.round(v).toLocaleString("ru-RU").replace(/,/g," ");
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const api=(p,b)=>fetch(p+"?k="+K,b?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)}:{}).then(r=>r.json());
function arts(nms){if(!nms.length)return '<span class="muted">без товаров</span>';
 const chip=x=>`<span class="art" title="${esc(x.title)}">${x.vendor?`<b>${esc(x.vendor)}</b> <span>${x.nm}</span>`:`<b>${x.nm}</b>`}</span>`;
 if(nms.length<=8)return `<div class="arts">${nms.map(chip).join("")}</div>`;
 return `<div class="arts">${nms.slice(0,8).map(chip).join("")}</div><details style="padding:4px 0"><summary>ещё ${nms.length-8}</summary><div class="arts">${nms.slice(8).map(chip).join("")}</div></details>`}
function inp(cab,id,f,cur,base,dis){if(dis)return '<span class="muted">—</span>';
 return `<input class="v" inputmode="numeric" data-cab="${esc(cab)}" data-id="${id}" data-f="${f}" data-orig="${cur??""}" value="${cur??""}">`+
 (base!=null&&base!==cur?`<div class="hint">в правилах ${fmt(base)}</div>`:"")}
function badges(r){const b=[`<span class="pill g">${r.pay}</span>`,`<span class="pill ${r.active?"":"g"}">${r.status}</span>`];
 r.flags.forEach(x=>b.push(`<span class="pill w">${x}</span>`));
 if(r.manual)b.push(`<span class="pill">вручную ${r.manual.split("-").reverse().join(".")}</span>`);
 if(r.raised)b.push(`<span class="pill">wb_budget ${r.raised.split("-").reverse().join(".")}</span>`);
 if(r.until)b.push(`<span class="pill g">правило до ${r.until.split("-").reverse().join(".")}</span>`);return b.join("")}
function render(){const el=document.getElementById("cabs");el.innerHTML="";
 DATA.cabinets.forEach(c=>{const s=document.createElement("section");
  if(c.error){s.innerHTML=`<div class="cabhead"><h2>${esc(c.cab)}</h2></div><div class="err">Не загрузилось из WB: ${esc(c.error)}</div>`;el.appendChild(s);return}
  const wb=c.week_budget, sum=c.camps.filter(r=>r.topup&&r.active).reduce((a,r)=>a+(r.cur.topup_week_cap||0),0);
  let h=`<div class="cabhead"><h2>${esc(c.cab)}</h2>`+(c.experiment?`<span class="pill w">эксперимент до ${c.experiment.split("-").reverse().join(".")}</span>`:"")+
   (wb.cur!=null?`<span>Недельный бюджет кабинета ${inp(c.cab,"_week_budget","_week_budget",wb.cur,wb.base)}</span><span class="muted">пополнено за 7 дн.: ${fmt(wb.topped)} ₽</span>`:`<span class="muted">недельного бюджета кабинета нет</span>`)+
   `<span class="muted">сумма недельных потолков работающих: ${fmt(sum)} ₽</span></div>`;
  h+=`<table><thead><tr><th>Кампания</th><th>Артикулы в кампании</th><th class="num">${LBL.daily_limit}, ₽</th><th class="num">${LBL.topup_day_cap}, ₽</th><th class="num wk">${LBL.topup_week_cap}, ₽</th><th class="num">Пополнено за 7 дн.</th></tr></thead><tbody>`;
  c.camps.forEach(r=>{const hay=(r.id+" "+r.name+" "+r.nms.map(x=>x.vendor+" "+x.nm+" "+x.title).join(" ")).toLowerCase();
   h+=`<tr data-hay="${esc(hay)}" data-act="${r.active?1:0}"><td><div class="cid">${r.id}</div><div class="name">${esc(r.name)}</div><div class="badges">${badges(r)}</div></td>`+
   `<td>${arts(r.nms)}</td><td class="num">${inp(c.cab,r.id,"daily_limit",r.cur.daily_limit,r.base.daily_limit,false)}</td>`+
   `<td class="num">${inp(c.cab,r.id,"topup_day_cap",r.cur.topup_day_cap,r.base.topup_day_cap,!r.topup)}</td>`+
   `<td class="num wk">${inp(c.cab,r.id,"topup_week_cap",r.cur.topup_week_cap,r.base.topup_week_cap,!r.topup)}</td>`+
   `<td class="num">${r.topup?fmt(r.topped)+" из "+fmt(r.cur.topup_week_cap):'<span class="muted">без пополнений</span>'}</td></tr>`});
  h+=`</tbody></table>`;
  if(c.other.length)h+=`<details><summary>Не под управлением автопилота — только для справки (${c.other.length})</summary><table><tbody>`+
   c.other.map(r=>`<tr data-hay="${esc((r.id+" "+r.name+" "+r.nms.map(x=>x.vendor+" "+x.nm).join(" ")).toLowerCase())}" data-act="${r.active?1:0}"><td><div class="cid">${r.id}</div><div class="name">${esc(r.name)}</div><div class="badges"><span class="pill g">${r.pay}</span><span class="pill g">${r.status}</span></div></td><td>${arts(r.nms)}</td></tr>`).join("")+`</tbody></table></details>`;
  s.innerHTML=h;el.appendChild(s)});
 document.querySelectorAll("input.v").forEach(i=>i.addEventListener("input",onEdit));filter()}
function changes(){return [...document.querySelectorAll("input.v")].filter(i=>i.value.trim()!==i.dataset.orig).map(i=>({cab:i.dataset.cab,id:i.dataset.id,field:i.dataset.f,value:i.value.trim()}))}
function onEdit(e){e.target.classList.toggle("changed",e.target.value.trim()!==e.target.dataset.orig);
 const n=changes().length;document.getElementById("chk").disabled=!n;document.getElementById("reset").disabled=!n;
 document.getElementById("save").disabled=true;lastDigest=null;document.getElementById("plan").innerHTML=n?'<div class="box i">Есть несохранённые правки ('+n+'). Нажмите «Проверить изменения».</div>':""}
function filter(){const q=document.getElementById("q").value.trim().toLowerCase(),act=document.getElementById("act").checked;let n=0;
 document.querySelectorAll("tr[data-hay]").forEach(tr=>{const v=(!q||tr.dataset.hay.includes(q))&&(!act||tr.dataset.act==="1");tr.style.display=v?"":"none";if(v)n++});
 document.getElementById("cnt").textContent=q||act?("показано кампаний: "+n):""}
function box(cls,title,items){return items&&items.length?`<div class="box ${cls}"><b>${title}</b><ul>${items.map(x=>"<li>"+esc(x)+"</li>").join("")}</ul></div>`:""}
async function load(){document.getElementById("sub").textContent="Загружаю кампании из WB…";
 const d=await api("/data");if(d.error){document.getElementById("sub").innerHTML='<span class="err">'+esc(d.error)+'</span>';return}
 DATA=d;document.getElementById("sub").textContent=`Кампании и артикулы — из WB на ${d.at}. Снижение потолков пополнений действует сразу, повышение — с ближайшего запуска автопилота (8:00, 13:00, 18:00); свой дневной лимит (показы, клики ниже 1 000 ₽) — в течение 5 минут. Деньги здесь не переводятся.`;
 render();["chk","save","reset"].forEach(x=>document.getElementById(x).disabled=true);
 document.getElementById("undo").title=d.last_backup?("вернёт файл поправок как до "+d.last_backup):"копий ещё нет"}
document.getElementById("q").addEventListener("input",filter);document.getElementById("act").addEventListener("change",filter);
document.getElementById("chk").onclick=async()=>{const r=await api("/preview",{changes:changes()});
 let h=box("e","Не принято:",r.errors)+(r.lines&&r.lines.length?box("i","Будет записано:",r.lines):'<div class="box i">Менять нечего — значения те же.</div>')+box("w","Обратите внимание:",r.warnings);
 document.getElementById("plan").innerHTML=h;lastDigest=(!r.errors||!r.errors.length)&&r.lines&&r.lines.length?r.digest:null;
 document.getElementById("save").disabled=!lastDigest;if(lastDigest)document.getElementById("save").classList.add("p");document.getElementById("plan").scrollIntoView({behavior:"smooth"})};
document.getElementById("save").onclick=async()=>{const r=await api("/save",{changes:changes(),digest:lastDigest});
 if(r.errors&&r.errors.length){document.getElementById("plan").innerHTML=box("e","Не записано:",r.errors);return}
 await load();document.getElementById("plan").innerHTML=`<div class="box o">Записано правок: ${r.saved} (резервная копия ${esc(r.backup)}). Отменить — кнопка «Отменить последнее сохранение».</div>`};
document.getElementById("reset").onclick=()=>{render();onEdit({target:document.createElement("input")});document.getElementById("plan").innerHTML=""};
document.getElementById("undo").onclick=async()=>{if(!confirm("Вернуть поправки как до последнего сохранения (вашего или wb_budget.py)? Применяется сразу."))return;
 const r=await api("/undo",{});await load();document.getElementById("plan").innerHTML=r.undone?`<div class="box o">Отменено: поправки как до ${esc(r.undone)}.</div>`:'<div class="box w">Отменять нечего — копий нет.</div>'};
setInterval(()=>fetch("/ping?k="+K).catch(()=>{}),60000);
window.addEventListener("pagehide",()=>navigator.sendBeacon("/bye?k="+K));
load();
</script></body></html>"""


if __name__ == "__main__":
    main()
