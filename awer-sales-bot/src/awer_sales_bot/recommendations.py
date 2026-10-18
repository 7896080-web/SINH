"""Рекомендации по увеличению продаж — правила на данных продавца и команды.

Каждое правило возвращает (приоритет, текст). В сообщение идут 1–2 самых важных.
Опционально (recommendations.llm: true) текст переформулирует Claude API — без новых цифр.
"""
from __future__ import annotations

from datetime import date, timedelta

from . import metrics as M
from .config import Settings
from .db import DB
from .planning.service import Status


def _d1(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def _rub(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + " ₽"


def rules(db: DB, cfg: Settings, sid: int, st: Status, day: date) -> list[tuple[int, str]]:
    m_start = day.replace(day=1)
    lb_start = day - timedelta(days=cfg.recommendations.lookback_days)
    me = M.seller(db, sid, m_start, day)
    tm = M.team(db, m_start, day, exclude_admins=True)
    out: list[tuple[int, str]] = []

    # 1. Темп отстаёт от плана
    if st.plan_month and st.pace_per_shift is not None and st.per_shift_needed_base and st.remaining_shifts:
        if st.pace_per_shift < st.per_shift_needed_base:
            out.append((90, f"Чтобы выйти на план, нужно в среднем {_rub(st.per_shift_needed_base)} за смену "
                            f"(сейчас {_rub(st.pace_per_shift)}). Каждый лишний товар в чеке приближает к цели."))

    # 2. Комплектность ниже команды
    if me.upt and tm.upt and me.upt < tm.upt * 0.9:
        share = M.single_item_share(db, sid, m_start, day)
        cat = M.top_single_category(db, sid, lb_start, day)
        pairs = M.cross_sell(db, lb_start, day).get(cat or "", [])
        tip = ""
        if cat and pairs:
            tip = f" К категории «{cat}» чаще всего докупают: " + ", ".join(f"«{b}»" for b, _ in pairs[:2]) + "."
        share_txt = f" {share:.0%} ваших чеков — одна вещь." if share else ""
        out.append((80, f"Вещей в чеке {_d1(me.upt)} против {_d1(tm.upt)} у команды.{share_txt} "
                        f"Предлагайте второй товар к образу.{tip}"))

    # 3. Средний чек ниже команды
    if me.avg_check and tm.avg_check and me.avg_check < tm.avg_check * 0.9:
        my_p = M.avg_item_price(db, m_start, day, sid)
        tm_p = M.avg_item_price(db, m_start, day)
        extra = ""
        if my_p and tm_p and my_p < tm_p * 0.9:
            extra = f" Средняя цена вещи у вас {_rub(my_p)}, у команды {_rub(tm_p)} — начинайте показ с моделей подороже."
        out.append((70, f"Средний чек {_rub(me.avg_check)} против {_rub(tm.avg_check)} у команды.{extra}"))

    # 4. Возвраты
    if me.sales > 0 and me.returns / (me.sales + me.returns) > 0.08:
        out.append((50, f"Возвраты {me.returns / (me.sales + me.returns):.0%} от продаж — проверяйте размер "
                        "и посадку на примерке, это сохранит выручку."))

    # 5. Всё хорошо
    if not out and st.performance and st.performance >= 1:
        out.append((10, "Отличный темп! Держите его — до следующей ступени совсем немного."))
    return sorted(out, key=lambda x: -x[0])


def advise(db: DB, cfg: Settings, sid: int, st: Status, day: date, limit: int = 2) -> list[str]:
    tips = [t for _, t in rules(db, cfg, sid, st, day)[:limit]]
    if cfg.recommendations.llm and cfg.anthropic_key and tips:
        try:
            return [_llm_rephrase(cfg, " ".join(tips))]
        except Exception:  # noqa: BLE001 — советы по правилам остаются
            pass
    return tips


def _llm_rephrase(cfg: Settings, text: str) -> str:
    import httpx

    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": cfg.anthropic_key, "anthropic-version": "2023-06-01"},
        json={"model": cfg.recommendations.llm_model, "max_tokens": 300,
              "messages": [{"role": "user", "content":
                            "Перепиши совет продавцу магазина мужской одежды коротко (2–3 предложения), дружелюбно, "
                            "на «вы». Не добавляй новых цифр и фактов.\n\n" + text}]},
        timeout=30)
    r.raise_for_status()
    return r.json()["content"][0]["text"].strip()
