# -*- coding: utf-8 -*-
"""
«Реклама WB» в браузере: две страницы — «Дашборд» (wb_dashboard.py, только чтение) и «Бюджеты» — недельные потолки
пополнений, пополнения в сутки и дневные лимиты кампаний с номером, названием и артикулами каждой кампании, рядом —
рекомендуемый бюджет (запросы пользователя 16.10.2026: «страница корректировки недельных бюджетов по кампаниям; для
идентификации помимо id — артикулы»; «и рекомендуемый бюджет там же; не делай отдельный значок — ещё одну страницу в
„Реклама WB“»).

Как работает:
  • запускает маленький веб-сервер только для этого компьютера (127.0.0.1, одноразовый ключ в адресе — снаружи и без
    ключа страницу не открыть) и открывает «Дашборд» в браузере; второй запуск открывает уже работающий;
  • «Дашборд» — тот же, что wb_dashboard.html (собирается из файлов на диске при каждом открытии);
  • «Бюджеты»: кампании и их товары — из WB (только чтение списка кампаний), артикулы продавца и названия — из
    wb_history.db, пополнения за 7 дней — из wb_bids_state.json, рекомендации — из wb_budget_proposals.json (их раз в
    сутки после утреннего автопилота считает wb_budget.py; кнопка «Пересчитать» — сейчас, ≈1–3 мин на кабинет);
  • править можно кампании под управлением автопилота (RULES в wb_bids.py); остальные показаны для справки;
  • «Проверить» — план: что было → что станет и предупреждения (ничего не меняет); «Сохранить» — запись в
    wb_budget_overrides.json (тот же файл, что у wb_budget.py, поверх RULES) с резервной копией в wb_budget_backups;
    «Отменить последнее сохранение» — вернуть файл как до последней записи (своей или wb_budget.py), сразу;
  • кампании, поправленные здесь, wb_budget.py больше не меняет (ручное решение важнее автоматики).
Когда действует: снижение потолков пополнений — сразу (часовая проверка берёт меньшее); повышение — с ближайшего
запуска автопилота (8:00, 13:00, 18:00); дневной лимит кампаний за клики WB получит при ближайшем запуске автопилота;
свой лимит (показы; клики ниже 1 000 ₽) — частая проверка в течение 5 минут. Деньги здесь не переводятся.

Запуск:  python wb_web.py                       (на сервере — ярлык «Реклама WB» на рабочем столе)
         python wb_web.py --browser "C:\\путь\\к\\браузеру.exe"
Закрывается сама через 30 минут без действий или вскоре после закрытия вкладки. Не запустилась (порт и т. п.) —
открывает файл wb_dashboard.html, как раньше. Нужна категория токена «Продвижение» (только чтение списка кампаний).
Журнал записей бюджетов — wb_budget_page_log.txt.
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
import wb_dashboard as DASH

# ---------------- НАСТРОЙКИ ----------------
IDLE_MIN = 30                      # без запросов столько минут — сервер закрывается
PORT = 8765                        # постоянный порт: браузер помнит тему, период и кабинет дашборда; занят — любой свободный
FIELDS = ("daily_limit", "topup_day_cap", "topup_week_cap")      # то же, что правит wb_budget.py (B.BUDGET_KEYS)
WB_MIN_CPC_LIMIT = 1000        # меньше дневной лимит кампании за клики WB не ставит — меньший держит пауза (wb_guard.py --limits)
MAX_VALUE = 300000                 # ₽ — больше в одно поле не принимаю (защита от лишнего нуля)
EXPERIMENT = {"Ребрик": "2026-10-27", "Караман": "2026-10-27"}   # предупреждать: деньги влияют на сравнение
# -------------------------------------------
HERE = B.HERE
LOCK = HERE / "wb_web.running"
LOG = HERE / "wb_budget_page_log.txt"
OUT = HERE / "wb_web_out.txt"
BACKUPS = HERE / "wb_budget_backups"           # те же копии, что у wb_budget.py: откат общий
OVERRIDES = B.BUDGET_OVERRIDES
PROPOSALS = HERE / "wb_budget_proposals.json"
LABEL = {"daily_limit": "дневной лимит", "topup_day_cap": "пополнения в сутки", "topup_week_cap": "недельный потолок",
         "_week_budget": "недельный бюджет кабинета"}
PAY = {"cpm": "показы", "cpc": "клики"}
STATUS = {9: "работает", 11: "пауза", 4: "готова к запуску"}
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_rules_lock = threading.Lock()
_write_lock = threading.Lock()
PROGRESS_FILE = HERE / "wb_budget_progress.json"   # этап пересчёта — пишет wb_budget.py


class Progress:
    """Что сейчас делает страница (загрузка из WB, запись) — для шкалы прогресса: done из total и подпись."""

    def __init__(self):
        self.lock = threading.Lock()
        self.d = {}

    def start(self, op, text, total=None):
        with self.lock:
            self.d = {"op": op, "text": text, "done": 0, "total": total, "started": time.time()}

    def step(self, text=None):
        with self.lock:
            if self.d:
                self.d["done"] = self.d.get("done", 0) + 1
                if text:
                    self.d["text"] = text

    def end(self):
        with self.lock:
            self.d = {}

    def get(self):
        with self.lock:
            d = dict(self.d)
        if d:
            d["elapsed"] = round(time.time() - d.pop("started"))
        return d


PROG = Progress()


def ddmm(iso):
    return f"{iso[8:10]}.{iso[5:7]}" if iso and len(iso) >= 10 else (iso or "")


def rub(v):
    return "—" if v is None else B.fm_rub(v)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


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
        out.append("выключена — в стоп-листе wb_hold.txt")
    return out


def managed(rules, cab, cid, camps, state):
    """(под управлением автопилота?, почему нет) — так же, как кампании выбирает wb_bids.plan_cabinet: по своему правилу
    (номер или название) — в любом статусе; по общему «*» — только работающая или стоящая на паузе программы (лимит,
    график расхода). Остальное на паузе автопилот не трогает (кроме паузы из-за пустого бюджета — тогда пополнит)."""
    r = rules.get(cab) or {}
    if B.on_hold(cid) and rule_of(rules, cab, cid, camps) is not None:
        return True, ""                  # выключена переключателем (стоп-лист) — под управлением, чтобы её можно было включить
    today = dt.date.today().isoformat()
    for k, v in r.items():
        if k == "*" or k.startswith("_") or (v.get("until") and today > v["until"]):
            continue
        ids = B.find(camps, k)
        if cid in ids:
            if len(ids) > 1:
                return False, f"правило «{k}» подходит к нескольким кампаниям — автопилот их пропускает"
            return True, ""
    if "*" not in r:
        return False, "нет в правилах автопилота"
    if cid in {c for k in r.get("_except") or [] for c in B.find(camps, k)}:
        return False, "в исключениях «_except»"
    st = (camps.get(cid) or {}).get("_status")
    if st == 9 or (st == 11 and (B.limit_paused(state, cid) or B.pace_paused(state, cid))):
        return True, ""
    if st == 11 and str(cid) in (state.get("managed") or {}):
        return False, "на паузе — автопилот запустит её, только если кончились деньги (в бюджете меньше 100 ₽)"
    return False, ("на паузе" if st == 11 else STATUS.get(st, "не работает")) + " — общее правило «*» ведёт только работающие"


def collect():
    """Всё для страницы «Бюджеты»: кабинеты, кампании с артикулами, текущие и «по правилам» значения, пополнения
    за 7 дней, рекомендации wb_budget.py."""
    rules = rules_now()
    ov = load_json(OVERRIDES, {"items": {}, "raised": {}})
    props = load_json(PROPOSALS, {})
    recs = {str(p.get("advert_id")): p for p in props.get("items") or []}
    state = B.load_state()
    cabs = {k: v for k, v in B.load_tokens().items() if k in B.RULES_BASE}
    res, threads = {}, []
    PROG.start("load", f"Загружаю кампании из WB: 0 из {len(cabs)} кабинетов", total=len(cabs) + 1)

    def one(cab, token):
        try:
            res[cab] = ("ok", B.get_campaigns(token))
        except B.ApiError as e:
            res[cab] = ("err", str(e))
        PROG.step(f"Загружаю кампании из WB: {len(res)} из {len(cabs)} кабинетов (готов «{cab}»)")

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
            ok, why = managed(rules, cab, cid, camps, state)
            if r is None or not ok:
                row["why"] = why or "нет в правилах автопилота"
                c["other"].append(row)
                continue
            base = rule_of(B.RULES_BASE, cab, cid, camps) or {}
            p = recs.get(str(cid)) or {}
            row.update({"on": not B.on_hold(cid), "own_limit": own_limit(r, st.get("payment_type")),
                        "cur": {k: r.get(k) for k in FIELDS}, "base": {k: base.get(k) for k in FIELDS},
                        "topup": bool(r.get("topup_below")), "topped": B.topped_week(state, cid),
                        "manual": (manual.get(str(cid)) or {}).get("date"),
                        "raised": (raised.get(str(cid)) or {}).get("date"),
                        "until": ((B.RULES_BASE.get(cab) or {}).get(str(cid)) or {}).get("until"),
                        "rec": p.get("rec"), "rec_text": (f"Расчёт от {ddmm(p['stale'])} (при последнем пересчёте WB не "
                                                          "ответил). " if p.get("stale") else "")
                        + (p.get("rec_text") or p.get("text") or "")})
            c["camps"].append(row)
        c["camps"].sort(key=lambda x: (not x["active"], -(x["cur"].get("topup_week_cap") or 0) if x["topup"] else 0,
                                       x["id"]))
        c["other"].sort(key=lambda x: (not x["active"], x["id"]))
        out.append(c)
    PROG.step("Собираю таблицу")
    return {"at": dt.datetime.now().strftime("%d.%m.%Y %H:%M"), "cabinets": out,
            "rec_at": props.get("at"), "overrides_at": ov.get("at"), "last_backup": last_backup_name(),
            "recalc": RECALC.status()}


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
            v = int(round(float(str(ch.get("value")).replace(" ", "").replace("\u00a0", "").replace(",", "."))))
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
        if c.get("error") or not any(k[0] == c["cab"] for k in after):
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
            if row["topup"] and wk == 0:
                warns.append(f"{nm}: недельный потолок 0 — пополнений не будет, кампания встанет, когда кончатся деньги в её "
                             "бюджете. Остановить сразу — пауза в кабинете WB и номер в wb_hold.txt (или wb_cpm_stop.py)")
            elif row["topup"] and wk and wk % B.MIN_DEPOSIT:
                warns.append(f"{nm}: недельный потолок {rub(wk)} ₽ не кратен 1 000 — WB не пополняет меньше чем на 1 000 ₽, "
                             f"последние {rub(wk % B.MIN_DEPOSIT)} ₽ недели не используются")
            if row["topup"] and wk and dl and wk < dl * 7:
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
    ov = load_json(OVERRIDES, {"items": {}, "raised": {}})
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


def own_limit(rule, pay):
    """Дневной лимит кампании держит частая проверка лимита паузой (за показы — всегда, за клики — если лимит в правилах
    ниже минимума WB): включённую такую кампанию запускает она, по графику расхода, а не страница сразу."""
    lim = (rule or {}).get("daily_limit")
    return bool(lim) and (pay != "cpc" or lim < WB_MIN_CPC_LIMIT)


def hold_edit(cid, name, on):
    """Стоп-лист wb_hold.txt: выключить — дописать номер с пометкой, включить — убрать строку. Прежний файл — в копию."""
    try:
        text = B.HOLD_FILE.read_text(encoding="utf-8-sig")
    except OSError:
        text = "# Стоп-лист: номера кампаний WB (по одному в строке), которые скрипты не запускают и не пополняют.\n"
    BACKUPS.mkdir(exist_ok=True)
    (BACKUPS / f"hold_{dt.datetime.now():%Y%m%d_%H%M%S}.txt").write_text(text, encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.split("#")[0].strip() != str(cid)]
    if not on:
        lines.append(f"{cid}   # «{name}» — выключена на странице «Бюджеты» {dt.datetime.now():%d.%m.%Y %H:%M}")
    tmp = B.HOLD_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, B.HOLD_FILE)


def toggle(cab, cid, on):
    """Переключатель «вкл/выкл» кампании под управлением. Выкл — стоп-лист и пауза: автопилот и проверки её не запускают
    и не пополняют (деньги в бюджете остаются). Вкл — убрать из стоп-листа и запустить; кампанию со своим дневным лимитом
    запускает частая проверка лимита по графику расхода (≈5 мин; ночью — после 7:00). Возвращает (удалось?, текст)."""
    token = B.load_tokens().get(cab)
    if not token:
        return False, f"нет токена кабинета «{cab}»"
    camps = B.get_campaigns(token)
    a = camps.get(cid)
    if not a:
        return False, f"кампании {cid} нет среди работающих, на паузе и готовых к запуску"
    rules = rules_now()
    if not managed(rules, cab, cid, camps, B.load_state())[0]:
        return False, f"кампания {cid} не под управлением автопилота — включайте и выключайте её в кабинете WB"
    st = a.get("settings") or {}
    name = st.get("name") or str(cid)
    if not B.lock_acquire("wb_web.py (вкл/выкл)", wait_min=2):
        return False, "сейчас работает автопилот или проверка — попробуйте через пару минут"
    try:
        state = B.load_state()           # под блокировкой — свежее состояние
        if not on:
            hold_edit(cid, name, on=False)
            for k in ("limit_paused", "pace_paused", "start_pending"):
                (state.get(k) or {}).pop(str(cid), None)
            B.save_state(state)
            if a.get("_status") != 9:
                return True, f"«{name}» выключена: уже стояла на паузе, теперь в стоп-листе — скрипты её не запустят и не пополнят."
            B.pace(token, "start", 1.1)
            try:
                B.call("GET", B.ADV + "/adv/v0/pause", token, params={"id": cid}, what="Пауза кампании")
            except B.ApiError as e:
                return False, (f"«{name}» в стоп-листе, но пауза не встала: {e}. Поставьте её на паузу в кабинете WB — "
                               "скрипты её больше не запустят и не пополнят.")
            return True, f"«{name}» выключена: на паузе и в стоп-листе — скрипты её не запустят и не пополнят."
        hold_edit(cid, name, on=True)
        if a.get("_status") == 9:
            return True, f"«{name}» включена (она и так работала)."
        if own_limit(rule_of(rules, cab, cid, camps), st.get("payment_type")):
            state.setdefault("pace_paused", {})[str(cid)] = {
                "date": (dt.date.today() - dt.timedelta(days=1)).isoformat(), "at": "включена на странице «Бюджеты»",
                "spent": None, "limit": None}
            B.save_state(state)
            return True, (f"«{name}» включена: её запустит частая проверка лимита в ближайшие 5 минут, по графику расхода "
                          "(если сегодня лимит уже потрачен — завтра утром; ночью — после 7:00).")
        n = len(B.out)
        if B.start_campaign(token, {"advert_id": cid, "name": name, "amount": 0}, state):
            return True, f"«{name}» включена и запущена."
        why = " ".join(x.strip() for x in B.out[n:]) or "WB не запустил кампанию"
        return False, f"«{name}» убрана из стоп-листа, но не запустилась: {why}"
    finally:
        B.lock_release()


def write_log(lines):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("\n".join([f"{dt.datetime.now():%d.%m.%Y %H:%M}"] + [f"  {x}" for x in lines]) + "\n\n")
    except OSError:
        pass


def python_exe():
    """python.exe рядом с pythonw.exe — у пересчёта должен быть вывод в журнал."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return str(exe)


class Recalc:
    """Пересчёт рекомендаций: пробный прогон wb_budget.py (ничего не меняет) в отдельном процессе."""

    def __init__(self):
        self.proc, self.started, self.finished, self.rc, self.t0 = None, None, None, None, 0.0
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return False
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            if os.name == "nt":
                env["TZ"] = "MSK-3"           # как у заданий по расписанию (wb_run.py)
            try:
                PROGRESS_FILE.unlink()        # этап прошлого расчёта не показывать как этап этого
            except OSError:
                pass
            f = open(OUT, "a", encoding="utf-8")
            f.write(f"\n[{dt.datetime.now():%d.%m.%Y %H:%M}] пересчёт рекомендаций: wb_budget.py (пробный прогон)\n")
            f.flush()
            self.proc = subprocess.Popen([python_exe(), str(HERE / "wb_budget.py")], cwd=str(HERE), stdout=f,
                                         stderr=subprocess.STDOUT, env=env, creationflags=NO_WINDOW)
            self.started, self.finished, self.rc = dt.datetime.now(), None, None
            self.t0 = time.time()
            return True

    def status(self):
        with self.lock:
            if not self.proc:
                return {"running": False}
            rc = self.proc.poll()
            if rc is not None and self.finished is None:
                self.finished, self.rc = dt.datetime.now(), rc
            st = {"running": rc is None, "started": self.started.strftime("%H:%M"),
                  "finished": self.finished.strftime("%H:%M") if self.finished else None, "rc": self.rc,
                  "elapsed": round(((self.finished or dt.datetime.now()) - self.started).total_seconds())}
        try:   # по времени изменения файла: пересчёт идёт по Москве (TZ=MSK-3), страница — по часам сервера
            fresh = PROGRESS_FILE.stat().st_mtime >= self.t0 - 1
        except OSError:
            fresh = False
        if fresh:
            st["stage"] = load_json(PROGRESS_FILE, {})
        return st


RECALC = Recalc()


class App:
    def __init__(self):
        self.token = secrets.token_urlsafe(16)
        self.last = time.time()
        self.bye_at = None
        self.data = None
        self.server = None


APP = App()


def nav(active):
    """Переключатель страниц. На дашборде — ещё и отметка «страница открыта» раз в минуту (иначе через IDLE_MIN минут
    просмотра сервер закрылся бы и «Бюджеты» не открылись) и «закрыта» при уходе со страницы."""
    k = APP.token
    html = ('<nav class="wbnav">'
            f'<a href="/?k={k}"' + (' aria-current="page"' if active == "dash" else "") + '>Дашборд</a>'
            f'<a href="/budgets?k={k}"' + (' aria-current="page"' if active == "budgets" else "") + '>Бюджеты</a>'
            '</nav>'
            '<style>.wbload{position:fixed;top:0;left:0;right:0;height:3px;z-index:99;display:none;overflow:hidden}'
            '.wbload i{position:absolute;top:0;bottom:0;width:30%;background:var(--s1);animation:wbl2 1.1s ease-in-out infinite}'
            '@keyframes wbl2{0%{left:-30%}100%{left:100%}}</style><div class="wbload" id="wbload"><i></i></div>'
            '<script>document.querySelectorAll(".wbnav a").forEach(a=>a.addEventListener("click",()=>{'
            'document.getElementById("wbload").style.display="block"}));'
            'window.addEventListener("pageshow",()=>{document.getElementById("wbload").style.display="none"});</script>')
    if active == "dash":
        html += (f'<script>setInterval(()=>fetch("/ping?k={k}").catch(()=>{{}}),60000);'
                 f'window.addEventListener("pagehide",()=>navigator.sendBeacon("/bye?k={k}"));</script>')
    return html


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _ok(self):
        u = urlparse(self.path)
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost") or (parse_qs(u.query).get("k") or [""])[0] != APP.token:
            self.send_response(403)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Нет доступа: откройте страницу ярлыком «Реклама WB».".encode("utf-8"))
            return None
        APP.last = time.time()
        APP.bye_at = None
        return u.path

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
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
        try:
            if path == "/":
                self._send(200, DASH.build_html(nav("dash")), "text/html; charset=utf-8")
            elif path == "/budgets":
                self._send(200, PAGE.replace("__TOKEN__", APP.token).replace("<!--NAV-->", nav("budgets")),
                           "text/html; charset=utf-8")
            elif path == "/data":
                try:
                    APP.data = collect()
                finally:
                    PROG.end()
                self._send(200, APP.data)
            elif path == "/recalc":
                self._send(200, RECALC.status())
            elif path == "/progress":
                self._send(200, {**PROG.get(), "recalc": RECALC.status()})
            elif path == "/ping":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "нет такой страницы"})
        except Exception as e:      # noqa: BLE001 — показать на странице, а не уронить сервер
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

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
        if path == "/recalc":
            started = RECALC.start()
            self._send(200, {**RECALC.status(), "started_now": started})
            return
        if path in ("/preview", "/save"):
            if APP.data is None:
                try:
                    APP.data = collect()
                finally:
                    PROG.end()
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
            write_log(["ЗАПИСАНО (страница «Бюджеты»), резервная копия " + name] + lines + [f"! {w}" for w in warns])
            APP.data = None
            self._send(200, {"saved": len(norm), "backup": name})
            return
        if path == "/toggle":
            try:
                cab, cid, on = str(body.get("cab") or ""), int(body.get("id")), bool(body.get("on"))
            except (TypeError, ValueError):
                self._send(200, {"ok": False, "text": "неверный запрос"})
                return
            with _write_lock:
                try:
                    ok, text = toggle(cab, cid, on)
                except B.ApiError as e:
                    ok, text = False, f"WB не ответил: {e}"
            write_log([("ВКЛЮЧЕНИЕ" if on else "ВЫКЛЮЧЕНИЕ") + f" {cab} {cid}: " + ("" if ok else "НЕ ВЫШЛО — ") + text])
            APP.data = None
            self._send(200, {"ok": ok, "text": text})
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
        busy = RECALC.status().get("running")
        if not busy and (time.time() - APP.last > IDLE_MIN * 60 or (APP.bye_at and time.time() - APP.bye_at > 15)):
            APP.server.shutdown()
            return


def open_browser(url):
    br = sys.argv[sys.argv.index("--browser") + 1] if "--browser" in sys.argv[:-1] else None
    if br and Path(br).exists():
        subprocess.Popen([br, url])
    else:
        webbrowser.open(url)


def fallback_static():
    """Сервер не поднялся — как раньше: собрать wb_dashboard.html и открыть файл."""
    DASH.OUT.write_text(DASH.build_html(), encoding="utf-8")
    open_browser(DASH.OUT.as_uri())


def main():
    if sys.stdout is None:                      # запуск ярлыком через pythonw.exe — консоли нет
        sys.stdout = sys.stderr = open(OUT, "a", encoding="utf-8")
    # уже открыта — показать ту же страницу, второй сервер не поднимать
    try:
        old = json.loads(LOCK.read_text(encoding="utf-8"))
        with urllib.request.urlopen(f"http://127.0.0.1:{old['port']}/ping?k={old['token']}", timeout=2) as r:
            if r.status == 200:
                open_browser(f"http://127.0.0.1:{old['port']}/?k={old['token']}")
                return
    except (OSError, ValueError, KeyError):
        pass
    want = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv[:-1] else PORT
    try:
        try:
            APP.server = ThreadingHTTPServer(("127.0.0.1", want), Handler)
        except OSError:
            APP.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)   # постоянный порт занят — любой свободный
    except OSError as e:
        print(f"{dt.datetime.now():%d.%m.%Y %H:%M} — сервер не поднялся ({e}), открываю файл дашборда", flush=True)
        fallback_static()
        return
    port = APP.server.server_address[1]
    LOCK.write_text(json.dumps({"pid": os.getpid(), "port": port, "token": APP.token}), encoding="utf-8")
    url = f"http://127.0.0.1:{port}/?k={APP.token}"
    print(f"{dt.datetime.now():%d.%m.%Y %H:%M} — «Реклама WB»: http://127.0.0.1:{port}/", flush=True)
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
<title>Реклама WB — бюджеты</title>
<style>
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;
--ring:rgba(11,11,11,.10);--s1:#2a78d6;--s1-wash:rgba(42,120,214,.10);--good:#006300;--bad:#d03b3b;--warn:#9a5b00;
--warn-wash:rgba(250,178,25,.16);--bad-wash:rgba(208,59,59,.10);--good-wash:rgba(0,99,0,.08);--chg:rgba(250,178,25,.22)}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;
--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--ring:rgba(255,255,255,.10);--s1:#3987e5;--s1-wash:rgba(57,135,229,.14);
--good:#0ca30c;--bad:#e66767;--warn:#fab219;--warn-wash:rgba(250,178,25,.12);--bad-wash:rgba(230,103,103,.12);--good-wash:rgba(12,163,12,.12);--chg:rgba(250,178,25,.20)}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;
--grid:#2c2c2a;--ring:rgba(255,255,255,.10);--s1:#3987e5;--s1-wash:rgba(57,135,229,.14);--good:#0ca30c;--bad:#e66767;--warn:#fab219;
--warn-wash:rgba(250,178,25,.12);--bad-wash:rgba(230,103,103,.12);--good-wash:rgba(12,163,12,.12);--chg:rgba(250,178,25,.20)}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1500px;margin:0 auto;padding:20px 16px 0}
.wbnav{display:flex;gap:4px;margin:0 0 14px;border-bottom:1px solid var(--grid)}
.wbnav a{padding:8px 14px;color:var(--ink2);text-decoration:none;border-bottom:2px solid transparent;margin-bottom:-1px;font-weight:500}
.wbnav a[aria-current="page"]{color:var(--ink);border-bottom-color:var(--s1)}.wbnav a:hover{color:var(--ink)}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 16px;margin-bottom:6px}h1{font-size:20px;margin:0;font-weight:600}
.sub{color:var(--ink2);font-size:13px}.spacer{flex:1}
.seg{display:inline-flex;border:1px solid var(--ring);border-radius:8px;overflow:hidden;background:var(--surface)}
.seg button{border:0;background:none;color:var(--ink2);padding:6px 12px;font:inherit;cursor:pointer}
.seg button[aria-pressed="true"]{background:var(--ink);color:var(--surface)}
.tools{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:10px 0}
input[type=search]{font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--ring);border-radius:8px;padding:6px 10px;min-width:min(420px,100%)}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:12px;margin:0 0 12px;overflow:hidden}
.cabhead{display:flex;gap:10px 16px;align-items:center;flex-wrap:wrap;padding:12px 14px;border-bottom:1px solid var(--grid)}
.cabhead h2{margin:0;font-size:16px;font-weight:600}
.pill{font-size:12px;padding:1px 8px;border-radius:999px;background:var(--grid);color:var(--ink2);white-space:nowrap}
.pill.on{background:var(--s1-wash);color:var(--s1)}.pill.w{background:var(--warn-wash);color:var(--warn)}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:8px 10px;border-bottom:1px solid var(--grid);vertical-align:top;text-align:left}
th{font-size:12px;color:var(--ink2);font-weight:600;background:var(--surface)}th.num,td.num{text-align:right;white-space:nowrap}
td{font-variant-numeric:tabular-nums}th.wk,td.wk{background:var(--s1-wash)}
.cidrow{display:flex;align-items:center;justify-content:space-between;gap:10px}
.tg{display:inline-flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;color:var(--ink2);user-select:none;position:relative}
.tg input{position:absolute;opacity:0;width:1px;height:1px;margin:0}
.tg i{position:relative;width:34px;height:20px;border-radius:999px;background:var(--grid);transition:background .2s;flex:0 0 auto}
.tg i::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:var(--surface);box-shadow:0 1px 2px rgba(0,0,0,.3);transition:left .2s}
.tg input:checked+i{background:var(--s1)}.tg input:checked+i::after{left:16px}
.tg input:focus-visible+i{outline:2px solid var(--s1);outline-offset:2px}.tg input:disabled+i{opacity:.5}
.cid{font-weight:600}.name{color:var(--ink2);font-size:12px}.badges{margin-top:4px;display:flex;gap:4px;flex-wrap:wrap}
.arts{display:flex;flex-wrap:wrap;gap:4px;max-width:440px}.art{font-size:12px;padding:1px 6px;border:1px solid var(--ring);border-radius:6px;white-space:nowrap}
.art b{font-weight:600}.art span{color:var(--muted)}
input.v{width:92px;padding:5px 7px;text-align:right;border:1px solid var(--ring);border-radius:7px;background:var(--surface);color:var(--ink);font:inherit;font-variant-numeric:tabular-nums}
input.v.changed{background:var(--chg);border-color:var(--warn)}.hint{font-size:11px;color:var(--muted);margin-top:2px}
.rec{min-width:230px;max-width:340px}.rec .n{font-weight:600}.rec .why{font-size:12px;color:var(--ink2);margin-top:2px}
.up{color:var(--good)}.down{color:var(--bad)}
button.b{padding:6px 12px;border-radius:8px;border:1px solid var(--ring);background:var(--surface);color:var(--ink);font:inherit;cursor:pointer}
button.b.p{background:var(--ink);border-color:var(--ink);color:var(--surface)}button.b.sm{padding:2px 8px;font-size:12px;margin-top:4px}
button.b:disabled{opacity:.45;cursor:default}
details.other{padding:8px 14px}details.other .why{font-size:12px;color:var(--ink2);margin-top:2px}summary{cursor:pointer;color:var(--ink2);font-size:13px}
.bar{position:sticky;bottom:0;background:var(--surface);border-top:1px solid var(--ring);padding:10px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 -16px}
#plan .box{border-radius:10px;padding:10px 14px;margin:0 0 10px}.box ul{margin:4px 0;padding-left:18px}
.box.e{background:var(--bad-wash);color:var(--bad)}.box.w{background:var(--warn-wash);color:var(--warn)}
.box.o{background:var(--good-wash);color:var(--good)}.box.i{background:var(--surface);border:1px solid var(--ring)}
.err{color:var(--bad);padding:12px 14px}.muted{color:var(--muted)}
.prog{display:none;align-items:center;gap:10px;flex:1;min-width:260px}.prog.on{display:flex}.prog.on+.spacer{display:none}
.prog .track{position:relative;flex:0 0 200px;height:8px;border-radius:4px;background:var(--grid);overflow:hidden}
.prog .fill{position:absolute;top:0;bottom:0;left:0;width:0;border-radius:4px;background:var(--s1);transition:width .4s ease}
.prog.ind .fill{width:30%;transition:none;animation:wbl 1.1s ease-in-out infinite}
@keyframes wbl{0%{left:-30%}100%{left:100%}}
.prog .pt{flex:1;min-width:0;font-size:12.5px;line-height:1.35;color:var(--ink2)}
</style></head><body><div class="wrap">
<!--NAV-->
<header><h1>Бюджеты кампаний</h1><span class="sub" id="sub">Загружаю кампании из WB…</span><span class="spacer"></span>
<div class="seg" role="group" aria-label="Тема"><button data-theme-btn="auto" aria-pressed="true">Авто</button><button data-theme-btn="light" aria-pressed="false">Светлая</button><button data-theme-btn="dark" aria-pressed="false">Тёмная</button></div></header>
<div class="sub" id="recinfo"></div>
<div class="tools"><input type="search" id="q" placeholder="Найти: номер кампании, артикул, название…">
<label class="sub"><input type="checkbox" id="mine" checked> только под управлением</label><label class="sub"><input type="checkbox" id="act"> только работающие</label><span class="sub" id="cnt"></span></div>
<div id="cabs"></div><div id="plan"></div>
<div class="bar"><button class="b p" id="chk" disabled>Проверить изменения</button><button class="b" id="save" disabled>Сохранить</button>
<button class="b" id="reset" disabled>Сбросить правки</button>
<div class="prog" id="prog" role="progressbar" aria-live="polite"><div class="track"><div class="fill"></div></div><span class="pt"></span></div>
<span class="spacer"></span><button class="b" id="undo">Отменить последнее сохранение</button></div>
</div>
<script>
const K="__TOKEN__";let DATA=null,lastDigest=null;
const fmt=v=>v==null?"—":Math.round(v).toLocaleString("ru-RU");
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const dm=s=>s?s.slice(0,10).split("-").reverse().slice(0,2).join("."):"";
const api=(p,b)=>fetch(p+"?k="+K,b?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)}:{}).then(r=>r.json());
function setTheme(t){if(t==="auto")document.documentElement.removeAttribute("data-theme");else document.documentElement.setAttribute("data-theme",t);
 document.querySelectorAll("[data-theme-btn]").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.themeBtn===t)));try{localStorage.setItem("wbdash-theme",t)}catch(e){}}
document.querySelectorAll("[data-theme-btn]").forEach(b=>b.onclick=()=>setTheme(b.dataset.themeBtn));
try{setTheme(localStorage.getItem("wbdash-theme")||"auto")}catch(e){setTheme("auto")}
function arts(nms){if(!nms.length)return '<span class="muted">без товаров</span>';
 const chip=x=>`<span class="art" title="${esc(x.title)}">${x.vendor?`<b>${esc(x.vendor)}</b> <span>${x.nm}</span>`:`<b>${x.nm}</b>`}</span>`;
 if(nms.length<=8)return `<div class="arts">${nms.map(chip).join("")}</div>`;
 return `<div class="arts">${nms.slice(0,8).map(chip).join("")}</div><details><summary>ещё ${nms.length-8}</summary><div class="arts">${nms.slice(8).map(chip).join("")}</div></details>`}
function inp(cab,id,f,cur,base,dis){if(dis)return '<span class="muted">—</span>';
 return `<input class="v" inputmode="numeric" data-cab="${esc(cab)}" data-id="${id}" data-f="${f}" data-orig="${cur??""}" value="${cur??""}">`+
 (base!=null&&base!==cur?`<div class="hint">в правилах ${fmt(base)}</div>`:"")}
function badges(r){const b=[`<span class="pill">${r.pay}</span>`,`<span class="pill ${r.active?"on":""}">${r.status}</span>`];
 r.flags.forEach(x=>b.push(`<span class="pill w">${x}</span>`));
 if(r.manual)b.push(`<span class="pill on">вручную ${dm(r.manual)}</span>`);
 if(r.raised)b.push(`<span class="pill on">wb_budget ${dm(r.raised)}</span>`);
 if(r.until)b.push(`<span class="pill">правило до ${dm(r.until)}</span>`);return b.join("")}
function recCell(cab,r){const R=r.rec;if(!R)return `<div class="rec"><span class="muted">—</span>${r.rec_text?`<div class="why">${esc(r.rec_text)}</div>`:""}</div>`;
 const cur=r.topup?r.cur.topup_week_cap:(r.cur.daily_limit||0)*7, w=R.week, cls=w>cur?"up":w<cur?"down":"", ar=w>cur?"↑":w<cur?"↓":"=";
 return `<div class="rec"><span class="n ${cls}">${ar} ${fmt(w)} ₽/нед</span>`+(R.daily?` <span class="sub">· ${fmt(R.daily)} ₽/сут</span>`:"")+
 `<div class="why">${esc(r.rec_text||"")}</div><button class="b sm" data-take="${esc(cab)}|${r.id}">Взять</button></div>`}
function take(cab,id){const c=DATA.cabinets.find(x=>x.cab===cab),r=c&&c.camps.find(x=>String(x.id)===String(id));if(!r||!r.rec)return;
 const set=(f,v)=>{const i=document.querySelector(`input.v[data-cab="${CSS.escape(cab)}"][data-id="${id}"][data-f="${f}"]`);if(i&&v!=null){i.value=v;onEdit({target:i})}};
 if(r.topup){set("topup_week_cap",r.rec.week);if(r.rec.daily)set("topup_day_cap",r.rec.daily)}if(r.rec.daily)set("daily_limit",r.rec.daily)}
function render(){const el=document.getElementById("cabs");el.innerHTML="";
 DATA.cabinets.forEach(c=>{const s=document.createElement("div");s.className="card";
  if(c.error){s.innerHTML=`<div class="cabhead"><h2>${esc(c.cab)}</h2></div><div class="err">Не загрузилось из WB: ${esc(c.error)}</div>`;el.appendChild(s);return}
  const wb=c.week_budget,act=c.camps.filter(r=>r.topup&&r.active),sum=act.reduce((a,r)=>a+(r.cur.topup_week_cap||0),0),
   rsum=act.reduce((a,r)=>a+(r.rec?r.rec.week:(r.cur.topup_week_cap||0)),0);
  let h=`<div class="cabhead"><h2>${esc(c.cab)}</h2>`+(c.experiment?`<span class="pill w">эксперимент до ${dm(c.experiment)}</span>`:"")+
   (wb.cur!=null?`<span>Недельный бюджет кабинета ${inp(c.cab,"_week_budget","_week_budget",wb.cur,wb.base)}</span><span class="sub">пополнено за 7 дн.: ${fmt(wb.topped)} ₽</span>`:`<span class="sub">недельного бюджета кабинета нет</span>`)+
   `<span class="sub">под управлением ${c.camps.length} из ${c.camps.length+c.other.length}</span>`+
   `<span class="sub">недельные потолки работающих: ${fmt(sum)} ₽ · рекомендуется ${fmt(rsum)} ₽</span><span class="spacer"></span>`+
   `<button class="b sm" data-takeall="${esc(c.cab)}">Взять все рекомендации кабинета</button></div>`;
  h+=`<div class="scroll"><table><thead><tr><th>Кампания</th><th>Артикулы в кампании</th><th class="num">Дневной лимит, ₽</th><th class="num">Пополнений в сутки, ₽</th><th class="num wk">Недельный потолок, ₽</th><th>Рекомендуется</th><th class="num">Пополнено за 7 дн.</th></tr></thead><tbody>`;
  c.camps.forEach(r=>{const hay=(r.id+" "+r.name+" "+r.nms.map(x=>x.vendor+" "+x.nm+" "+x.title).join(" ")).toLowerCase();
   const tg=`<label class="tg" title="${r.on?"Выключить: пауза и стоп-лист — скрипты не запустят и не пополнят":"Включить: убрать из стоп-листа и запустить"}">`+
    `<input type="checkbox" data-tg="${esc(c.cab)}|${r.id}"${r.on?" checked":""} aria-label="${r.on?"Выключить":"Включить"} кампанию ${r.id}"><i></i><span>${r.on?"вкл":"выкл"}</span></label>`;
   h+=`<tr data-hay="${esc(hay)}" data-act="${r.active?1:0}"><td><div class="cidrow"><span class="cid">${r.id}</span>${tg}</div><div class="name">${esc(r.name)}</div><div class="badges">${badges(r)}</div></td>`+
   `<td>${arts(r.nms)}</td><td class="num">${inp(c.cab,r.id,"daily_limit",r.cur.daily_limit,r.base.daily_limit,false)}</td>`+
   `<td class="num">${inp(c.cab,r.id,"topup_day_cap",r.cur.topup_day_cap,r.base.topup_day_cap,!r.topup)}</td>`+
   `<td class="num wk">${inp(c.cab,r.id,"topup_week_cap",r.cur.topup_week_cap,r.base.topup_week_cap,!r.topup)}</td>`+
   `<td>${recCell(c.cab,r)}</td><td class="num">${r.topup?fmt(r.topped)+" из "+fmt(r.cur.topup_week_cap):'<span class="muted">без пополнений</span>'}</td></tr>`});
  h+=`</tbody></table></div>`;
  if(c.other.length)h+=`<details class="other"><summary>Не под управлением автопилота — только для справки (${c.other.length})</summary><div class="scroll"><table><tbody>`+
   c.other.map(r=>`<tr data-hay="${esc((r.id+" "+r.name+" "+r.nms.map(x=>x.vendor+" "+x.nm).join(" ")).toLowerCase())}" data-act="${r.active?1:0}"><td><div class="cid">${r.id}</div><div class="name">${esc(r.name)}</div><div class="badges"><span class="pill">${r.pay}</span><span class="pill">${r.status}</span></div><div class="why">${esc(r.why||"")}</div></td><td>${arts(r.nms)}</td></tr>`).join("")+`</tbody></table></div></details>`;
  s.innerHTML=h;el.appendChild(s)});
 document.querySelectorAll("input.v").forEach(i=>i.addEventListener("input",onEdit));
 document.querySelectorAll("[data-take]").forEach(b=>b.onclick=()=>{const [c,i]=b.dataset.take.split("|");take(c,i)});
 document.querySelectorAll("[data-tg]").forEach(cb=>cb.onclick=e=>{e.preventDefault();toggleCamp(cb)});
 document.querySelectorAll("[data-takeall]").forEach(b=>b.onclick=()=>{const c=DATA.cabinets.find(x=>x.cab===b.dataset.takeall);c.camps.forEach(r=>{if(r.rec&&r.active)take(c.cab,r.id)})});
 filter()}
function changes(){return [...document.querySelectorAll("input.v")].filter(i=>i.value.trim()!==i.dataset.orig).map(i=>({cab:i.dataset.cab,id:i.dataset.id,field:i.dataset.f,value:i.value.trim()}))}
function onEdit(e){if(e.target.dataset)e.target.classList.toggle("changed",e.target.value.trim()!==e.target.dataset.orig);
 const n=changes().length;lastDigest=null;document.getElementById("save").classList.remove("p");if(typeof syncButtons==="function")syncButtons();
 document.getElementById("plan").innerHTML=n?'<div class="box i">Несохранённых правок: '+n+'. Нажмите «Проверить изменения».</div>':""}
function filter(){const q=document.getElementById("q").value.trim().toLowerCase(),act=document.getElementById("act").checked,
  mine=document.getElementById("mine").checked;let n=0;
 try{localStorage.setItem("wbBudgetsMine",mine?"1":"0")}catch(e){}
 document.querySelectorAll("details.other").forEach(d=>{d.style.display=mine?"none":"";if(!mine&&(q||act))d.open=true});
 document.querySelectorAll("tr[data-hay]").forEach(tr=>{const v=(!q||tr.dataset.hay.includes(q))&&(!act||tr.dataset.act==="1");tr.style.display=v?"":"none";
  if(v&&!(mine&&tr.closest("details.other")))n++});
 document.getElementById("cnt").textContent=q||act||mine?("показано кампаний: "+n):""}
function box(cls,title,items){return items&&items.length?`<div class="box ${cls}"><b>${title}</b><ul>${items.map(x=>"<li>"+esc(x)+"</li>").join("")}</ul></div>`:""}
// ---------- шкала прогресса: каждое действие идёт через busy(); долгие — с этапами с сервера (/progress) ----------
const $id=id=>document.getElementById(id);let STACK=[],LOCKS=0,PT=null,RECALC_ON=false;
const fmtSec=s=>s<60?s+" с":Math.floor(s/60)+" мин "+(s%60)+" с";
function toggleCamp(cb){const [cab,id]=cb.dataset.tg.split("|"),c=DATA.cabinets.find(x=>x.cab===cab),r=c&&c.camps.find(x=>String(x.id)===id);
 if(!r||LOCKS>0)return;
 if(changes().length){alert("Сначала сохраните или сбросьте правки лимитов: после переключения таблица загрузится заново.");return}
 const on=!r.on,msg=on?`Включить «${r.name}» (${r.id})?\n\nУберу её из стоп-листа и запущу`+(r.own_limit?
   ". Запустит частая проверка лимита в ближайшие 5 минут, по графику расхода (если дневной лимит уже потрачен — завтра утром; ночью — после 7:00).":".")
  :`Выключить «${r.name}» (${r.id})?\n\nКампания встанет на паузу и попадёт в стоп-лист: автопилот и проверки не будут её запускать и пополнять. Деньги в её бюджете останутся. Включить обратно — этим же переключателем.`;
 if(!confirm(msg))return;
 busy(on?"Включаю кампанию…":"Выключаю кампанию…",true,async()=>{const res=await api("/toggle",{cab,id:+id,on});await load();
  $id("plan").innerHTML=`<div class="box ${res.ok?"o":"e"}">${esc(res.text||res.error||"нет ответа")}</div>`;$id("plan").scrollIntoView({behavior:"smooth"})})}
function syncButtons(){const n=changes().length,lk=LOCKS>0;document.querySelectorAll("[data-tg]").forEach(x=>x.disabled=lk);$id("chk").disabled=lk||!n;$id("reset").disabled=lk||!n;
 $id("save").disabled=lk||!lastDigest;$id("undo").disabled=lk;const r=$id("recalc");if(r)r.disabled=lk||RECALC_ON}
function showProg(pct,text){const p=$id("prog");p.classList.add("on");p.classList.toggle("ind",pct==null);
 p.querySelector(".fill").style.width=pct==null?"":Math.max(3,Math.round(pct*100))+"%";p.querySelector(".pt").textContent=text;
 if(pct==null)p.removeAttribute("aria-valuenow");else p.setAttribute("aria-valuenow",Math.round(pct*100))}
async function tickProg(){if(!STACK.length)return;let s={};try{s=await fetch("/progress?k="+K).then(r=>r.json())}catch(e){}
 if(!STACK.length)return;const top=STACK[STACK.length-1],el=Math.round((Date.now()-top.t0)/1000);let pct=null,text=top.label;
 if(s.op&&s.total){pct=s.done/s.total;text=s.text}
 else if(top.recalc&&s.recalc&&s.recalc.running){const f=s.recalc.stage;text="Пересчёт рекомендаций: "+(f?f.text:"запускаю…");
  pct=f?(f.done?1:(f.i*f.stages+f.stage+.5)/(f.n*f.stages)):null}
 else if(top.recalc&&s.recalc&&s.recalc.finished){pct=1;text=s.recalc.rc?"Пересчёт рекомендаций не удался":"Пересчёт рекомендаций готов — обновляю страницу"}
 showProg(pct,text+" · "+fmtSec(el))}
async function busy(label,lock,fn,recalc){const it={label,t0:Date.now(),recalc:!!recalc};STACK.push(it);if(lock)LOCKS++;syncButtons();
 showProg(null,label);if(!PT)PT=setInterval(tickProg,500);
 try{return await fn()}finally{STACK.splice(STACK.indexOf(it),1);if(lock)LOCKS--;
  if(!STACK.length){clearInterval(PT);PT=null;$id("prog").classList.remove("on")}else tickProg();syncButtons()}}
function recInfo(d){const r=d.recalc||{},el=$id("recinfo");
 let t=d.rec_at?`Рекомендации посчитаны ${d.rec_at.slice(8,10)}.${d.rec_at.slice(5,7)} ${d.rec_at.slice(11,16)} (wb_budget.py, раз в сутки после утреннего автопилота). `:"Рекомендаций ещё нет. ";
 if(r.running){el.innerHTML=esc(t)+`<b>Пересчитываю с ${r.started}…</b> Ход — на шкале внизу; править можно, страница обновится сама.`;if(!RECALC_ON)waitRecalc();return}
 el.innerHTML=esc(t)+(r.finished&&r.rc?`<span class="down">Пересчёт в ${r.finished} не удался (код ${r.rc}) — журнал wb_web_out.txt. </span>`:"")+`<button class="b sm" id="recalc">Пересчитать сейчас</button>`;
 $id("recalc").onclick=async()=>{$id("recalc").disabled=true;const s=await api("/recalc",{});d.recalc=s;recInfo(d)};syncButtons()}
async function waitRecalc(){RECALC_ON=true;syncButtons();
 await busy("Пересчёт рекомендаций…",false,async()=>{for(;;){await new Promise(r=>setTimeout(r,2000));const s=await api("/recalc");if(!s.running){DATA.recalc=s;return}}},true);
 RECALC_ON=false;if(changes().length){recInfo(DATA);$id("plan").innerHTML='<div class="box i">Рекомендации пересчитаны. Чтобы увидеть их, сохраните или сбросьте правки — страница обновится.</div>'}else await load()}
async function load(){return busy("Загружаю кампании из WB…",true,async()=>{$id("sub").textContent="Загружаю кампании из WB…";
 const d=await api("/data");if(d.error){$id("sub").innerHTML='<span class="err">'+esc(d.error)+'</span>';return}
 DATA=d;lastDigest=null;$id("save").classList.remove("p");$id("sub").textContent=`Кампании и артикулы — из WB на ${d.at}. Снижение потолков действует сразу, повышение — с ближайшего автопилота (8:00, 13:00, 18:00); свой дневной лимит — за 5 минут. Деньги здесь не переводятся.`;
 render();recInfo(d);$id("undo").title=d.last_backup?("вернёт файл поправок как до "+d.last_backup):"копий ещё нет"})}
try{if(localStorage.getItem("wbBudgetsMine")==="0")$id("mine").checked=false}catch(e){}
$id("q").addEventListener("input",filter);$id("act").addEventListener("change",filter);$id("mine").addEventListener("change",filter);
$id("chk").onclick=()=>busy("Проверяю правки…",true,async()=>{const r=await api("/preview",{changes:changes()});
 $id("plan").innerHTML=box("e","Не принято:",r.errors)+(r.lines&&r.lines.length?box("i","Будет записано:",r.lines):'<div class="box i">Менять нечего — значения те же.</div>')+box("w","Обратите внимание:",r.warnings);
 lastDigest=(!r.errors||!r.errors.length)&&r.lines&&r.lines.length?r.digest:null;$id("save").classList.toggle("p",!!lastDigest);
 $id("plan").scrollIntoView({behavior:"smooth"})});
$id("save").onclick=()=>busy("Сохраняю…",true,async()=>{const r=await api("/save",{changes:changes(),digest:lastDigest});
 if(r.errors&&r.errors.length){$id("plan").innerHTML=box("e","Не записано:",r.errors);return}
 await load();$id("plan").innerHTML=`<div class="box o">Записано правок: ${r.saved} (резервная копия ${esc(r.backup)}). Отменить — «Отменить последнее сохранение».</div>`});
$id("reset").onclick=()=>{render();onEdit({target:{}});$id("plan").innerHTML=""};
$id("undo").onclick=()=>{if(!confirm("Вернуть поправки как до последнего сохранения (вашего или wb_budget.py)? Применяется сразу."))return;
 busy("Отменяю последнее сохранение…",true,async()=>{const r=await api("/undo",{});await load();
  $id("plan").innerHTML=r.undone?`<div class="box o">Отменено: поправки как до ${esc(r.undone)}.</div>`:'<div class="box w">Отменять нечего — копий нет.</div>'})};
setInterval(()=>fetch("/ping?k="+K).catch(()=>{}),60000);
window.addEventListener("pagehide",()=>navigator.sendBeacon("/bye?k="+K));
load();
</script></body></html>"""


if __name__ == "__main__":
    main()
