"""Связка движка с БД: настройки месяца, ручные правки, фиксация плана, статус продавца, закрытие месяца."""
from __future__ import annotations

import calendar
import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime

from ..config import Settings
from ..db import DB
from .engine import History, Params, StorePlan, evaluate, month_add, rate_for, seller_plan, steps_for, store_plan

ADJUSTABLE = ("step", "step_bonus", "base_rate", "max_rate")


# ----------------------------------------------------------------------------- settings
def month_params(db: DB, cfg: Settings, month: str) -> Params:
    """Параметры месяца: config.yaml, перекрытые ручными значениями со страницы настроек."""
    p = dataclasses.replace(cfg.params)
    r = db.one("SELECT * FROM month_settings WHERE month=?", month)
    if r:
        for k in ADJUSTABLE:
            if r[k] is not None:
                setattr(p, k, float(r[k]))
    return p


def trend_manual(db: DB, month: str) -> float | None:
    r = db.one("SELECT trend_manual FROM month_settings WHERE month=?", month)
    return r["trend_manual"] if r and r["trend_manual"] is not None else None


def save_month_settings(db: DB, month: str, **vals) -> None:
    cols = ["trend_manual", *ADJUSTABLE, "note"]
    data = {k: vals.get(k) for k in cols}
    with db.tx() as c:
        c.execute(
            f"INSERT INTO month_settings(month,{','.join(cols)},updated_at) VALUES(?,{','.join('?' * len(cols))},?) "
            f"ON CONFLICT(month) DO UPDATE SET {','.join(f'{k}=excluded.{k}' for k in cols)}, updated_at=excluded.updated_at",
            (month, *[data[k] for k in cols], datetime.now().isoformat(timespec="seconds")))


def save_plan_override(db: DB, month: str, sid: int, plan: float | None, note: str | None = None) -> bool:
    """Ручной план на полный месяц (None — снять). True — значение изменилось."""
    old = db.one("SELECT plan_manual FROM plan_overrides WHERE month=? AND seller_id=?", month, sid)
    if (old["plan_manual"] if old else None) == plan:
        return False
    with db.tx() as c:
        if plan is None:
            c.execute("DELETE FROM plan_overrides WHERE month=? AND seller_id=?", (month, sid))
        else:
            c.execute("INSERT INTO plan_overrides(month,seller_id,plan_manual,note,updated_at) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(month,seller_id) DO UPDATE SET plan_manual=excluded.plan_manual, note=excluded.note, "
                      "updated_at=excluded.updated_at",
                      (month, sid, plan, note, datetime.now().isoformat(timespec="seconds")))
    return True


def apply_override(db: DB, cfg: Settings, month: str, sid: int) -> bool:
    """План месяца уже зафиксирован, а владелец вписал или снял ручной план (обычно новичку, появившемуся после
    1-го): меняется строка в plans только этого продавца — остальным цели не пересчитываются. True — изменено."""
    if not db.one("SELECT 1 FROM plans WHERE month=?", month):
        return False  # ещё не зафиксирован — ручной план попадёт в фиксацию
    p = month_params(db, cfg, month)
    h = db.history(cfg.norm_shifts, until_month=month_add(month, -1))
    sp = store_plan(h, month, p, trend_override=trend_manual(db, month))
    man = db.one("SELECT plan_manual FROM plan_overrides WHERE month=? AND seller_id=?", month, sid)
    calc = seller_plan(h, month, str(sid), sp, p, man["plan_manual"] if man else None)
    with db.tx() as c:
        c.execute("INSERT INTO plans(month,seller_id,plan_full,method,level,trend,store_plan,coef,frozen_at,role) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(month,seller_id) DO UPDATE SET plan_full=excluded.plan_full, "
                  "method=excluded.method, level=excluded.level, frozen_at=excluded.frozen_at",
                  (month, sid, calc.plan_full, calc.method, calc.level, sp.trend, sp.plan, sp.coef,
                   datetime.now().isoformat(timespec="seconds"), db.seller(sid)["role"]))
    refresh_results(db, cfg, month)  # месяц уже закрыт — итоги по новому плану (аудит Д3)
    return True


# ----------------------------------------------------------------------------- plans
@dataclass
class PlanRow:
    seller_id: int
    name: str
    role: str
    plan_calc: float | None
    plan_manual: float | None
    plan_final: float | None
    method: str
    level: float | None


@dataclass
class MonthPlan:
    month: str
    params: Params
    store: StorePlan
    trend_manual: float | None
    rows: list[PlanRow] = field(default_factory=list)
    frozen_at: str | None = None


def participants(db: DB, month: str, h: History) -> list:
    """Активные продавцы, работавшие в окне 3 мес., в самом месяце, стоящие в графике или уже в плане месяца."""
    window = {month_add(month, -k) for k in range(0, 4)}
    ids = {int(s) for m in window for s in h.months.get(m, {})}
    ids |= {r["seller_id"] for r in db.q("SELECT DISTINCT seller_id FROM schedule WHERE substr(day,1,7)=?", month)}
    ids |= {r["seller_id"] for r in db.q("SELECT seller_id FROM plans WHERE month=?", month)}
    ids |= {r["seller_id"] for r in db.q("SELECT DISTINCT seller_id FROM sales_lines WHERE substr(day,1,7)=?", month)}
    return [r for r in db.sellers() if r["id"] in ids]


def compute(db: DB, cfg: Settings, month: str) -> MonthPlan:
    p = month_params(db, cfg, month)
    h = db.history(cfg.norm_shifts, until_month=month_add(month, -1))
    tm = trend_manual(db, month)
    sp = store_plan(h, month, p, trend_override=tm)
    manual = {r["seller_id"]: r["plan_manual"] for r in db.q("SELECT * FROM plan_overrides WHERE month=?", month)}
    mp = MonthPlan(month, p, sp, tm)
    for s in participants(db, month, h):
        calc = seller_plan(h, month, str(s["id"]), sp, p)
        man = manual.get(s["id"])
        mp.rows.append(PlanRow(s["id"], s["name"], s["role"], calc.plan_full, man,
                               man if man is not None else calc.plan_full,
                               "manual" if man is not None else calc.method, calc.level))
    fr = db.one("SELECT MAX(frozen_at) f FROM plans WHERE month=?", month)
    mp.frozen_at = fr["f"] if fr else None
    return mp


def freeze(db: DB, cfg: Settings, month: str) -> MonthPlan:
    """Фиксирует план месяца. Ежедневные сообщения берут план только из зафиксированного."""
    mp = compute(db, cfg, month)
    now = datetime.now().isoformat(timespec="seconds")
    with db.tx() as c:
        c.execute("DELETE FROM plans WHERE month=?", (month,))
        for r in mp.rows:
            c.execute("INSERT INTO plans(month,seller_id,plan_full,method,level,trend,store_plan,coef,frozen_at,role) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (month, r.seller_id, r.plan_final, r.method, r.level, mp.store.trend, mp.store.plan,
                       mp.store.coef, now, r.role))
    mp.frozen_at = now
    refresh_results(db, cfg, month)
    return mp


def ensure_plans(db: DB, cfg: Settings, month: str) -> list[dict]:
    """SPEC п. 10: продавцу с продажами в месяце, у которого нет строки в зафиксированном плане, — его план
    (обычно новичка) одной строкой, не трогая остальных. Возвращает добавленные строки."""
    if not db.one("SELECT 1 FROM plans WHERE month=?", month):
        return []
    missing = db.q("SELECT DISTINCT s.id, s.name, s.role FROM sales_lines l JOIN sellers s ON s.id=l.seller_id "
                   "WHERE substr(l.day,1,7)=? AND s.active=1 AND s.role<>'system' "
                   "AND s.id NOT IN (SELECT seller_id FROM plans WHERE month=?)", month, month)
    if not missing:
        return []
    p = month_params(db, cfg, month)
    h = db.history(cfg.norm_shifts, until_month=month_add(month, -1))
    sp = store_plan(h, month, p, trend_override=trend_manual(db, month))
    manual = {r["seller_id"]: r["plan_manual"] for r in db.q("SELECT * FROM plan_overrides WHERE month=?", month)}
    now = datetime.now().isoformat(timespec="seconds")
    out = []
    with db.tx() as c:
        for r in missing:
            calc = seller_plan(h, month, str(r["id"]), sp, p, manual.get(r["id"]))
            c.execute("INSERT INTO plans(month,seller_id,plan_full,method,level,trend,store_plan,coef,frozen_at,role) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (month, r["id"], calc.plan_full, calc.method, calc.level, sp.trend, sp.plan, sp.coef, now,
                       r["role"]))
            out.append(dict(seller_id=r["id"], name=r["name"], plan=calc.plan_full, method=calc.method))
    return out


def frozen_plan(db: DB, month: str, sid: int) -> float | None:
    r = db.one("SELECT plan_full FROM plans WHERE month=? AND seller_id=?", month, sid)
    return r["plan_full"] if r else None


# ----------------------------------------------------------------------------- daily status
@dataclass
class Status:
    seller_id: int
    month: str
    day: date
    plan_full: float | None  # личный план на полный месяц
    planned_shifts: int  # смен в месяце: отработанные + по графику впереди (без графика — не меньше нормы)
    worked: int  # отработано смен до дня включительно
    sales: float  # продажи с начала месяца
    plan_month: float | None  # план на свои смены = план × смены месяца / норма (SPEC «Ежедневное сообщение»)
    to_base: float | None  # осталось до 100 %
    next_level: float | None  # следующая ступень, доля (1.1 ...)
    to_next: float | None  # осталось до следующей ступени
    next_rate: float | None
    remaining_shifts: int
    per_shift_needed_base: float | None
    per_shift_needed_next: float | None
    pace_per_shift: float | None
    performance: float | None  # по факту на сегодня (на смену)
    projected_rate: float | None
    current_rate: float


def status(db: DB, cfg: Settings, sid: int, day: date) -> Status:
    month = day.strftime("%Y-%m")
    p = month_params(db, cfg, month)
    role = db.seller(sid)["role"]
    plan = frozen_plan(db, month, sid)
    if plan is None:  # план ещё не зафиксирован — считаем на лету
        row = next((r for r in compute(db, cfg, month).rows if r.seller_id == sid), None)
        plan = row.plan_final if row else None
    worked = db.worked_shifts(sid, month, upto=day)
    ahead = db.scheduled_after(sid, month, day)
    # отработанные + дни по графику впереди: к концу месяца «% плана месяца» = выполнению для ставки.
    # Без графика — норма, но не больше, чем позволяют оставшиеся дни календаря (аудит Д6)
    left = days_in(month) - day.day
    planned = worked + ahead if ahead is not None else max(worked, min(cfg.norm_shifts, worked + left))
    sales = db.one("SELECT COALESCE(SUM(amount),0) s FROM sales_lines WHERE seller_id=? AND substr(day,1,7)=? AND day<=?",
                   sid, month, day.isoformat())["s"]
    remaining = max(0, planned - worked)
    st = Status(sid, month, day, plan, planned, worked, sales, None, None, None, None, None, remaining,
                None, None, None, None, None, rate_for(0, role, p))
    if not plan:
        return st
    st.plan_month = plan * planned / cfg.norm_shifts
    st.to_base = max(0.0, st.plan_month - sales)
    achieved = sales / st.plan_month if st.plan_month else 0.0
    k = steps_for(achieved, p) + 1  # первая ступень выше набранного за месяц
    if p.base_rate + p.step_bonus * k <= p.max_rate:
        st.next_level = 1 + p.step * k
        st.to_next = max(0.0, st.plan_month * st.next_level - sales)
        st.next_rate = rate_for(k, role, p)
    if remaining:
        st.per_shift_needed_base = st.to_base / remaining if st.to_base else 0.0
        st.per_shift_needed_next = st.to_next / remaining if st.to_next is not None else None
    if worked:
        st.pace_per_shift = sales / worked
        fte = worked / cfg.norm_shifts
        st.performance = (sales / fte) / plan
        st.projected_rate = evaluate(sales, fte, plan, role, p).rate
        st.current_rate = st.projected_rate
    return st


# ----------------------------------------------------------------------------- month close
def close_month(db: DB, cfg: Settings, month: str, final: bool | None = None) -> list[dict]:
    """Итоги месяца → results. final — данные полные (все дни × магазины выгружены или отмечены «закрыто»);
    None — определить самому. Предварительные итоги бот пересчитает сам, когда данные станут полными (аудит Д2).
    Роль — на день фиксации плана (повышение 1-го числа не добавляет доплату за прошлый месяц, Д3). Строки месяца,
    которых нет в новом наборе (объединённый дубль, служебная запись), удаляются."""
    if final is None:
        final = not db.month_missing(month, cfg.stores)
    p = month_params(db, cfg, month)
    h = db.history(cfg.norm_shifts, until_month=month)
    prev = {r["seller_id"]: r["steps"] for r in db.q("SELECT seller_id, steps FROM results WHERE month=?",
                                                       month_add(month, -1))}
    out = []
    now = datetime.now().isoformat(timespec="seconds")
    with db.tx() as c:
        # все из плана + все, у кого есть продажи или график (SPEC п. 10): без плана — базовая ставка, но в ведомости
        for r in db.q("SELECT s.id seller_id, p.plan_full, COALESCE(p.role, s.role) role, s.name FROM sellers s "
                      "LEFT JOIN plans p ON p.seller_id=s.id AND p.month=? "
                      "WHERE s.role<>'system' AND (p.seller_id IS NOT NULL OR s.id IN "
                      "(SELECT seller_id FROM sales_lines WHERE substr(day,1,7)=?) OR s.id IN "
                      "(SELECT seller_id FROM schedule WHERE substr(day,1,7)=?))", month, month, month):
            x = h.get(month, str(r["seller_id"]))
            sales, fte = (x.sales, x.fte) if x else (0.0, 0.0)
            res = evaluate(sales, fte, r["plan_full"], r["role"], p, prev.get(r["seller_id"]))
            c.execute("INSERT INTO results(month,seller_id,sales,fte,performance,steps,rate,closed_at) "
                      "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(month,seller_id) DO UPDATE SET sales=excluded.sales, "
                      "fte=excluded.fte, performance=excluded.performance, steps=excluded.steps, rate=excluded.rate, "
                      "closed_at=excluded.closed_at",
                      (month, r["seller_id"], sales, fte, res.performance, res.steps, res.rate, now))
            out.append(dict(seller_id=r["seller_id"], name=r["name"], role=r["role"], sales=sales, fte=fte,
                            plan=r["plan_full"], performance=res.performance, steps=res.steps, rate=res.rate,
                            percent_pay=sales * p.sales_percent))
        keep = [r["seller_id"] for r in out]
        c.execute(f"DELETE FROM results WHERE month=? AND seller_id NOT IN ({','.join('?' * len(keep)) or 'NULL'})",
                  (month, *keep))
        c.execute("INSERT INTO month_close(month, final, closed_at) VALUES(?,?,?) ON CONFLICT(month) DO UPDATE SET "
                  "final=excluded.final, closed_at=excluded.closed_at", (month, int(final), now))
    return out


def is_final(db: DB, month: str) -> bool:
    r = db.one("SELECT final FROM month_close WHERE month=?", month)
    return bool(r and r["final"])


def month_results(db: DB, cfg: Settings, month: str) -> list[dict]:
    """Сохранённые итоги месяца в том же виде, что возвращает close_month (для сообщений и ведомости)."""
    p = month_params(db, cfg, month)
    return [dict(seller_id=r["seller_id"], name=r["name"], role=r["role"], sales=r["sales"], fte=r["fte"],
                 plan=r["plan_full"], performance=r["performance"], steps=r["steps"], rate=r["rate"],
                 percent_pay=r["sales"] * p.sales_percent)
            for r in db.q("SELECT r.*, s.name, COALESCE(p.role, s.role) role, p.plan_full FROM results r "
                          "JOIN sellers s ON s.id=r.seller_id LEFT JOIN plans p ON p.month=r.month AND "
                          "p.seller_id=r.seller_id WHERE r.month=?", month)]


def refresh_results(db: DB, cfg: Settings, month: str | None = None) -> list[str]:
    """Пересчитать уже подведённые итоги после правки (ручной план, фиксация, объединение продавцов, ответ о дне
    вне графика) — аудит Д3. month=None — все месяцы с итогами. Возвращает пересчитанные месяцы."""
    months = [month] if month else [r["month"] for r in db.q("SELECT DISTINCT month FROM results ORDER BY month")]
    done = []
    for m in months:
        if db.one("SELECT 1 FROM results WHERE month=?", m):
            close_month(db, cfg, m, final=is_final(db, m) or not db.month_missing(m, cfg.stores))
            done.append(m)
    return done


def days_in(month: str) -> int:
    y, m = map(int, month.split("-"))
    return calendar.monthrange(y, m)[1]
