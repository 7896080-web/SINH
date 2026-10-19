"""Тексты сообщений (MAX, format=markdown). Все суммы — в рублях, округлены."""
from __future__ import annotations

from datetime import date, timedelta

from . import achievements, goals
from .achievements import plural
from . import metrics as M
from .config import Settings
from .db import DB
from .planning.engine import threshold_amounts
from .planning.service import days_in, month_params, status
from .recommendations import advise, pairs_from_1c

MONTHS = ["", "январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
          "ноябрь", "декабрь"]
MONTHS_GEN = ["", "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря"]


def rub(x: float | None) -> str:
    return "—" if x is None else f"{x:,.0f}".replace(",", " ") + " ₽"


def d1(x: float | None) -> str:
    return "—" if x is None else f"{x:.1f}".replace(".", ",")


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.0%}"


def daily(db: DB, cfg: Settings, sid: int, day: date) -> str:
    """Сообщение продавцу на следующий день после закрытия дня `day`."""
    s = db.seller(sid)
    st = status(db, cfg, sid, day)
    y = M.seller(db, sid, day, day)
    mtd = M.seller(db, sid, day.replace(day=1), day)
    tm = M.team(db, day.replace(day=1), day)
    shop = M.store(db, day, day)
    L = [f"Доброе утро, {s['first_name'] or s['name']}! Итоги {day.day} {MONTHS_GEN[day.month]}.", ""]
    if y.sales or y.items:
        if y.has_receipts:
            L.append(f"**Вчера:** {rub(y.sales)} · чеков {y.receipts} · средний чек {rub(y.avg_check)} · "
                     f"вещей в чеке {d1(y.upt)}")
        else:
            L.append(f"**Вчера:** {rub(y.sales)} · вещей {y.items:.0f} · средняя цена вещи {rub(y.avg_price)}")
            if y.card_purchases:
                L.append(f"Покупок по картам: {y.card_purchases}, средняя {rub(y.card_avg)}")
            if shop.checks:
                L.append(f"Магазины вчера: средний чек {rub(shop.avg_check)} · вещей в чеке {d1(shop.upt)}")
    else:
        L.append("**Вчера:** продаж нет (выходной?)")
    L += [f"🏅 {a.text}" for a in achievements.for_day(db, cfg, sid, day, st.plan_full)]
    L.append("")
    month = day.strftime("%Y-%m")
    trial = cfg.trial(month)
    if st.plan_month and day.day == days_in(month):  # последний день: итог месяца, а не «до базы» (аудит Д6)
        L += [f"**{MONTHS[day.month].capitalize()} закончился**, рабочих дней {st.worked}:",
              f"Продано {rub(st.sales)} — {pct(st.sales / st.plan_month)} плана месяца ({rub(st.plan_month)})"]
        if trial:
            L.append(f"{MONTHS[day.month].capitalize()} был пробным: ставка по новой схеме — с "
                     f"{MONTHS_GEN[int(cfg.first_month[5:])]}.")
        else:
            L.append(f"Ставка по схеме: {rub(st.current_rate)} за полный месяц. Итоги месяца бот пришлёт, когда "
                     "в 1С будут закрыты все дни.")
    elif st.plan_month:
        L += [f"**Ваш план на {MONTHS[day.month]}**, рабочих дней {st.worked} из {st.planned_shifts}:",
              f"Продано {rub(st.sales)} — {pct(st.sales / st.plan_month)} плана месяца",
              f"Базовый план: {rub(st.plan_month)}"]
        if st.to_base:
            per = f" (≈ {rub(st.per_shift_needed_base)} в рабочий день)" if st.per_shift_needed_base else ""
            L.append(f"До базы осталось: **{rub(st.to_base)}**{per}")
        else:
            L.append("Базовый план выполнен ✅")
        if st.next_level and st.to_next is not None:
            per = f" (≈ {rub(st.per_shift_needed_next)} в рабочий день)" if st.per_shift_needed_next else ""
            L.append(f"До ступени {pct(st.next_level)} (ставка {rub(st.next_rate)}): **{rub(st.to_next)}**{per}")
        elif st.plan_month:
            L.append("Максимальная ступень достигнута 🏆")
        if st.pace_per_shift:
            L.append(f"Ваш темп: {rub(st.pace_per_shift)} за рабочий день → выполнение {pct(st.performance)}, "
                     f"ставка по итогам месяца при таком темпе {rub(st.projected_rate)}")
        if trial:
            L.append(f"_{MONTHS[day.month].capitalize()} — пробный месяц: ставка по новой схеме начисляется с "
                     f"{MONTHS_GEN[int(cfg.first_month[5:])]}._")
    else:
        L.append("План на месяц ещё не рассчитан — уточните у администратора.")
    gl = goals.lines(db, sid, day)
    if gl:
        L += [""] + gl
    shop_m = M.store(db, day.replace(day=1), day)
    L += ["", "**Ваши показатели с начала месяца** (вы / команда):",
          f"Продажи за рабочий день: {rub(mtd.per_shift)} / {rub(tm.per_shift)}"]
    if mtd.has_receipts:
        L += [f"Средний чек: {rub(mtd.avg_check)} / {rub(tm.avg_check)}",
              f"Вещей в чеке: {d1(mtd.upt)} / {d1(tm.upt)}"]
    else:
        L += [f"Вещей за рабочий день: {d1(mtd.items_per_shift)} / {d1(tm.items_per_shift)}",
              f"Средняя цена вещи: {rub(mtd.avg_price)} / {rub(tm.avg_price)}"]
        if mtd.card_purchases or tm.card_purchases:
            L.append(f"Покупка клиента с картой: {rub(mtd.card_avg)} / {rub(tm.card_avg)}, "
                     f"вещей {d1(mtd.card_upt)} / {d1(tm.card_upt)}")
        if shop_m.checks:
            L.append(f"Магазины: средний чек {rub(shop_m.avg_check)} · вещей в чеке {d1(shop_m.upt)}")
    arr = M.arrivals_summary(db, day)
    if arr:
        L += ["", "📦 **Новые поступления:** " + ", ".join(f"{c} — {m} мод. ({q:.0f} шт.)" for c, m, q in arr)]
        L += pairing(db, cfg, day, [c for c, _, _ in arr])
    tips = advise(db, cfg, sid, st, day)
    if tips:
        L += ["", "💡 " + "\n💡 ".join(tips)]
    return "\n".join(L)


def pairing(db: DB, cfg: Settings, day: date, categories: list[str], top: int = 2) -> list[str]:
    """«С чем предлагать» новинки: что покупатели чаще всего берут вместе с этой категорией (чеки или
    покупки по дисконтной карте за recommendations.lookback_days). Только связки от 15 %."""
    pairs = pairs_from_1c(db, cfg, day)  # без того, что идёт в комплекте (джинсы + ремень)
    tips = []
    for c in categories[:top]:
        with_c = [(b, sh) for b, sh in pairs.get(c, []) if sh >= 0.15][:2]
        if with_c:
            tips.append(f"к «{c}» чаще берут " + ", ".join(f"«{b}» ({sh:.0%})" for b, sh in with_c))
    return ["С чем предлагать: " + "; ".join(tips)] if tips else []


def store_goal(db: DB, cfg: Settings, day: date) -> list[str]:
    """Продажи обоих магазинов с начала месяца против зафиксированного плана магазина — только в сводке
    владельца; продавцу — личный план и личные показатели (решение владельца 18.10.2026)."""
    if not cfg.motivation.store_goal:
        return []
    month = day.strftime("%Y-%m")
    plan = db.one("SELECT MAX(store_plan) p FROM plans WHERE month=?", month)["p"]
    if not plan:
        return []
    sales = db.one("SELECT COALESCE(SUM(amount),0) s FROM sales_lines WHERE substr(day,1,7)=? AND day<=?",
                   month, day.isoformat())["s"]
    n = days_in(month)
    return [f"Магазины вместе: {rub(sales)} из {rub(plan)} — {pct(sales / plan)} плана, "
            f"прошло {day.day} из {n} {'дня' if n % 10 == 1 else 'дней'}"]


def month_start(db: DB, cfg: Settings, sid: int, month: str) -> str:
    s = db.seller(sid)
    p = month_params(db, cfg, month)
    r = db.one("SELECT plan_full FROM plans WHERE month=? AND seller_id=?", month, sid)
    if not r or not r["plan_full"]:
        return ""
    planned = db.planned_shifts(sid, month) or cfg.norm_shifts
    k = planned / cfg.norm_shifts
    admin = s["role"] == "admin"
    L = [f"{s['first_name'] or s['name']}, ваш план на {MONTHS[int(month[5:])]} ({planned} {plural(planned, 'рабочий день', 'рабочих дня', 'рабочих дней')}):", ""]
    for amount, rate in threshold_amounts(r["plan_full"], p):
        lvl = round(amount / r["plan_full"] * 100)
        add = f" + {rub(p.admin_bonus)} админ." if admin else ""
        L.append(f"{lvl}% — {rub(amount * k)} → ставка {rub(rate)}{add}")
    L += ["", f"Каждые +{p.step:.0%} сверх плана = +{rub(p.step_bonus)} к ставке. Удачного месяца!"]
    if cfg.trial(month):
        L.append(f"_{MONTHS[int(month[5:])].capitalize()} — пробный месяц: план настоящий, а ставка по новой схеме "
                 f"начисляется с {MONTHS_GEN[int(cfg.first_month[5:])]}._")
    return "\n".join(L)


def month_result(row: dict) -> str:
    return (f"Итоги месяца: продажи {rub(row['sales'])}, выполнение плана {pct(row['performance'])}, "
            f"ставка {rub(row['rate'])} (за полный месяц; при неполном — пропорционально рабочим дням).")


def owner_summary(db: DB, cfg: Settings, day: date) -> str:
    rows = []
    for s in db.sellers():
        st = status(db, cfg, s["id"], day)
        if not st.sales:
            continue
        m = M.seller(db, s["id"], day.replace(day=1), day)
        rows.append((s["name"], st, m))
    tot = M.store(db, day.replace(day=1), day)
    allm = M.team(db, day.replace(day=1), day, exclude_admins=False)
    L = [f"**Сводка на {day:%d.%m.%Y}**",
         f"С начала месяца: {rub(allm.sales)} · средний чек {rub(tot.avg_check)} · вещей в чеке {d1(tot.upt)}",
         *store_goal(db, cfg, day), ""]
    for name, st, m in sorted(rows, key=lambda x: -(x[1].performance or 0)):
        q = (f"чек {rub(m.avg_check)} · {d1(m.upt)} вещ./чек" if m.has_receipts
             else f"{d1(m.items_per_shift)} вещ./день · цена вещи {rub(m.avg_price)}")
        manual = f" · ручные скидки {rub(m.manual_discount)}" if m.manual_discount else ""
        cards = f" · по картам {pct(m.card_share)}" if m.card_purchases else ""
        L.append(f"{name}: {rub(st.sales)} · выполнение {pct(st.performance)} · ставка "
                 f"{rub(st.projected_rate or st.current_rate)} · за день {rub(m.per_shift)} · {q}{manual}{cards}")
    good = achievements.praise(db, cfg, day)
    if good:
        L += ["", "👏 **Кого похвалить:**"] + [f"{name} — {'; '.join(why)}" for _, name, why in good]
    start = day.replace(day=1)
    no_seller = M.unattributed(db, start, day, cfg.onec.no_seller_name)
    if no_seller:
        L += ["", f"⚠️ Продажи без продавца с начала месяца: {rub(no_seller)} — ни в чей план не попали."]
    pend = db.offschedule_pending()
    if pend:
        L += ["", f"❓ Продажи в дни вне графика ждут вашего ответа: {len(pend)} — админка → «График работы». "
                  "Пока ответа нет, эти дни не считаются рабочими."]
    if allm.manual_discount:
        L.append(f"Ручные скидки с начала месяца: {rub(allm.manual_discount)} ({pct(allm.manual_share)} от суммы до скидок)")
    hrs = M.hours(db, start, day)
    if hrs:
        best = sorted(hrs.items(), key=lambda x: -x[1])[:3]
        L.append("Лучшие часы: " + ", ".join(f"{h}:00–{h + 1}:00" for h, _ in sorted(best)))
    bs = M.broken_sizes(db, day, limit=5)
    if bs:
        L += ["", "**Ходовые модели, осталось 1–2 размера:**"] + [
            f"{st_}: {item} — {sz} разм., {q:.0f} шт. (продано за 30 дн.: {n:.0f})" for st_, item, sz, q, n in bs]
    stale_m, stale_q = M.stale_stock(db, day)
    if stale_m:
        L.append(f"Без продаж 90+ дней на остатке: {stale_m} моделей, {stale_q:.0f} шт.")
    return "\n".join(x for x in L if x is not None)


def yesterday(today: date) -> date:
    return today - timedelta(days=1)
