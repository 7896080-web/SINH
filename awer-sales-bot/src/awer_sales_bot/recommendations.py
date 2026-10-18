"""Рекомендации по увеличению продаж — правила на данных продавца и команды (без админов).

Каждое правило возвращает (приоритет, текст). В сообщение идут 1–2 самых важных.
Опционально (recommendations.llm: true) текст переформулирует Claude API — без новых цифр.
"""
from __future__ import annotations

from datetime import date, timedelta

from . import metrics as M
from .config import Settings
from .db import DB
from .planning.service import Status

# категории-«дополнения»: их стоит предлагать ко второму товару (поправьте под свой ассортимент)
ADD_ONS = ("Ремень", "Футболка", "Рубашка", "Носки", "Галстук", "Свитшот", "Джемпер")


def _rub(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + " ₽"


def _d1(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def _main_store(db: DB, sid: int, start: date, end: date) -> str | None:
    r = db.one("SELECT store, COUNT(*) n FROM sales_lines WHERE seller_id=? AND day BETWEEN ? AND ? "
               "GROUP BY store ORDER BY n DESC LIMIT 1", sid, start.isoformat(), end.isoformat())
    return r["store"] if r else None


def rules(db: DB, cfg: Settings, sid: int, st: Status, day: date) -> list[tuple[int, str]]:
    m_start = day.replace(day=1)
    lb_start = day - timedelta(days=cfg.recommendations.lookback_days)
    me = M.seller(db, sid, m_start, day)
    tm = M.team(db, m_start, day)
    out: list[tuple[int, str]] = []

    # 1. Темп отстаёт от плана
    if st.plan_month and st.pace_per_shift is not None and st.per_shift_needed_base and st.remaining_shifts:
        if st.pace_per_shift < st.per_shift_needed_base:
            out.append((90, f"Чтобы выйти на план, нужно в среднем {_rub(st.per_shift_needed_base)} за смену "
                            f"(сейчас {_rub(st.pace_per_shift)})."))

    # 2. Комплектность: по чекам (если есть) или вещей за смену; какую категорию-дополнение продавец «недопредлагает»
    low_upt = (me.has_receipts and tm.upt and me.upt and me.upt < tm.upt * 0.9) or \
              (not me.has_receipts and me.items_per_shift and tm.items_per_shift
               and me.items_per_shift < tm.items_per_shift * 0.9)
    if low_upt:
        mine, team_mix = M.category_mix(db, sid, lb_start, day), M.category_mix(db, None, lb_start, day)
        stock = M.stock_by_category(db, _main_store(db, sid, m_start, day))
        gaps = sorted(((c, team_mix[c] - mine.get(c, 0.0)) for c in team_mix
                       if any(c.startswith(a) for a in ADD_ONS) and (not stock or stock.get(c, (0, 0))[1] > 0)),
                      key=lambda x: -x[1])
        tip = ""
        if gaps and gaps[0][1] > 0.02:
            c = gaps[0][0]
            have = f" В наличии {stock[c][0]} моделей." if stock.get(c) else ""
            tip = f" У команды «{c}» — {team_mix[c]:.0%} проданных вещей, у вас {mine.get(c, 0):.0%}.{have}"
        if me.has_receipts:
            single = f" {me.single_share:.0%} ваших чеков — одна вещь." if me.single_share else ""
            head = f"Вещей в чеке {_d1(me.upt)} против {_d1(tm.upt)} у команды.{single}"
        else:
            head = f"Вещей за смену {_d1(me.items_per_shift)} против {_d1(tm.items_per_shift)} у команды."
        out.append((80, f"{head} Предлагайте второй товар к образу.{tip}"))

    # 2б. Средний чек ниже команды (только если есть чеки)
    if me.has_receipts and me.avg_check and tm.avg_check and me.avg_check < tm.avg_check * 0.9:
        out.append((75, f"Средний чек {_rub(me.avg_check)} против {_rub(tm.avg_check)} у команды — "
                        "собирайте образ целиком: брюки + рубашка + ремень."))

    # 3. Дешёвая корзина → начинать показ с моделей подороже
    if me.avg_price and tm.avg_price and me.avg_price < tm.avg_price * 0.9:
        out.append((70, f"Средняя цена проданной вещи у вас {_rub(me.avg_price)}, у команды {_rub(tm.avg_price)} — "
                        "начинайте показ с моделей среднего и верхнего ценового сегмента."))

    # 4. Скидки выше команды
    if me.discount_share and tm.discount_share is not None and me.discount_share > max(0.05, tm.discount_share * 1.3):
        out.append((60, f"Скидки — {me.discount_share:.0%} от суммы продаж (у команды {tm.discount_share:.0%}). "
                        "Сначала рассказывайте о ценности вещи, скидку — в конце и только когда она решает покупку."))

    # 5. Возвраты
    if me.returns_share and me.returns_share > 0.08:
        out.append((50, f"Возвраты {me.returns_share:.0%} от продаж — проверяйте размер и посадку на примерке."))

    # 6. Всё хорошо
    if not out and st.performance and st.performance >= 1:
        out.append((10, "Отличный темп! Держите его — следующая ступень уже близко."))
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
