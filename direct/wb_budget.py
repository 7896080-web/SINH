# -*- coding: utf-8 -*-
"""
Бюджет по окупаемости: кампании WB, которые окупаются, но упираются в деньги, — предложить больше денег;
поднятый раньше бюджет, который перестал окупаться, — вернуть. Решение пользователя 14.10.2026: «кампанию, которая
приносит деньги, неверно держать на голодном пайке».

Для каждой кампании с дневным лимитом или автопополнением в RULES (wb_bids.py):
  1. Окупаемость за 14 дней: цена заказа с рекламы (расход ÷ заказы) против допустимой — та же, что у автопилота:
     max_cost_per_order × поправка на выкуп товара (или лимит из юнит-экономики при "auto").
  2. «Голодный паёк» за 7 дней (часовые снимки wb_spend_hourly.csv, их пишет wb_guard.py):
       • за клики — дневной лимит кончился раньше 20:00;
       • за показы — бюджет пуст (меньше 50 ₽) и за час не пополнился (потолки пополнений не дали);
     плюс недельный потолок пополнений кампании или кабинета исчерпан.
  3. Остаток: хватит ли товара (WB + свой склад) на продажи с прибавкой — не меньше чем на MIN_COVER_DAYS дней.
Предложение: цена заказа ≤ 60% допустимой → +50% к дневному лимиту и потолкам пополнений; ≤ 80% → +30%;
≤ 90% → +15%; дороже — не увеличивать (дополнительные деньги покупают более дорогой трафик: каждый следующий
заказ дороже среднего, поэтому нужен запас). Одну кампанию — не чаще раза в RAISE_EVERY_DAYS дней; через столько
дней после повышения — проверка: если цена заказа после повышения выше допустимой — предложение вернуть как было.
Не выше MAX_FACTOR × значения в RULES без правки кода. Кабинеты эксперимента — не раньше его конца (EXPERIMENT).

Запуск:
  python wb_budget.py                     — пробный прогон: таблица и предложения, ничего не меняет
  python wb_budget.py --apply             — записать предложенное в wb_budget_overrides.json (резервная копия в
                                            wb_budget_backups\\); автопилот и часовая проверка берут новые лимиты
                                            со следующего запуска (wb_bids.py ставит лимит, wb_guard.py пополняет)
  python wb_budget.py --apply --only 31049161,31048671   — только эти кампании
  python wb_budget.py --rollback          — вернуть лимиты, какие были до последнего --apply (сразу)
Деньги в кампании этот скрипт не переводит и ставки не меняет — только лимиты и потолки, по которым работают
wb_bids.py и wb_guard.py. Токен с категориями «Продвижение» и «Аналитика». Время — 1–3 минуты на кабинет.
Результат: вывод на экран + wb_budget_report.txt + wb_budget_proposals.json (его показывает дашборд
wb_dashboard.py; по расписанию пробный прогон идёт раз в день после утреннего автопилота — см. wb_run.py).
"""
import csv
import datetime as dt
import json
import math
import os
import sys
from collections import defaultdict

import wb_bids as B

# ---------------- НАСТРОЙКИ ----------------
STATS_DAYS = 14            # окупаемость — за столько дней
STARVE_DAYS = 7            # «голодный паёк» — за столько последних полных дней
STARVE_HOUR = 20           # деньги кончились раньше этого часа — день на голодном пайке
STARVE_MIN = 2             # минимум таких дней, чтобы предлагать прибавку
MIN_ORDERS = 10            # меньше заказов с рекламы за 14 дней — окупаемость не доказана, не предлагаю
STEPS = [(0.6, 0.5), (0.8, 0.3), (0.9, 0.15)]   # (цена заказа / допустимая ≤, прибавка)
MARGINAL = 1.3             # дополнительные заказы считаю дороже средних в 1,3 раза (для прогноза)
MIN_COVER_DAYS = 21        # остатка (WB + свой склад) должно хватить на столько дней продаж с прибавкой
MAX_FACTOR = 3.0           # не выше ×3 от значения в RULES без правки кода
RAISE_EVERY_DAYS = 7       # одной кампании — не чаще; через столько дней после повышения — проверка
EXPERIMENT = {"Ребрик": "2026-10-27", "Караман": "2026-10-27"}   # до этой даты бюджеты кабинета не трогаю
EMPTY_BUDGET = 50          # ₽: бюджет кампании за показы меньше — «пусто»
# -------------------------------------------

HERE = B.HERE
SPEND_LOG = HERE / "wb_spend_hourly.csv"
OVERRIDES = B.BUDGET_OVERRIDES
BACKUPS = HERE / "wb_budget_backups"
REPORT = HERE / "wb_budget_report.txt"
PROPOSALS = HERE / "wb_budget_proposals.json"   # последние предложения — для дашборда
log = B.log
rub = B.fm_rub


def f(v):
    try:
        return float(str(v).replace(",", ".").replace(" ", "")) if str(v).strip() != "" else None
    except ValueError:
        return None


def hm(h):
    return f"{int(h):02d}:{int(round(h % 1 * 60)) % 60:02d}"


def up100(x):
    return int(math.ceil(x / 100.0) * 100)


def load_overrides():
    try:
        return json.loads(OVERRIDES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"items": {}, "raised": {}}


def save_overrides(d):
    tmp = OVERRIDES.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, OVERRIDES)


def snapshots():
    """{(кабинет, кампания): {день: [(время в часах, строка)]}} за STARVE_DAYS полных дней."""
    res = defaultdict(lambda: defaultdict(list))
    if not SPEND_LOG.exists():
        return res
    today = dt.date.today()
    since = (today - dt.timedelta(days=STARVE_DAYS)).isoformat()
    raw = SPEND_LOG.read_bytes().decode("utf-8-sig", errors="replace").splitlines()
    for r in csv.DictReader(raw, delimiter=";"):
        t = r.get("время") or ""
        if len(t) < 16 or not (since <= t[:10] < today.isoformat()):
            continue
        try:
            h = int(t[11:13]) + int(t[14:16]) / 60
        except ValueError:
            continue
        res[(r.get("кабинет"), str(r.get("кампания")))][t[:10]].append((h, r))
    for days in res.values():
        for v in days.values():
            v.sort(key=lambda x: x[0])
    return res


def starved_days(days, pay):
    """(дней с наблюдениями, [(день, час, когда кончились деньги)])."""
    seen, short = 0, []
    for d, rows in sorted(days.items()):
        if len(rows) < 6:            # снимков мало (компьютер был выключен) — день не судим
            continue
        seen += 1
        out = None
        if pay == "cpc":
            for h, r in rows:
                s_, lim = f(r.get("потрачено сегодня ₽")), f(r.get("дневной лимит ₽"))
                if s_ is not None and lim and s_ >= lim * 0.97:
                    out = h
                    break
        if out is None:              # бюджет пуст и в течение часа не пополнился (у кампаний за клики — тоже бывает)
            for i, (h, r) in enumerate(rows):
                b = f(r.get("бюджет ₽"))
                if b is not None and b < EMPTY_BUDGET:
                    later = [f(x.get("бюджет ₽")) for hh, x in rows[i + 1:] if hh - h <= 1.2]
                    if not any(x is not None and x >= EMPTY_BUDGET for x in later):
                        out = h
                        break
        if out is not None and out < STARVE_HOUR:
            short.append((d, out))
    return seen, short


def camp_days(token, ids, d1, d2):
    """{кампания: {день: {sum, orders, clicks, sum_price}}} — расход и заказы с рекламы по дням."""
    res = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for part in B.chunks(ids, 50):
        B.pace(token, "fullstats", 21)
        data = B.call("GET", B.ADV + "/adv/v3/fullstats", token, what="Статистика кампаний",
                      params={"ids": ",".join(map(str, part)), "beginDate": d1.isoformat(), "endDate": d2.isoformat()})
        for item in data or []:
            for day in item.get("days") or []:
                d = str(day.get("date") or "")[:10]
                for app in day.get("apps") or []:          # как в wb_bids.nm_stats: по товарам внутри площадок
                    for n in app.get("nms") or []:
                        for k in ("sum", "orders", "clicks", "sum_price"):
                            res[item.get("advertId")][d][k] += B.num(n.get(k))
    return res


def base_rule(cab, cid, camps):
    """Правило кампании по RULES без поднятых wb_budget.py лимитов."""
    now, B.RULES = B.RULES, B.RULES_BASE
    try:
        return B.live_rule(cab, cid, camps) or {}
    finally:
        B.RULES = now


def total(days, d1, d2=None):
    t = defaultdict(float)
    for d, v in days.items():
        if d >= d1 and (d2 is None or d <= d2):
            for k, x in v.items():
                t[k] += x
    return t


def plan_cabinet(cab, token, state, ov):
    log(f"\n══════════ {cab} ══════════")
    camps = B.get_campaigns(token)
    rules_now = {}
    for cid, a in camps.items():
        if a.get("_status") not in (9, 11):
            continue
        r = B.live_rule(cab, cid, camps)
        if r and (r.get("daily_limit") or r.get("topup_below")):
            rules_now[cid] = r
    if not rules_now:
        log("  Кампаний с дневным лимитом или автопополнением в RULES нет.")
        return []
    y = dt.date.today() - dt.timedelta(days=1)
    d1 = y - dt.timedelta(days=STATS_DAYS - 1)
    stats = camp_days(token, sorted(rules_now), d1, y)
    nms = {cid: [s.get("nm_id") for s in camps[cid].get("nm_settings") or []] for cid in rules_now}
    all_nms = sorted({n for v in nms.values() for n in v})
    stock = B.get_stock(token, all_nms) or {}
    B.load_buyout(cab, token, all_nms, {n: (stock.get(n) or {}).get("vendor") for n in all_nms})
    snaps = snapshots()
    exp_until = EXPERIMENT.get(cab)
    exp_on = bool(exp_until and dt.date.today().isoformat() <= exp_until)
    cab_cap = (B.RULES.get(cab) or {}).get("_week_budget")
    cab_left = cab_cap - B.topped_cabinet(state, cab, camps) if cab_cap else None
    raised = (ov.get("raised") or {})
    out = []
    log(f"  {'кампания':34}{'расход/дн':>10}{'заказов':>8}{'цена зак.':>10}{'допуст.':>9}{'голод':>7}{'остаток':>9}")
    for cid, r in sorted(rules_now.items(), key=lambda x: -total(stats.get(x[0], {}), d1.isoformat())["sum"]):
        a = camps[cid]
        st = a.get("settings") or {}
        name, pay = st.get("name") or str(cid), st.get("payment_type") or "cpm"
        tt = total(stats.get(cid, {}), d1.isoformat())
        spend, orders = tt["sum"], tt["orders"]
        cpo = spend / orders if orders else None
        # допустимая цена заказа — как у автопилота (с поправкой на выкуп), в среднем по товарам кампании
        allowed = sum(B.mcpo(cab, r, n) for n in nms[cid]) / len(nms[cid]) if nms[cid] else None
        seen, short = starved_days(snaps.get((cab, str(cid)), {}), pay)
        week_cap = r.get("topup_week_cap") if r.get("topup_below") else None
        caps_hit = bool(week_cap and B.topped_week(state, cid) + B.MIN_DEPOSIT > week_cap)
        cab_hit = bool(cab_cap and cab_left is not None and cab_left < B.MIN_DEPOSIT)
        q = sum(B.available(stock.get(n)) or 0 for n in nms[cid])
        sold = sum(B.num((stock.get(n) or {}).get("orders")) for n in nms[cid]) / B.SIZE_DAYS
        cover = q / sold if sold else None
        log(f"  {B.cut(name, 32):34}{rub(spend / STATS_DAYS):>10}{orders:>8.0f}"
            f"{(rub(cpo) if cpo else '—'):>10}{(rub(allowed) if allowed else '—'):>9}"
            f"{(f'{len(short)}/{seen}' if seen else '—'):>7}{(f'{cover:.0f} дн' if cover else '—'):>9}")
        cur = {k: r.get(k) for k in B.BUDGET_KEYS}
        base = base_rule(cab, cid, camps)
        prop = {"cabinet": cab, "advert_id": cid, "name": name, "cur": cur, "cpo": cpo, "allowed": allowed}
        why = []
        last = raised.get(str(cid))
        # проверка поднятого раньше: окупается ли после повышения
        if last and (dt.date.today() - dt.date.fromisoformat(last["date"])).days >= RAISE_EVERY_DAYS:
            ta = total(stats.get(cid, {}), last["date"])
            cpo_a = ta["sum"] / ta["orders"] if ta["orders"] else None
            if ta["sum"] and (cpo_a is None or (allowed and cpo_a > allowed)):
                prop.update(action="revert", new=last["prev"],
                            text=f"после повышения {last['date']} заказ стоит "
                                 + (f"{rub(cpo_a)} ₽" if cpo_a else "— (заказов нет)")
                                 + f" при допустимых {rub(allowed)} ₽ — вернуть прежние лимиты")
                out.append(prop)
                continue
        if exp_on:
            why.append(f"эксперимент до {exp_until} — бюджеты кабинета не меняю")
        elif orders < MIN_ORDERS:
            why.append(f"заказов с рекламы за {STATS_DAYS} дн. {orders:.0f} (< {MIN_ORDERS}) — окупаемость не доказана")
        elif not allowed or cpo > allowed * STEPS[-1][0]:
            why.append(f"заказ {rub(cpo)} ₽ при допустимых {rub(allowed)} ₽ — запаса нет, больше денег не окупится")
        elif last and (dt.date.today() - dt.date.fromisoformat(last["date"])).days < RAISE_EVERY_DAYS:
            why.append(f"поднимали {last['date']} — жду {RAISE_EVERY_DAYS} дн., чтобы проверить окупаемость прибавки")
        elif seen < 3:
            why.append(f"окупается (заказ {rub(cpo)} из {rub(allowed)} ₽), но часовых снимков мало ({seen} дн.) — "
                       "не видно, кончаются ли деньги")
        elif len(short) < STARVE_MIN:
            why.append(f"окупается, но деньги кончались раньше {STARVE_HOUR}:00 только в {len(short)} из {seen} дн. — "
                       "бюджет не мешает")
        if why:
            prop.update(action="hold", text=why[0])
            out.append(prop)
            continue
        step = next(s for lim_, s in STEPS if cpo <= allowed * lim_)
        if cover is not None and cover / (1 + step) < MIN_COVER_DAYS:
            prop.update(action="hold", text=f"окупается (заказ {rub(cpo)} из {rub(allowed)} ₽), но товара хватит на "
                                            f"{cover:.0f} дн. — прибавка быстро его распродаст")
            out.append(prop)
            continue
        new = {}
        for k in B.BUDGET_KEYS:
            if cur.get(k) and (k != "topup_day_cap" and k != "topup_week_cap" or r.get("topup_below")):
                lim_ = (base.get(k) or cur[k]) * MAX_FACTOR
                new[k] = min(up100(cur[k] * (1 + step)), up100(lim_))
        if new.get("daily_limit") and new.get("topup_day_cap"):
            new["topup_day_cap"] = max(new["topup_day_cap"], new["daily_limit"])
        if not new or all(new[k] <= (cur.get(k) or 0) for k in new):
            prop.update(action="hold", text=f"уже ×{MAX_FACTOR:g} от RULES — дальше только правкой RULES")
            out.append(prop)
            continue
        add_week = (new.get("topup_week_cap") or 0) - (cur.get("topup_week_cap") or 0) if "topup_week_cap" in new \
            else ((new.get("daily_limit") or 0) - (cur.get("daily_limit") or 0)) * 7
        more = add_week / (cpo * MARGINAL) if cpo else 0
        why_s = (f"в {len(short)} из {seen} дн. деньги кончились раньше {STARVE_HOUR}:00 (в среднем к "
                 f"{hm(sum(h for _, h in short) / len(short))})"
                 + ("; недельный потолок пополнений исчерпан" if caps_hit else "")
                 + ("; недельный бюджет кабинета исчерпан" if cab_hit else ""))
        prop.update(action="raise", step=step, new=new, add_week=add_week,
                    text=f"заказ {rub(cpo)} ₽ при допустимых {rub(allowed)} ₽; {why_s} → +{step:.0%}: "
                         + ", ".join(f"{LABEL[k]} {rub(cur.get(k) or 0)} → {rub(v)} ₽" for k, v in new.items())
                         + f". Это до +{rub(add_week)} ₽ в неделю ≈ +{more:.0f} заказов (каждый доп. заказ считаю дороже "
                           f"среднего в {str(MARGINAL).replace('.', ',')} раза)")
        out.append(prop)
    if cab_cap and not exp_on:
        add = sum(p.get("add_week") or 0 for p in out if p.get("action") == "raise" and "topup_week_cap" in p["new"])
        if add:
            out.append({"cabinet": cab, "advert_id": "_week_budget", "name": "недельный бюджет кабинета",
                        "action": "raise", "cur": {"_week_budget": cab_cap}, "new": {"_week_budget": up100(cab_cap + add)},
                        "text": f"недельный бюджет пополнений кабинета {rub(cab_cap)} → {rub(up100(cab_cap + add))} ₽ "
                                "(иначе прибавка кампаний упрётся в него)"})
    return out


LABEL = {"daily_limit": "дневной лимит", "topup_day_cap": "пополнения в сутки", "topup_week_cap": "пополнения в неделю",
         "_week_budget": "недельный бюджет кабинета"}


def show(props):
    log("\n  Предложения:")
    any_ = False
    for p in props:
        if p["action"] == "hold":
            log(f"    · {p['cabinet']}: «{B.cut(p['name'], 30)}» — {p['text']}.")
        else:
            any_ = True
            mark = "▲" if p["action"] == "raise" else "▼"
            log(f"    {mark} {p['cabinet']}: «{B.cut(p['name'], 30)}» ({p['advert_id']}) — {p['text']}.")
    return any_


def apply(props, ov, only):
    BACKUPS.mkdir(exist_ok=True)
    path = BACKUPS / f"overrides_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(ov, ensure_ascii=False, indent=1), encoding="utf-8")
    items, raised = ov.setdefault("items", {}), ov.setdefault("raised", {})
    n = 0
    done, add_cab = set(), defaultdict(int)
    for p in props:
        if p["action"] == "hold" or p["advert_id"] == "_week_budget" or (only and str(p["advert_id"]) not in only):
            continue
        cab, key = p["cabinet"], str(p["advert_id"])
        if p["action"] == "raise":
            items.setdefault(cab, {}).setdefault(key, {}).update(p["new"])
            raised[key] = {"date": dt.date.today().isoformat(), "prev": {k: v for k, v in p["cur"].items() if v},
                           "cpo": p["cpo"], "allowed": p["allowed"]}
            if "topup_week_cap" in p["new"]:
                add_cab[cab] += p.get("add_week") or 0
        else:                          # revert: прежние значения
            items.setdefault(cab, {})[key] = dict(p["new"])
            raised.pop(key, None)
        done.add(key)
        n += 1
    # недельный бюджет кабинета — на сумму прибавки только тех кампаний, что записаны сейчас
    for p in props:
        if p["advert_id"] == "_week_budget" and add_cab.get(p["cabinet"]):
            items.setdefault(p["cabinet"], {})["_week_budget"] = up100(p["cur"]["_week_budget"] + add_cab[p["cabinet"]])
            done.add("_week_budget")
            n += 1
    ov["at"] = dt.datetime.now().isoformat(timespec="seconds")
    save_overrides(ov)
    save_proposals(props, done)
    log(f"\n  Записано изменений: {n} (резервная копия {path.name}). Действуют с ближайшего запуска автопилота (8:00, "
        "13:00 или 18:00): он поставит новые дневные лимиты, а часовая проверка после него — пополнения по новым "
        "потолкам. Откат: python wb_budget.py --rollback")


def save_proposals(props, applied_ids=()):
    """Предложения последнего расчёта — для дашборда (только данные, без токенов)."""
    items = []
    for p in props:
        it = {k: p.get(k) for k in ("cabinet", "advert_id", "name", "action", "text", "cpo", "allowed", "cur", "new",
                                    "add_week", "step")}
        it["applied"] = str(p.get("advert_id")) in applied_ids
        items.append(it)
    try:
        tmp = PROPOSALS.with_suffix(".tmp")
        tmp.write_text(json.dumps({"at": dt.datetime.now().isoformat(timespec="minutes"), "items": items},
                                  ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, PROPOSALS)
    except OSError as e:
        log(f"  ! Предложения для дашборда не сохранены: {e}")


def rollback():
    files = sorted(BACKUPS.glob("overrides_*.json")) if BACKUPS.exists() else []
    if not files:
        log("Резервных копий нет — откатывать нечего.")
        return
    prev = json.loads(files[-1].read_text(encoding="utf-8"))
    save_overrides(prev)
    files[-1].rename(files[-1].with_suffix(".rolled_back"))
    log(f"Откат: лимиты как до {files[-1].name} (действуют со следующего запуска автопилота и часовой проверки).")


def main():
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — бюджет по окупаемости (WB)")
    if "--rollback" in sys.argv:
        rollback()
        REPORT.write_text("\n".join(B.out), encoding="utf-8")
        return
    only = set()
    if "--only" in sys.argv:
        i = sys.argv.index("--only")
        only = {x.strip() for x in (sys.argv[i + 1] if i + 1 < len(sys.argv) else "").split(",") if x.strip()}
    B.UNIT_LIMITS.update(B.load_unit_limits())
    state = B.load_state()
    ov = load_overrides()
    props = []
    for cab, token in B.load_tokens().items():
        if cab not in B.RULES:
            continue
        try:
            props += plan_cabinet(cab, token, state, ov)
        except B.ApiError as e:
            log(f"  ✗ {cab}: {e}")
    has = show(props)
    save_proposals(props)
    if not SPEND_LOG.exists():
        log("\n  Часовых снимков расхода (wb_spend_hourly.csv) ещё нет — их пишет wb_guard.py с 14.10.2026.")
    if not has:
        log("\n  Менять нечего.")
    elif "--apply" in sys.argv:
        apply(props, ov, only)
    else:
        log("\n  Это пробный прогон — ничего не изменено. Записать все предложения: python wb_budget.py --apply; "
            "только некоторые: python wb_budget.py --apply --only <номера кампаний через запятую>")
    REPORT.write_text("\n".join(B.out), encoding="utf-8")
    print(f"\nСохранено: {REPORT.name}")


if __name__ == "__main__":
    main()
