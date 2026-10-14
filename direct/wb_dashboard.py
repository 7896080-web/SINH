# -*- coding: utf-8 -*-
"""
Дашборд Wildberries — главные показатели рекламы и автопилота на одной странице (wb_dashboard.html).
Только чтение: к WB не обращается, токен не нужен — берёт то, что уже накоплено на компьютере:
  • wb_history.db (копит wb_positions.py каждый день): реклама по товарам и дням, все заказы, позиции, остатки;
  • wb_bids_state.json, wb_bids_backups\\, wb_guard_backups\\, журналы — что делали автопилот и часовая проверка.

На странице (фильтр периода 7 / 14 / 30 / 90 дней и кабинета — сверху, действует на всё):
  1. Плитки: расход рекламы, заказы с рекламы, цена заказа с рекламы, все заказы, ДРР общий (реклама ÷ все заказы),
     ДРР рекламный (реклама ÷ заказы с рекламы, выручка — заказы × средняя цена), доля заказов с рекламы —
     с изменением к прошлому такому же периоду и мини-графиком.
  2. Графики по дням: расход; все заказы и заказы с рекламы; цена заказа с рекламы;
     позиция в поиске (медиана) у 4 товаров с наибольшим расходом.
  3. Автопилот: последние запуски (что изменил), пополнения бюджетов за 7 дней против потолков, последняя часовая проверка.
  4. Таблица товаров: расход, клики, заказы, цена заказа, конверсия, позиция, остаток — с поиском и сортировкой.
У каждого графика есть «Таблица» — те же цифры без графика.

Запуск:  python wb_dashboard.py            (соберёт страницу и откроет её в браузере)
         python wb_dashboard.py --no-open  (только собрать — так делает расписание после каждого запуска)
         ярлык «Реклама WB» на рабочем столе — то же, что первая команда (ставится wb_dashboard_shortcut.ps1)
Данные свежие настолько, насколько свежий последний запуск wb_positions.py (каждое утро).
Результат: wb_dashboard.html (открывается двойным щелчком, интернет не нужен).
"""
import datetime as dt
import json
import re
import sqlite3
import sys
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "wb_history.db"
STATE = HERE / "wb_bids_state.json"
OUT = HERE / "wb_dashboard.html"
DAYS_KEEP = 120          # сколько дней истории класть в страницу


def load_db():
    res = {"cabinets": [], "products": {}, "ad": {}, "fn": {}, "pos": {}, "stock": {}}
    if not DB.exists():
        return res
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    since = (dt.date.today() - dt.timedelta(days=DAYS_KEEP)).isoformat()
    q = lambda sql, args=(): con.execute(sql, args).fetchall()
    cabs = sorted({r[0] for r in q("SELECT DISTINCT cab FROM ad_day UNION SELECT DISTINCT cab FROM funnel_day")})
    res["cabinets"] = cabs
    for cab in cabs:
        res["products"][cab] = {str(nm): [v or "", t or ""] for nm, v, t in
                                q("SELECT nm, vendor, title FROM products WHERE cab=?", (cab,))}
        res["ad"][cab] = [[nm, d, v or 0, c or 0, s or 0, o or 0] for nm, d, v, c, s, o in
                          q("SELECT nm, date, views, clicks, spend, orders FROM ad_day WHERE cab=? AND date>=?", (cab, since))]
        res["fn"][cab] = [[nm, d, o or 0, s or 0] for nm, d, o, s in
                          q("SELECT nm, date, orders, order_sum FROM funnel_day WHERE cab=? AND date>=?", (cab, since))]
        res["pos"][cab] = [[nm, d, p] for nm, d, p in
                           q("SELECT nm, date, median_pos FROM product_pos_day WHERE cab=? AND date>=? "
                             "AND median_pos IS NOT NULL", (cab, since))]
        st = {}
        for nm, d, wb, mp in q("SELECT nm, date, stock_wb, stock_mp FROM stock_day WHERE cab=? ORDER BY date", (cab,)):
            st[str(nm)] = [wb, mp, d]
        res["stock"][cab] = st
    con.close()
    return res


def load_autopilot():
    ap = {"runs": [], "guard_runs": [], "topups": [], "rules": {}, "caps_at": None, "guard_last": [], "bids_last": None,
          "budget": None}
    try:                                   # предложения wb_budget.py (пробный прогон раз в сутки после автопилота)
        ap["budget"] = json.loads((HERE / "wb_budget_proposals.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    ap["caps_at"] = (state.get("caps") or {}).get("at")
    for cid, t in (state.get("topup_rules") or {}).items():
        ap["rules"][cid] = {"name": t.get("name"), "week_cap": t.get("week_cap"), "day_cap": t.get("day_cap"),
                            "cabinet": t.get("cabinet")}
    for cid, lst in (state.get("topups") or {}).items():
        for t in lst:
            ap["topups"].append({"cid": cid, "date": t.get("date"), "sum": t.get("sum")})
    stamp = lambda f: dt.datetime.strptime(re.search(r"(\d{8}_\d{6})", f.name).group(1), "%Y%m%d_%H%M%S")
    for f in sorted((HERE / "wb_bids_backups").glob("bids_*.json"))[-30:]:
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
            bids = p.get("bids") or []
            ap["runs"].append({"at": stamp(f).isoformat(timespec="minutes"),
                               "up": sum(1 for b in bids if (b.get("new") or 0) > (b.get("old") or 0)),
                               "down": sum(1 for b in bids if (b.get("new") or 0) < (b.get("old") or 0)),
                               "trim": len(p.get("trim") or []), "untrim": len(p.get("untrim") or []),
                               "limits": len(p.get("limits") or []),
                               "topup": sum(t.get("amount") or 0 for t in p.get("topup") or [])})
        except (OSError, ValueError, AttributeError):
            continue
    for f in sorted((HERE / "wb_guard_backups").glob("guard_*.json"))[-48:]:
        try:
            items = json.loads(f.read_text(encoding="utf-8"))
            ap["guard_runs"].append({"at": stamp(f).isoformat(timespec="minutes"),
                                     "up": sum(1 for b in items if b["new"] > b["old"]),
                                     "down": sum(1 for b in items if b["new"] < b["old"])})
        except (OSError, ValueError, KeyError):
            continue
    log = HERE / "wb_guard_log.txt"
    if log.exists():
        blocks = log.read_text(encoding="utf-8", errors="replace").strip().split("\n\n")
        ap["guard_last"] = [l for l in blocks[-1].splitlines() if l.strip()][:20] if blocks else []
    log = HERE / "wb_bids_log.txt"
    if log.exists():
        heads = re.findall(r"^(\d\d\.\d\d\.\d{4} \d\d:\d\d) — (ПРИМЕНЕНИЕ|пробный прогон)", log.read_text(
            encoding="utf-8", errors="replace"), re.M)
        if heads:
            ap["bids_last"] = {"at": heads[-1][0], "kind": heads[-1][1]}
    return ap


def main():
    if sys.stdout is None:          # запуск ярлыком через pythonw.exe — консоли нет, print некуда писать
        sys.stdout = sys.stderr = open(HERE / "wb_dashboard_log.txt", "a", encoding="utf-8")
    data = {"generated": dt.datetime.now().strftime("%d.%m.%Y %H:%M"), **load_db(), "autopilot": load_autopilot(),
            "has_db": DB.exists()}
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    OUT.write_text(TEMPLATE.replace("__DATA__", payload), encoding="utf-8")
    print(f"Сохранено: {OUT.name}" + ("" if DB.exists() else " (нет wb_history.db — только раздел автопилота; "
                                                             "запустите python wb_positions.py)"))
    if "--no-open" not in sys.argv:
        webbrowser.open(OUT.as_uri())


TEMPLATE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Реклама WB — дашборд</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s4: #eda100;
  --s1-wash: rgba(42,120,214,0.10);
  --good: #006300; --bad: #d03b3b; --warn: #fab219;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
    --s1-wash: rgba(57,135,229,0.12);
    --good: #0ca30c; --bad: #e66767; --warn: #fab219;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
  --s1-wash: rgba(57,135,229,0.12);
  --good: #0ca30c; --bad: #e66767; --warn: #fab219;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink); font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
.wrap { max-width: 1240px; margin: 0 auto; padding: 20px 16px 48px; }
header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 16px; margin-bottom: 14px; }
h1 { font-size: 20px; margin: 0; font-weight: 600; }
.sub { color: var(--ink2); font-size: 13px; }
.filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 0 0 16px; }
.seg { display: inline-flex; border: 1px solid var(--ring); border-radius: 8px; overflow: hidden; background: var(--surface); }
.seg button { border: 0; background: none; color: var(--ink2); padding: 6px 12px; font: inherit; cursor: pointer; }
.seg button[aria-pressed="true"] { background: var(--ink); color: var(--surface); }
select, input[type=search] { font: inherit; color: var(--ink); background: var(--surface); border: 1px solid var(--ring); border-radius: 8px; padding: 6px 10px; }
.spacer { flex: 1; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)); gap: 12px; margin-bottom: 16px; }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 14px 16px; min-width: 0; }
.tile .label { color: var(--ink2); font-size: 13px; }
.tile .value { font-size: 26px; font-weight: 600; margin: 2px 0; }
.tile .delta { font-size: 12px; color: var(--ink2); }
.tile .delta.good { color: var(--good); } .tile .delta.bad { color: var(--bad); }
.tile svg { display: block; margin-top: 6px; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 520px), 1fr)); gap: 12px; margin-bottom: 12px; }
.card h2 { font-size: 15px; font-weight: 600; margin: 0 0 2px; }
.card .note { color: var(--muted); font-size: 12px; margin: 0 0 8px; }
.chart { position: relative; width: 100%; }
.chart svg { display: block; width: 100%; overflow: visible; }
.chart svg:focus { outline: 2px solid var(--s1); outline-offset: 2px; border-radius: 4px; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 14px; color: var(--ink2); font-size: 12px; margin: 0 0 6px; }
.legend span::before { content: ""; display: inline-block; width: 14px; height: 2px; vertical-align: middle; margin-right: 6px; background: var(--k); border-radius: 1px; }
.legend.rect span::before { height: 10px; width: 10px; border-radius: 2px; }
.tip { position: absolute; pointer-events: none; background: var(--surface); border: 1px solid var(--ring); border-radius: 8px; padding: 6px 9px; font-size: 12px; box-shadow: 0 4px 14px rgba(0,0,0,.12); white-space: nowrap; z-index: 5; display: none; }
.tip .t { color: var(--ink2); margin-bottom: 2px; }
.tip .r { display: flex; align-items: center; gap: 6px; }
.tip .r i { display: inline-block; width: 12px; height: 2px; border-radius: 1px; }
.tip .r b { font-weight: 600; }
.tip .r span { color: var(--ink2); }
details { margin-top: 6px; } summary { color: var(--ink2); font-size: 12px; cursor: pointer; }
table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
th, td { padding: 5px 8px; text-align: right; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th:first-child, td:first-child, th.l, td.l { text-align: left; }
th { color: var(--ink2); font-weight: 600; position: sticky; top: 0; background: var(--surface); cursor: pointer; user-select: none; }
td { font-variant-numeric: tabular-nums; }
.scroll { overflow: auto; max-height: 560px; }
.meter { margin: 8px 0 12px; }
.meter .row { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; }
.meter .row span { color: var(--ink2); }
.meter .track { height: 8px; border-radius: 4px; background: var(--grid); overflow: hidden; margin-top: 4px; }
.meter .fill { height: 100%; border-radius: 4px; background: var(--s1); }
.meter .fill.warn { background: var(--warn); } .meter .fill.full { background: var(--bad); }
pre { white-space: pre-wrap; font: 12px/1.45 ui-monospace, Consolas, monospace; color: var(--ink2); margin: 6px 0 0; max-height: 300px; overflow: auto; }
.empty { color: var(--muted); padding: 24px 0; text-align: center; }
.status { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
.status i { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.muted { color: var(--muted); }
.bud { display: flex; gap: 10px; align-items: flex-start; padding: 8px 0; border-bottom: 1px solid var(--grid); font-size: 13px; }
.bud .mk { font-weight: 700; width: 16px; flex: none; } .bud .mk.up { color: var(--good); } .bud .mk.down { color: var(--bad); }
.bud .tx { flex: 1; min-width: 0; } .bud .tx b { font-weight: 600; }
.bud code { display: inline-block; margin-top: 4px; font: 12px ui-monospace, Consolas, monospace; background: var(--page);
  border: 1px solid var(--grid); border-radius: 6px; padding: 2px 6px; user-select: all; overflow-wrap: anywhere; }
.bud .done { color: var(--good); font-size: 12px; }
.hold { font-size: 12.5px; color: var(--ink2); padding: 3px 0; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Реклама Wildberries</h1>
    <span class="sub" id="sub"></span>
    <span class="spacer"></span>
    <div class="seg" role="group" aria-label="Тема">
      <button data-theme-btn="auto" aria-pressed="true">Авто</button><button data-theme-btn="light" aria-pressed="false">Светлая</button><button data-theme-btn="dark" aria-pressed="false">Тёмная</button>
    </div>
  </header>
  <div class="filters">
    <div class="seg" role="group" aria-label="Период" id="period">
      <button data-d="7">7 дней</button><button data-d="14">14 дней</button><button data-d="30">30 дней</button><button data-d="90">90 дней</button>
    </div>
    <select id="cab" aria-label="Кабинет"></select>
    <span class="sub" id="range"></span>
  </div>
  <div id="nodb" class="card empty" hidden>Нет wb_history.db — графики рекламы появятся после запуска <b>python wb_positions.py</b>. Раздел автопилота ниже работает.</div>
  <section id="main">
    <div class="tiles" id="tiles"></div>
    <div class="grid2">
      <div class="card"><h2>Расход рекламы по дням, ₽</h2><p class="note">Сумма по всем рекламным кампаниям кабинета</p><div class="chart" id="c-spend"></div></div>
      <div class="card"><h2>Заказы по дням</h2><p class="note">Все заказы (реклама + без рекламы) и заказы с рекламы</p><div class="chart" id="c-orders"></div></div>
      <div class="card"><h2>Цена заказа с рекламы по дням, ₽</h2><p class="note">Расход ÷ заказы с рекламы; дни без рекламных заказов — пропуск</p><div class="chart" id="c-cpo"></div></div>
      <div class="card"><h2>Позиция в поиске (медиана)</h2><p class="note">4 товара с наибольшим расходом за период; выше — лучше</p><div class="chart" id="c-pos"></div></div>
    </div>
  </section>
  <div class="grid2">
    <div class="card"><h2>Автопилот</h2><p class="note" id="ap-note"></p><div id="ap-runs"></div></div>
    <div class="card"><h2>Пополнения бюджетов за 7 дней</h2><p class="note">Сколько автопилот перевёл с «Баланса» против недельного потолка кампании</p><div id="ap-topups"></div>
      <h2 style="margin-top:14px">Последняя часовая проверка</h2><pre id="ap-guard"></pre></div>
  </div>
  <section class="card" id="bud-sec" style="margin-bottom:16px"><h2>Бюджет по окупаемости</h2>
    <p class="note" id="bud-note"></p><div id="bud"></div></section>
  <section id="prod-sec" class="card">
    <div class="filters" style="margin:0 0 8px"><h2 style="margin:0">Товары за период</h2><span class="spacer"></span><input type="search" id="q" placeholder="Поиск: артикул или название" aria-label="Поиск товара"></div>
    <div class="scroll"><table id="prod"></table></div>
  </section>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById("data").textContent);
const $ = (s) => document.querySelector(s);
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const NS = "http://www.w3.org/2000/svg";
const fmt0 = new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 0});
const fmt1 = new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 1});
const money = (v) => v == null || !isFinite(v) ? "—" : fmt0.format(v) + " ₽";
const num = (v) => v == null || !isFinite(v) ? "—" : fmt0.format(v);
const pct = (v) => v == null || !isFinite(v) ? "—" : fmt1.format(v * 100) + "%";
const compact = (v) => Math.abs(v) >= 1000 ? fmt1.format(v / 1000) + " тыс." : fmt0.format(v);
const ddmm = (iso) => iso.slice(8, 10) + "." + iso.slice(5, 7);
function el(tag, attrs, parent, text) {
  const e = document.createElementNS(tag === "div" || tag === "span" || tag === "i" || tag === "b" ? "http://www.w3.org/1999/xhtml" : NS, tag);
  for (const k in attrs || {}) e.setAttribute(k, attrs[k]);
  if (text != null) e.textContent = text;
  if (parent) parent.appendChild(e);
  return e;
}
function h(tag, attrs, parent, text) {
  const e = document.createElement(tag);
  for (const k in attrs || {}) e.setAttribute(k, attrs[k]);
  if (text != null) e.textContent = text;
  if (parent) parent.appendChild(e);
  return e;
}

// ---------- состояние фильтров ----------
let state = {days: 14, cab: D.cabinets[0] || null};
try { const s = JSON.parse(localStorage.getItem("wbdash") || "{}"); if (s.days) state.days = s.days; if (s.cab && D.cabinets.includes(s.cab)) state.cab = s.cab; } catch (e) {}
const save = () => { try { localStorage.setItem("wbdash", JSON.stringify(state)); } catch (e) {} };

// ---------- тема ----------
function setTheme(t) {
  if (t === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", t);
  document.querySelectorAll("[data-theme-btn]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.themeBtn === t)));
  try { localStorage.setItem("wbdash-theme", t); } catch (e) {}
  render();
}
document.querySelectorAll("[data-theme-btn]").forEach(b => b.onclick = () => setTheme(b.dataset.themeBtn));

// ---------- данные за период ----------
// дата по часам компьютера (toISOString даёт UTC: с 00:00 до 03:00 по Москве это ещё «вчера»)
function localISO(d) { return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); }
function dateList(end, n) {
  const out = []; const d = new Date(end + "T00:00:00Z");
  for (let i = n - 1; i >= 0; i--) { const x = new Date(d); x.setUTCDate(d.getUTCDate() - i); out.push(x.toISOString().slice(0, 10)); }
  return out;
}
function lastDate(cab) {
  let m = "";
  for (const r of D.ad[cab] || []) if (r[1] > m) m = r[1];
  for (const r of D.fn[cab] || []) if (r[1] > m) m = r[1];
  return m || localISO(new Date());
}
function aggregate(cab, dates) {
  const set = new Set(dates);
  const day = {}; dates.forEach(d => day[d] = {spend: 0, clicks: 0, views: 0, adOrders: 0, orders: 0, sum: 0, hasAd: false, hasFn: false});
  const prod = {};
  const P = (nm) => prod[nm] || (prod[nm] = {nm, spend: 0, clicks: 0, views: 0, adOrders: 0, orders: 0, sum: 0});
  for (const [nm, d, v, c, s, o] of D.ad[cab] || []) if (set.has(d)) {
    const x = day[d]; x.spend += s; x.clicks += c; x.views += v; x.adOrders += o; x.hasAd = true;
    const p = P(nm); p.spend += s; p.clicks += c; p.views += v; p.adOrders += o;
  }
  for (const [nm, d, o, s] of D.fn[cab] || []) if (set.has(d)) {
    const x = day[d]; x.orders += o; x.sum += s; x.hasFn = true;
    const p = P(nm); p.orders += o; p.sum += s;
  }
  const tot = {spend: 0, clicks: 0, adOrders: 0, orders: 0, sum: 0};
  for (const d of dates) for (const k in tot) tot[k] += day[d][k];
  return {day, prod, tot};
}

// ---------- плитки ----------
function sparkline(parent, vals, color) {
  const w = 160, hgt = 28, svg = el("svg", {viewBox: `0 0 ${w} ${hgt}`, width: "100%", height: hgt, "aria-hidden": "true"}, parent);
  const xs = vals.map((v, i) => [i, v]).filter(p => p[1] != null && isFinite(p[1]));
  if (xs.length < 2) return;
  const mn = Math.min(...xs.map(p => p[1])), mx = Math.max(...xs.map(p => p[1])), r = mx - mn || 1;
  const X = (i) => 2 + i * (w - 4) / (vals.length - 1), Y = (v) => hgt - 3 - (v - mn) / r * (hgt - 6);
  el("path", {d: xs.map((p, k) => (k ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1)).join(""), fill: "none", stroke: css("--muted"), "stroke-width": 1.5, "stroke-linejoin": "round", "stroke-linecap": "round"}, svg);
  const lp = xs[xs.length - 1];
  el("circle", {cx: X(lp[0]), cy: Y(lp[1]), r: 3, fill: color}, svg);
}
function tile(parent, label, value, cur, prev, upIsGood, series, hint) {
  const t = h("div", hint ? {class: "card tile", title: hint} : {class: "card tile"}, parent);
  h("div", {class: "label"}, t, label);
  h("div", {class: "value"}, t, value);
  let txt = "нет данных за прошлый период", cls = "delta";
  if (prev != null && isFinite(prev) && prev !== 0 && cur != null && isFinite(cur)) {
    const ch = (cur - prev) / Math.abs(prev);
    txt = (ch >= 0 ? "▲ +" : "▼ ") + fmt0.format(ch * 100) + "% к прошлым " + state.days + " дн.";
    if (upIsGood !== null && Math.abs(ch) >= 0.02) cls += ((ch > 0) === upIsGood ? " good" : " bad");
  }
  h("div", {class: cls}, t, txt);
  sparkline(t, series, css("--s1"));
}

// ---------- графики ----------
function niceTicks(mn, mx, n) {
  if (mn === mx) { mx = mn + 1; }
  const span = mx - mn, step0 = span / n, mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= n) || 10 * mag;
  const lo = Math.floor(mn / step) * step, hi = Math.ceil(mx / step - 1e-9) * step, out = [];
  for (let v = lo; v <= hi + step / 2; v += step) out.push(+v.toFixed(10));
  return out;
}
function chart(host, opt) {
  // opt: {dates, series:[{name, color, vals}], kind: "line"|"bar", fmt, invert, endLabels}
  host.textContent = "";
  const dates = opt.dates, S = opt.series;
  const all = S.flatMap(s => s.vals).filter(v => v != null && isFinite(v));
  if (!all.length) { h("div", {class: "empty"}, host, "Нет данных за период"); return; }
  if (S.length > 1) {
    const lg = h("div", {class: "legend" + (opt.kind === "bar" ? " rect" : "")}, host);
    S.forEach(s => { const sp = h("span", {}, lg, s.name); sp.style.setProperty("--k", s.color); });
  }
  const W = Math.max(host.clientWidth, 280), H = 210, m = {l: 48, r: opt.endLabels ? 70 : 14, t: 10, b: 26};
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  let mn = opt.kind === "bar" ? 0 : Math.min(...all), mx = Math.max(...all);
  if (opt.kind !== "bar" && !opt.invert) mn = Math.min(0, mn);
  if (opt.invert) { mn = Math.max(1, Math.floor(mn)); }
  let ticks = niceTicks(mn, mx, 4);
  if (opt.invert) { ticks = ticks.filter(t => t >= 1); if (ticks[0] !== 1) ticks.unshift(1); }
  mn = ticks[0]; mx = ticks[ticks.length - 1];
  const n = dates.length, band = iw / n;
  const X = (i) => m.l + band * (i + 0.5);
  const Y = (v) => opt.invert ? m.t + (v - mn) / (mx - mn || 1) * ih : m.t + ih - (v - mn) / (mx - mn || 1) * ih;
  const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, height: H, tabindex: "0", role: "img", "aria-label": opt.label || ""}, host);
  for (const t of ticks) {
    el("line", {x1: m.l, x2: W - m.r, y1: Y(t), y2: Y(t), stroke: css("--grid"), "stroke-width": 1}, svg);
    el("text", {x: m.l - 8, y: Y(t) + 4, "text-anchor": "end", fill: css("--muted"), "font-size": 11}, svg, opt.fmtAxis ? opt.fmtAxis(t) : compact(t));
  }
  el("line", {x1: m.l, x2: W - m.r, y1: m.t + ih, y2: m.t + ih, stroke: css("--axis"), "stroke-width": 1}, svg);
  const every = Math.ceil(n / Math.max(2, Math.floor(iw / 56)));
  dates.forEach((d, i) => { if ((n - 1 - i) % every === 0) el("text", {x: X(i), y: H - 8, "text-anchor": "middle", fill: css("--muted"), "font-size": 11}, svg, ddmm(d)); });
  if (opt.kind === "bar") {
    const bw = Math.max(2, Math.min(24, band - 2)), r = Math.min(4, bw / 2);
    S[0].vals.forEach((v, i) => {
      if (!v) return;
      const x = X(i) - bw / 2, y = Y(v), y0 = m.t + ih, hh = y0 - y;
      const rr = Math.min(r, hh);
      el("path", {d: `M${x} ${y0}V${y + rr}Q${x} ${y} ${x + rr} ${y}H${x + bw - rr}Q${x + bw} ${y} ${x + bw} ${y + rr}V${y0}Z`, fill: S[0].color}, svg);
    });
  } else {
    S.forEach(s => {
      let d = "", pen = false;
      s.vals.forEach((v, i) => { if (v == null || !isFinite(v)) { pen = false; return; } d += (pen ? "L" : "M") + X(i).toFixed(1) + " " + Y(v).toFixed(1); pen = true; });
      el("path", {d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round"}, svg);
      const li = s.vals.map((v, i) => [v, i]).filter(p => p[0] != null && isFinite(p[0])).pop();
      if (li) {
        el("circle", {cx: X(li[1]), cy: Y(li[0]), r: 4, fill: s.color, stroke: css("--surface"), "stroke-width": 2}, svg);
        s._end = [X(li[1]), Y(li[0]), li[0]];
      }
    });
    if (opt.endLabels) {
      const ends = S.filter(s => s._end).map(s => ({s, y: s._end[1]})).sort((a, b) => a.y - b.y);
      const clash = ends.some((e, k) => k && e.y - ends[k - 1].y < 14);
      if (!clash) ends.forEach(e => el("text", {x: e.s._end[0] + 8, y: e.y + 4, fill: css("--ink2"), "font-size": 11}, svg, (opt.fmt || num)(e.s._end[2])));
    }
  }
  // наведение: вертикальная линия и подсказка со всеми рядами
  const tip = h("div", {class: "tip"}, host);
  const cross = el("line", {y1: m.t, y2: m.t + ih, stroke: css("--axis"), "stroke-width": 1, visibility: "hidden"}, svg);
  const hit = el("rect", {x: m.l, y: m.t, width: iw, height: ih, fill: "transparent"}, svg);
  let cur = -1;
  function show(i) {
    cur = Math.max(0, Math.min(n - 1, i));
    cross.setAttribute("x1", X(cur)); cross.setAttribute("x2", X(cur)); cross.setAttribute("visibility", "visible");
    tip.textContent = ""; h("div", {class: "t"}, tip, ddmm(dates[cur]) + "." + dates[cur].slice(0, 4));
    S.forEach(s => { const r = h("div", {class: "r"}, tip); const k = h("i", {}, r); k.style.background = s.color; h("b", {}, r, (opt.fmt || num)(s.vals[cur])); h("span", {}, r, s.name); });
    tip.style.display = "block";
    const px = X(cur) / W * host.clientWidth, tw = tip.offsetWidth;
    tip.style.left = Math.min(Math.max(0, px + 12), host.clientWidth - tw) + "px";
    tip.style.top = (S.length > 1 ? 24 : 0) + "px";
  }
  function hide() { tip.style.display = "none"; cross.setAttribute("visibility", "hidden"); }
  hit.addEventListener("pointermove", (e) => { const r = svg.getBoundingClientRect(); show(Math.floor(((e.clientX - r.left) / r.width * W - m.l) / band)); });
  hit.addEventListener("pointerleave", hide);
  svg.addEventListener("focus", () => show(n - 1)); svg.addEventListener("blur", hide);
  svg.addEventListener("keydown", (e) => { if (e.key === "ArrowLeft") { show(cur - 1); e.preventDefault(); } if (e.key === "ArrowRight") { show(cur + 1); e.preventDefault(); } });
  // таблица — те же цифры без графика
  const det = h("details", {}, host); h("summary", {}, det, "Таблица");
  const tb = h("table", {}, h("div", {class: "scroll", style: "max-height:260px"}, det)), tr = h("tr", {}, h("thead", {}, tb)); h("th", {class: "l"}, tr, "Дата"); S.forEach(s => h("th", {}, tr, s.name));
  const body = h("tbody", {}, tb);
  dates.forEach((d, i) => { const r = h("tr", {}, body); h("td", {class: "l"}, r, ddmm(d)); S.forEach(s => h("td", {}, r, (opt.fmt || num)(s.vals[i]))); });
}

// ---------- автопилот ----------
function renderAutopilot() {
  const A = D.autopilot;
  const note = [];
  if (A.bids_last) note.push("последний запуск автопилота: " + A.bids_last.at + (A.bids_last.kind === "ПРИМЕНЕНИЕ" ? " (применение)" : " (пробный прогон)"));
  if (A.caps_at) note.push("окупаемые потолки от " + A.caps_at.replace("T", " ").slice(0, 16));
  $("#ap-note").textContent = note.join(" · ") || "Автопилот ещё не применялся.";
  const box = $("#ap-runs"); box.textContent = "";
  const runs = A.runs.slice(-12).reverse();
  if (!runs.length) h("div", {class: "empty"}, box, "Запусков с применением пока нет.");
  else {
    const tb = h("table", {}, h("div", {class: "scroll"}, box)), tr = h("tr", {}, h("thead", {}, tb));
    ["Когда", "Ставки ↑", "Ставки ↓", "Убрано", "Возвращено", "Лимиты", "Пополнено ₽"].forEach((t, i) => h("th", {class: i ? "" : "l"}, tr, t));
    const body = h("tbody", {}, tb);
    runs.forEach(r => { const row = h("tr", {}, body); h("td", {class: "l"}, row, r.at.replace("T", " ")); [r.up, r.down, r.trim, r.untrim, r.limits, r.topup].forEach(v => h("td", {}, row, num(v))); });
  }
  const g = A.guard_runs.slice(-24);
  if (g.length) h("p", {class: "note", style: "margin-top:10px"}, box, "Часовая проверка за последние запуски с изменениями: ставок ↑ " + g.reduce((a, r) => a + r.up, 0) + ", ↓ " + g.reduce((a, r) => a + r.down, 0) + " (запусков с изменениями: " + g.length + ")");
  // пополнения против потолка
  const tb2 = $("#ap-topups"); tb2.textContent = "";
  const since = new Date(); since.setDate(since.getDate() - 6); const s7 = localISO(since);
  const rules = Object.entries(A.rules).filter(([c, r]) => !state.cab || !r.cabinet || r.cabinet === state.cab);
  if (!rules.length) h("div", {class: "empty"}, tb2, "Правил пополнения пока нет — появятся после wb_bids.py --apply.");
  rules.sort((a, b) => (a[1].name || "").localeCompare(b[1].name || "")).forEach(([cid, r]) => {
    const used = A.topups.filter(t => t.cid === cid && t.date >= s7).reduce((a, t) => a + (t.sum || 0), 0);
    const share = r.week_cap ? used / r.week_cap : 0;
    const m = h("div", {class: "meter"}, tb2), row = h("div", {class: "row"}, m);
    h("b", {}, row, r.name || cid); h("span", {}, row, money(used) + " из " + money(r.week_cap) + (share >= 1 ? " — потолок исчерпан" : ""));
    const f = h("div", {class: "fill" + (share >= 1 ? " full" : share >= 0.8 ? " warn" : "")}, h("div", {class: "track", role: "meter", "aria-valuemin": 0, "aria-valuemax": r.week_cap, "aria-valuenow": used, "aria-label": (r.name || cid) + ": пополнено за 7 дней"}, m));
    f.style.width = Math.min(100, share * 100) + "%";
  });
  $("#ap-guard").textContent = A.guard_last.length ? A.guard_last.join("\n") : "Часовая проверка ещё не запускалась.";
}

// ---------- бюджет по окупаемости (wb_budget.py) ----------
function renderBudget() {
  const B = D.autopilot.budget, box = $("#bud"); box.textContent = "";
  if (!B) {
    $("#bud-note").textContent = "";
    h("div", {class: "empty"}, box, "Предложений пока нет — их раз в сутки после утреннего автопилота считает wb_budget.py (или запустите: python wb_budget.py).");
    return;
  }
  const at = B.at.slice(8, 10) + "." + B.at.slice(5, 7) + " " + B.at.slice(11, 16);
  $("#bud-note").textContent = "Расчёт от " + at + ". Окупаемым кампаниям, которым не хватает денег, — прибавка лимитов; не окупившуюся прибавку — вернуть. Сам расчёт ничего не меняет: применить — командой под предложением (в C:\\direct), откат — python wb_budget.py --rollback.";
  const mine = (B.items || []).filter(p => !state.cab || p.cabinet === state.cab);
  const act = mine.filter(p => p.action !== "hold"), hold = mine.filter(p => p.action === "hold");
  if (!act.length) h("div", {class: "empty", style: "padding:12px 0"}, box, "Прибавлять сейчас некому" + (state.cab ? " в этом кабинете" : "") + " — причины ниже.");
  act.forEach(p => {
    const row = h("div", {class: "bud"}, box);
    h("span", {class: "mk " + (p.action === "raise" ? "up" : "down")}, row, p.action === "raise" ? "▲" : "▼");
    const tx = h("div", {class: "tx"}, row);
    h("b", {}, tx, (p.name || p.advert_id) + (p.advert_id !== "_week_budget" ? " (" + p.advert_id + ")" : ""));
    h("div", {}, tx, p.text);
    if (p.applied) h("div", {class: "done"}, tx, "✓ записано — действует с ближайшего запуска автопилота");
    else if (p.advert_id !== "_week_budget") h("code", {}, tx, "python wb_budget.py --apply --only " + p.advert_id);
    else h("div", {class: "muted", style: "font-size:12px"}, tx, "Поднимется вместе с прибавкой кампаний кабинета.");
  });
  if (hold.length) {
    const d = h("details", {}, box); h("summary", {}, d, "Без прибавки: " + hold.length + " — почему");
    hold.forEach(p => h("div", {class: "hold"}, d, "· " + (state.cab ? "" : p.cabinet + ": ") + (p.name || p.advert_id) + " — " + p.text));
  }
}

// ---------- товары ----------
let sortKey = "spend", sortDir = -1;
function renderProducts(agg, cab) {
  const prods = Object.values(agg.prod).filter(p => p.spend > 0 || p.orders > 0);
  const names = D.products[cab] || {}, stock = D.stock[cab] || {};
  const lastPos = {}; for (const [nm, d, p] of D.pos[cab] || []) if (!lastPos[nm] || d > lastPos[nm][0]) lastPos[nm] = [d, p];
  prods.forEach(p => {
    const n = names[p.nm] || ["", ""]; p.vendor = n[0]; p.title = n[1];
    p.cpo = p.adOrders ? p.spend / p.adOrders : null; p.cr = p.clicks ? p.adOrders / p.clicks : null;
    p.cpc = p.clicks ? p.spend / p.clicks : null; p.pos = lastPos[p.nm] ? lastPos[p.nm][1] : null;
    const s = stock[p.nm]; p.stock = s ? (s[0] || 0) + (s[1] || 0) : null;
    p.drr = p.sum ? p.spend / p.sum : null;
    p.drrAd = p.orders && p.adOrders ? p.spend / (p.adOrders * p.sum / p.orders) : null;
  });
  const q = $("#q").value.trim().toLowerCase();
  const rows = prods.filter(p => !q || String(p.nm).includes(q) || (p.vendor + " " + p.title).toLowerCase().includes(q));
  rows.sort((a, b) => { const x = a[sortKey], y = b[sortKey]; if (x == null) return 1; if (y == null) return -1; return (typeof x === "string" ? x.localeCompare(y) : x - y) * sortDir; });
  const cols = [["nm", "Артикул WB", (v) => String(v), "l"], ["vendor", "Арт. продавца", (v) => v, "l"], ["spend", "Расход", money], ["clicks", "Клики", num],
    ["cpc", "₽ за клик", (v) => v == null ? "—" : fmt1.format(v)], ["adOrders", "Заказы с рекламы", num], ["cpo", "Цена заказа", money], ["cr", "Конверсия", pct],
    ["orders", "Все заказы", num], ["drr", "ДРР общий", pct], ["drrAd", "ДРР рекл.", pct],
    ["pos", "Позиция", (v) => v == null ? "—" : fmt0.format(v)], ["stock", "Остаток WB+свой", num]];
  const tb = $("#prod"); tb.textContent = "";
  const tr = h("tr", {}, h("thead", {}, tb));
  cols.forEach(([k, t, f, c]) => { const th = h("th", {class: c || "", scope: "col", tabindex: "0", "aria-sort": k === sortKey ? (sortDir > 0 ? "ascending" : "descending") : "none"}, tr, t + (k === sortKey ? (sortDir > 0 ? " ↑" : " ↓") : ""));
    const go = () => { if (sortKey === k) sortDir = -sortDir; else { sortKey = k; sortDir = (c === "l") ? 1 : -1; } renderProducts(agg, cab); };
    th.onclick = go; th.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } }; });
  const body = h("tbody", {}, tb);
  if (!rows.length) { const r = h("tr", {}, body); h("td", {colspan: cols.length, class: "l muted"}, r, "Нет товаров с расходом или заказами за период"); }
  rows.forEach(p => { const r = h("tr", {}, body); cols.forEach(([k, t, f, c]) => { const td = h("td", {class: c || ""}, r, f(p[k]) || ""); if (k === "vendor") td.title = p.title; }); });
}

// ---------- сборка ----------
function render() {
  $("#sub").textContent = "собрано " + D.generated;
  document.querySelectorAll("#period button").forEach(b => b.setAttribute("aria-pressed", String(+b.dataset.d === state.days)));
  if (!D.has_db || !state.cab) { $("#nodb").hidden = false; $("#main").hidden = true; $("#prod-sec").hidden = true; renderAutopilot(); renderBudget(); return; }
  const cab = state.cab, end = lastDate(cab), dates = dateList(end, state.days), prevDates = dateList(dateList(end, state.days + 1)[0], state.days);
  $("#range").textContent = ddmm(dates[0]) + " – " + ddmm(end) + "." + end.slice(0, 4) + " (последний день в данных)";
  const A = aggregate(cab, dates), B = aggregate(cab, prevDates), t = A.tot, p = B.tot;
  const day = (k) => dates.map(d => A.day[d][k]);
  const tiles = $("#tiles"); tiles.textContent = "";
  tile(tiles, "Расход рекламы", money(t.spend), t.spend, p.spend, null, day("spend"));
  tile(tiles, "Заказы с рекламы", num(t.adOrders), t.adOrders, p.adOrders, true, day("adOrders"));
  const cpo = t.adOrders ? t.spend / t.adOrders : null, cpoP = p.adOrders ? p.spend / p.adOrders : null;
  tile(tiles, "Цена заказа с рекламы", money(cpo), cpo, cpoP, false, dates.map(d => A.day[d].adOrders ? A.day[d].spend / A.day[d].adOrders : null));
  tile(tiles, "Все заказы", num(t.orders), t.orders, p.orders, true, day("orders"));
  const drr = t.sum ? t.spend / t.sum : null, drrP = p.sum ? p.spend / p.sum : null;
  tile(tiles, "ДРР общий", pct(drr), drr, drrP, false, dates.map(d => A.day[d].sum ? A.day[d].spend / A.day[d].sum : null),
    "Расход рекламы ÷ сумма ВСЕХ заказов (с рекламы и без): сколько реклама стоит от всех продаж");
  // выручка заказов с рекламы ≈ заказы с рекламы × средняя цена заказа товара (WB в историю её не отдаёт)
  const adRev = (x) => Object.values(x.prod).reduce((a, q) => a + (q.orders ? q.adOrders * q.sum / q.orders : 0), 0);
  const ra = adRev(A), rb = adRev(B), drrAd = ra ? t.spend / ra : null, drrAdP = rb ? p.spend / rb : null;
  tile(tiles, "ДРР рекламный", pct(drrAd), drrAd, drrAdP, false, dates.map(d => { const x = A.day[d]; return x.orders && x.adOrders ? x.spend / (x.adOrders * x.sum / x.orders) : null; }),
    "Расход рекламы ÷ сумма заказов С РЕКЛАМЫ (заказы с рекламы × средняя цена заказа товара — оценка)");
  const share = t.orders ? t.adOrders / t.orders : null, shareP = p.orders ? p.adOrders / p.orders : null;
  tile(tiles, "Доля с рекламы", pct(share), share, shareP, null, dates.map(d => A.day[d].orders ? A.day[d].adOrders / A.day[d].orders : null),
    "Какая часть всех заказов пришла с рекламы");
  const s1 = css("--s1"), s2 = css("--s2"), s3 = css("--s3"), s4 = css("--s4");
  chart($("#c-spend"), {dates, kind: "bar", fmt: money, label: "Расход рекламы по дням", series: [{name: "Расход", color: s1, vals: day("spend")}]});
  chart($("#c-orders"), {dates, kind: "line", endLabels: true, label: "Заказы по дням", series: [
    {name: "Все заказы", color: s1, vals: dates.map(d => A.day[d].hasFn ? A.day[d].orders : null)},
    {name: "С рекламы", color: s2, vals: dates.map(d => A.day[d].hasAd ? A.day[d].adOrders : null)}]});
  chart($("#c-cpo"), {dates, kind: "line", fmt: money, label: "Цена заказа с рекламы по дням", series: [{name: "Цена заказа", color: s1, vals: dates.map(d => A.day[d].adOrders ? A.day[d].spend / A.day[d].adOrders : null)}]});
  const top = Object.values(A.prod).sort((a, b) => b.spend - a.spend).slice(0, 4).filter(x => x.spend > 0);
  const names = D.products[cab] || {}, colors = [s1, s2, s3, s4];
  const posBy = {}; for (const [nm, d, v] of D.pos[cab] || []) (posBy[nm] || (posBy[nm] = {}))[d] = v;
  chart($("#c-pos"), {dates, kind: "line", invert: true, endLabels: true, fmt: (v) => v == null ? "—" : fmt0.format(v), fmtAxis: (v) => fmt0.format(v), label: "Позиция в поиске",
    series: top.map((x, i) => ({name: (names[x.nm] || [String(x.nm)])[0] || String(x.nm), color: colors[i], vals: dates.map(d => (posBy[x.nm] || {})[d] ?? null)}))});
  renderAutopilot();
  renderBudget();
  renderProducts(A, cab);
}
// фильтры
const cabSel = $("#cab");
D.cabinets.forEach(c => h("option", {value: c}, cabSel, c));
if (D.cabinets.length < 2) cabSel.hidden = true;
cabSel.value = state.cab || "";
cabSel.onchange = () => { state.cab = cabSel.value; save(); render(); };
document.querySelectorAll("#period button").forEach(b => b.onclick = () => { state.days = +b.dataset.d; save(); render(); });
$("#q").addEventListener("input", () => render());
let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(render, 150); });
let theme = "auto"; try { theme = localStorage.getItem("wbdash-theme") || "auto"; } catch (e) {}
setTheme(theme);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
