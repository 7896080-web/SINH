# -*- coding: utf-8 -*-
"""
Часовая проверка ставок WB (дополнение к автопилоту wb_bids.py, который идёт 3 раза в день).

За час рекомендованная ставка WB может заметно измениться. При оплате за клики WB списывает ровно нашу ставку:
если для нужных мест теперь хватает 10 ₽, а у нас стоит 20 — каждый клик вдвое дороже, чем нужно. И наоборот:
если WB поднял минимальную ставку выше нашей — товар выпадает из показов.

Раз в час проверка:
  1. Смотрит минимальные ставки WB. Ставка ниже минимума → поднимает до минимума, только если минимум НЕ выше
     окупаемого потолка товара; если выше — не трогает и пишет в отчёт (решать вам).
  2. Для самых кликабельных товаров (до REC_MAX штук, по кликам за 14 дней) смотрит рекомендованную ставку WB
     и двигает нашу за ней — по тем же правилам, что автопилот:
       • не выше окупаемого потолка (цена заказа × конверсия × поправка на выкуп — из последнего wb_bids.py --apply);
       • снижение — сразу; повышение — сразу, но только до потолка (решение 12.10.2026);
       • не ниже минимальной ставки WB;
       • товарам «без ходовых размеров» и «много кликов без заказов» — не повышает;
       • товары, которым автопилот сознательно держит минимальную ставку (нет в наличии и т. п.), не трогает.
  3. Бюджеты: меньше порога → пополнить по тем же правилам и потолкам (в сутки/неделю), что у автопилота; кампания,
     вставшая на паузу из-за пустого бюджета, после пополнения запускается. Пополнения не откатываются.
  4. Дневной лимит кампаний за показы (у WB его нет, держим сами) и кампаний за клики, у которых лимит в RULES ниже
     минимума WB (1 000 ₽): потрачено сегодня ≥ 97% лимита из RULES или по
     темпу последнего часа к следующей проверке ушло бы за лимит больше чем на 15% — пауза до утра; первая проверка
     следующего дня (7:40) запускает снова. Такую паузу автопилот и проверка знают как свою (state["limit_paused"]).
Лимиты, минус-фразы, ставки по запросам, уборка товаров — только wb_bids.py.

Безопасность:
  • без --apply — пробный прогон: только показывает;
  • --apply сохраняет резервную копию в wb_guard_backups\\, --rollback возвращает ставки последнего запуска;
  • общая блокировка с wb_bids.py (файл wb_bids.running): пока применяет автопилот — проверка пропускается,
    пока применяет проверка — автопилот ждёт её окончания;
  • кампанию на паузе, в бюджете которой есть деньги (≥ PAUSED_MONEY_BELOW в wb_bids.py), не пополняет и не
    запускает — паузу поставили вы;
  • потолки старше 2 суток (автопилот давно не применялся) — ничего не меняет, только сообщает.

Запуск:  python wb_guard.py              (пробный прогон)
         python wb_guard.py --apply      (применить)
         python wb_guard.py --rollback   (вернуть ставки последнего запуска)
По расписанию — каждый час с --apply (см. wb_schedule.ps1). Токен с категорией «Продвижение».
Время: WB отдаёт рекомендации раз в 12,5 с на кабинет; кабинеты проверяются одновременно. Обычно ≈9–10 минут (по
самому большому кабинету, REC_MAX = 40). У кампаний с графиком расхода сверяются все товары — это дольше, поэтому
сверка с рекомендациями обрывается через REC_DEADLINE_MIN минут (остальным товарам — только проверка минимума WB):
планировщик останавливает проверку через 22 минуты, и без этого не применилось бы ничего.
Результат: вывод на экран + wb_guard_log.txt (дописывается).
"""
import datetime as dt
import json
import sys
import time
from collections import defaultdict

import wb_bids as B

# ---------------- НАСТРОЙКИ ----------------
REC_MAX = 40                # сколько товаров за проверку сверять с рекомендацией WB (самые кликабельные) — основной кабинет
REC_MAX_BY_CAB = {"Яворская": 40}   # по кабинетам; остальным — REC_MAX_OTHER (WB отдаёт рекомендацию раз в 12 с)
REC_MAX_OTHER = 15
CAPS_MAX_AGE_HOURS = 48     # потолки из wb_bids.py старше — не доверяю, ничего не меняю (по каждому кабинету)
REC_DEADLINE_MIN = 13       # сверку с рекомендациями WB — не дольше стольких минут от начала проверки (wb_run
                            # останавливает её через 22 мин; после сверки ещё остатки, пополнения и запись ставок)
TOPUP_LATE_MIN = 18         # позже стольких минут от начала пополнения не начинаю: остановка посреди пополнения
                            # оставила бы его неясным — пополню в следующий час
# -------------------------------------------

SPEND_LOG = B.HERE / "wb_spend_hourly.csv"   # расход кампаний по часам (только запись наблюдений; см. wb_pacing.py)
T0 = [time.time()]                            # время начала проверки (от него — REC_DEADLINE_MIN и TOPUP_LATE_MIN)
SNAPSHOTS = {}                                # {кабинет: [строки]} — заполняется в потоках кабинетов
CPM = {}                                      # {кабинет: {"limit": {кампания: ₽}, "spent": {кампания: ₽}}} — показы
PACE = {}                                     # {кабинет: {кампания: {m, why}}} — график расхода (wb_bids.pace_factor)
BACKUP_DIR = B.HERE / "wb_guard_backups"
LOG_FILE = B.HERE / "wb_guard_log.txt"
log = B.log


def write_log():
    B.write_rec_log()
    B.save_rec_cache()
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write("\n".join(B.out) + "\n\n")


def rec_for(token, cid, nm, pay, target):
    data = B.get_rec(token, cid, nm)
    rec = B.rec_cpc(data, B.CPC_LEVEL[target]) if pay == "cpc" else B.rec_cpm(data, B.CPM_LEVEL[target])
    if rec is None and data and target != "3-10":    # нет уровня для мест 1–2 — как автопилот, берём 3–10
        rec = B.rec_cpc(data, B.CPC_LEVEL["3-10"]) if pay == "cpc" else B.rec_cpm(data, B.CPM_LEVEL["3-10"])
    return rec


def plan_topups(cab, token, camps, state):
    """Пополнения бюджетов по правилам последнего wb_bids.py --apply (те же порог, сумма и потолки в сутки/неделю),
    сверенным с RULES в wb_bids.py прямо сейчас: из двух значений берётся более строгое, кампания без правила в RULES
    не пополняется. Кампания на паузе с бюджетом ниже порога (кончились деньги) — пополнить и запустить, если
    автопилот ею управлял. Пополненная кампания, запуск которой не прошёл, — только запустить."""
    rules = {int(k): v for k, v in (state.get("topup_rules") or {}).items() if v.get("cabinet") == cab}
    ids = [cid for cid, t in rules.items() if cid in camps and (camps[cid].get("_status") == 9 or
                                                                (camps[cid].get("_status") in (4, 11) and t.get("start")))]
    if not ids:
        return []
    buds = B.get_budgets(token, ids)
    res = []
    # общий недельный бюджет пополнений кабинета (как в wb_bids.py: "_week_budget") — по всем кампаниям кабинета
    caps_ = [t.get("cab_cap") for t in rules.values()] + [(B.RULES.get(cab) or {}).get("_week_budget")]
    cab_cap = min((x for x in caps_ if x), default=None)
    cab_left = cab_cap - B.topped_cabinet(state, cab, camps) if cab_cap else None
    for cid in ids:
        t, bud = dict(rules[cid]), buds.get(cid)
        live = B.live_topup(cab, cid, camps)
        if live is None:
            log(f"  «{t['name']}»: в RULES wb_bids.py пополнение этой кампании больше не задано — не пополняю.")
            continue
        raised = any(live[k] > t[k] for k in ("amount", "day_cap", "week_cap"))   # потолки в RULES подняли после --apply
        for k in ("below", "amount", "day_cap", "week_cap"):
            t[k] = min(t[k], live[k])
        B.note_broken_topups(state, cid, t["name"])
        if bud is None:
            continue
        status = camps[cid].get("_status")
        lp = B.limit_paused(state, cid)
        if status == 11 and lp == dt.date.today().isoformat():
            continue                       # пауза по дневному лимиту — до утра (сообщает plan_cpm_limits)
        if status in (4, 11) and bud >= B.PAUSED_MONEY_BELOW and B.start_retry(state, cid):
            log(f"  «{t['name']}»: пополнена {B.start_pending(state, cid)}, а запуск не прошёл (в бюджете {B.fm_rub(bud)} ₽) — "
                "запущу снова, без пополнения.")
            res.append({"cabinet": cab, "advert_id": cid, "name": t["name"], "amount": 0, "source": t["source"],
                        "budget_before": bud, "start": True})
            continue
        if bud >= t["below"]:
            continue
        if status == 11 and bud >= B.PAUSED_MONEY_BELOW:
            if not lp:                     # пауза по дневному лимиту вчера — запустит plan_cpm_limits
                log(f"  «{t['name']}» на паузе, в бюджете {B.fm_rub(bud)} ₽ — паузу, видимо, поставили вы: не пополняю и не запускаю.")
            continue
        amount = B.deposit_amount(t["amount"], t["day_cap"], B.topped_week(state, cid, days=1), t["week_cap"],
                                  B.topped_week(state, cid), cab_left if cab_cap else None,
                                  t.get("min_deposit") or B.MIN_DEPOSIT)
        paused = status in (4, 11)
        if not amount:
            log(f"  ! «{t['name']}»: бюджет {B.fm_rub(bud)} ₽, но до потолка пополнений меньше {t.get('min_deposit') or B.MIN_DEPOSIT:.0f} ₽ "
                "(меньше WB не принимает) — не пополняю"
                + (" (кампания стоит на паузе)" if paused else "")
                + (". Потолки в RULES с последнего запуска автопилота с --apply подняли — проверка берёт меньшие, "
                   "пока автопилот их не применит (по расписанию 8:00, 13:00, 18:00)" if raised else ""))
            continue
        res.append({"cabinet": cab, "advert_id": cid, "name": t["name"], "amount": amount, "source": t["source"],
                    "budget_before": bud, "start": paused, "enough": t["below"]})
        if cab_cap:
            cab_left -= amount
    return res


def spend_snapshot(cab, token, camps, state=None):
    """Снимок «сколько потрачено к этому часу»: бюджет каждой активной / на паузе кампании и, у кампаний за клики,
    потрачено сегодня и дневной лимит. Только чтение — два запроса на кабинет. Для оценки равномерного расхода."""
    ids = [cid for cid, a in camps.items() if a.get("_status") in (9, 11)]
    if not ids:
        return []
    buds = B.get_budgets(token, ids)
    cpc = [cid for cid in ids if (camps[cid].get("settings") or {}).get("payment_type") == "cpc"]
    try:
        lims = B.get_limits(token, cpc) if cpc else {}
    except B.ApiError:
        lims = {}
    # кампании за показы с дневным лимитом в RULES: расход за сегодня считаем сами (лимит держит пауза, см. plan_cpm_limits)
    cpm_lim = {}
    for cid in ids:
        if cid not in cpc:
            r = B.live_rule(cab, cid, camps)
            if r and r.get("daily_limit"):
                cpm_lim[cid] = r["daily_limit"]
    cpm_spent = B.spent_today(token, sorted(cpm_lim)) if cpm_lim else {}
    # кампании за клики, у которых лимит в RULES меньше, чем WB даёт поставить (минимум WB — 1 000 ₽ в сутки): свой лимит
    # держим так же паузой (решение пользователя 15.10.2026 — равные условия для обоих типов кампаний)
    for cid in cpc:
        r = B.live_rule(cab, cid, camps)
        lim = lims.get(cid) or {}
        if r and r.get("daily_limit") and lim.get("enabled") and lim.get("spentToday") is not None \
                and r["daily_limit"] * 100 < B.num(lim.get("dailyLimit")):
            cpm_lim[cid] = r["daily_limit"]
            cpm_spent[cid] = B.num(lim["spentToday"]) / 100
    # запасной способ (WB не отдал расход за сегодня): бюджет на первой проверке дня + пополнения после неё − бюджет сейчас.
    # Ночь до первой проверки (7:40) не видна — это оценка снизу.
    est = set()
    if state is not None:
        dbud = state.setdefault("day_budget", {})
        today = dt.date.today().isoformat()
        for cid in [c for c in cpm_lim if c not in cpc]:
            b = buds.get(cid)
            if b is None:
                continue
            rec = dbud.get(str(cid))
            if not rec or rec.get("date") != today:
                rec = dbud[str(cid)] = {"date": today, "budget": b, "topups": B.topped_week(state, cid, days=1)}
            if cid not in cpm_spent:
                cpm_spent[cid] = max(rec["budget"] + B.topped_week(state, cid, days=1) - rec["topups"] - b, 0.0)
                est.add(cid)
    CPM[cab] = {"limit": cpm_lim, "spent": cpm_spent, "est": est}
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = []
    for cid in ids:
        st = camps[cid].get("settings") or {}
        lim = lims.get(cid) or {}
        if cid in cpm_lim:                     # свой лимит (показы; клики — если он ниже минимума WB)
            spent_, limit_ = cpm_spent.get(cid, ""), cpm_lim[cid]
        else:
            spent_ = B.num(lim.get("spentToday")) / 100 if lim.get("spentToday") is not None else ""
            limit_ = B.num(lim.get("dailyLimit")) / 100 if lim.get("enabled") else ""
        rows.append({"время": now, "кабинет": cab, "кампания": cid, "название": st.get("name") or "",
                     "оплата": st.get("payment_type") or "", "статус": camps[cid].get("_status"),
                     "бюджет ₽": buds.get(cid, ""), "потрачено сегодня ₽": spent_, "дневной лимит ₽": limit_})
    return rows


def plan_cpm_limits(cab, camps, state, topups):
    """Дневной лимит кампаний за показы: (поставить на паузу, запустить снова)."""
    info = CPM.get(cab) or {}
    today = dt.date.today().isoformat()
    starting = {t["advert_id"] for t in topups if t.get("start")}
    pauses, resumes = [], []
    for cid, limit in (info.get("limit") or {}).items():
        a = camps.get(cid) or {}
        name = (a.get("settings") or {}).get("name") or str(cid)
        lp = B.limit_paused(state, cid)
        if a.get("_status") == 9:
            spent = (info.get("spent") or {}).get(cid)
            stop, why = B.cpm_limit_check(cid, spent, limit, state)
            if stop and cid in (info.get("est") or set()):
                why += " (расход — оценка по бюджету с первой проверки дня)"
            if stop:
                pauses.append({"cabinet": cab, "advert_id": cid, "name": name, "spent": spent, "limit": limit, "why": why})
        elif a.get("_status") == 11 and lp == today:
            log(f"  «{name}»: на паузе по дневному лимиту {B.fm_rub(limit)} ₽ (с "
                f"{(state.get('limit_paused') or {}).get(str(cid), {}).get('at', '?')}) — запущу утром.")
        elif a.get("_status") == 11 and lp and cid not in starting:
            resumes.append({"cabinet": cab, "advert_id": cid, "name": name, "amount": 0, "source": "balance",
                            "budget_before": None, "start": True})
    return pauses, resumes


def write_spend_log(rows):
    import csv
    new = not SPEND_LOG.exists()
    try:
        with open(SPEND_LOG, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter=";")
            if new:
                w.writeheader()
            for r in rows:
                w.writerow({k: (str(v).replace(".", ",") if isinstance(v, float) else v) for k, v in r.items()})
    except OSError as e:
        log(f"  ! Расход по часам не записан: {e}")


def plan_cabinet(cab, token, caps, state, budget):
    """Возвращает (изменения, «минимум выше окупаемого», сверено, пополнения). budget — сколько ещё товаров
    можно сверить с рекомендацией."""
    B._tl.cab = cab                    # для строки «идёт работа» и сбоев данных по кабинету
    by_c = defaultdict(dict)
    for key, zones in caps.items():
        cid, nm = key.split(":")
        by_c[int(cid)][int(nm)] = zones
    camps = B.get_campaigns(token)
    try:
        SNAPSHOTS[cab] = spend_snapshot(cab, token, camps, state)
        if SNAPSHOTS[cab]:
            with B._file_lock:                 # сразу: если проверку остановят позже, снимок этого часа уже записан
                write_spend_log(SNAPSHOTS[cab])
    except B.ApiError as e:
        log(f"  ! {cab}: снимок расхода не получен — {e}")
    topups = plan_topups(cab, token, camps, state)
    pauses, resumes = plan_cpm_limits(cab, camps, state, topups)
    cand = []                          # (кампания, товар, зона, данные кампании) — только активные кампании
    for cid, items in by_c.items():
        a = camps.get(cid)
        if a and a.get("_status") == 9:
            for nm, zones in items.items():
                for zone, c in zones.items():
                    cand.append((cid, nm, zone, c if isinstance(c, dict) else {"cap": c, "fixed": True}, a))
    # минимальные ставки — по всем товарам (дёшево: один запрос на кампанию и зону)
    mins = {}
    for cid, zone in sorted({(x[0], x[2]) for x in cand}):
        a = camps[cid]
        nms = [x[1] for x in cand if x[0] == cid and x[2] == zone]
        for nm, v in B.get_min_bids(token, cid, nms, (a.get("settings") or {}).get("payment_type"), zone).items():
            mins[(cid, nm, zone)] = v
    # график расхода: кампании за клики, у которых дневной лимит кончится раньше вечера, — ставка ниже
    today = dt.date.today().isoformat()
    prev = {k: v for k, v in (state.get("pacing") or {}).items() if v.get("date") == today}
    pace = {}
    for row in SNAPSHOTS.get(cab, []):
        if row.get("потрачено сегодня ₽") != "" and row.get("дневной лимит ₽") != "":     # за клики и за показы
            m, why = B.pace_factor(row["кампания"], row["потрачено сегодня ₽"], row["дневной лимит ₽"])
            pace[row["кампания"]] = {"m": m, "why": why}
            if m < 1:
                log(f"  ⏱ «{row['название']}»: {why}.")
            elif (prev.get(str(row["кампания"])) or {}).get("m", 1) < 1:
                log(f"  ⏱ «{row['название']}»: расход вернулся к графику — ставки снова по рекомендации WB.")
    PACE[cab] = pace
    # такие кампании (и те, где ставку снижали раньше сегодня) сверяются целиком, а не только самые кликабельные
    pace_all = {cid for cid, p in pace.items() if p["m"] < 1 or (prev.get(str(cid)) or {}).get("m", 1) < 1}
    # рекомендации — самым кликабельным, не «зафиксированным» правилом
    rec_ok = {(x[0], x[1], x[2]) for x in sorted((x for x in cand if not x[3].get("fixed")),
                                                  key=lambda x: -(x[3].get("clicks") or 0))[:budget]}
    rec_ok |= {(x[0], x[1], x[2]) for x in cand if x[0] in pace_all and not x[3].get("fixed")}
    changes, losing, checked, late = [], [], 0, 0
    # по убыванию кликов: если время на сверку кончится, самые важные товары уже сверены
    for cid, nm, zone, c, a in sorted(cand, key=lambda x: -(x[3].get("clicks") or 0)):
        st = a.get("settings") or {}
        cur_all = {s.get("nm_id"): s.get("bids_kopecks") or {} for s in a.get("nm_settings") or []}
        if nm not in cur_all:
            continue                                   # товар убрали из кампании
        cur = cur_all[nm].get("search" if zone == "combined" else zone)
        mn, cap = mins.get((cid, nm, zone)), c["cap"]
        if not cur:
            continue
        base = {"cabinet": cab, "advert_id": cid, "campaign": st.get("name"), "nm_id": nm, "placement": zone,
                "old": cur, "cap": cap}
        if mn and mn > cap:                            # окупаемо только ниже минимума WB
            if cur < mn:
                losing.append({**base, "new": mn})
            continue
        if (cid, nm, zone) in rec_ok and time.time() - T0[0] > REC_DEADLINE_MIN * 60:
            late += 1
        elif (cid, nm, zone) in rec_ok:
            checked += 1
            try:
                rec = rec_for(token, cid, nm, c.get("pay") or st.get("payment_type"), c.get("target") or "3-10")
            except B.ApiError as e:
                log(f"  ! {e}")
                rec = None
            B.note_rec(cab, cid, nm, c.get("pay") or st.get("payment_type"), c.get("target") or "3-10", rec, cur, mn)
            if rec:
                step = c.get("step") or 1
                nb, why = B.new_bid(cur, rec, cap, mn or 0, step, c.get("max_raise"),
                                    B.day_base(state, "bids", cid, nm, zone), c.get("max_drop"))
                if c.get("no_raise") and nb > cur:
                    nb, why = cur, "не повышаю (мало размеров или много кликов без заказов)"
                pm = (pace.get(cid) or {}).get("m", 1.0)
                if pm < 1:
                    nb2 = B.paced(nb, pm, mn, step, cur, (prev.get(str(cid)) or {}).get("m", 1.0))
                    if nb2 != nb:
                        nb, why = nb2, f"{why}; график расхода ×{pm}" + (
                            " — расход впереди графика, не повышаю" if nb2 == cur else "")
                if B.changed(nb, cur, step):
                    changes.append({**base, "new": nb, "rec": rec, "why": why})
                continue
        if mn and cur < mn:                            # рекомендацию не смотрели — только минимум
            changes.append({**base, "new": mn, "rec": None, "why": "ниже минимума WB → минимальная"})
    if late:
        log(f"  {cab}: не успел сверить с рекомендацией WB {late} тов. с меньшим числом кликов (на сверку — не больше "
            f"{REC_DEADLINE_MIN} мин) — у них проверен только минимум WB; сверю в следующий час.")
    changes.sort(key=lambda c: (c["advert_id"], c["nm_id"]))
    # потолки посчитаны автопилотом до 14 ч назад; товар мог за это время закончиться — повышать ему ставку не нужно
    ups = sorted({c["nm_id"] for c in changes if c["new"] > c["old"] and c.get("rec")})
    if ups:
        stock = B.get_stock(token, ups)
        if stock is None:
            log("  ! Остатки не загрузились — ставки не повышаю (снижения применяю).")
            changes = [c for c in changes if not (c["new"] > c["old"] and c.get("rec"))]
        else:
            low_min = B.DEFAULTS.get("min_stock") or 0
            keep = []
            for c in changes:
                q = B.available(stock.get(c["nm_id"])) if c["nm_id"] in ups else None
                if c["new"] > c["old"] and c.get("rec") and q is not None and q < low_min:
                    log(f"  «{c['campaign']}» {c['nm_id']}: в наличии {q:.0f} шт (< {low_min}) — ставку не повышаю.")
                    continue
                keep.append(c)
            changes = keep
    return changes, losing, checked, topups + resumes, pauses


def main():
    cabs = B.load_tokens()
    if "--rollback" in sys.argv:
        files = sorted(BACKUP_DIR.glob("guard_*.json"))
        if not files:
            sys.exit("Резервных копий нет.")
        items = json.loads(files[-1].read_text(encoding="utf-8"))
        log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — ОТКАТ по {files[-1].name}: {len(items)} ставок (применяется сразу). "
            "Ставки, которые после этого уже поменял автопилот или вы, не трогаю.")
        if not B.lock_acquire("wb_guard.py --rollback", wait_min=25):
            log("  ✗ Блокировка занята больше 25 мин — откат не сделан, запустите позже.")
            write_log()
            return
        try:
            for cab in sorted({i["cabinet"] for i in items}):
                if cab not in cabs:
                    continue
                camps = B.get_campaigns(cabs[cab])
                todo, moved = [], 0
                for i in (x for x in items if x["cabinet"] == cab):
                    a = camps.get(i["advert_id"]) or {}
                    cur = next(((s.get("bids_kopecks") or {}).get("search" if i["placement"] == "combined" else i["placement"])
                                for s in a.get("nm_settings") or [] if s.get("nm_id") == i["nm_id"]), None)
                    if cur == i["new"]:
                        todo.append(i)
                    else:
                        moved += 1
                n = B.apply_bids(cabs[cab], todo, "old") if todo else 0
                log(f"  {cab}: возвращено ставок {n}" + (f"; не тронуто {moved} — их уже изменили после проверки" if moved else ""))
            files[-1].rename(files[-1].with_suffix(".rolled_back"))
            log("  Внимание: следующая часовая проверка снова поведёт ставки за рекомендацией WB. Чтобы этого не было, "
                "отключите задачу: Disable-ScheduledTask -TaskName AWER_WB_guard")
        finally:
            B.lock_release()
        write_log()
        return

    apply = "--apply" in sys.argv
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — часовая проверка ставок WB "
        f"({'ПРИМЕНЕНИЕ' if apply else 'пробный прогон, ничего не меняется'})")
    if apply and not B.lock_acquire("wb_guard.py"):
        info = B.lock_info()
        log(f"  Сейчас применяет {info[1] if info else 'wb_bids.py'} — проверку пропускаю.")
        write_log()
        return
    try:
        run(cabs, apply)
    finally:
        if apply:
            B.lock_release()


def run(cabs, apply):
    state = B.load_state()
    caps = state.get("caps") or {}
    try:
        age = dt.datetime.now() - dt.datetime.fromisoformat(caps.get("at") or "")
    except ValueError:
        age = None
    if not caps.get("items") or age is None:
        log("  Нет окупаемых потолков: автопилот ещё ни разу не применялся (python wb_bids.py --apply). Ничего не меняю.")
        write_log()
        return
    # возраст потолков — по каждому кабинету: у кабинета, расчёт которого в автопилоте падал, они старше
    fresh = {}
    for cab, token in cabs.items():
        try:
            a_ = dt.datetime.now() - dt.datetime.fromisoformat((caps.get("cab_at") or {}).get(cab) or caps["at"])
        except ValueError:
            a_ = None
        if a_ is None or a_ > dt.timedelta(hours=CAPS_MAX_AGE_HOURS):
            log(f"  {cab}: потолки устарели (автопилот удачно считал этот кабинет "
                + (f"{a_.days} дн. {a_.seconds // 3600} ч назад" if a_ else "давно") + ") — в кабинете ничего не меняю. "
                "Запустите wb_bids.py и посмотрите, что мешает (✗ в его журнале).")
        else:
            fresh[cab] = token
    if not fresh:
        write_log()
        return

    changes, losing, topups, checked, pauses = [], [], [], 0, []
    # кабинеты — одновременно (у каждого продавца свои лимиты WB); ID кампаний у WB уникальны: в каждом кабинете
    # проверяются только его кампании
    jobs = [(cab, lambda cab=cab, token=token: plan_cabinet(cab, token, caps["items"], state,
                                                           REC_MAX_BY_CAB.get(cab, REC_MAX_OTHER)))
            for cab, token in fresh.items()]
    results = B.run_parallel(jobs)
    if apply and not B.lock_mine():
        log("  ✗ Общую блокировку снял другой процесс, пока шла проверка, — ничего не применяю (иначе два процесса "
            "меняли бы кабинеты одновременно).")
        write_log()
        return
    if apply:
        cs = state.setdefault("cpm_spent", {})            # для темпа расхода кампаний за показы (cpm_limit_check)
        for cab, _ in jobs:
            for cid, s in ((CPM.get(cab) or {}).get("spent") or {}).items():
                cs[str(cid)] = {"at": dt.datetime.now().isoformat(timespec="minutes"), "spent": s}
        pst = state.setdefault("pacing", {})
        for cab, _ in jobs:
            for cid, p in PACE.get(cab, {}).items():
                pst[str(cid)] = {"date": dt.date.today().isoformat(), "m": p["m"],
                                 "at": dt.datetime.now().isoformat(timespec="minutes")}
        B.save_state(state)
    for cab, _ in jobs:
        kind, res = results[cab]
        if kind == "err":
            if not isinstance(res, B.ApiError):
                raise res
            log(f"  ✗ {cab}: {res}")
            continue
        ch, lo, n, tp, pz = res
        changes += ch
        losing += lo
        topups += tp
        checked += n
        pauses += pz
    B.progress(" " * 60)
    log(f"  Сверено с рекомендацией WB: {checked} тов.; изменений ставок: {len(changes)}.")
    for it in changes:
        arrow = "↑" if it["new"] > it["old"] else "↓"
        log(f"  {arrow} «{it['campaign']}» {it['nm_id']}: {B.rub(it['old'])} → {B.rub(it['new'])}"
            + (f" (рекоменд. WB {B.rub(it['rec'])}, потолок {B.rub(it['cap'])})" if it.get("rec") else
               f" (потолок {B.rub(it['cap'])})") + f" — {it['why']}")
    if losing:
        log("  Минимальная ставка WB выше окупаемого потолка — НЕ поднимаю (товар может выпасть из показов):")
        for it in losing:
            log(f"    ! «{it['campaign']}» {it['nm_id']}: ставка {B.rub(it['old'])}, минимум WB {B.rub(it['new'])}, "
                f"окупается до {B.rub(it['cap'])}")
        log("    Решить: оставить как есть, убрать товар из кампании или поднять допустимую цену заказа в wb_bids.py.")

    for it in pauses:
        log(f"  ⏸ «{it['name']}»: {it['why']} — пауза до утра")
    if apply and pauses:
        for cab in sorted({i["cabinet"] for i in pauses}):
            for it in (i for i in pauses if i["cabinet"] == cab):
                B.pause_campaign(cabs[cab], it, state)
    for t in topups:
        if not t["amount"] and B.limit_paused(state, t["advert_id"]):
            log(f"  ▶ «{t['name']}»: новый день — запустить (вчера стояла на паузе по дневному лимиту)")
            continue
        if not t["amount"]:
            log(f"  ▶ «{t['name']}»: бюджет {B.fm_rub(t['budget_before'])} ₽ — запустить (пополнили раньше, запуск тогда не прошёл)")
            continue
        log(f"  ₽ «{t['name']}»: бюджет {B.fm_rub(t['budget_before'])} ₽ → пополнить на {t['amount']} ₽"
            + (" и запустить (стоит на паузе — кончились деньги)" if t["start"] else ""))
    if apply and topups and time.time() - T0[0] > TOPUP_LATE_MIN * 60:
        log(f"  ! Проверка идёт дольше {TOPUP_LATE_MIN} мин — пополнения не начинаю (остановка посреди пополнения "
            "оставила бы его неясным), сделаю в следующий час.")
        topups = []
    if apply and topups:
        for cab in sorted({t["cabinet"] for t in topups}):
            mine = [t for t in topups if t["cabinet"] == cab]
            n = B.apply_topups(cabs[cab], mine, state)
            if any(t["amount"] for t in mine):
                log(f"  {cab}: пополнено бюджетов {n} из {sum(1 for t in mine if t['amount'])} (пополнения не откатываются)")
    if apply and changes:
        BACKUP_DIR.mkdir(exist_ok=True)
        path = BACKUP_DIR / f"guard_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(changes, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"  Резервная копия: {path.name}")
        B.record_day_base(state, {"bids": changes, "cluster_bids": []})   # ставка на начало дня (если в правилах вернут ограничения в сутки)
        for cab in sorted({i["cabinet"] for i in changes}):
            n = B.apply_bids(cabs[cab], [i for i in changes if i["cabinet"] == cab], "new")
            log(f"  {cab}: изменено ставок {n} из {sum(1 for i in changes if i['cabinet'] == cab)}")
        B.save_state(state)
        log("  Откат: python wb_guard.py --rollback")
    elif not apply and (changes or topups or pauses):
        log("  Это пробный прогон. Применить: python wb_guard.py --apply")
    write_log()


if __name__ == "__main__":
    main()
