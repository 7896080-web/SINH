# -*- coding: utf-8 -*-
"""
Автопилот рекламы Wildberries: ставки, ставки на поисковые кластеры, минус-фразы, дневные лимиты.
Трогает только кампании из RULES ("*" — все активные, кроме «_except»). Без --apply ничего не меняет.

Что делает для каждой кампании из RULES ("*" — все активные):
  • Ставки товаров — подтягивает к рекомендованной WB для нужных позиций, но не выше окупаемой:
      за клики:  допустимая цена заказа × конверсия товара (клик → заказ);
      за показы: допустимая цена заказа × конверсия × CTR × 1000.
    Меняется сразу в обе стороны, но не выше окупаемого потолка (решение 12.10.2026; ограничения роста/снижения
    в сутки можно вернуть: max_raise_pct / max_drop_pct — считаются от ставки «на начало дня» в wb_bids_state.json).
    Нет в наличии (склады WB + свой склад FBS, см. COUNT_FBS_STOCK) → минимальная.
  • cluster_bids — ставки на поисковые кластеры (только ручная ставка + оплата за показы):
      "all" — на все кластеры, по которым WB даёт рекомендацию;
      "top" — только на лучшие запросы из кампании clusters_from (сколько заказов принесли).
             Если запрос за показы не окупается (заказ дороже лимита в 1,5 раза или ни одного заказа
             при ожидаемых трёх) — ставка снимается и запрос возвращается в кампанию за клики
             на 30 дней.
  • auto_minus — минус-фразы для запросов, которые много кликали и ни разу не заказали
      (ожидалось ≥ 3 заказов при средней конверсии кампании, а их 0). Не больше 20 за запуск,
      слова из PROTECT не трогаются, ваши собственные минус-фразы сохраняются.
      Только для товара, который в наличии (WB + FBS) не меньше MINUS_AFTER_RESTOCK_DAYS (14) дней подряд:
      без остатка запросы не продают, и судить их рано. Слова «не наша аудитория» (MINUS_WORDS) — сразу.
  • route_from — запросы, на которые стоят ставки в кампании route_from (за показы),
      добавляются в минус-фразы этой кампании (за клики), чтобы кампании не перебивали друг друга.
      Если запрос уберут из route_from — минус-фраза снимется сама.
  • daily_limit — дневной лимит расхода, ₽ (только кампании с оплатой за клики).
  Минус-фразы (auto_minus, route_from) работают только у кампаний за показы: у кампаний за клики WB не даёт
  исключать запросы — там этот шаг пропускается.

Запуск:
  python wb_bids.py              — пробный прогон, ничего не меняет
  python wb_bids.py --apply      — применить (с резервной копией)
  python wb_bids.py --rollback   — вернуть всё, что изменил последний запуск

Токены — из wb_tokens.txt. Нужен токен БЕЗ «Только на чтение», категории «Продвижение» и «Аналитика».
"""
import datetime as dt
import json
import math
import os
import shutil
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

import requests

# Переезд на сервер (deploy/export_for_server.ps1 положил MOVED_TO_SERVER.txt): здесь скрипты больше не запускаются —
# два компьютера меняли бы ставки и пополняли бюджеты одновременно, а лимиты WB у них общие.
if (Path(__file__).resolve().parent / "MOVED_TO_SERVER.txt").exists():
    sys.exit("Скрипты перенесены на сервер — запускайте их там (RDP, C:\\direct). Здесь не запускаю: два компьютера "
             "меняли бы рекламу одновременно. Подробно — MOVED_TO_SERVER.txt рядом со скриптами.")

# Вывод в консоль Windows (cp1251) или в файл без UTF-8 не должен падать на ₽ ═ ✓: непечатаемое заменяется «?».
try:
    if sys.stdout is not None and (sys.stdout.encoding or "").lower().replace("-", "") != "utf8":
        sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass

# ============================ НАСТРОЙКИ ============================
# Кабинет (как в wb_tokens.txt) → кампания (ID или уникальная часть названия) → правила.
# target: позиции в поиске "1-2", "3-10" или "11-34".
# max_cost_per_order: сколько рублей рекламы готовы платить за один заказ.
#   "auto" — свой лимит для каждого товара из юнит-экономики (wb_unit.py → wb_unit_economics.csv);
#   для товаров без юнит-экономики берётся max_cost_per_order_default.
#   Джемперы: средний заказ ≈ 7 400 ₽, выкуп ≈ 50% → 15% от выкупа ≈ 550–600 ₽.
#   Куртки на запуске (первые 2–3 недели) ≈ 2 400 ₽, потом ≈ 1 200 ₽.
# "*" — все АКТИВНЫЕ кампании кабинета (статус «активна»; пауза и «готова» не трогаются) с этими правилами.
#   Кампании, перечисленные по названию или ID отдельно, берут свои правила вместо "*".
#   "_except": [ID или часть названия, …] — активные кампании, которые автопилот не трогает (например, на время теста).
#   "until": "ГГГГ-ММ-ДД" в правиле кампании — правило действует до этой даты включительно, потом кампания
#   переходит на общие правила "*" (режим запуска новинок: дороже допустимый заказ на 2–3 недели).
#   Дневной лимит: у кампаний за клики его ставит WB (не меньше 1 000 ₽ — меньший держит, как и у показов, проверка
#   паузой); у кампаний за показы WB его не даёт — держит часовая проверка
#   wb_guard.py: потрачено за сегодня ≈ лимит (или по темпу уйдёт за него до следующей проверки) → пауза до утра,
#   в 7:40 следующего дня — запуск (решение пользователя 15.10.2026: все способы оптимизации для обоих типов).
EXP_MAX_CPC = 20   # эксперимент Ребрик/Караман, кампании за клики: ставка за WB, но не дороже 20 ₽ за клик (решение
                   # 14.10.2026: WB просил до 55 ₽ за клик для товаров со 137 кликами без заказов; остальным хватает 8–18 ₽)
# ── Распределение бюджета по часам (решение пользователя 14.10.2026; уточнить по снимкам ≈7–10.11) ──
# Кампания за клики, у которой дневной лимит кончится раньше конца дня (прогноз по тому, сколько уже потрачено
# к этому часу), — ставка снижается, пока расход опережает график спроса; догнала график — снова по рекомендации.
# График — доля дневного спроса по часам, общий по 3 кабинетам (заказы за 90 дн., wb_dayparts.py, 14.10.2026).
PACING = True
PACE_WD = [2.49, 0.65, 0.76, 0.52, 1.31, 1.44, 2.24, 3.67, 5.10, 5.74, 4.73, 5.64,     # будни, % заказов в час
           5.82, 5.92, 5.12, 5.03, 4.84, 5.57, 5.23, 6.21, 6.41, 6.91, 4.78, 3.86]
PACE_WE = [2.35, 0.61, 0.72, 0.31, 0.78, 0.86, 1.63, 2.66, 3.70, 5.17, 4.26, 5.08,     # сб и вс
           6.40, 6.51, 5.63, 6.41, 6.18, 7.11, 5.51, 6.55, 6.76, 6.59, 4.55, 3.68]
# опережает график больше чем на 50 п. п. → ставка ×0,4; на 30–50 → ×0,55; на 15–30 → ×0,7; на 5–15 → ×0,85 (глубже 0,7 —
# решение пользователя 15.10.2026: кампании за показы тратили 1 000 ₽ за час утром); не ниже минимальной ставки WB
PACE_STEPS = [(0.50, 0.40), (0.30, 0.55), (0.15, 0.70), (0.05, 0.85)]
PACE_MIN_SHARE = 0.12       # раньше, чем набралось 12% дневного спроса (≈ до 7–8 утра), прогноз ненадёжен — не трогать
PACE_LIMITED = 1.05         # «денег не хватит»: прогноз расхода за день больше лимита на 5%+
# кампании, которые график расхода не трогает до указанной даты: {"ID": "ГГГГ-ММ-ДД"}. Эксперимент Ребрик/Караман
# тоже под графиком (решение пользователя 14.10.2026) — учитывать при сравнении 27.10: у стороны «за клики» ставка
# днём могла быть снижена, у «за показы» графика нет.
PACING_SKIP_UNTIL = {}

RULES = {
    "Яворская": {
        "*": {"target": "3-10", "max_cost_per_order": 600, "daily_limit": 1000, "auto_minus": True,
              "topup_below": 500, "topup_amount": 1000, "topup_week_cap": 7000, "topup_day_cap": 1000,
              "topup_source": "balance"},
        "_except": [],
        # Дневной лимит (решение 12.10.2026): 1 000 ₽ в сутки на кампанию, у «36446 клик» — 2 000 ₽, у «27643 клик» — 3 000 ₽
        # (заказы 27643 идут ровно с 7 до 24 ч; лимит впритык выключал бы рекламу в сильные дни к 18–20 ч).
        # ── Главные кампании (решение 12.10.2026): потолок пополнений 2 000 ₽ в сутки и 14 000 ₽ в неделю; если встали на паузу
        #    из-за пустого бюджета — после пополнения запустить. Остальное — как в "*".
        "31049161": {"daily_limit": 3000, "topup_week_cap": 21000, "topup_day_cap": 3000, "start_after_topup": True},   # «27643 клик»
        "31048671": {"daily_limit": 2000, "topup_week_cap": 14000, "topup_day_cap": 2000, "start_after_topup": True},   # «36446 клик»
        # «Куртка общ» включена пользователем 12.10.2026 с пустым бюджетом: общие правила "*", после пополнения — запустить.
        "36114933": {"start_after_topup": True},   # «Куртка общ от 25.04.2026», за клики
        # ── Новинки (запуск, решение 12.10.2026): цель — места 1–2, допустимый заказ вдвое выше обычного,
        #    запросы не минусуем, пока набираются продажи и отзывы. С 03.11 — общие правила "*".
        "39661494": {"target": "1-2", "max_cost_per_order": 1200, "auto_minus": False,
                     "until": "2026-11-02"},   # ZJYM269002, за клики
        "39829896": {"target": "1-2", "max_cost_per_order": 1200, "auto_minus": False,
                     "until": "2026-11-02"},   # 256250 («Кампания от 29.08.2026»), за клики
        "39661508": {"target": "1-2", "max_cost_per_order": 2000, "auto_minus": False,
                     "until": "2026-11-02"},   # «пух нов 22.08.2026» — новинки-куртки, за клики; 2 000 ₽ за заказ (решение 12.10.2026)

        # ── Этап 1. Те же товары в рекомендациях, оплата за показы. Создать:
        #    python wb_create.py --name "27643 рекомендации показы" --pay cpm --where recommendations --from "27643 клик"
        # "27643 рекомендации показы": {"target": "11-34", "max_cost_per_order": 600},

        # ── Этап 2 (через 2–3 недели, когда товар стабильно есть на складах WB).
        #    Поиск за показы только по лучшим запросам. Создать:
        #    python wb_create.py --name "27643 поиск показы" --pay cpm --where search --from "27643 клик"
        # "27643 поиск показы": {"cluster_bids": "top", "clusters_from": "27643 клик", "top_clusters": 10,
        #                        "base_bid": "min", "max_cost_per_order": 600},
        #    …и в правило "27643 клик" добавить:  "route_from": "27643 поиск показы"
    },
    # ── Эксперимент 14–27.10.2026 (решение пользователя): одни и те же модели-новинки в двух кабинетах, перекрёстно:
    #    Ребрик — куртки за клики, джемперы/2101/TC26 за показы; Караман — пуховики за показы, остальное за клики.
    #    Так у каждого кабинета есть оба способа, и разница кабинетов не смешивается с разницей способа оплаты.
    #    Ставки по рекомендации WB (follow_rec, места 3–10), без окупаемого потолка; деньги держит недельный бюджет
    #    пополнений 7 000 ₽ на кабинет ("_week_budget") и дневные лимиты (за клики). Сравнить 27.10: цена заказа, заказы, выкуп.
    # Недельные потолки пополнений (решение пользователя 15.10.2026, по модели месяца): 7 × дневной лимит, округлённо ВВЕРХ
    # до 1 000 ₽ — WB не принимает пополнение меньше 1 000, и с потолком 3 500 доступно лишь 3 000: в конце недели
    # кампания стояла без денег. Расход это не увеличивает — его держит дневной лимит (у показов — пауза стражем).
    "Ребрик": {
        "_week_budget": 16000,      # было 14 200 (решения 14.10.2026); 15.10 — до 7 × сумма дневных лимитов (15 750), кратно 1 000
        # новинки за клики, «Кампания от 10.10.2026», запущена пользователем 14.10.2026: свой бюджет 5 000 ₽/нед
        "40842596": {"target": "3-10", "follow_rec": True, "max_bid_cpc": EXP_MAX_CPC, "auto_minus": False, "start_after_topup": True, "daily_limit": 700,
                     "topup_below": 700, "topup_amount": 700, "topup_day_cap": 700, "topup_week_cap": 5000},
        # за показы дневного лимита у WB нет — держит wb_guard.py паузой (решение 15.10.2026: с 14.10 пополнение не меньше
        # 1 000 ₽, и тысяча уходила за ≈2 ч вечером); недельный потолок — лимит × 7 вверх до 1 000 (решение 15.10.2026)
        # куртки — ЗА КЛИКИ (решение 14.10.2026 по сравнению 40769287 Ребрика ≈606 ₽/заказ и 40769350 Карамана ≈3 505 ₽):
        # 40769287 включена обратно, кампания за показы на эти же куртки 40863039 — на паузе (в правила не входит)
        "40769287": {"target": "3-10", "follow_rec": True, "max_bid_cpc": EXP_MAX_CPC, "auto_minus": False, "start_after_topup": True, "daily_limit": 500,
                     "topup_below": 500, "topup_amount": 500, "topup_day_cap": 500, "topup_week_cap": 4000},
        # джемперы 256250 + ZJYM: бюджет ×2 (решение 14.10.2026) — 600 ₽/сутки, как 300 + 300 у Карамана за клики
        "40863019": {"target": "3-10", "follow_rec": True, "auto_minus": True, "minus_after_restock_days": 0, "start_after_topup": True, "daily_limit": 600,
                     "topup_below": 600, "topup_amount": 600, "topup_day_cap": 600, "topup_week_cap": 5000},
        # «Ребрик 2101 показы» — создана 14.10.2026 через wb_create.py
        "40955628": {"target": "3-10", "follow_rec": True, "auto_minus": True, "minus_after_restock_days": 0, "start_after_topup": True, "daily_limit": 300,
                               "topup_below": 150, "topup_amount": 150, "topup_day_cap": 150, "topup_week_cap": 3000},
        # «Ребрик TC26 показы» — создана 14.10.2026 через wb_create.py
        "40955524": {"target": "3-10", "follow_rec": True, "auto_minus": True, "minus_after_restock_days": 0, "start_after_topup": True, "daily_limit": 150,
                               "topup_below": 100, "topup_amount": 100, "topup_day_cap": 100, "topup_week_cap": 2000},
    },
    "Караман": {
        "_week_budget": 16200,      # 11 200 — эксперимент (джемперы ×2) + 5 000 — новинки 40842612 (решения 14.10.2026)
        # новинки за клики, «Кампания от 10.10.2026», запущена пользователем 14.10.2026: свой бюджет 5 000 ₽/нед
        "40842612": {"target": "3-10", "follow_rec": True, "max_bid_cpc": EXP_MAX_CPC, "auto_minus": False, "start_after_topup": True, "daily_limit": 700,
                     "topup_below": 700, "topup_amount": 700, "topup_day_cap": 700, "topup_week_cap": 5000},
        # дневной лимит = порог = сумма пополнения = потолок в сутки; за неделю — лимит × 7 вверх до 1 000 ₽
        # (WB не принимает пополнение меньше 1 000; решение пользователя 15.10.2026)
        "40769334": {"target": "3-10", "follow_rec": True, "max_bid_cpc": EXP_MAX_CPC, "auto_minus": False, "start_after_topup": True, "daily_limit": 300,
                     "topup_below": 300, "topup_amount": 300, "topup_day_cap": 300, "topup_week_cap": 3000},   # куртки
        # пуховики 2651/2717/2718 — ЗА ПОКАЗЫ (решение 14.10.2026): 40769350 за клики (≈3 505 ₽/заказ) — на паузе,
        # новая кампания за показы с теми же товарами; дневного лимита у WB нет — пополнения по 500 ₽ в сутки
        # (как куртки за клики в Ребрике — равные деньги для честного сравнения; было 200 ₽ — мало, решение 14.10.2026)
        # «Караман пуховики показы» — создана 14.10.2026 через wb_create.py
        "40955797": {"target": "3-10", "follow_rec": True, "auto_minus": True, "minus_after_restock_days": 0, "start_after_topup": True, "daily_limit": 500,
                                    "topup_below": 500, "topup_amount": 500, "topup_day_cap": 500, "topup_week_cap": 4000},
        "40769357": {"target": "3-10", "follow_rec": True, "max_bid_cpc": EXP_MAX_CPC, "auto_minus": False, "start_after_topup": True, "daily_limit": 300,
                     "topup_below": 300, "topup_amount": 300, "topup_day_cap": 300, "topup_week_cap": 3000},   # 256250
        "40769361": {"target": "3-10", "follow_rec": True, "max_bid_cpc": EXP_MAX_CPC, "auto_minus": False, "start_after_topup": True, "daily_limit": 300,
                     "topup_below": 300, "topup_amount": 300, "topup_day_cap": 300, "topup_week_cap": 3000},   # ZJYM
        "40769369": {"target": "3-10", "follow_rec": True, "max_bid_cpc": EXP_MAX_CPC, "auto_minus": False, "start_after_topup": True, "daily_limit": 200,
                     "topup_below": 200, "topup_amount": 200, "topup_day_cap": 200, "topup_week_cap": 2000},   # 2101
    },
}
DEFAULTS = {
    "target": "3-10",
    "max_cost_per_order": 600,
    "max_cost_per_order_default": 600,
    "max_bid_cpc": 60,          # потолок ставки за клик, ₽
    "max_bid_cpm": 800,         # потолок ставки за 1000 показов, ₽
    "max_raise_pct": None,      # максимум роста ставки в сутки, % (от ставки на начало дня); None — без ограничения:
                                # ставка поднимается сразу, но не выше окупаемого потолка (решение 12.10.2026)
    "stop_clicks": 150,         # столько кликов за 14 дней без единого заказа — ставку не повышать и в отчёт
                                # «проверить карточку» (фото, цена, размеры, отзывы); None — выключено
    "min_stock": 15,            # товар, которого в наличии меньше (WB + свой склад), убрать из кампании (None — не убирать).
                                # Правило пользователя от 12.10.2026. Кампанию целиком не опустошает. Откат возвращает товар
    "return_stock": 20,         # убранный из-за малого остатка товар вернуть в кампанию, когда в наличии снова столько
                                # (WB + свой склад); выше min_stock, чтобы товар не дёргался туда-сюда (решение 12.10.2026)
    "max_drop_pct": None,       # максимум снижения ставки в сутки, %; None — без ограничения (решение 12.10.2026:
                                # переплачивать нет смысла, снижать сразу). Нет в наличии или
                                # нет ходовых размеров — минимальная ставка сразу, это ограничение не действует
    "no_stock": "min",          # нет в наличии (WB + FBS, см. COUNT_FBS_STOCK): "min" — минимальная ставка, "skip" — не трогать
    "base_bid": "auto",         # "min" — ставка товара минимальная (для кампаний «только лучшие запросы»)
    "cluster_bids": None,       # None / "all" / "top"
    "clusters_from": None,      # для "top": кампания, откуда брать лучшие запросы
    "top_clusters": 10,         # для "top": сколько лучших запросов на товар
    "top_min_orders": 2,        # для "top": минимум заказов у запроса за 30 дней
    "auto_minus": False,
    "route_from": None,
    "daily_limit": None,        # ₽ в сутки
    # Автопополнение бюджета кампании (у WB нет настройки автопополнения в API — делаем сами методом «пополнить»).
    # ДЕНЬГИ НЕ ВОЗВРАЩАЮТСЯ: пополнение не откатывается. Только при --apply, в пробном прогоне — план.
    "topup_below": None,        # пополнять, когда в бюджете кампании меньше стольких ₽ (None — не пополнять)
    "topup_amount": 1000,       # сумма одного пополнения, ₽
    "topup_week_cap": 7000,     # не больше стольких ₽ пополнений на кампанию за 7 дней
    "topup_day_cap": 1000,      # и не больше стольких ₽ за сутки — чтобы недельные деньги не ушли за 2–3 дня
    "topup_source": "balance",  # balance — «Баланс» (взаиморасчёт), account — «Счёт», bonus — «Бонусы»
    "start_after_topup": False, # кампания на паузе из-за пустого бюджета — запустить после пополнения
    "carry_over": False,        # переносить неизрасходованный дневной лимит на следующий день
}
PROTECT = ["awer", "авер", "mcl"]   # запросы с этими словами никогда не уходят в минус
MINUS_WORDS = ["женск", "детск", "девочк", "мальчик", "для девушки", "подростк"]  # не наша аудитория — в минус сразу
MINUS_MIN_CLICKS = 30               # минимум кликов по запросу, чтобы его судить
MINUS_EXPECTED = 3.0                # сколько заказов «должно было быть» при средней конверсии
MINUS_MAX_PER_RUN = 20              # не больше стольких новых минус-фраз на кампанию за запуск
# за показы платят и за показы без кликов: запрос, который съел деньги, а заказа не дал, — в минус и без 30 кликов
# (сверка с MPSTATS, решение пользователя 15.10.2026)
MINUS_SPEND_ORDERS = 1.5            # потрачено на запрос ≥ 1,5 допустимой цены заказа товара, заказов нет — в минус
MINUS_MIN_VIEWS = 1000              # низкий CTR судим не раньше стольких показов запроса
# «Контроль органики» (сверка с MPSTATS, решение пользователя 15.10.2026), только кампании за показы с автоминусом.
# Органической позиции отдельно от рекламной официальные данные WB не дают, поэтому судим по результату — по заказам
# из «Джема» (все заказы по запросу, wb_positions.py → wb_history.db) против заказов с рекламы по этому кластеру:
ORGANIC_CONTROL = True
ORG_DAYS = 14                       # за столько дней
ORG_MIN_ORDERS = 10                 # всех заказов по запросу меньше — не судим
ORG_AD_SHARE = 0.2                  # реклама дала ≤ 20% заказов по запросу и товар в топе — запрос в минус (экономия)
ORG_TOP_POS = 10                    # …«в топе» — медианная позиция по запросу не хуже этой
ORG_CHECK_DAYS = 5                  # после минуса столько дней ждём и сравниваем заказы по запросу в день с прежними:
ORG_RETURN_DROP = 0.7               # меньше 70% прежних (или позиция хуже ORG_TOP_POS × 1,5) — вернуть в рекламу
HISTORY_DB = Path(__file__).resolve().parent / "wb_history.db"
MINUS_RETEST_DAYS = 45              # запрос, который автопилот сам отправил в минус, через столько дней возвращается на
                                    # перепроверку (спрос сезонный: то, что не продавало в сентябре, может продавать в
                                    # ноябре); снова не окупился — в минус, следующая перепроверка вдвое позже (до 180 дн.)
MINUS_LOW_CTR = 0.5                 # CTR запроса ниже половины среднего CTR товара в кампании, заказов нет, потрачено
                                    # ≥ половины допустимой цены заказа — в минус
RETURN_FACTOR = 1.5                 # "top": запрос возвращается в клики, если заказ за показы дороже лимита в 1,5 раза
RETURN_PAUSE_DAYS = 30              # …и после возврата столько дней не передаётся за показы снова
STATS_DAYS = 14                     # статистика товаров для конверсии
UNIT_MAX_AGE = 14                   # "auto": предупредить, если юнит-экономика старше стольких дней
COUNT_FBS_STOCK = True             # остаток = склады WB + свой склад (FBS). False — только склады WB (FBO)
# Размеры: если в наличии нет размеров, на которые приходится основной спрос, реклама приводит покупателей впустую.
SIZE_RULE = True                    # учитывать размеры (остатки и заказы по размерам, API «Аналитика»)
SIZE_DAYS = 30                      # за сколько дней смотреть заказы по размерам
SIZE_MIN_ORDERS = 5                 # меньше заказов за период — по размерам не судим
SIZE_COVER_NO_RAISE = 0.6           # в наличии размеры с долей спроса < 60% — ставку не повышать, автоминус не ставить
SIZE_COVER_MIN_BID = 0.3            # < 30% — как «нет в наличии»: минимальная ставка
SIZE_CACHE_HOURS = 6                # данные по размерам обновляются у WB раз в 2 часа; чаще 6 ч не запрашиваем
MINUS_AFTER_RESTOCK_DAYS = 14       # автоминус по товару — только после стольких дней подряд в наличии (WB + FBS)
# Выкуп (решение 12.10.2026): заказ цвета, который чаще не выкупают, стоит дороже — потолок цены заказа по каждому
# артикулу умножается на (его выкуп ÷ средний выкуп кабинета). Деньги перетекают к тому, что реально продаётся.
BUYOUT_RULE = True                  # False — один потолок для всех товаров, как раньше
BUYOUT_FROM_DAYS, BUYOUT_TO_DAYS = 120, 21   # «созревшие» заказы: 21–120 дней назад (почти все уже выкуплены или нет)
BUYOUT_PRIOR = 20                   # у товара мало истории — выкуп тянется к среднему (вес среднего, в заказах)
BUYOUT_MIN, BUYOUT_MAX = 0.6, 1.4   # множитель потолка не меньше ×0.6 и не больше ×1.4
DROP_TO_REC_UNLIMITED = True        # если в правиле задан max_drop_pct — к рекомендации WB всё равно снижать сразу
CLUSTER_DAYS = 30                   # статистика запросов
PRIOR_CR, K_CLICKS = 0.03, 100      # конверсия по умолчанию и её вес
PRIOR_CTR, K_VIEWS = 0.03, 1000     # CTR в поиске по умолчанию и его вес
# ===================================================================

ADV = "https://advert-api.wildberries.ru"
ANA = "https://seller-analytics-api.wildberries.ru"
HERE = Path(__file__).resolve().parent
BUDGET_OVERRIDES = HERE / "wb_budget_overrides.json"   # лимиты, поднятые wb_budget.py --apply (поверх RULES)
BUDGET_KEYS = ("daily_limit", "topup_day_cap", "topup_week_cap")


def apply_budget_overrides(rules):
    """Поднятые wb_budget.py лимиты — поверх RULES. Только эти ключи; правило кампании, которой в RULES нет явно
    (идёт по «*»), создаётся и наследует «*». Файла нет или он испорчен — RULES как есть."""
    try:
        d = json.loads(BUDGET_OVERRIDES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for cab, items in (d.get("items") or {}).items():
        if cab not in rules or not isinstance(items, dict):
            continue
        for key, ov in items.items():
            if key == "_week_budget":
                if isinstance(ov, (int, float)):
                    rules[cab]["_week_budget"] = ov
            elif isinstance(ov, dict):
                rules[cab].setdefault(key, {}).update({k: v for k, v in ov.items() if k in BUDGET_KEYS})
    return d


RULES_BASE = json.loads(json.dumps(RULES))     # RULES без поднятых wb_budget.py лимитов (для сравнения и отката)
BUDGET_OV = apply_budget_overrides(RULES)
TOKENS_FILE = HERE / "wb_tokens.txt"
BACKUP_DIR = HERE / "wb_bids_backups"
BACKUP_KEEP_DAYS = 45   # резервные копии старше — удаляются (откат берёт последнюю, уборка товаров смотрит 30 дн.);
                        # без этого автопилот и страж копили бы ~20 файлов в сутки без конца
TOKEN_WARN_DAYS = 14    # за сколько дней до окончания токена WB предупреждать (в журнале и на дашборде)
STATE_FILE = HERE / "wb_bids_state.json"
LOG_FILE = HERE / "wb_bids_log.txt"
HOLD_FILE = HERE / "wb_hold.txt"   # стоп-лист: номера кампаний (по одному в строке), которые скрипты не запускают и не
                                   # пополняют — чтобы выключить кампанию, которую сами ставят на паузу (ночь, лимит)
CPC_LEVEL = {"1-2": "range1To2", "3-10": "range3To10", "11-34": "range11To34"}
CPM_LEVEL = {"1-2": "top2", "3-10": "leadersBid", "11-34": "competitiveBid"}
REACH = {"1-2": "reachMax", "3-10": "reachMedium", "11-34": "reachMin"}

S = requests.Session()
out = []
PARALLEL = True     # кабинеты считаются одновременно (лимиты WB — у каждого продавца свои): запуск в ~3 раза быстрее
REC_COUNT = {}                   # {кабинет: запрошено рекомендаций WB} — для строки «идёт работа»
PROGRESS_EVERY = 60              # с, как часто печатать её при ручном запуске
_tl = threading.local()          # в потоке кабинета: свой журнал (печатается целиком по порядку) и своё соединение
_file_lock = threading.Lock()    # запись общих файлов (кэш размеров, образец ответа) из разных потоков


def log(s=""):
    buf = getattr(_tl, "buf", None)
    if buf is not None:
        buf.append(str(s))
        return
    print(s)
    out.append(str(s))


def progress(msg, end="\r"):
    """Строка «идёт работа…» — только в обычном режиме (при параллельном счёте строки разных кабинетов смешались бы)."""
    if getattr(_tl, "buf", None) is None:
        print(msg, end=end, flush=True)


def run_parallel(jobs):
    """jobs: [(имя, функция)]. Запускает одновременно, журнал каждого печатает целиком в исходном порядке.
    Возвращает {имя: ("ok", результат) | ("err", исключение)}."""
    if not PARALLEL or len(jobs) < 2:
        res = {}
        for name, fn in jobs:
            try:
                res[name] = ("ok", fn())
            except Exception as e:      # noqa: BLE001 — разбирает вызывающий
                res[name] = ("err", e)
        return res
    res, bufs = {}, {}

    def worker(name, fn):
        _tl.buf, _tl.session = [], requests.Session()
        try:
            res[name] = ("ok", fn())
        except Exception as e:          # noqa: BLE001
            res[name] = ("err", e)
        finally:
            bufs[name] = _tl.buf
            _tl.buf = None
    threads = [threading.Thread(target=worker, args=(n, f), daemon=True) for n, f in jobs]
    print(f"Считаю кабинеты одновременно: {', '.join(n for n, _ in jobs)} — журнал каждого появится, когда он закончит…",
          flush=True)
    for t in threads:
        t.start()
    # вручную в консоли — раз в минуту строка «идёт работа», чтобы не казалось, что всё зависло
    # (по расписанию вывод идёт в файл — там её нет)
    live = bool(getattr(sys.stdout, "isatty", lambda: False)())
    t0 = time.time()
    while live and any(t.is_alive() for t in threads):
        threads[[t.is_alive() for t in threads].index(True)].join(timeout=PROGRESS_EVERY)
        if any(t.is_alive() for t in threads):
            done = [n for (n, _), t in zip(jobs, threads) if not t.is_alive()]
            busy = [f"{n} (рекомендаций WB: {REC_COUNT.get(n, 0)})" for (n, _), t in zip(jobs, threads) if t.is_alive()]
            print(f"  … {(time.time() - t0) / 60:.0f} мин: считаю — {', '.join(busy)}"
                  + (f"; готово — {', '.join(done)}" if done else ""), flush=True)
    for (name, _), t in zip(jobs, threads):
        t.join()
        for line in bufs.get(name, []):
            print(line)
            out.append(line)
    return res


class ApiError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


_last = {}


def pace(token, key, interval):
    k = (token[-16:], key)
    wait = _last.get(k, 0) + interval - time.time()
    if wait > 0:
        time.sleep(wait)
    _last[k] = time.time()


UNCLEAR = "unclear"   # ApiError.status: запрос мог дойти до WB, а ответ потерялся (тайм-аут, обрыв, ошибка 5xx)


def call(method, url, token, params=None, body=None, what="", retry=True):
    """retry=False — для запросов, повтор которых опасен (пополнение бюджета, запуск): повторяется только то, что
    WB точно не выполнил (429, не удалось соединиться); тайм-аут или ошибка 5xx → ApiError(status=UNCLEAR)."""
    for attempt in range(6):
        try:
            r = getattr(_tl, "session", S).request(method, url, headers={"Authorization": token}, params=params,
                                                   json=body, timeout=120)
        except requests.RequestException as e:
            if not retry and not isinstance(e, requests.ConnectTimeout):
                raise ApiError(f"{what}: WB не ответил ({e.__class__.__name__}) — запрос мог пройти", UNCLEAR)
            if attempt < 3:
                time.sleep(5)
                continue
            raise ApiError(f"{what}: нет связи с WB ({e.__class__.__name__})")
        if r.status_code == 200:
            try:
                return r.json() if r.content else None
            except ValueError:
                return None
        if r.status_code == 204:
            return None
        if r.status_code == 429:
            try:
                wait = float(r.headers.get("X-Ratelimit-Retry") or r.headers.get("Retry-After") or 20)
            except ValueError:
                wait = 20
            if wait > 180:
                raise ApiError(f"{what}: WB просит подождать {wait / 60:.0f} мин — нужен Персональный токен.")
            time.sleep(wait + 1)
            continue
        if r.status_code >= 500 and not retry:
            raise ApiError(f"{what}: ошибка WB {r.status_code} — запрос мог пройти", UNCLEAR)
        if r.status_code >= 500 and attempt < 3:
            time.sleep(10)
            continue
        text = r.text[:300].replace("\n", " ")
        if r.status_code in (401, 403):
            hint = " Нужен токен с правом изменений (без «Только на чтение»), категории «Продвижение» и «Аналитика»."
            raise ApiError(f"{what}: доступ запрещён ({r.status_code}).{hint} Ответ WB: {text}", r.status_code)
        raise ApiError(f"{what}: ошибка {r.status_code}. Ответ WB: {text}", r.status_code)
    raise ApiError(f"{what}: WB не ответил после нескольких попыток")


def chunks(seq, n):
    seq = list(seq)
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def rub(kop):
    return "—" if kop is None else f"{kop / 100:,.2f}".replace(",", " ")


def cut(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[:n - 1] + "…"


def load_unit_limits():
    """{(кабинет, артикул): лимит цены заказа, ₽} из wb_unit_economics.csv.
    Если в RULES есть "auto", предупреждает: файла нет, он старый или себестоимость в нём — оценка."""
    import csv
    p = HERE / "wb_unit_economics.csv"
    res = {}
    auto_cabs = {cab for cab, rules in RULES.items() for k, rule in rules.items()
                 if not k.startswith("_") and dict(DEFAULTS, **rule)["max_cost_per_order"] == "auto"}
    if not p.exists():
        if auto_cabs:
            log(f"! Нет файла wb_unit_economics.csv, а в RULES стоит \"auto\" ({', '.join(sorted(auto_cabs))}) — "
                "для всех товаров беру max_cost_per_order_default. Запустите: python wb_unit.py")
        return res
    raw = p.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251")
    est, dates = set(), []
    for r in csv.DictReader(text.splitlines(), delimiter=";"):
        try:
            v = float(str(r.get("лимит цены заказа") or "").replace(",", ".").replace(" ", ""))
            res[(r.get("кабинет"), int(r.get("артикул WB")))] = v
        except (TypeError, ValueError):
            continue
        if r.get("кабинет") in auto_cabs:
            if (r.get("себестоимость") or "").strip() == "оценка":
                est.add((r.get("кабинет"), r.get("артикул WB")))
            if r.get("дата расчёта"):
                dates.append(r["дата расчёта"])
    if auto_cabs:
        try:
            made = dt.date.fromisoformat(min(dates)) if dates else dt.date.fromtimestamp(p.stat().st_mtime)
            age = (dt.date.today() - made).days
        except ValueError:
            age = (dt.date.today() - dt.date.fromtimestamp(p.stat().st_mtime)).days
        if not any(k[0] in auto_cabs for k in res):
            log("! В wb_unit_economics.csv нет лимитов для кабинетов с \"auto\" — беру max_cost_per_order_default. "
                "Запустите: python wb_unit.py")
        elif age > UNIT_MAX_AGE:
            log(f"! Юнит-экономике {age} дн. (лимиты цены заказа для \"auto\" могли устареть) — обновите: python wb_unit.py")
        if est:
            log(f"! У {len(est)} товаров в wb_unit_economics.csv себестоимость — оценка (доля от цены, "
                "в wb_unit.py по умолчанию 37%), "
                "лимит цены заказа для \"auto\" приблизительный. Впишите себестоимость в wb_cost.csv и запустите wb_unit.py.")
    return res


UNIT_LIMITS = {}
MANAGED = {}      # {кампания: кабинет} — какими кампаниями управлял этот прогон (для «пауза из-за денег»)
TOPUP_RULES = {}  # {кампания: правила пополнения} — для часовой проверки wb_guard.py
CAPS = {}         # {"кампания:артикул": {зона: {потолок, правила товара}}} — для часовой проверки wb_guard.py
CAPS_CAB = {}     # {"кампания:артикул": кабинет} — чтобы при сбое одного кабинета сохранить его прежние потолки
FAILED_CABS = set()   # кабинеты, планирование которых в этом запуске упало с ошибкой
LOCK_FILE = HERE / "wb_bids.running"   # общая блокировка: пока применяет автопилот или страж, второй не стартует
LOCK_MAX_AGE_MIN = 65                  # блокировка старше (задача в планировщике снимается через 1 ч) — процесс упал
LOCK_DEAD_MIN = 3                      # …а если процесса, взявшего её, уже нет — снять через столько минут (21.10 оборванная
                                       # проверка лимита оставила блокировку, и час кампании за показы шли без контроля)
PAUSED_MONEY_BELOW = 100               # кампания на паузе с бюджетом меньше этого, ₽ — «кончились деньги»: пополнить
                                       # и запустить можно; больше — паузу поставили вы, не трогать
_lock_id = None
BUYOUT = {}       # {(кабинет, артикул): (множитель потолка, выкуп товара, заказов в расчёте)}


def mcpo(cab, rule, nm):
    """Допустимая цена заказа для товара: число из правила × поправка на выкуп товара,
    или лимит из юнит-экономики ("auto" — там выкуп уже учтён)."""
    v = rule["max_cost_per_order"]
    if v == "auto":
        return UNIT_LIMITS.get((cab, nm), rule["max_cost_per_order_default"])
    return v * BUYOUT.get((cab, nm), (1.0,))[0]


TRIM_BACKUPS = HERE / "wb_trim_backups"


TRIM_HISTORY_DAYS = 30   # старые резервные копии (до появления state["trimmed"]) учитываются не дольше этого


def trimmed_items(state, cab):
    """Товары, убранные из кампаний из-за малого остатка: {(кампания, артикул): запись с прежними ставками}.
    Главный источник — state["trimmed"] (автопилот добавляет при уборке и удаляет при возврате). Резервные копии
    wb_trim.py и прошлых --apply учитываются в порядке дат и не старше TRIM_HISTORY_DAYS: более поздний возврат
    («untrim») отменяет более раннюю уборку — иначе товар, который вы потом убрали сами, вернулся бы обратно."""
    since = (dt.date.today() - dt.timedelta(days=TRIM_HISTORY_DAYS)).strftime("%Y%m%d")
    files = [(f, None) for f in TRIM_BACKUPS.glob("trim_*.json")] if TRIM_BACKUPS.exists() else []
    files += [(f, "bids") for f in BACKUP_DIR.glob("bids_*.json")] if BACKUP_DIR.exists() else []
    stamp = lambda f: f.stem.split("_", 1)[-1]          # «20261013_001000»
    res = {}
    for f, kind in sorted(files, key=lambda x: stamp(x[0])):
        if stamp(f)[:8] < since:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if kind is None:
                adds, removes = data, []
            else:
                adds, removes = data.get("trim") or [], data.get("untrim") or []
            for i in adds:
                if i.get("cabinet") == cab:
                    res[(i["advert_id"], i["nm_id"])] = i
            for i in removes:
                if i.get("cabinet") == cab:
                    res.pop((i["advert_id"], i["nm_id"]), None)
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            continue
    for i in (state.get("trimmed") or {}).values():
        if i.get("cabinet") == cab:
            res[(i["advert_id"], i["nm_id"])] = i
    return res


def load_buyout(cab, token, nm_ids, vendor):
    """Выкуп по артикулам за «созревший» период → множитель потолка цены заказа. Ошибка — без поправки."""
    if not BUYOUT_RULE or not nm_ids:
        return
    d1 = dt.date.today() - dt.timedelta(days=BUYOUT_FROM_DAYS)
    d2 = dt.date.today() - dt.timedelta(days=BUYOUT_TO_DAYS)
    got = {}
    try:
        for part in chunks(nm_ids, 100):
            pace(token, "funnel", 21)
            body = {"selectedPeriod": {"start": d1.isoformat(), "end": d2.isoformat()}, "nmIds": part,
                    "brandNames": [], "subjectIds": [], "tagIds": [], "skipDeletedNm": False, "limit": 1000, "offset": 0}
            data = call("POST", ANA + "/api/analytics/v3/sales-funnel/products", token, body=body,
                        what="Выкуп по товарам (воронка продаж)") or {}
            for p in (data.get("data") or {}).get("products") or []:
                sel = (p.get("statistic") or {}).get("selected") or {}
                b, c = num(sel.get("buyoutCount")), num(sel.get("cancelCount"))
                got[(p.get("product") or {}).get("nmId")] = (b, c)
    except ApiError as e:
        log(f"  ! {e} — потолок цены заказа без поправки на выкуп.")
        return
    tb, tn = sum(b for b, c in got.values()), sum(b + c for b, c in got.values())
    if tn < 50:
        log(f"  Выкуп: мало истории ({tn:.0f} заказов за {BUYOUT_TO_DAYS}–{BUYOUT_FROM_DAYS} дн. назад) — потолок без поправки.")
        return
    avg = tb / tn
    if avg <= 0:
        log("  Выкуп: за период ни одного выкупа — потолок без поправки (проверьте отчёт выкупа).")
        return
    rows = []
    for nm, (b, c) in got.items():
        share = (b + BUYOUT_PRIOR * avg) / (b + c + BUYOUT_PRIOR)
        k = min(max(share / avg, BUYOUT_MIN), BUYOUT_MAX)
        BUYOUT[(cab, nm)] = (k, share, b + c)
        if abs(k - 1) >= 0.1:
            rows.append((k, nm, share, b + c))
    log(f"  Выкуп: средний {avg:.0%} (заказы {BUYOUT_TO_DAYS}–{BUYOUT_FROM_DAYS} дн. назад). Потолок цены заказа по товару "
        f"× (его выкуп ÷ средний), от ×{BUYOUT_MIN} до ×{BUYOUT_MAX}; при малой истории — ближе к среднему.")
    if rows:
        log("    Заметная поправка (±10% и больше):")
        for k, nm, share, n in sorted(rows):
            log(f"      {nm:>11} {str(vendor.get(nm) or '')[:22]:22} выкуп {share:.0%} (по {n:.0f} заказам) → потолок ×{k:.2f}")


def negative(q):
    q = (q or "").lower()
    return any(w in q for w in MINUS_WORDS)


def protected(q):
    q = (q or "").lower()
    return any(w in q for w in PROTECT)


def load_tokens():
    if not TOKENS_FILE.exists():
        sys.exit(f"Нет файла {TOKENS_FILE}. Сначала запустите wb_ads_report.py — он создаст шаблон.")
    raw = TOKENS_FILE.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251")
    cabs = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, tok = line.split("=", 1)
        tok = tok.strip().strip('"').strip("'")
        if len(tok) >= 20:
            cabs[name.strip()] = tok
    return cabs


STATE_PREV = HERE / "wb_bids_state.prev.json"   # предыдущая версия файла состояния — на случай порчи основного


def load_state():
    """Состояние автопилота. Нет файла — начинаем с пустого. Файл испорчен — берём предыдущую версию; если и её нет —
    останавливаемся: с пустым состоянием автопилот «забыл» бы пополнения и снова потратил недельные потолки."""
    if not STATE_FILE.exists():
        return {"minus_auto": {}, "minus_route": {}, "cluster_returned": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        bad = STATE_FILE.with_name(f"wb_bids_state.corrupt_{dt.datetime.now():%Y%m%d_%H%M%S}.json")
        try:
            shutil.copyfile(STATE_FILE, bad)
        except OSError:
            pass
        try:
            state = json.loads(STATE_PREV.read_text(encoding="utf-8"))
            log(f"! Файл состояния испорчен (копия: {bad.name}) — взял предыдущую версию {STATE_PREV.name}.")
            return state
        except (OSError, ValueError):
            sys.exit(f"✗ Файл состояния {STATE_FILE.name} испорчен, а предыдущей версии нет (копия: {bad.name}). "
                     "Ничего не меняю: без учёта пополнений автопилот мог бы потратить недельные потолки заново.")


def prune_backups(folder, prefix):
    """Удалить резервные копии старше BACKUP_KEEP_DAYS (имя: prefix_ГГГГММДД_ЧЧММСС.json или .rolled_back)."""
    edge = (dt.date.today() - dt.timedelta(days=BACKUP_KEEP_DAYS)).strftime("%Y%m%d")
    try:
        for f in folder.glob(prefix + "_*"):
            stamp = f.name[len(prefix) + 1:len(prefix) + 9]
            if stamp.isdigit() and stamp < edge:
                f.unlink()
    except OSError:
        pass


def token_expires(token):
    """Когда кончается токен WB (поле exp внутри самого токена) или None. Сам токен никуда не выводится."""
    try:
        import base64
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        exp = json.loads(base64.urlsafe_b64decode(part)).get("exp")
        return dt.datetime.fromtimestamp(int(exp)) if exp else None
    except Exception:          # noqa: BLE001 — не токен в формате JWT: просто не предупреждаем
        return None


def token_warning(cab, token):
    e = token_expires(token)
    if e and (e - dt.datetime.now()).days < TOKEN_WARN_DAYS:
        return (f"! Токен кабинета «{cab}» действует до {e:%d.%m.%Y} — перевыпустите заранее (кабинет WB → Настройки → "
                "Доступ к API, тот же набор категорий) и замените в wb_tokens.txt, иначе автопилот остановится.")
    return None


def lock_info():
    """(время, кто) текущей блокировки или None. Старый формат — просто время."""
    try:
        raw = LOCK_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        d = json.loads(raw)
        return dt.datetime.fromisoformat(d["at"]), d.get("owner") or "?", d.get("id"), d.get("pid")
    except (ValueError, KeyError, TypeError):
        try:
            return dt.datetime.fromisoformat(raw), "wb_bids.py", None, None
        except ValueError:
            # пусто или мусор: файл, возможно, как раз пишется — судим по времени его изменения
            try:
                return dt.datetime.fromtimestamp(LOCK_FILE.stat().st_mtime), "?", None, None
            except OSError:
                return None


def pid_alive(pid):
    """Жив ли процесс (на Windows — через OpenProcess; os.kill(pid, 0) там ЗАВЕРШИЛ бы процесс)."""
    if not pid:
        return False
    try:
        if os.name == "nt":
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k32.OpenProcess(0x1000, False, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return ctypes.get_last_error() == 5           # «отказано в доступе» — процесс есть (чужой, SYSTEM)
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            k32.CloseHandle(h)
            return bool(ok) and code.value == 259             # STILL_ACTIVE
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError, AttributeError):
        return False


def lock_mine():
    """Блокировка всё ещё наша (её не сняли как устаревшую, пока мы считали)."""
    info = lock_info()
    return bool(_lock_id) and bool(info) and info[2] == _lock_id


def lock_acquire(owner, wait_min=0):
    """Общая блокировка автопилота и стража (создаётся атомарно). Чужая свежая — ждём до wait_min минут.
    Возвращает True, если блокировка наша."""
    global _lock_id
    start = time.time()
    deadline = start + wait_min * 60
    told = False
    while True:
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            _lock_id = f"{os.getpid()}-{time.time():.0f}"
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"), "owner": owner,
                                    "pid": os.getpid(), "id": _lock_id}))
            return True
        except FileExistsError:
            info = lock_info()
            if info is None:
                continue                                   # файл только что удалили — пробуем снова
            at, who, _, pid = info
            old = dt.datetime.now() - at
            # процесса, взявшего блокировку, уже нет (оборвался, его сняли по времени) — снимаем через LOCK_DEAD_MIN мин;
            # старая блокировка живого процесса (ноутбук спал посреди проверки) — не отбираем; совсем старую (номер
            # процесса мог достаться другой программе) — снимаем всё равно
            dead = old > dt.timedelta(minutes=LOCK_DEAD_MIN) and not pid_alive(pid)
            if dead or old > dt.timedelta(minutes=3 * LOCK_MAX_AGE_MIN):
                log(f"! Блокировка от {who} ({at:%d.%m %H:%M}) — " + ("процесса, который её взял, уже нет" if dead else
                    f"старше {3 * LOCK_MAX_AGE_MIN} мин") + "; снимаю.")
                try:
                    LOCK_FILE.unlink()
                except OSError:
                    pass
                continue
            if time.time() >= deadline:
                return False
            if not told:
                log(f"  Сейчас работает {who} (с {at:%H:%M}) — жду, пока закончит (до {wait_min} мин)…")
                told = True
            time.sleep(20)
        except OSError:                                    # Windows: файл удаляется или занят антивирусом — подождать
            if time.time() >= max(deadline, start + 10):
                return False
            time.sleep(1)


def lock_release():
    """Снимает блокировку, только если она наша."""
    info = lock_info()
    if _lock_id and info and info[2] == _lock_id:
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass


def pk(cid, nm):
    return f"{cid}:{nm}"


def save_state(state):
    """Атомарно: пишем во временный файл и подменяем — оборванная запись не испортит состояние.
    Предыдущая версия остаётся в STATE_PREV."""
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    if STATE_FILE.exists():
        try:
            shutil.copyfile(STATE_FILE, STATE_PREV)
        except OSError:
            pass
    for attempt in range(6):
        try:
            os.replace(tmp, STATE_FILE)
            return
        except PermissionError:      # Windows: файл на миг открыт антивирусом, индексатором или дашбордом — подождать
            if attempt == 5:
                raise
            time.sleep(0.5)


def day_base(state, kind, cid, nm, sub):
    """Ставка на начало суток (kind "bids": sub = зона, "clusters": sub = запрос) или None, если сегодня
    --apply по ней ещё не было — тогда база = текущая ставка."""
    db = state.get("day_base") or {}
    if db.get("date") != dt.date.today().isoformat():
        return None
    return ((db.get(kind) or {}).get(pk(cid, nm)) or {}).get(sub)


def record_day_base(state, plan):
    """Только при --apply: запоминает ставку до первого за сутки изменения (повторные запуски её не трогают)."""
    today = dt.date.today().isoformat()
    db = state.get("day_base")
    if not db or db.get("date") != today:
        db = state["day_base"] = {"date": today, "bids": {}, "clusters": {}}
    for i in plan["bids"]:
        v = i["old"] or i["new"]
        if v:
            db["bids"].setdefault(pk(i["advert_id"], i["nm_id"]), {}).setdefault(i["placement"], v)
    for i in plan["cluster_bids"]:
        v = i["old"] if i["old"] is not None else i["new"]
        if v and not i.get("returned"):
            db["clusters"].setdefault(pk(i["advert_id"], i["nm_id"]), {}).setdefault(i["query"], v)


# ============================ чтение из WB ============================

def get_campaigns(token):
    pace(token, "count", 0.3)
    data = call("GET", ADV + "/adv/v1/promotion/count", token, what="Список кампаний") or {}
    ids, status = [], {}
    for grp in data.get("adverts") or []:
        if grp.get("status") in (4, 9, 11):
            for a in grp.get("advert_list") or []:
                ids.append(a["advertId"])
                status[a["advertId"]] = grp.get("status")
    camps = {}
    for part in chunks(ids, 50):
        pace(token, "adverts", 0.3)
        d = call("GET", ADV + "/api/advert/v2/adverts", token, params={"ids": ",".join(map(str, part))},
                 what="Настройки кампаний") or {}
        for a in d.get("adverts") or []:
            a.setdefault("_status", status.get(a["id"]))
            camps[a["id"]] = a
    return camps


def find(camps, key):
    key = str(key).strip()
    if key.isdigit() and int(key) in camps:      # номер кампании — только она, без совпадений в названиях
        return [int(key)]
    return [cid for cid, a in camps.items()
            if str(cid) == key or key.lower() in str((a.get("settings") or {}).get("name", "")).lower()]


# Сбои загрузки данных, без которых ставки считать нельзя: [(кабинет, что случилось)]. --apply не меняет кабинет
# с таким сбоем (остальные применяются); сбой вне кабинета (None) — не меняет ничего.
DATA_PROBLEMS = []


def data_problem(msg):
    DATA_PROBLEMS.append((getattr(_tl, "cab", None), msg))


def nm_stats(token, ids):
    d2 = dt.date.today() - dt.timedelta(days=1)
    d1 = d2 - dt.timedelta(days=STATS_DAYS - 1)
    cn = defaultdict(lambda: defaultdict(float))
    for part in chunks(ids, 50):
        pace(token, "fullstats", 21)
        try:
            data = call("GET", ADV + "/adv/v3/fullstats", token,
                        params={"ids": ",".join(map(str, part)), "beginDate": d1.isoformat(), "endDate": d2.isoformat()},
                        what="Статистика кампаний")
        except ApiError as e:
            log(f"  ! {e} — считаю по конверсии по умолчанию.")
            data_problem(f"статистика кампаний не загрузилась ({e})")
            continue
        for item in data or []:
            aid = item.get("advertId")
            for day in item.get("days") or []:
                for app in day.get("apps") or []:
                    for n in app.get("nms") or []:
                        for k in ("views", "clicks", "orders", "sum", "sum_price"):
                            cn[(aid, n.get("nmId"))][k] += num(n.get(k))
    return cn


def cluster_stats(token, pairs):
    """{(cid, nm): {кластер: {views, clicks, orders, atbs, spend}}} за CLUSTER_DAYS (или 7, если WB не даст)."""
    res = defaultdict(dict)
    if not pairs:
        return res
    d2 = dt.date.today() - dt.timedelta(days=1)
    for days in (CLUSTER_DAYS, 7):
        d1 = d2 - dt.timedelta(days=days - 1)
        try:
            for part in chunks(pairs, 100):
                pace(token, "nqstats", 7)
                body = {"from": d1.isoformat(), "to": d2.isoformat(),
                        "items": [{"advert_id": a, "nm_id": n} for a, n in part]}
                data = call("POST", ADV + "/adv/v0/normquery/stats", token, body=body, what="Статистика запросов") or {}
                for it in data.get("stats") or []:
                    for s in it.get("stats") or []:
                        q = s.get("norm_query")
                        if q:
                            res[(it.get("advert_id"), it.get("nm_id"))][q] = {
                                k: num(s.get(k)) for k in ("views", "clicks", "orders", "atbs", "spend")}
            if days != CLUSTER_DAYS:
                log(f"  ! Статистика запросов — только за последние 7 дней.")
            return res
        except ApiError as e:
            if days == 7:
                log(f"  ! {e}")
                data_problem(f"статистика запросов не загрузилась ({e})")
            res.clear()
    return res


def get_stock(token, nm_ids):
    y = dt.date.today() - dt.timedelta(days=1)
    res = {}
    try:
        for part in chunks(nm_ids, 100):
            pace(token, "funnel", 21)
            # остатки — на текущий день; заказы — за SIZE_DAYS (нужны, чтобы спрашивать размеры только у продающих)
            body = {"selectedPeriod": {"start": (y - dt.timedelta(days=SIZE_DAYS - 1)).isoformat(), "end": y.isoformat()},
                    "nmIds": part, "brandNames": [], "subjectIds": [],
                    "tagIds": [], "skipDeletedNm": False, "limit": 1000, "offset": 0}
            data = call("POST", ANA + "/api/analytics/v3/sales-funnel/products", token, body=body,
                        what="Остатки (воронка продаж)") or {}
            for p in (data.get("data") or {}).get("products") or []:
                pr = p.get("product") or {}
                st = pr.get("stocks") or {}
                sel = (p.get("statistic") or {}).get("selected") or {}
                res[pr.get("nmId")] = {"wb": st.get("wb"), "mp": st.get("mp"), "vendor": pr.get("vendorCode"),
                                       "orders": num(sel.get("orderCount"))}
    except ApiError as e:
        log(f"  ! {e} — остатки неизвестны, минимальные ставки «по остаткам» не ставлю.")
        data_problem(f"остатки не загрузились ({e})")
        return None
    return res


def available(s):
    """Сколько штук можно продать: склады WB + свой склад (FBS), если COUNT_FBS_STOCK. None — неизвестно."""
    if not s:
        return None
    wb, mp = s.get("wb"), s.get("mp") if COUNT_FBS_STOCK else None
    if wb is None and mp is None:
        return None
    return (wb or 0) + (mp or 0)


def stock_label(s):
    if not s or (s.get("wb") is None and s.get("mp") is None):
        return ""
    wb = "" if s.get("wb") is None else str(int(s["wb"]))
    return f"{wb}/{int(s['mp'])}" if COUNT_FBS_STOCK and s.get("mp") is not None else wb


SIZE_CACHE = HERE / "wb_sizes_cache.json"


def fetch_sizes(token, nm):
    """Размеры одного товара: [{"name", "orders", "stock"}] — заказы за SIZE_DAYS и остаток (склады WB + свой склад,
    если COUNT_FBS_STOCK). Без пауз — их ставит вызывающий (WB: раз в 20 с). Ошибка — ApiError."""
    y = dt.date.today() - dt.timedelta(days=1)
    body = {"nmID": nm, "currentPeriod": {"start": (y - dt.timedelta(days=SIZE_DAYS - 1)).isoformat(),
                                          "end": y.isoformat()},
            "stockType": "" if COUNT_FBS_STOCK else "wb", "orderBy": {"field": "ordersCount", "mode": "desc"},
            "includeOffice": False}
    data = call("POST", ANA + "/api/v2/stocks-report/products/sizes", token, body=body, what=f"Размеры {nm}") or {}
    sizes = []
    for z in ((data.get("data") or {}).get("sizes") or []):
        m = z.get("metrics") or {}
        sizes.append({"name": str(z.get("name") or z.get("chrtID")), "orders": num(m.get("ordersCount")),
                      "stock": num(m.get("stockCount"))})
    return sizes


def get_sizes(token, nm_ids):
    """{nm: [{"name", "orders", "stock"}]} — заказы за SIZE_DAYS и текущий остаток по каждому размеру
    (склады WB + свой склад, если COUNT_FBS_STOCK). Кэш на SIZE_CACHE_HOURS. Сбой — пустой результат и пометка."""
    try:
        cache = json.loads(SIZE_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    now = dt.datetime.now()
    fresh = lambda e: e and now - dt.datetime.fromisoformat(e["at"]) < dt.timedelta(hours=SIZE_CACHE_HOURS)
    res, failed = {}, 0
    if COLLECTOR_MODE == "use":           # размеры от сборщика (он обновляет их раз в 2 ч) — если свежее своих
        for nm in nm_ids:
            c = collector_snapshot()["sizes"].get(nm)
            e = cache.get(str(nm))
            if c and fresh({"at": c[0]}) and (not e or c[0] > e.get("at", "")):
                cache[str(nm)] = {"at": c[0], "sizes": c[1]}
    todo_n = sum(1 for nm in nm_ids if not fresh(cache.get(str(nm))))
    if todo_n:
        progress(f"  Размеры: запрашиваю {todo_n} тов. (WB — раз в 20 с, ≈{todo_n * 20 // 60 + 1} мин; дальше 6 ч из кэша)…", end="\n")
    k = 0
    for nm in nm_ids:
        e = cache.get(str(nm))
        if not fresh(e):
            k += 1
            progress(f"    размеры {k}/{todo_n}")
            pace(token, "sizes", 20.5)
            try:
                sizes = fetch_sizes(token, nm)
            except ApiError as ex:
                failed += 1
                if failed == 1:
                    log(f"  ! {ex} — размеры не учтены.")
                continue
            if COLLECTOR_MODE == "check":
                check_against_collector("sizes", nm, sizes)
            e = cache[str(nm)] = {"at": now.isoformat(timespec="seconds"), "sizes": sizes}
        res[nm] = e["sizes"]
    if todo_n:
        progress(" " * 60)
    if failed:
        log(f"  ! Размеры не получены для {failed} тов. — для них правило размеров не применяется.")
    with _file_lock:     # другой кабинет мог записать кэш, пока этот считал, — дописываем, а не затираем
        try:
            disk = json.loads(SIZE_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            disk = {}
        disk.update({k: v for k, v in cache.items() if fresh(v)})
        try:
            tmp = SIZE_CACHE.with_suffix(".tmp")
            tmp.write_text(json.dumps(disk, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, SIZE_CACHE)
        except OSError:
            pass
    return res


def size_cover(sizes):
    """(доля спроса, приходящаяся на размеры в наличии, [нет в наличии: ходовые размеры]) или (None, []) — мало данных."""
    total = sum(z["orders"] for z in sizes or [])
    if total < SIZE_MIN_ORDERS:
        return None, []
    have = sum(z["orders"] for z in sizes if z["stock"] > 0)
    missing = [z["name"] for z in sorted(sizes, key=lambda z: -z["orders"]) if z["stock"] <= 0 and z["orders"] > 0]
    return have / total, missing


def get_min_bids(token, aid, nm_ids, pay, placement):
    ptype = {"search": "search", "recommendations": "recommendation", "combined": "combined"}[placement]
    res = {}
    for part in chunks(nm_ids, 100):
        pace(token, "bidsmin", 3.1)
        try:
            data = call("POST", ADV + "/api/advert/v1/bids/min", token,
                        body={"advert_id": aid, "nm_ids": part, "payment_type": pay, "placement_types": [ptype]},
                        what="Минимальные ставки") or {}
        except ApiError as e:
            log(f"  ! {e}")
            continue
        for b in data.get("bids") or []:
            vals = [x.get("value") for x in b.get("bids") or [] if x.get("type") == ptype and x.get("value")]
            if vals:
                res[b.get("nm_id")] = int(vals[0])
    return res


_rec_cache = {}


REC_SAMPLE = HERE / "wb_bids_rec_sample.json"   # образец ответа WB с рекомендациями — для сверки формата


REC_HISTORY = HERE / "wb_rec_history.csv"   # рекомендованные ставки WB при каждом запуске — динамика цены рекламы
REC_LOG = []


def note_rec(cab, cid, nm, pay, target, rec, cur, mn):
    """Запомнить рекомендацию WB (копейки) — пишется в wb_rec_history.csv в конце запуска (write_rec_log)."""
    if rec:
        REC_LOG.append({"время": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "кабинет": cab, "кампания": cid,
                        "товар": nm, "оплата": pay, "места": target, "рекомендация ₽": round(rec / 100, 2),
                        "ставка ₽": round(cur / 100, 2) if cur else "", "минимум WB ₽": round(mn / 100, 2) if mn else ""})


def write_rec_log():
    write_check_log()
    if not REC_LOG:
        return
    import csv
    with _file_lock:
        try:
            if REC_HISTORY.exists() and REC_HISTORY.stat().st_size > 15 * 1024 * 1024:   # ≈ полгода — начать новый
                old = REC_HISTORY.with_name(REC_HISTORY.stem + ".old.csv")
                if old.exists():
                    old.unlink()
                REC_HISTORY.rename(old)
            new = not REC_HISTORY.exists()
            with open(REC_HISTORY, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(REC_LOG[0]), delimiter=";")
                if new:
                    w.writeheader()
                for r in REC_LOG:
                    w.writerow({k: (str(v).replace(".", ",") if isinstance(v, float) else v) for k, v in r.items()})
            REC_LOG.clear()
        except OSError as e:
            log(f"! История рекомендаций не записана: {e}")


# ---------- сборщик wb_collector.py: рекомендации и размеры в базе wb_rec.db (решение пользователя 15.10.2026) ----------
# Сборщик каждые 15 минут (7:00–24:00) спрашивает WB о рекомендованных ставках всех товаров кампаний и о размерах и
# складывает ответы в базу. Режим чтения базы автопилотом и часовой проверкой:
#   "check" — первые сутки: спрашивают WB сами, как раньше, и сверяют ответ с базой (wb_rec_check.csv) — проверка,
#             что сборщик кладёт то же, что отдаёт WB;
#   "use"   — берут из базы рекомендации моложе REC_CACHE_MIN мин и размеры моложе SIZE_CACHE_HOURS ч; WB — только
#             недостающее. Сборщик остановился — данные стареют, и скрипты сами спрашивают WB, как раньше;
#   "off"   — базу не читают.
COLLECTOR_MODE = "use"           # 15.10.2026: сверка за утро — 356 из 356 совпали; было "check"
COLLECTOR_DB = HERE / "wb_rec.db"
CHECK_FILE = HERE / "wb_rec_check.csv"
CHECK_MAX_AGE_MIN = 60          # сверять с записью сборщика не старше (рекомендации WB меняются в течение дня)
BUSY_FILE = HERE / "wb_api.busy"   # автопилот или страж сейчас спрашивают WB — сборщик ждёт (лимит рекомендаций общий)
COLLECTOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS rec (advert_id INTEGER, nm INTEGER, cab TEXT, pay TEXT, at TEXT, data TEXT,
                                PRIMARY KEY (advert_id, nm));
CREATE TABLE IF NOT EXISTS rec_hist (at TEXT, cab TEXT, advert_id INTEGER, nm INTEGER, pay TEXT,
                                     r12 INTEGER, r310 INTEGER, r1134 INTEGER);
CREATE INDEX IF NOT EXISTS rec_hist_at ON rec_hist (at);
CREATE INDEX IF NOT EXISTS rec_hist_pair ON rec_hist (advert_id, nm, at);
CREATE TABLE IF NOT EXISTS sizes (nm INTEGER PRIMARY KEY, cab TEXT, at TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS runs (cab TEXT PRIMARY KEY, started TEXT, finished TEXT, info TEXT);
"""
_coll = None
_coll_lock = threading.Lock()
CHECK_ROWS = []


def collector_connect():
    con = sqlite3.connect(str(COLLECTOR_DB), timeout=30, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")      # читать можно, пока сборщик пишет
    con.executescript(COLLECTOR_SCHEMA)
    return con


def age_min(at):
    try:
        return (dt.datetime.now() - dt.datetime.fromisoformat(at)).total_seconds() / 60
    except (TypeError, ValueError):
        return None


def collector_snapshot():
    """Последнее, что сборщик положил в базу: {"rec": {(кампания, товар): (время, ответ WB)}, "sizes": {товар:
    (время, размеры)}} — читается один раз за запуск. Базы нет или она не читается — пусто (спросим WB сами)."""
    global _coll
    with _coll_lock:
        if _coll is None:
            _coll = {"rec": {}, "sizes": {}}
            if COLLECTOR_MODE != "off" and COLLECTOR_DB.exists():
                try:
                    con = sqlite3.connect(str(COLLECTOR_DB), timeout=10)
                    try:
                        for aid, nm, at, data in con.execute("SELECT advert_id, nm, at, data FROM rec"):
                            _coll["rec"][(aid, nm)] = (at, json.loads(data))
                        for nm, at, data in con.execute("SELECT nm, at, data FROM sizes"):
                            _coll["sizes"][nm] = (at, json.loads(data))
                    finally:
                        con.close()
                except (sqlite3.Error, ValueError) as e:
                    log(f"  ! База сборщика {COLLECTOR_DB.name} не прочиталась ({e}) — спрашиваю WB сам.")
                    _coll = {"rec": {}, "sizes": {}}
        return _coll


def rec_levels(data, pay):
    """(места 1–2, 3–10, 11–34) рекомендованной ставки в копейках — для истории и сверки."""
    f = (lambda k: rec_cpc(data, CPC_LEVEL[k])) if pay == "cpc" else (lambda k: rec_cpm(data, CPM_LEVEL[k]))
    return tuple(f(k) for k in ("1-2", "3-10", "11-34"))


def check_against_collector(kind, key, mine):
    """Режим "check": свой ответ WB против записи сборщика (если она не старше CHECK_MAX_AGE_MIN)."""
    snap = collector_snapshot()["rec" if kind == "rec" else "sizes"].get(key)
    if not snap:
        return
    a = age_min(snap[0])
    if a is None or a > CHECK_MAX_AGE_MIN:
        return
    if kind == "rec":
        m = (rec_levels(mine, "cpc")[1], rec_levels(mine, "cpm")[1])
        c = (rec_levels(snap[1], "cpc")[1], rec_levels(snap[1], "cpm")[1])
    else:
        norm = lambda zs: sorted((str(z.get("name")), z.get("orders"), z.get("stock")) for z in zs or [])
        m, c = norm(mine), norm(snap[1])
    CHECK_ROWS.append({"время": dt.datetime.now().isoformat(timespec="seconds"), "кабинет": getattr(_tl, "cab", "") or "",
                       "что": "рекомендация" if kind == "rec" else "размеры",
                       "кампания": key[0] if kind == "rec" else "", "товар": key[1] if kind == "rec" else key,
                       "возраст записи сборщика, мин": round(a, 1),
                       "WB сейчас": json.dumps(m, ensure_ascii=False), "в базе сборщика": json.dumps(c, ensure_ascii=False),
                       "совпало": "да" if m == c else "нет"})


def write_check_log():
    if not CHECK_ROWS:
        return
    import csv
    with _file_lock:
        try:
            new = not CHECK_FILE.exists()
            with open(CHECK_FILE, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(CHECK_ROWS[0]), delimiter=";")
                if new:
                    w.writeheader()
                w.writerows(CHECK_ROWS)
            CHECK_ROWS.clear()
        except OSError as e:
            log(f"! Сверка со сборщиком не записана: {e}")


def busy_mark(owner):
    """Отметка «спрашиваю WB» для сборщика: пока она стоит (и процесс жив), сборщик не тратит общий лимит."""
    try:
        BUSY_FILE.write_text(json.dumps({"pid": os.getpid(), "owner": owner,
                                         "at": dt.datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
    except OSError:
        return
    import atexit
    atexit.register(busy_clear)


def busy_clear():
    try:
        if json.loads(BUSY_FILE.read_text(encoding="utf-8")).get("pid") == os.getpid():
            BUSY_FILE.unlink()
    except (OSError, ValueError, AttributeError):
        pass


# Рекомендованные ставки WB: 5 запросов в минуту на продавца (интервал 12 с, всплеск 5 — swagger WB, проверено 15.10.2026),
# по одному товару за запрос, пакетного метода нет. Ускорение: первые REC_BURST запросов — сразу, дальше раз в
# REC_INTERVAL с; ответы запоминаются на диске на REC_CACHE_MIN минут — автопилот и часовая проверка берут друг у друга
# свежие (страж в 7:40 спросил главные товары — автопилот в 8:00 их не переспрашивает). Упёрлись в лимит (другой
# процесс или сервис тратит те же 5 в минуту) — call() ждёт столько, сколько скажет WB, и повторяет.
REC_INTERVAL = 12.5
REC_BURST = 5
REC_CACHE_MIN = 45
REC_CACHE_FILE = HERE / "wb_rec_cache.json"
_bucket = {}
_bucket_locks = {}        # своя блокировка на каждое ведро: фоновая подгрузка и основной расчёт кабинета делят один лимит
_rec_lock = threading.Lock()
_rec_inflight = {}        # (кампания, товар) → threading.Event: ответ уже запрашивается в другом потоке — подождать его
_rec_main = set()         # кабинеты, где основной расчёт сам дошёл до рекомендаций (фоновая подгрузка там больше не нужна)
_rec_disk = None          # {"кампания:товар": {"at": ISO, "data": ответ WB}} — загружается при первом обращении
_rec_fresh = {}           # то, что получено в этом запуске, — дописывается в файл в конце (save_rec_cache)


def pace_burst(token, key, interval, burst):
    """Ведро на burst запросов, пополняется по одному раз в interval секунд (как считает WB)."""
    k = (token[-16:], key)
    with _bucket_locks.setdefault(k, threading.Lock()):
        now = time.time()
        tokens, t0 = _bucket.get(k, (float(burst), now))
        tokens = min(float(burst), tokens + (now - t0) / interval)
        if tokens < 1:
            time.sleep((1 - tokens) * interval)
            now = time.time()
            tokens = 1.0
        _bucket[k] = (tokens - 1, now)


def _rec_disk_load():
    global _rec_disk
    if _rec_disk is None:
        with _file_lock:
            if _rec_disk is None:
                try:
                    _rec_disk = json.loads(REC_CACHE_FILE.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    _rec_disk = {}
    return _rec_disk


def save_rec_cache():
    """Дописать полученные в этом запуске рекомендации в общий файл (старше 6 ч — выбросить)."""
    if not _rec_fresh:
        return
    with _file_lock:
        try:
            try:
                disk = json.loads(REC_CACHE_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                disk = {}
            disk.update(_rec_fresh)
            edge = (dt.datetime.now() - dt.timedelta(hours=6)).isoformat(timespec="seconds")
            disk = {k: v for k, v in disk.items() if (v.get("at") or "") >= edge}
            tmp = REC_CACHE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(disk, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, REC_CACHE_FILE)
            _rec_fresh.clear()
        except OSError:
            pass


def fetch_rec_raw(token, aid, nm):
    """Один запрос рекомендаций WB (без пауз — их ставит вызывающий: pace_burst)."""
    data = call("GET", ADV + "/api/advert/v0/bids/recommendations", token,
                params={"nmId": nm, "advertId": aid}, what=f"Рекомендованные ставки {nm}") or {}
    return data if isinstance(data, dict) else {}


def _fetch_rec(token, aid, nm):
    hit = _rec_disk_load().get(f"{aid}:{nm}") or {}
    best = (hit.get("at"), hit.get("data")) if isinstance(hit.get("data"), dict) and hit["data"] else (None, None)
    if COLLECTOR_MODE == "use":                      # запись сборщика — если она свежее своей
        c = collector_snapshot()["rec"].get((aid, nm))
        if c and c[1] and (best[0] is None or c[0] > best[0]):
            best = c
    age = age_min(best[0]) if best[0] else None
    if age is not None and 0 <= age < REC_CACHE_MIN:
        return best[1]                               # свежий ответ из прошлого запуска (сборщика, стража или автопилота)
    progress(f"    рекомендации WB: {len(_rec_cache) + 1}-й товар…")
    cab_ = getattr(_tl, "cab", None)
    REC_COUNT[cab_] = REC_COUNT.get(cab_, 0) + 1
    pace_burst(token, "rec", REC_INTERVAL, REC_BURST)
    data = fetch_rec_raw(token, aid, nm)
    progress(" " * 60)
    if COLLECTOR_MODE == "check" and data:
        check_against_collector("rec", (aid, nm), data)
    if not _rec_fresh:   # первый ответ за запуск сохраняем как образец (токенов в нём нет)
        with _file_lock:
            try:
                REC_SAMPLE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            except OSError:
                pass
    data = data if isinstance(data, dict) else {}
    if data:                    # пустой ответ (ошибка, лимит) не запоминаем — следующий запуск спросит снова
        _rec_fresh[f"{aid}:{nm}"] = {"at": dt.datetime.now().isoformat(timespec="seconds"), "data": data}
    return data


def get_rec(token, aid, nm):
    """Рекомендация WB по товару кампании. Безопасно из нескольких потоков: если этот товар уже запрашивает фоновая
    подгрузка (start_rec_prefetch), ждёт её ответа, а не спрашивает второй раз."""
    key = (aid, nm)
    if not getattr(_tl, "prefetch", False):
        _rec_main.add(getattr(_tl, "cab", None))       # основной расчёт дошёл до рекомендаций — фон больше не нужен
    while True:
        with _rec_lock:
            if key in _rec_cache:
                return _rec_cache[key]
            ev = _rec_inflight.get(key)
            if ev is None:
                ev = _rec_inflight[key] = threading.Event()
                break
        ev.wait(180)                 # ответ придёт в другом потоке; не пришёл (ошибка) — спрошу сам на следующем круге
    try:
        _rec_cache[key] = _fetch_rec(token, aid, nm)
    finally:
        with _rec_lock:
            _rec_inflight.pop(key, None)
        ev.set()
    return _rec_cache[key]


def start_rec_prefetch(cab, token, pairs):
    """Пока основной расчёт кабинета грузит выкуп, размеры (WB отдаёт их по одному товару раз в 20 с — у Яворской
    ≈10 мин), настройки и статистику запросов, рекомендации WB (у них свой лимит) уже запрашиваются в фоне — по
    порядку, в каком понадобятся. Как только расчёт сам дошёл до рекомендаций, фон останавливается: дальше лимит
    у них общий, и быстрее от второго потока не станет. Ошибку фона не показываю — расчёт спросит товар сам и сообщит."""
    if not pairs:
        return

    def worker():
        _tl.buf, _tl.session, _tl.cab, _tl.prefetch = [], requests.Session(), cab, True
        for aid, nm in pairs:
            if cab in _rec_main:
                break
            try:
                get_rec(token, aid, nm)
            except Exception:       # noqa: BLE001 — см. описание
                pass
    threading.Thread(target=worker, daemon=True).start()


def _kop(v):
    """Ставка в копейках из {"bidKopecks": N} или просто числа."""
    if isinstance(v, dict):
        v = v.get("bidKopecks")
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def rec_cpc(data, key):
    """Рекомендованная ставка за клик для уровня key (range3To10 …). WB встречался в двух видах:
    levels = [{range…: …}] или levels = {range…: …} / {"search": {range…: …}}. Непонятно — None."""
    lv = data.get("levels")
    if isinstance(lv, list):
        lv = lv[0] if lv and isinstance(lv[0], dict) else {}
    if not isinstance(lv, dict):
        return None
    if key in lv:
        return _kop(lv[key])
    if isinstance(lv.get("search"), dict):
        return _kop(lv["search"].get(key))
    return None


def rec_cpm(data, key):
    base = data.get("base")
    return _kop(base.get(key)) if isinstance(base, dict) else None


def rec_queries(data):
    """{запрос: рекомендации по нему}; normQueries — список или словарь."""
    nq = data.get("normQueries")
    if isinstance(nq, dict):
        return {k: v for k, v in nq.items() if isinstance(v, dict)}
    if isinstance(nq, list):
        return {q.get("normQuery"): q for q in nq if isinstance(q, dict)}
    return {}


def get_config(token):
    try:
        pace(token, "config", 61)
        return call("GET", ADV + "/api/advert/v1/config", token, what="Настройки продвижения") or {}
    except ApiError:
        return {}


def get_minus(token, pairs):
    res = {}
    for part in chunks(pairs, 100):
        pace(token, "nq", 0.25)
        data = call("POST", ADV + "/adv/v0/normquery/get-minus", token,
                    body={"items": [{"advert_id": a, "nm_id": n} for a, n in part]}, what="Минус-фразы") or {}
        for it in data.get("items") or []:
            nq = it.get("norm_queries")
            if isinstance(nq, list):
                res[(it.get("advert_id"), it.get("nm_id"))] = list(nq)
    # Пары, которых нет в ответе, в res не попадают: текущий список неизвестен. Записывать такой паре нельзя —
    # WB заменяет список целиком, и пустой «текущий» стёр бы все её минус-фразы.
    return res


def organic_data(cab, nms):
    """{(товар, запрос): {день: (все заказы по запросу, медианная позиция, заказы с рекламы)}} из wb_history.db
    (её пишет wb_positions.py: «Джем» — все заказы по запросу, статистика кластеров — заказы с рекламы). Только чтение;
    базы нет или она занята — пусто (контроль органики пропускается)."""
    res = defaultdict(dict)
    if not ORGANIC_CONTROL or not nms or not HISTORY_DB.exists():
        return res
    since = (dt.date.today() - dt.timedelta(days=ORG_DAYS + 60)).isoformat()
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{HISTORY_DB}?mode=ro", uri=True, timeout=30)
        try:
            marks = ",".join("?" * len(nms))
            ad = {}
            for nm, q, d, o in con.execute(f"SELECT nm, query, date, orders FROM ad_query_day WHERE cab=? AND date>=? "
                                           f"AND nm IN ({marks})", (cab, since, *nms)):
                ad[(nm, q, d)] = num(o)
            for nm, q, d, o, mp in con.execute(f"SELECT nm, query, date, orders, median_pos FROM query_day WHERE cab=? "
                                               f"AND date>=? AND nm IN ({marks})", (cab, since, *nms)):
                res[(nm, q)][d] = (num(o), mp, ad.get((nm, q, d), 0.0))
        finally:
            con.close()
    except sqlite3.Error as e:
        log(f"    ! Контроль органики: база истории не прочиталась ({e}) — пропускаю.")
        return defaultdict(dict)
    return res


def org_window(days_map, d1, d2):
    """(все заказы, заказы с рекламы, медианная позиция, дней с данными) за [d1, d2]."""
    rows = [v for d, v in days_map.items() if d1 <= d <= d2]
    pos = sorted(v[1] for v in rows if v[1])
    return (sum(v[0] for v in rows), sum(v[2] for v in rows), pos[len(pos) // 2] if pos else None, len(rows))


def get_excluded(token, pairs):
    """{(кампания, товар): исключённые кластеры} из списка кластеров (normquery/list). Нужен как вторая проверка:
    get-minus по новой кампании за показы может не вернуть ничего — если и здесь исключённых нет, список пуст."""
    res = {}
    for part in chunks(pairs, 100):
        pace(token, "nq", 0.25)
        data = call("POST", ADV + "/adv/v0/normquery/list", token, what="Кластеры товаров",
                    body={"items": [{"advertId": a, "nmId": n} for a, n in part]}) or {}
        for it in data.get("items") or []:
            nq = it.get("normQueries")
            if isinstance(nq, dict):
                res[(it.get("advertId"), it.get("nmId"))] = list(nq.get("excluded") or [])
    return res


def get_cluster_bids(token, pairs):
    res = defaultdict(dict)
    for part in chunks(pairs, 100):
        pace(token, "nq", 0.25)
        data = call("POST", ADV + "/adv/v0/normquery/get-bids", token,
                    body={"items": [{"advert_id": a, "nm_id": n} for a, n in part]}, what="Ставки запросов") or {}
        for b in data.get("bids") or []:
            kop = b.get("bid_kopecks")
            if kop is None and b.get("bid") is not None:
                kop = int(b["bid"]) * 100
            res[(b.get("advert_id"), b.get("nm_id"))][b.get("norm_query")] = kop
    return res


def get_limits(token, ids):
    res = {}
    for part in chunks(ids, 100):
        pace(token, "limits", 12.5)
        data = call("GET", ADV + "/api/advert/v0/daily-limits", token, params={"advertIds": ",".join(map(str, part))},
                    what="Дневные лимиты") or {}
        for a in data.get("adverts") or []:
            res[a.get("advertId")] = a
    return res


# ============================ расчёт ============================

def smooth(hit, total, prior, k):
    return (hit + prior * k) / (total + k)


def new_bid(current, rec, cap, mn, step, max_raise, base=None, max_drop=None):
    """Возвращает (новая ставка, причина). base — ставка на начало суток (первая, увиденная сегодня):
    рост за сутки не больше max_raise %, снижение — не больше max_drop % от неё, сколько бы запусков ни было."""
    if not rec:
        return current, "WB не дал рекомендацию"
    new = min(rec, cap)
    reason = "до рекомендованной" if rec <= cap else "потолок по цене заказа"
    base = base or current
    if base:
        top = max(current or 0, int(base * (1 + max_raise / 100))) if max_raise is not None else None
        if top is not None and new > top:
            new = top
            reason += f", рост ограничен +{max_raise}% в сутки"
        # снижение ВСЛЕД ЗА РЕКОМЕНДАЦИЕЙ WB — без ограничения (решение 12.10.2026): WB сам говорит, что этой ставки
        # хватает для тех же мест, держать выше — переплата. Ограничение −max_drop % — только когда ставку снижает
        # наш окупаемый потолок (он считается по конверсии и может колебаться).
        if max_drop is not None and current and not (DROP_TO_REC_UNLIMITED and rec <= cap):
            floor_ = min(current, int(math.ceil(base * (1 - max_drop / 100) / step) * step))
            if new < floor_:
                new = floor_
                reason += f", снижение ограничено −{max_drop}% в сутки"
    if new == rec and rec <= cap:
        # ставка = рекомендации WB: округляем ВВЕРХ до шага (12,5 → 13 ₽), иначе чуть не дотянем до нужных мест,
        # но не выше окупаемого потолка
        up = int(math.ceil(new / step) * step)
        new = up if up <= cap else int(math.floor(new / step) * step)
    else:
        new = int(math.floor(new / step) * step)
    if mn and new < mn:
        new = mn
        reason = ("окупаемая ставка ниже минимальной WB → минимальная" if cap < mn else
                  "рекомендация WB ниже его же минимальной ставки → минимальная")
    return new, reason


def changed(new, cur, step):
    return new is not None and cur is not None and abs(new - cur) >= max(step, cur * 0.03)


def overlap_warning(camps):
    """Товар сразу в нескольких АКТИВНЫХ кампаниях с общим местом показа (поиск / рекомендации): они соревнуются друг с
    другом за один показ, а единая и ручная ставка вместе у WB не работают — показывается только одна (MPSTATS, вики)."""
    by_nm = defaultdict(list)
    for cid, a in camps.items():
        if a.get("_status") != 9:
            continue
        st = a.get("settings") or {}
        unified = a.get("bid_type") == "unified"
        pl = st.get("placements") or {}
        zones = {"поиск", "рекомендации"} if unified or not pl else \
            {z for z, k in (("поиск", "search"), ("рекомендации", "recommendations")) if pl.get(k)}
        kind = "единая ставка" if unified else ("за клики" if st.get("payment_type") == "cpc" else "за показы")
        for s in a.get("nm_settings") or []:
            by_nm[s.get("nm_id")].append((cid, st.get("name") or str(cid), kind, zones))
    dup = {}
    for nm, lst in by_nm.items():
        pairs = [(x, y) for i, x in enumerate(lst) for y in lst[i + 1:] if x[3] & y[3]]
        if pairs:
            dup[nm] = lst
    if not dup:
        return
    mixed = sum(1 for lst in dup.values() if {k for _, _, k, _ in lst} >= {"единая ставка"} and
                any(k != "единая ставка" for _, _, k, _ in lst))
    log(f"  ! Товар сразу в нескольких активных кампаниях с общим местом показа: {len(dup)} тов. Кампании соревнуются друг "
        "с другом за один и тот же показ" + (f"; у {mixed} — единая и ручная ставка вместе, у WB "
                                                                "работает только одна" if mixed else "") + ":")
    for nm, lst in sorted(dup.items(), key=lambda x: -len(x[1]))[:15]:
        log(f"    • {nm}: " + "; ".join(f"«{cut(n, 28)}» {c} ({k}, {'+'.join(sorted(z))})" for c, n, k, z in lst))
    if len(dup) > 15:
        log(f"    … и ещё {len(dup) - 15}")


def plan_cabinet(cab, token, rules, state):
    _tl.cab = cab                           # для data_problem: в каком кабинете сбой
    _rec_main.discard(cab)
    log(f"\n══════════ {cab} ══════════")
    plan = {"bids": [], "cluster_bids": [], "minus": [], "limits": [], "trim": [], "untrim": [], "topup": []}
    camps = get_campaigns(token)
    overlap_warning(camps)
    todo = {}
    explicit = {}
    for k, v in rules.items():
        if k == "*" or k.startswith("_"):     # служебные ключи: "_except", "_week_budget"
            continue
        if v.get("until") and dt.date.today().isoformat() > v["until"]:
            log(f"  Правило «{k}» действовало до {v['until']} — теперь кампания на общих правилах.")
            continue
        explicit[k] = {kk: vv for kk, vv in v.items() if kk != "until"}
        if v.get("until"):
            explicit[k]["_until"] = v["until"]
    for key, rule in explicit.items():
        ids = find(camps, key)
        if not ids:
            log(f"  ! Кампания «{key}» не найдена среди активных, на паузе и готовых к запуску.")
        if len(ids) > 1:
            log(f"  ! «{key}» подходит к нескольким кампаниям ({', '.join(map(str, ids))}) — уточните название или укажите ID.")
            continue
        for cid in ids:
            todo[cid] = {**DEFAULTS, **rules.get("*", {}), **rule}
    if "*" in rules:
        skip = {cid for k in rules.get("_except") or [] for cid in find(camps, k)}
        auto = [cid for cid, a in camps.items() if a.get("_status") == 9 and cid not in todo and cid not in skip]
        for cid in auto:
            todo[cid] = dict(DEFAULTS, **rules["*"])
        log(f"  Все активные кампании: {len(auto)}" + (f" (кроме {len(skip)} из «_except»)" if skip else "") + ": "
            + ", ".join(f"«{(camps[c].get('settings') or {}).get('name')}» {c}" for c in auto))
        # кампания, которой автопилот управлял, встала на паузу: бюджет меньше порога — пауза из-за денег (пополнить
        # и запустить); деньги в бюджете есть — паузу поставили вы, не трогаю
        managed = state.get("managed") or {}
        paused = [cid for cid, a in camps.items() if a.get("_status") == 11 and str(cid) in managed
                  and cid not in todo and cid not in skip]
        thr = {**DEFAULTS, **rules["*"]}.get("topup_below")
        if paused:
            buds = get_budgets(token, paused)
            for cid in paused:
                nm_ = (camps[cid].get("settings") or {}).get("name")
                b = buds.get(cid)
                if limit_paused(state, cid) == dt.date.today().isoformat():
                    log(f"  «{nm_}» {cid} на паузе по дневному лимиту — запустит часовая проверка утром.")
                elif pace_paused(state, cid):
                    log(f"  «{nm_}» {cid} на паузе по графику расхода — запустит проверка лимита, когда спрос догонит.")
                elif thr and b is not None and b < PAUSED_MONEY_BELOW:
                    todo[cid] = dict(DEFAULTS, **rules["*"], _resume=True)
                    log(f"  «{nm_}» {cid} на паузе, бюджет {fm_rub(b)} ₽ — кончились деньги: пополню и запущу.")
                elif thr and b is not None and limit_paused(state, cid):
                    todo[cid] = dict(DEFAULTS, **rules["*"], _resume=True)
                    log(f"  «{nm_}» {cid} вчера встала на паузу по дневному лимиту — запущу.")
                elif thr and b is not None and start_retry(state, cid):
                    todo[cid] = dict(DEFAULTS, **rules["*"], _resume=True)
                    log(f"  «{nm_}» {cid} на паузе, бюджет {fm_rub(b)} ₽ — её пополнили {start_pending(state, cid)}, "
                        "а запуск не прошёл: запущу.")
                else:
                    log(f"  «{nm_}» {cid} на паузе, в бюджете {fm_rub(b) if b is not None else '?'} ₽ — паузу, видимо, "
                        "поставили вы: не трогаю.")
    if not todo:
        log("  Нет кампаний для управления.")
        return plan

    def ref(key):
        ids = find(camps, key) if key else []
        return ids[0] if len(ids) == 1 else None

    for cid, r in todo.items():
        for k in ("clusters_from", "route_from"):
            if r.get(k):
                r[k + "_id"] = ref(r[k])
                if not r[k + "_id"]:
                    log(f"  ! {k} = «{r[k]}» для «{camps[cid]['settings'].get('name')}»: кампания не найдена однозначно.")

    # общий недельный потолок пополнений на кабинет ("_week_budget"): сумма по всем его кампаниям за 7 дней
    cab_cap = rules.get("_week_budget")
    cab_left = [cab_cap - topped_cabinet(state, cab, camps) if cab_cap else None]
    if cab_cap:
        log(f"  Недельный бюджет пополнений кабинета: {cab_cap} ₽, за 7 дней уже {cab_cap - cab_left[0]:.0f} ₽.")
    involved = set(todo) | {r.get("clusters_from_id") for r in todo.values()} | {r.get("route_from_id") for r in todo.values()}
    involved.discard(None)
    # статистику WB отдаёт только для статусов 7, 9, 11; «готовая к запуску» (4) в пакете могла бы сорвать весь запрос
    stats = nm_stats(token, [c for c in involved if camps[c].get("_status") != 4])
    nms_of = {cid: [s.get("nm_id") for s in camps[cid].get("nm_settings") or []] for cid in involved}
    was_trimmed = {k: v for k, v in trimmed_items(state, cab).items() if k[0] in todo and k[1] not in nms_of.get(k[0], [])}
    stock = get_stock(token, sorted({nm for cid in involved for nm in nms_of[cid]} | {k[1] for k in was_trimmed}))
    for cid in todo:
        if camps[cid].get("_status") in (9, 11):
            MANAGED[cid] = cab
    load_buyout(cab, token, sorted({nm for cid in involved for nm in nms_of[cid]}),
                {nm: v.get("vendor") for nm, v in (stock or {}).items()})
    def no_stock(nm):
        q = available((stock or {}).get(nm))
        return q is not None and q <= 0

    # рекомендации WB — в фоне, пока грузятся выкуп, размеры и статистика запросов (свой лимит у каждого метода);
    # те же товары и тот же порядок, что ниже в расчёте ставок: без «нет в наличии», убираемых по малому остатку
    # и кампаний с минимальной ставкой
    pre = []
    for cid in sorted(todo, key=lambda c: 0 if todo[c]["cluster_bids"] else 1):
        r_ = todo[cid]
        if r_["base_bid"] == "min":
            continue
        nms_ = nms_of[cid]
        if r_.get("min_stock") and stock is not None:
            low_ = {nm for nm in nms_ if available(stock.get(nm)) is not None and available(stock.get(nm)) < r_["min_stock"]}
            if len(low_) < len(nms_):
                nms_ = [nm for nm in nms_ if nm not in low_]
        pre += [(cid, nm) for nm in nms_ if not no_stock(nm)]
    start_rec_prefetch(cab, token, pre)

    # с какого дня товар непрерывно в наличии (WB + FBS) (для автоминуса). Меняется только в памяти —
    # в wb_bids_state.json попадает лишь при --apply; пробный прогон ничего не записывает.
    today = dt.date.today()
    since = state.setdefault("in_stock_since", {})
    started = []
    if stock is not None:
        for nm in sorted({nm for cid in involved for nm in nms_of[cid]}):
            q = available(stock.get(nm))
            if q is None:
                continue                      # остаток по товару неизвестен — отсчёт не меняем
            if q > 0 and str(nm) not in since:
                since[str(nm)] = today.isoformat()
                started.append(nm)
            elif q <= 0:
                since.pop(str(nm), None)
    # товар, убранный из-за малого остатка и не рекламируемый в других кампаниях: отсчёт начнётся заново после
    # возвращения — иначе автоминус включился бы сразу, судя запросы по периоду, когда товара почти не было
    in_ads = {nm for cid in involved for nm in nms_of[cid]}
    for k in was_trimmed:
        if k[1] not in in_ads:
            since.pop(str(k[1]), None)

    # размеры: доля спроса, которую закрывают размеры в наличии
    cover = {}
    if SIZE_RULE and stock is not None:
        # размеры решают что-то только у товаров с заказами (меньше SIZE_MIN_ORDERS за период — по размерам не судим):
        # WB отдаёт размеры по одному товару раз в 20 секунд, поэтому остальные не спрашиваем
        in_stock = [nm for nm in sorted({nm for cid in involved for nm in nms_of[cid]})
                    if not no_stock(nm) and ((stock.get(nm) or {}).get("orders") or 0) >= SIZE_MIN_ORDERS]
        for nm, sizes in get_sizes(token, in_stock).items():
            c, miss = size_cover(sizes)
            if c is not None:
                cover[nm] = (c, miss)
        short = {nm: v for nm, v in cover.items() if v[0] < SIZE_COVER_NO_RAISE}
        if short:
            log(f"  Размеры: у {len(short)} тов. в наличии меньше {SIZE_COVER_NO_RAISE:.0%} спроса по размерам "
                f"(ставку не повышаю, автоминус не ставлю; меньше {SIZE_COVER_MIN_BID:.0%} — минимальная ставка):")
            for nm, (c, miss) in sorted(short.items(), key=lambda x: x[1][0]):
                log(f"      {nm:>11} закрыто {c:.0%} спроса, нет: {', '.join(miss[:6])}")

    def size_short(nm):
        c = cover.get(nm)
        return None if c is None else ("min" if c[0] < SIZE_COVER_MIN_BID else "hold" if c[0] < SIZE_COVER_NO_RAISE else None)

    def stock_days(nm):
        d = since.get(str(nm))
        return (today - dt.date.fromisoformat(d)).days if d else None

    started = [nm for nm in started if any(todo[c]["auto_minus"] and nm in nms_of[c] for c in todo)]
    if started:
        log(f"  Остатки{' (WB + свой склад FBS)' if COUNT_FBS_STOCK else ' на WB'}: отсчёт наличия начат сегодня для {len(started)} тов. "
            f"({', '.join(map(str, started[:10]))}{' …' if len(started) > 10 else ''}) — автоминус по ним включится "
            f"через {MINUS_AFTER_RESTOCK_DAYS} дн. (слова «не наша аудитория» — сразу).")

    cfg = get_config(token)
    # минимальное пополнение — из настроек WB (minTopUp, в копейках), но не меньше MIN_DEPOSIT, на котором WB уже
    # отказывал; если WB поднимет минимум — возьмём его, а не будем получать отказы
    min_dep = max(MIN_DEPOSIT, num(cfg.get("minTopUp")) / 100)
    steps = {"cpc": int(cfg.get("cpcStep") or 100), "cpm": int(cfg.get("cpmStep") or 100)}

    # статистика запросов нужна для auto_minus, cluster_bids и источников "top"
    need_cl = set()
    for cid, r in todo.items():
        if (r["auto_minus"] and (camps[cid].get("settings") or {}).get("payment_type") != "cpc") or r["cluster_bids"]:
            need_cl.add(cid)
        if r.get("clusters_from_id"):
            need_cl.add(r["clusters_from_id"])
    cl = cluster_stats(token, [(cid, nm) for cid in need_cl for nm in nms_of[cid]])

    # порядок: сначала кампании со ставками на запросы, потом остальные (для route_from)
    order = sorted(todo, key=lambda c: 0 if todo[c]["cluster_bids"] else 1)
    planned_cluster = defaultdict(set)   # (cid, nm) → запросы, на которые будут стоять ставки
    check_card = []                      # товары с кликами без заказов — проверить карточку
    returned_now = defaultdict(set)      # (cid, nm) → запросы, которые в этом запуске возвращаются в клики

    # бюджеты и дневные лимиты всех кампаний — одним запросом на кабинет (раньше — по запросу на кампанию)
    want_lim = [c for c in order if todo[c]["daily_limit"] and (camps[c].get("settings") or {}).get("payment_type") == "cpc"]
    lim_err = None
    try:
        lims_all = get_limits(token, want_lim) if want_lim else {}
    except ApiError as e:
        lims_all, lim_err = None, e
    want_bud = [c for c in order if todo[c].get("topup_below")]
    buds_all = get_budgets(token, want_bud) if want_bud else {}
    # кампании за показы с дневным лимитом: расход за сегодня (лимит держит пауза в wb_guard.py; здесь — график расхода)
    want_cpm = [c for c in order if todo[c]["daily_limit"] and (camps[c].get("settings") or {}).get("payment_type") != "cpc"
                and camps[c].get("_status") == 9]
    cpm_spent = spent_today(token, want_cpm) if want_cpm and PACING else {}

    for cid in order:
        r, a = todo[cid], camps[cid]
        st = a.get("settings") or {}
        name = st.get("name")
        pay = st.get("payment_type") or "cpm"
        unified = a.get("bid_type") == "unified"
        pl = st.get("placements") or {}
        step = steps[pay]
        target = r["target"] if r["target"] in CPC_LEVEL else "3-10"
        cap_abs = int((r["max_bid_cpc"] if pay == "cpc" else r["max_bid_cpm"]) * 100)
        pace_m, pace_why = 1.0, ""
        if pay == "cpc" and r.get("pacing", True) and lims_all:
            lm = lims_all.get(cid) or {}
            if lm.get("enabled") and lm.get("spentToday") is not None:
                # лимит — тот, что будет после этого запуска: автопилот ставит лимит из правила (если он задан)
                lim_rub = r.get("daily_limit") or num(lm.get("dailyLimit")) / 100
                pace_m, pace_why = pace_factor(cid, num(lm["spentToday"]) / 100, lim_rub, state=state)
        elif pay != "cpc" and r.get("pacing", True) and cid in cpm_spent:      # за показы — по своему дневному лимиту
            pace_m, pace_why = pace_factor(cid, cpm_spent[cid], r["daily_limit"], state=state)
        pace_prev = ((state.get("pacing") or {}).get(str(cid)) or {})
        pace_prev = pace_prev.get("m", 1.0) if pace_prev.get("date") == dt.date.today().isoformat() else 1.0
        nms = nms_of[cid]
        c_clicks = sum(v["clicks"] for (x, _), v in stats.items() if x == cid)
        c_orders = sum(v["orders"] for (x, _), v in stats.items() if x == cid)
        c_views = sum(v["views"] for (x, _), v in stats.items() if x == cid)
        camp_cr = smooth(c_orders, c_clicks, PRIOR_CR, K_CLICKS)
        camp_ctr = smooth(c_clicks, c_views, PRIOR_CTR, K_VIEWS)
        log(f"\n  ▶ {name} (ID {cid}) — {'единая' if unified else 'ручная'} ставка, "
            f"{'за клики' if pay == 'cpc' else 'за показы'}, цель {target}, "
            f"{'ставка по рекомендации WB (эксперимент, без окупаемого потолка)' if r.get('follow_rec') else 'заказ до ' + str(r['max_cost_per_order']) + ' ₽' + (' × выкуп товара' if any(k[0] == cab for k in list(BUYOUT)) else '') if r['max_cost_per_order'] != 'auto' else 'цена заказа — из юнит-экономики'}, "
            f"конверсия {camp_cr * 100:.1f}%" + (f"; режим запуска новинки до {r['_until']}" if r.get("_until") else ""))
        if pace_m < 1:
            log(f"    График расхода: {pace_why}.")

        # ---- товары с малым остатком: убрать из кампании ----
        if r.get("min_stock") and stock is not None:
            low = [nm for nm in nms if (available(stock.get(nm)) is not None and available(stock.get(nm)) < r["min_stock"])]
            if low and len(low) == len(nms):
                log(f"    ! У всех товаров кампании меньше {r['min_stock']} шт — кампанию не опустошаю; "
                    "если реклама ей не нужна, поставьте её на паузу в кабинете WB.")
            elif low:
                bk = {s_.get("nm_id"): s_.get("bids_kopecks") or {} for s_ in a.get("nm_settings") or []}
                log(f"    Убрать из кампании (в наличии меньше {r['min_stock']} шт): "
                    + ", ".join(f"{nm} ({stock_label(stock.get(nm))})" for nm in low))
                for nm in low:
                    plan["trim"].append({"cabinet": cab, "advert_id": cid, "nm_id": nm, "bid_type": a.get("bid_type"),
                                         "bids_kopecks": bk.get(nm, {}), "stock": available(stock.get(nm))})
                nms = [nm for nm in nms if nm not in low]
        # ---- убранные раньше из-за малого остатка: вернуть, когда в наличии снова return_stock ----
        if r.get("return_stock") and stock is not None:
            back = [i for (c_, nm), i in was_trimmed.items() if c_ == cid
                    and (available(stock.get(nm)) or 0) >= r["return_stock"]]
            if back:
                log(f"    Вернуть в кампанию (в наличии снова {r['return_stock']}+ шт): "
                    + ", ".join(f"{i['nm_id']} ({stock_label(stock.get(i['nm_id']))})" for i in back))
                for i in back:
                    plan["untrim"].append({"cabinet": cab, "advert_id": cid, "nm_id": i["nm_id"],
                                           "bid_type": i.get("bid_type") or a.get("bid_type"),
                                           # ставка на момент уборки могла быть выше нынешнего потолка — не выше потолка
                                           "bids_kopecks": {k: min(v, cap_abs) if isinstance(v, (int, float)) else v
                                                            for k, v in (i.get("bids_kopecks") or {}).items()}})

        # ---- ставки товаров ----
        if unified:
            placements = ["combined"]
        else:
            placements = [p for p in ("search", "recommendations") if pl.get(p)] if pl else ["search"]
            if pay == "cpc" and "recommendations" in placements:
                placements.remove("recommendations")
                log("    Рекомендации за клики WB пока тестирует — их ставки не трогаю, только поиск.")
        cur_all = {s.get("nm_id"): s.get("bids_kopecks") or {} for s in a.get("nm_settings") or []}
        unit = "₽/клик" if pay == "cpc" else "₽/1000"
        for placement in placements:
            mins = get_min_bids(token, cid, nms, pay, placement)
            key_cur = "search" if placement == "combined" else placement
            log(f"    Ставки товаров — {({'search': 'поиск', 'recommendations': 'рекомендации', 'combined': 'поиск+рекомендации'})[placement]}:")
            log(f"      {'артикул':>11} {'арт. продавца':16}{'WB/FBS' if COUNT_FBS_STOCK else 'WB шт':>9}{'клики':>6}{'зак.':>5}{'конв.':>6}"
                f"{'сейчас':>9}{'реком.':>9}{'потолок':>9}{'будет':>9}  {unit}")
            for nm in nms:
                v = stats.get((cid, nm)) or defaultdict(float)
                cr = smooth(v["orders"], v["clicks"], camp_cr, K_CLICKS)
                ctr = smooth(v["clicks"], v["views"], camp_ctr, K_VIEWS)
                econ = mcpo(cab, r, nm) * cr * 100 * (1 if pay == "cpc" else ctr * 1000)
                cap = int(min(econ, cap_abs))
                if r.get("follow_rec"):     # эксперимент: ставка по рекомендации WB, деньги держат бюджет и лимиты
                    cap = cap_abs
                fixed = True    # ставку задаёт правило (нет в наличии / нет размеров / «минимальная»), не рекомендация
                s_info = (stock or {}).get(nm) or {}
                current, mn = cur_all.get(nm, {}).get(key_cur), mins.get(nm) or 0
                rec = None
                if no_stock(nm):
                    nb, reason = (current, "нет на WB — не трогаю") if r["no_stock"] == "skip" else \
                        (mn or current, "нет в наличии → минимальная" if COUNT_FBS_STOCK else "нет на складах WB → минимальная")
                elif size_short(nm) == "min" and r["no_stock"] != "skip":
                    nb, reason = (mn or current), f"нет ходовых размеров ({', '.join(cover[nm][1][:3])}) → минимальная"
                elif r["base_bid"] == "min":
                    nb, reason = (mn or current), "минимальная (кампания только для лучших запросов)"
                else:
                    fixed = False
                    try:
                        data = get_rec(token, cid, nm)
                        rec = rec_cpc(data, CPC_LEVEL[target]) if pay == "cpc" else rec_cpm(data, CPM_LEVEL[target])
                        if rec is None and data and target != "3-10":   # нет уровня для мест 1–2 — берём 3–10
                            rec = rec_cpc(data, CPC_LEVEL["3-10"]) if pay == "cpc" else rec_cpm(data, CPM_LEVEL["3-10"])
                            if rec is not None:
                                log(f"      ({nm}: WB не дал ставку для мест {target}, беру для 3–10)")
                        if rec is None and data:
                            log(f"      ! {nm}: не понял ответ WB с рекомендацией (образец — {REC_SAMPLE.name}), ставку не трогаю.")
                            data_problem(f"непонятный формат рекомендаций WB — см. {REC_SAMPLE.name}")
                    except ApiError as e:
                        log(f"      ! {e}")
                    note_rec(cab, cid, nm, pay, target, rec, current, mn)
                    nb, reason = new_bid(current, rec, cap, mn, step, r["max_raise_pct"],
                                         day_base(state, "bids", cid, nm, placement), r.get("max_drop_pct"))
                    if r.get("follow_rec") and reason.startswith("потолок по цене заказа"):
                        reason = reason.replace("потолок по цене заказа", f"потолок ставки {cap / 100:.0f} ₽")
                    if pace_m < 1 and nb:
                        nb2 = paced(nb, pace_m, mn, step, current, pace_prev)
                        if nb2 != nb:
                            nb, reason = nb2, f"{reason}; график расхода ×{pace_m}" + (
                                " — расход впереди графика, не повышаю" if nb2 == current else "")
                    if size_short(nm) == "hold" and current and nb and nb > current:
                        nb, reason = current, f"мало ходовых размеров (закрыто {cover[nm][0]:.0%} спроса) — не повышаю"
                    if r.get("stop_clicks") and v["clicks"] >= r["stop_clicks"] and not v["orders"]:
                        check_card.append(f"{nm} ({cut(s_info.get('vendor'), 20)}) в «{name}»: {int(v['clicks'])} кликов "
                                          f"за {STATS_DAYS} дн без заказов")
                        if current and nb and nb > current:
                            nb, reason = current, f"{int(v['clicks'])} кликов без заказов — не повышаю, проверить карточку"
                # для часовой проверки wb_guard.py: потолок и правила товара на момент этого прогона
                CAPS_CAB[pk(cid, nm)] = cab
                CAPS.setdefault(pk(cid, nm), {})[placement] = {
                    "cap": cap, "pay": pay, "target": target, "step": step, "clicks": int(v["clicks"]),
                    "max_raise": r["max_raise_pct"], "max_drop": r.get("max_drop_pct"), "fixed": fixed,
                    "no_raise": bool(size_short(nm) == "hold" or (r.get("stop_clicks") and v["clicks"] >= r["stop_clicks"]
                                                                and not v["orders"]))}
                ch = changed(nb, current, step)
                log(f"      {nm:>11} {cut(s_info.get('vendor'), 16):16}{stock_label(s_info):>9}"
                    f"{int(v['clicks']):>6}{int(v['orders']):>5}{cr * 100:>5.1f}%{rub(current):>9}{rub(rec):>9}"
                    f"{rub(cap):>9}{rub(nb):>9}  {('↑' if nb > current else '↓') if ch else '='} {reason}")
                if ch:
                    plan["bids"].append({"cabinet": cab, "advert_id": cid, "nm_id": nm, "placement": placement,
                                         "old": current, "new": nb})

        # ---- ставки на поисковые запросы ----
        if r["cluster_bids"]:
            if unified or pay != "cpm":
                log("    ! Ставки на запросы WB разрешает только для ручной ставки с оплатой за показы — пропускаю.")
            else:
                cur_cb = get_cluster_bids(token, [(cid, nm) for nm in nms])
                mins = get_min_bids(token, cid, nms, pay, "search")
                rows, returns = [], []
                for nm in nms:
                    if no_stock(nm):
                        continue
                    try:
                        recs = rec_queries(get_rec(token, cid, nm))
                    except ApiError as e:
                        log(f"    ! {e}")
                        recs = {}
                    own = cl.get((cid, nm), {})
                    v = stats.get((cid, nm)) or defaultdict(float)
                    nm_cr = smooth(v["orders"], v["clicks"], camp_cr, K_CLICKS)
                    cutoff = (dt.date.today() - dt.timedelta(days=RETURN_PAUSE_DAYS)).isoformat()
                    recent = {q for q, d in state.get("cluster_returned", {}).get(pk(cid, nm), {}).items() if d >= cutoff}
                    returning = set()
                    if r["cluster_bids"] == "top":
                        # запросы, которые за показы не окупились, возвращаем в кампанию за клики
                        for q, cur_b in cur_cb.get((cid, nm), {}).items():
                            s_q = own.get(q) or {}
                            if s_q.get("clicks", 0) < MINUS_MIN_CLICKS:
                                continue
                            cpo = s_q["spend"] / s_q["orders"] if s_q.get("orders") else None
                            if (not s_q.get("orders") and s_q["clicks"] * nm_cr >= MINUS_EXPECTED) or \
                                    (cpo and cpo > mcpo(cab, r, nm) * RETURN_FACTOR):
                                returning.add(q)
                                returned_now[(cid, nm)].add(q)
                                returns.append((nm, q, s_q, cpo))
                                plan["cluster_bids"].append({"cabinet": cab, "advert_id": cid, "nm_id": nm, "query": q,
                                                             "old": cur_b, "new": None, "returned": True})
                        src = cl.get((r.get("clusters_from_id"), nm), {})
                        best = sorted(((q, s) for q, s in src.items() if s["orders"] >= r["top_min_orders"]
                                       and q not in recent and q not in returning),
                                      key=lambda x: (-x[1]["orders"], x[1]["spend"]))[:r["top_clusters"]]
                        wanted = [q for q, _ in best]
                    else:
                        wanted = list(recs)
                    for q in wanted:
                        s_own = own.get(q) or {}
                        s_src = cl.get((r.get("clusters_from_id"), nm), {}).get(q) or {}
                        clicks = s_own.get("clicks", 0) + s_src.get("clicks", 0)
                        orders = s_own.get("orders", 0) + s_src.get("orders", 0)
                        cr = smooth(orders, clicks, nm_cr, 50)
                        ctr = smooth(s_own.get("clicks", 0), s_own.get("views", 0), PRIOR_CTR, K_VIEWS)
                        cap = int(min(mcpo(cab, r, nm) * cr * ctr * 1000 * 100, cap_abs))
                        rq = recs.get(q) or {}
                        lvl = rq.get(REACH[target])
                        lvl = lvl if isinstance(lvl, dict) else {}
                        rec = _kop(lvl)
                        mn = _kop(lvl.get("bidKopecksMin")) or mins.get(nm) or 0
                        cur = cur_cb.get((cid, nm), {}).get(q)
                        if rec is None:
                            rec = rec_cpm(get_rec(token, cid, nm), CPM_LEVEL[target])
                        nb, reason = new_bid(cur, rec, cap, mn, step, r["max_raise_pct"],
                                             day_base(state, "clusters", cid, nm, q), r.get("max_drop_pct"))
                        if size_short(nm) and cur and nb and nb > cur:
                            nb, reason = cur, "мало ходовых размеров — не повышаю"
                        planned_cluster[(cid, nm)].add(q)
                        if nb is None:
                            continue
                        if cur is None or changed(nb, cur, step):
                            rows.append((nm, q, cur, rec, cap, nb, reason))
                            plan["cluster_bids"].append({"cabinet": cab, "advert_id": cid, "nm_id": nm, "query": q,
                                                         "old": cur, "new": nb})
                    for q in cur_cb.get((cid, nm), {}):
                        if q not in returning:
                            planned_cluster[(cid, nm)].add(q)
                log(f"    Ставки на запросы ({'лучшие из «' + str(r['clusters_from']) + '»' if r['cluster_bids'] == 'top' else 'все с рекомендацией'}): "
                    f"изменить {len(rows)}")
                for nm, q, cur, rec, cap, nb, reason in rows[:25]:
                    log(f"      {nm:>11} {cut(q, 34):34}{rub(cur):>9} →{rub(nb):>9}  (реком. {rub(rec)}, потолок {rub(cap)}) {reason}")
                if len(rows) > 25:
                    log(f"      … и ещё {len(rows) - 25}")
                for nm, q, s_q, cpo in returns:
                    log(f"      ⟲ {nm:>11} {cut(q, 34):34} вернуть в клики: за показы {int(s_q['clicks'])} кл., "
                        f"{int(s_q['orders'])} зак., " + (f"заказ {cpo:,.0f} ₽".replace(",", " ") if cpo else "без заказов"))

        # ---- минус-фразы ----
        # У кампаний за клики WB не даёт исключать запросы: в кабинете минус-фраз нет, get-minus по ним пуст
        # (статистику кластеров API при этом отдаёт — см. wb_clusters.py). Минус-фразы — только у кампаний за показы.
        if (r["auto_minus"] or r.get("route_from_id")) and pay == "cpc":
            log("    Минус-фразы: кампания за клики — WB не даёт исключать в ней запросы (кластеры и минус-фразы есть "
                "только у кампаний за показы). Пропускаю; лишние запросы здесь срезает только ставка.")
        elif r["auto_minus"] or r.get("route_from_id"):
            pairs = [(cid, nm) for nm in nms]
            try:
                cur_minus = get_minus(token, pairs)
            except ApiError as e:
                log(f"    ! {e} — минус-фразы пропускаю.")
                cur_minus = None
            route_want = defaultdict(set)
            if cur_minus is not None and r.get("route_from_id"):
                src = r["route_from_id"]
                src_bids = get_cluster_bids(token, [(src, nm) for nm in nms if nm in nms_of.get(src, [])])
                for nm in nms:
                    route_want[nm] = (set(src_bids.get((src, nm), {})) | planned_cluster.get((src, nm), set())) \
                        - returned_now.get((src, nm), set())
            added_total = 0
            tot_c = sum(s["clicks"] for p, d in cl.items() if p[0] == cid for s in d.values())
            tot_o = sum(s["orders"] for p, d in cl.items() if p[0] == cid for s in d.values())
            cr_ref = smooth(tot_o, tot_c, PRIOR_CR, K_CLICKS)
            held_notes = []
            # Формат ответа WB «подтверждён» для кампании, если WB вернул список хотя бы по одному её товару:
            # тогда товар без списка — это просто товар без минус-фраз. Если по кампании не вернулось ничего —
            # либо фраз нет совсем, либо тип кампании их не поддерживает; писать вслепую нельзя (запись заменяет список).
            camp_known = cur_minus is not None and any((cid, nm) in cur_minus for nm in nms)
            if cur_minus is not None and not camp_known:
                # вторая проверка: в списке кластеров WB у всех товаров нет исключённых — значит, минус-фраз нет, список пуст
                try:
                    excl = get_excluded(token, pairs)
                except ApiError:
                    excl = {}
                if nms and all(excl.get((cid, nm)) == [] for nm in nms):
                    for nm in nms:
                        cur_minus[(cid, nm)] = []
                    camp_known = True
                    log("    Минус-фразы: WB не вернул списков, но исключённых кластеров у товаров нет — список пуст, "
                        "автоминус работает.")
            if cur_minus is not None and not camp_known:
                log("    Минус-фразы: WB не вернул по этой кампании ни одного списка — скорее всего, исключённых запросов "
                    "в ней пока нет. Писать вслепую нельзя (запись заменяет весь список), поэтому автоминус здесь ждёт: "
                    "чтобы включить его, исключите в кабинете WB любой один кластер у любого товара этой кампании "
                    "(«Кластеры» → «Исключить»). Посмотреть кластеры: python wb_clusters.py --campaign " + str(cid))
            org = organic_data(cab, nms) if (r["auto_minus"] and r.get("organic_control", True) and camp_known) else {}
            org_notes = []
            for nm in (nms if camp_known else []):
                key = pk(cid, nm)
                current = list(cur_minus.get((cid, nm), []))
                cur_set = set(current)
                add_auto, add_route, rem_route, rem_auto, add_org, rem_org = [], [], [], [], [], []
                # перепроверка: запросы, которые автопилот сам отправил в минус давно, — вернуть (кроме «не наша аудитория»)
                if r["auto_minus"]:
                    at = (state.get("minus_auto_at") or {}).get(key) or {}
                    tries = (state.get("minus_retests") or {}).get(key) or {}
                    for q in sorted(set(state["minus_auto"].get(key, [])) & cur_set):
                        if negative(q):
                            continue
                        m_since = at.get(q)
                        if not m_since:                # записи до 15.10.2026 без даты — отсчёт с сегодняшнего дня
                            state.setdefault("minus_auto_at", {}).setdefault(key, {})[q] = dt.date.today().isoformat()
                            continue
                        wait = min(MINUS_RETEST_DAYS * 2 ** tries.get(q, 0), 180)
                        if (dt.date.today() - dt.date.fromisoformat(m_since)).days >= wait:
                            rem_auto.append(q)
                # маршрутизация
                prev_route = set(state["minus_route"].get(key, []))
                for q in sorted(route_want.get(nm, set()) - cur_set):
                    add_route.append(q)
                for q in sorted(prev_route - route_want.get(nm, set())):
                    if q in cur_set:
                        rem_route.append(q)
                # плохие запросы; пока товар меньше MINUS_AFTER_RESTOCK_DAYS дней в наличии — только MINUS_WORDS
                if r["auto_minus"]:
                    sd = stock_days(nm)
                    wait_days = r.get("minus_after_restock_days", MINUS_AFTER_RESTOCK_DAYS)
                    gated = (wait_days and (sd is None or sd < wait_days)) or size_short(nm) is not None
                    cs = cl.get((cid, nm), {})
                    allowed_nm = mcpo(cab, r, nm)
                    tv = sum(s["views"] for s in cs.values())
                    ctr_nm = sum(s["clicks"] for s in cs.values()) / tv if tv else None

                    def why_bad(q, s):
                        if s["orders"] or q in cur_set or protected(q) or q in planned_cluster.get((cid, nm), set()) \
                                or q in route_want.get(nm, set()):
                            return None
                        if negative(q):
                            return "не наша аудитория"
                        if s["clicks"] >= MINUS_MIN_CLICKS and s["clicks"] * cr_ref >= MINUS_EXPECTED:
                            return f"{int(s['clicks'])} кл., 0 заказов"
                        if allowed_nm and s["spend"] >= allowed_nm * MINUS_SPEND_ORDERS:
                            return (f"потрачено {fm_rub(s['spend'])} ₽ — больше {str(MINUS_SPEND_ORDERS).replace('.', ',')} допустимой цены заказа "
                                    f"({fm_rub(allowed_nm)} ₽), 0 заказов")
                        if ctr_nm and s["views"] >= MINUS_MIN_VIEWS and s["clicks"] / s["views"] < ctr_nm * MINUS_LOW_CTR \
                                and allowed_nm and s["spend"] >= allowed_nm * 0.5:
                            return (f"CTR {s['clicks'] / s['views']:.1%} при {ctr_nm:.1%} в среднем у товара, "
                                    f"{int(s['views'])} показов, {fm_rub(s['spend'])} ₽, 0 заказов")
                        return None
                    why_q = {q: why_bad(q, s) for q, s in cs.items()}
                    bad = sorted(((q, s) for q, s in cs.items() if why_q[q]),
                                 key=lambda x: (not negative(x[0]), -x[1]["spend"]))
                    held = {q for q, s in bad if gated and not negative(q)}
                    if held:
                        held_notes.append(
                            f"      … {nm:>11} {len(held)} запр. без заказов пока не в минус: " +
                            ("мало ходовых размеров в наличии — запросы не судим, пока размеры не вернутся"
                             if size_short(nm) and sd is not None and sd >= MINUS_AFTER_RESTOCK_DAYS else
                             f"товар в наличии {sd} дн. из {MINUS_AFTER_RESTOCK_DAYS}, автоминус включится через "
                             f"{MINUS_AFTER_RESTOCK_DAYS - sd} дн." if sd is not None else
                             f"товара нет в наличии (или остаток неизвестен) — автоминус включится через "
                             f"{MINUS_AFTER_RESTOCK_DAYS} дн. после появления"))
                    for q, s in bad:
                        if q in held:
                            continue
                        if added_total >= MINUS_MAX_PER_RUN:
                            break
                        add_auto.append(q)
                        added_total += 1
                # контроль органики: запрос, по которому товар продаётся и без рекламы, — в минус; заказы упали — вернуть
                if org:
                    y = dt.date.today() - dt.timedelta(days=1)
                    d1 = (y - dt.timedelta(days=ORG_DAYS - 1)).isoformat()
                    om = (state.get("organic_minus") or {}).get(key) or {}
                    for q, rec in sorted(om.items()):
                        if q not in cur_set:
                            continue
                        since_d = dt.date.fromisoformat(rec["date"])
                        if (y - since_d).days + 1 < ORG_CHECK_DAYS:
                            continue
                        allo, _, mpos, nd = org_window(org.get((nm, q), {}), (since_d + dt.timedelta(days=1)).isoformat(),
                                                       y.isoformat())
                        if nd < ORG_CHECK_DAYS:
                            continue                   # «Джем» за эти дни ещё не собран — ждём
                        per_day = allo / nd
                        if per_day < rec["base"] * ORG_RETURN_DROP or (mpos and mpos > ORG_TOP_POS * 1.5):
                            rem_org.append(q)
                            org_notes.append(f"      − {nm:>11} {cut(q, 40):40} органика не удержала: заказов в день "
                                             f"{per_day:.1f} против {rec['base']:.1f} до минуса" +
                                             (f", позиция {mpos:.0f}" if mpos else "") + " — возвращаю в рекламу")
                    cs = cl.get((cid, nm), {})
                    for q, s in sorted(cs.items(), key=lambda x: -x[1]["spend"]):
                        if q in cur_set or q in add_auto or protected(q) or negative(q) or \
                                q in planned_cluster.get((cid, nm), set()) or q in route_want.get(nm, set()):
                            continue
                        allo, ado, mpos, nd = org_window(org.get((nm, q.strip().lower()), {}), d1, y.isoformat())
                        if nd < ORG_DAYS // 2 or allo < ORG_MIN_ORDERS or not mpos or mpos > ORG_TOP_POS:
                            continue
                        if ado <= allo * ORG_AD_SHARE and added_total < MINUS_MAX_PER_RUN:
                            add_org.append(q)
                            added_total += 1
                            org_notes.append(f"      + {nm:>11} {cut(q, 40):40} в органике: из {allo:.0f} заказов по запросу за "
                                             f"{nd} дн. реклама дала {ado:.0f} ({ado / allo:.0%}), позиция {mpos:.0f} — "
                                             f"реклама не нужна, экономия ≈{fm_rub(s['spend'] / CLUSTER_DAYS * 7)} ₽/нед")
                if add_auto or add_route or rem_route or rem_auto or add_org or rem_org:
                    new = [q for q in current if q not in rem_route and q not in rem_auto and q not in rem_org] \
                        + add_route + add_auto + add_org
                    if len(new) > 1000:
                        log(f"    ! У товара {nm} больше 1000 минус-фраз — новые не добавляю.")
                        continue
                    plan["minus"].append({"cabinet": cab, "advert_id": cid, "nm_id": nm, "old": current, "new": new,
                                          "add_auto": add_auto, "add_route": add_route, "rem_route": rem_route,
                                          "rem_auto": rem_auto, "add_org": add_org, "rem_org": rem_org,
                                          "org_base": {q: org_window(org.get((nm, q.strip().lower()), {}),
                                                                     d1, y.isoformat())[0] / ORG_DAYS for q in add_org}
                                          if add_org else {},
                                          "stats": {q: cl.get((cid, nm), {}).get(q) for q in add_auto},
                                          "why": {q: why_q.get(q) for q in add_auto} if r["auto_minus"] else {}})
            items = [m for m in plan["minus"] if m["advert_id"] == cid]
            if items:
                log("    Минус-фразы:")
                for m in items:
                    for q in m["add_auto"]:
                        s = m["stats"].get(q) or {}
                        log(f"      + {m['nm_id']:>11} {cut(q, 40):40} " + ((m.get("why") or {}).get(q) or
                            f"{int(s.get('clicks', 0))} кл., 0 заказов, {s.get('spend', 0):,.0f} ₽".replace(",", " ")))
                    for q in m["add_route"]:
                        log(f"      + {m['nm_id']:>11} {cut(q, 40):40} передан в «{r['route_from']}»")
                    for q in m["rem_route"]:
                        log(f"      − {m['nm_id']:>11} {cut(q, 40):40} возвращён (в «{r['route_from']}» ставки больше нет)")
                    for q in m.get("rem_auto") or []:
                        m_since = ((state.get("minus_auto_at") or {}).get(pk(cid, m["nm_id"])) or {}).get(q, "?")
                        log(f"      − {m['nm_id']:>11} {cut(q, 40):40} возвращён на перепроверку (в минусе с {m_since}; "
                            "снова не окупится — уйдёт в минус)")
            elif cur_minus is not None:
                log("    Минус-фразы: добавлять нечего.")
            for note in held_notes:
                log(note)
            if org_notes:
                log("    Контроль органики (по «Джему»: все заказы по запросу против заказов с рекламы):")
                for note in org_notes:
                    log(note)

        # ---- дневной лимит ----
        limit_need = 0     # ₽: WB не ставит дневной лимит больше бюджета кампании («insufficient campaign budget»)
        if r["daily_limit"]:
            if pay != "cpc":
                log(f"    Дневной лимит {r['daily_limit']} ₽: у кампаний за показы его держит часовая проверка — пауза, "
                    "когда потрачен, запуск утром"
                    + (f" (сегодня потрачено {fm_rub(cpm_spent[cid])} ₽)." if cid in cpm_spent else "."))
            else:
                if lims_all is None:
                    log(f"    ! {lim_err}")
                    lim = None
                else:
                    lim = lims_all.get(cid) or {}
                if lim is not None:
                    want = int(r["daily_limit"] * 100)
                    min_dl = int(cfg.get("minDailyLimit") or 0)
                    if want < min_dl:
                        log(f"    Лимит {r['daily_limit']} ₽ меньше минимального WB ({rub(min_dl)} ₽): в WB ставлю минимальный, "
                            f"а свои {r['daily_limit']} ₽ в сутки держит часовая проверка — пауза, когда потрачены, запуск утром.")
                        want = min_dl
                    req = lim.get("requiredLimit")
                    note = f" WB рекомендует не меньше {rub(req)} ₽ при текущих ставках." if req and want < req else ""
                    same = lim.get("enabled") and lim.get("dailyLimit") == want and bool(lim.get("carryOverEnabled")) == bool(r["carry_over"])
                    log(f"    Дневной лимит: сейчас {rub(lim.get('dailyLimit')) if lim.get('enabled') else 'выключен'}"
                        f"{', потрачено сегодня ' + rub(lim.get('spentToday')) if lim.get('spentToday') is not None else ''}"
                        f" → {'без изменений' if same else rub(want) + ' ₽'}.{note}")
                    if not same:
                        limit_need = want / 100
                        plan["limits"].append({"cabinet": cab, "advert_id": cid,
                                               "old": {"enabled": bool(lim.get("enabled")), "dailyLimit": lim.get("dailyLimit"),
                                                       "carryOverEnabled": bool(lim.get("carryOverEnabled"))},
                                               "new": {"enabled": True, "dailyLimit": want, "carryOverEnabled": bool(r["carry_over"])}})
        # ---- автопополнение бюджета ----
        if r.get("topup_below"):
            can_start = bool(r.get("start_after_topup") or r.get("_resume") or str(cid) in (state.get("managed") or {})
                             or camps[cid].get("_status") == 9)
            TOPUP_RULES[str(cid)] = {"cabinet": cab, "name": name, "below": r["topup_below"], "amount": r["topup_amount"],
                                     "day_cap": r["topup_day_cap"], "week_cap": r["topup_week_cap"],
                                     "source": r["topup_source"], "start": can_start, "cab_cap": cab_cap,
                                     "min_deposit": min_dep}
            note_broken_topups(state, cid, name)
            bud = buds_all.get(cid)
            short = max(limit_need - (bud or 0), 0)       # сколько не хватает бюджета, чтобы WB принял новый лимит
            if bud is None:
                log("    ! Бюджет кампании не получен — пополнение пропускаю.")
            elif on_hold(cid) and a.get("_status") != 9:
                log(f"    В стоп-листе {HOLD_FILE.name} — не пополняю и не запускаю.")
                for k in ("limit_paused", "pace_paused"):
                    (state.get(k) or {}).pop(str(cid), None)
            elif a.get("_status") == 11 and limit_paused(state, cid) == dt.date.today().isoformat():
                log("    На паузе по дневному лимиту до утра — не пополняю и не запускаю.")
            elif a.get("_status") == 11 and pace_paused(state, cid) and bud >= r["topup_below"]:
                log(f"    На паузе по графику расхода (в бюджете {fm_rub(bud)} ₽) — запустит проверка лимита, когда спрос "
                    "по часам догонит.")
            elif a.get("_status") == 11 and limit_paused(state, cid) and bud >= PAUSED_MONEY_BELOW:
                log(f"    Вчера встала на паузу по дневному лимиту (в бюджете {fm_rub(bud)} ₽) — запустить.")
                plan["topup"].append({"cabinet": cab, "advert_id": cid, "name": name, "amount": 0,
                                      "source": r["topup_source"], "budget_before": bud, "start": True})
            elif a.get("_status") in (4, 11) and bud >= PAUSED_MONEY_BELOW and start_retry(state, cid):
                log(f"    Кампанию пополнили {start_pending(state, cid)}, а запуск тогда не прошёл (в бюджете {fm_rub(bud)} ₽) — "
                    "запустить снова, без пополнения.")
                plan["topup"].append({"cabinet": cab, "advert_id": cid, "name": name, "amount": 0,
                                      "source": r["topup_source"], "budget_before": bud, "start": True})
            elif a.get("_status") == 11 and bud >= PAUSED_MONEY_BELOW and not pace_paused(state, cid):
                if start_pending(state, cid):
                    log(f"    ! Кампанию пополнили {start_pending(state, cid)}, но запустить так и не удалось (в бюджете "
                        f"{fm_rub(bud)} ₽) — запустите её в кабинете WB вручную.")
                else:
                    log(f"    Кампания на паузе, в бюджете {fm_rub(bud)} ₽ — паузу, видимо, поставили вы: не пополняю и не запускаю.")
            elif short > 0:
                week = topped_week(state, cid)
                today_ = topped_week(state, cid, days=1)
                need = int(math.ceil(short / 100) * 100)
                amount = deposit_amount(max(need, r["topup_amount"] if bud < r["topup_below"] else 0),
                                        r["topup_day_cap"], today_, r["topup_week_cap"], week,
                                        cab_left[0] if cab_cap else None, min_dep)
                if amount < need:
                    newlim = int(max(bud + max(amount, 0), 0) // 100 * 100)
                    old_l = plan["limits"][-1]["old"]
                    cur_l = (old_l.get("dailyLimit") or 0) if old_l.get("enabled") else 0
                    floor_l = max(cur_l, int(cfg.get("minDailyLimit") or 0), 100 * 100)
                    if newlim * 100 <= floor_l:
                        plan["limits"].pop()
                        log(f"    ! Бюджет {fm_rub(bud)} ₽ меньше нового лимита {limit_need:.0f} ₽, а пополнить можно только на "
                            f"{max(amount, 0)} ₽ (потолки) — лимит не меняю ("
                            + (f"остаётся {rub(cur_l)} ₽" if cur_l else "выключен") + "), новый поставлю при следующем запуске.")
                    else:
                        plan["limits"][-1]["new"]["dailyLimit"] = newlim * 100
                        log(f"    ! Бюджет {fm_rub(bud)} ₽ меньше нового лимита {limit_need:.0f} ₽, а пополнить можно только на "
                            f"{max(amount, 0)} ₽ (потолки) — ставлю лимит {newlim} ₽, полный поставлю при следующем запуске.")
                if amount >= min_dep:
                    start = can_start and a.get("_status") in (4, 11) and not pace_paused(state, cid)
                    log(f"    Бюджет: {fm_rub(bud)} ₽ — WB не ставит дневной лимит больше бюджета → пополнить на {amount} ₽ "
                        f"из «{SOURCE_RU[r['topup_source']]}» (сегодня уже {fm_rub(today_)} из {r['topup_day_cap']} ₽, "
                        f"за 7 дней {fm_rub(week)} из {r['topup_week_cap']} ₽)" + (" и запустить кампанию" if start else "") + ".")
                    plan["topup"].append({"cabinet": cab, "advert_id": cid, "name": name, "amount": amount,
                                          "source": r["topup_source"], "budget_before": bud, "start": start,
                                          "enough": max(limit_need, r["topup_below"])})
                    if cab_cap:
                        cab_left[0] -= amount
            elif bud >= r["topup_below"]:
                log(f"    Бюджет: {fm_rub(bud)} ₽ — пополнять не нужно (порог {r['topup_below']} ₽).")
            else:
                week = topped_week(state, cid)
                today_ = topped_week(state, cid, days=1)
                amount = deposit_amount(r["topup_amount"], r["topup_day_cap"], today_, r["topup_week_cap"], week,
                                        cab_left[0] if cab_cap else None, min_dep)
                if not amount:       # до потолка осталось меньше 1 000 ₽ — WB такое пополнение не примет
                    log(f"    ! Бюджет {fm_rub(bud)} ₽ ниже порога, но до потолка меньше {min_dep:.0f} ₽ (меньше WB не принимает): "
                        f"сегодня пополнено на {fm_rub(today_)} "
                        f"из {r['topup_day_cap']} ₽, за 7 дней на {fm_rub(week)} из {r['topup_week_cap']} ₽"
                        + (f", недельный бюджет кабинета: осталось {max(cab_left[0], 0):.0f} из {cab_cap} ₽" if cab_cap else "")
                        + free_note(state, cid, r["topup_week_cap"], week, min_dep) + " — не пополняю.")
                else:
                    start = can_start and a.get("_status") in (4, 11) and not pace_paused(state, cid)
                    log(f"    Бюджет: {fm_rub(bud)} ₽ < {r['topup_below']} ₽ → пополнить на {amount} ₽ из «{SOURCE_RU[r['topup_source']]}» "
                        f"(сегодня уже {fm_rub(today_)} из {r['topup_day_cap']} ₽, за 7 дней {fm_rub(week)} из {r['topup_week_cap']} ₽)" + (" и запустить кампанию" if start else "") + ".")
                    plan["topup"].append({"cabinet": cab, "advert_id": cid, "name": name, "amount": amount,
                                          "source": r["topup_source"], "budget_before": bud, "start": start,
                                          "enough": r["topup_below"]})
                    if cab_cap:
                        cab_left[0] -= amount
    if check_card:
        log("\n  Проверить карточку (много кликов, ни одного заказа — дело скорее в фото, цене, размерах или отзывах, "
            "а не в ставке; ставку таким товарам не повышаю):")
        for c in check_card:
            log(f"    • {c}")
    _rec_main.add(cab)                  # фоновая подгрузка рекомендаций этого кабинета больше не нужна
    return plan


# ============================ запись в WB ============================

def apply_bids(token, items, key):
    by_c = defaultdict(list)
    for it in items:
        if it.get(key) is not None:
            by_c[it["advert_id"]].append({"nm_id": it["nm_id"], "bid_kopecks": int(it[key]), "placement": it["placement"]})
    reqs = [{"advert_id": aid, "nm_bids": part} for aid, nmb in by_c.items() for part in chunks(nmb, 50)]
    ok = 0
    for part in chunks(reqs, 50):
        pace(token, "patch", 0.3)
        try:
            data = call("PATCH", ADV + "/api/advert/v1/bids", token, body={"bids": part}, what="Ставки товаров") or {}
            ok += sum(len(b.get("nm_bids") or []) for b in data.get("bids") or [])
        except ApiError as e:
            log(f"  ✗ {e}")
    return ok


def apply_cluster_bids(token, items, key):
    to_set = [i for i in items if i.get(key) is not None]
    to_del = [i for i in items if i.get(key) is None]
    ok = 0
    for part in chunks(to_set, 100):
        pace(token, "nqbids", 0.6)
        try:
            data = call("POST", ADV + "/api/advert/v1/normquery/bids", token, what="Ставки запросов", body={"bids": [
                {"advertId": i["advert_id"], "nmId": i["nm_id"], "normQuery": i["query"], "bidMinorUnits": int(i[key])}
                for i in part]}) or {}
            ok += len(data.get("success") or [])
            for f in data.get("failed") or []:
                log(f"  ✗ запрос «{f.get('normQuery')}» ({f.get('nmId')}): {f.get('reason')}")
        except ApiError as e:
            if e.status in (401, 403):   # метод только для Персонального токена — пробуем старый
                try:
                    call("POST", ADV + "/adv/v0/normquery/bids", token, what="Ставки запросов (руб.)", body={"bids": [
                        {"advert_id": i["advert_id"], "nm_id": i["nm_id"], "norm_query": i["query"],
                         "bid": int(round(i[key] / 100))} for i in part]})
                    ok += len(part)
                except ApiError as e2:
                    log(f"  ✗ {e2}")
            else:
                log(f"  ✗ {e}")
    for part in chunks(to_del, 100):
        pace(token, "nqbids", 0.6)
        try:
            call("DELETE", ADV + "/adv/v0/normquery/bids", token, what="Удаление ставок запросов", body={"bids": [
                {"advert_id": i["advert_id"], "nm_id": i["nm_id"], "norm_query": i["query"]} for i in part]})
            ok += len(part)
        except ApiError as e:
            log(f"  ✗ {e}")
    return ok


def apply_minus(token, items, key):
    """Возвращает список товаров, которым WB принял минус-фразы."""
    done = []
    for i in items:
        pace(token, "nq", 0.25)
        try:
            call("POST", ADV + "/adv/v0/normquery/set-minus", token, what="Минус-фразы",
                 body={"advert_id": i["advert_id"], "nm_id": i["nm_id"], "norm_queries": i[key]})
            done.append(i)
        except ApiError as e:
            log(f"  ✗ {e}")
    return done


def apply_limits(token, items, key, cfg_min=0):
    ok = 0
    for i in items:
        v = i[key]
        body = {"advertIds": [i["advert_id"]], "enabled": bool(v["enabled"])}
        if v["enabled"]:
            body["dailyLimit"] = int(max(v["dailyLimit"] or 0, 1000))
            body["carryOverEnabled"] = bool(v["carryOverEnabled"])
        pace(token, "limits", 12.5)
        try:
            data = call("PUT", ADV + "/api/advert/v0/daily-limits", token, body=body, what="Дневной лимит") or {}
            ok += 1
            for a in data.get("adverts") or []:
                if v["enabled"] and a.get("belowMinLimit"):
                    log(f"  ! Лимит кампании {a.get('advertId')} ниже рекомендуемого WB ({rub(a.get('requiredLimit'))} ₽) — "
                        "кампания может останавливаться раньше вечера.")
        except ApiError as e:
            hint = (" — в бюджете кампании меньше нового лимита; следующий запуск пополнит бюджет и поставит лимит"
                    if "insufficient campaign budget" in str(e) else "")
            log(f"  ✗ Кампания {i['advert_id']}: {e}{hint}")
            continue
        if v["enabled"]:
            # Спецификация WB не говорит прямо, в каких единицах PUT принимает лимит (пишем в копейках, как его отдаёт GET).
            # Перечитываем и сравниваем: если WB понял иначе — громкое предупреждение, лимит проверить в кабинете.
            try:
                got = (get_limits(token, [i["advert_id"]]).get(i["advert_id"]) or {}).get("dailyLimit")
            except ApiError as e:
                log(f"  ! Не удалось перечитать лимит кампании {i['advert_id']}: {e}")
                continue
            if got is not None and int(got) != body["dailyLimit"]:
                log(f"  ✗✗ ВНИМАНИЕ: кампании {i['advert_id']} записан лимит {rub(body['dailyLimit'])} ₽, а WB показывает "
                    f"{rub(got)} ₽. Проверьте лимит в кабинете WB и сообщите Claude — единицы лимита поняты неверно.")
            else:
                log(f"  ✓ Лимит кампании {i['advert_id']}: {rub(got)} ₽ — WB подтвердил.")
    return ok


def update_state(state, minus_items, reverse=False):
    for m in minus_items:
        key = pk(m["advert_id"], m["nm_id"])
        auto = set(state["minus_auto"].get(key, []))
        route = set(state["minus_route"].get(key, []))
        at = state.setdefault("minus_auto_at", {}).setdefault(key, {})       # когда запрос ушёл в автоминус
        tries = state.setdefault("minus_retests", {}).setdefault(key, {})   # сколько раз возвращали на перепроверку
        rem_auto = set(m.get("rem_auto") or [])
        om = state.setdefault("organic_minus", {}).setdefault(key, {})
        if not reverse:
            for q in m.get("add_org") or []:
                om[q] = {"date": dt.date.today().isoformat(), "base": (m.get("org_base") or {}).get(q, 0)}
            for q in m.get("rem_org") or []:
                om.pop(q, None)
        else:
            for q in m.get("add_org") or []:
                om.pop(q, None)
        if not reverse:
            auto = (auto | set(m["add_auto"])) - rem_auto
            route = (route | set(m["add_route"])) - set(m["rem_route"])
            for q in m["add_auto"]:
                at[q] = dt.date.today().isoformat()
            for q in rem_auto:
                at.pop(q, None)
                tries[q] = tries.get(q, 0) + 1
        else:
            auto = (auto - set(m["add_auto"])) | rem_auto
            route = (route - set(m["add_route"])) | set(m["rem_route"])
            for q in m["add_auto"]:
                at.pop(q, None)
            for q in rem_auto:
                at[q] = dt.date.today().isoformat()
                tries[q] = max(tries.get(q, 1) - 1, 0)
        state["minus_auto"][key] = sorted(auto)
        state["minus_route"][key] = sorted(route)
    save_state(state)


SOURCE_TYPE = {"account": 0, "balance": 1, "bonus": 3}
SOURCE_RU = {"account": "Счёт", "balance": "Баланс (взаиморасчёт)", "bonus": "Бонусы"}


def fm_rub(v):
    return f"{v:,.0f}".replace(",", " ")


def get_budgets(token, ids):
    res = {}
    for part in chunks(ids, 50):
        pace(token, "budget", 3.1)
        try:
            d = call("POST", ADV + "/api/advert/v2/budget", token, body={"advertIds": part}, what="Бюджеты") or {}
        except ApiError as e:
            log(f"    ! {e}")
            continue
        for b in d.get("adverts") or []:
            if b.get("total") is not None:
                res[b.get("advertId")] = num(b["total"])
    return res


def prune_state(state, keep_days=35):
    """Файл состояния не должен расти бесконечно: пополнения старше 35 дней (потолки смотрят 7 дней) и кампании,
    которыми автопилот не управлял больше 35 дней, — убрать."""
    since = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()
    tops = state.get("topups") or {}
    for cid in list(tops):
        tops[cid] = [t for t in tops[cid] if t.get("date", "") >= since]
        if not tops[cid]:
            del tops[cid]
    man = state.get("managed") or {}
    for cid in [c for c, m in man.items() if (m.get("seen") or "") < since]:
        del man[cid]
    pend = state.get("start_pending") or {}
    for cid in [c for c, d in pend.items() if d < since]:
        del pend[cid]
    lp = state.get("limit_paused") or {}
    for cid in [c for c, v in lp.items() if (v.get("date") or "") < since]:
        del lp[cid]
    pp_ = state.get("pace_paused") or {}
    for cid in [c for c, v in pp_.items() if (v.get("date") or "") < since]:
        del pp_[cid]
    for k in ("pace_count", "cpm_seen", "pace_rate"):
        d_ = state.get(k) or {}
        for cid in [c for c, v in d_.items() if str(v.get("date") or v.get("at") or "")[:10] < since]:
            del d_[cid]
    for k in ("day_budget", "cpm_spent"):              # служебные замеры прошлых дней не нужны
        d = state.get(k) or {}
        today = dt.date.today().isoformat()
        for cid in [c for c, v in d.items() if str(v.get("date") or v.get("at") or "")[:10] < today]:
            del d[cid]


MIN_DEPOSIT = 1000   # WB не принимает пополнение бюджета меньше 1 000 ₽ («minimum deposit amount is 1000», 14.10.2026)


def deposit_amount(want, day_cap, today_, week_cap, week, cab_left=None, min_dep=MIN_DEPOSIT):
    """Сколько пополнить (решение пользователя 14.10.2026): не меньше MIN_DEPOSIT; недельный потолок кампании и кабинета —
    строго; дневной потолок меньше 1 000 ₽ можно превысить до 1 000. Возвращает 0, если пополнять нельзя."""
    room = min(max(day_cap, min_dep) - today_, week_cap - week)
    if cab_left is not None:
        room = min(room, cab_left)
    amount = min(max(want, min_dep), room)
    return int(math.ceil(amount)) if amount >= min_dep else 0


def pace_hour(now=None):
    """Спрос этого часа (% заказов дня, будни / выходные) — расход кампании за показы идёт за ним."""
    now = now or dt.datetime.now()
    return (PACE_WE if now.weekday() >= 5 else PACE_WD)[now.hour]


def pace_target(now=None):
    """Доля дневного спроса, которая обычно набирается к этому моменту (будни / выходные)."""
    now = now or dt.datetime.now()
    prof = PACE_WE if now.weekday() >= 5 else PACE_WD
    tot = sum(prof)
    h = now.hour
    return (sum(prof[:h]) + prof[h] * now.minute / 60) / tot


PACE_PAUSE_STEPS = [(8, 0.25), (6, 0.40), (4, 0.55), (2, 0.70)]   # пауз «по графику» за день → ставка не выше ×
                                                                  # (×0,25 — по сути минимальная ставка WB; решение 15.10.2026)
PACE_CHECK_MIN = 5          # как часто wb_guard.py --limits проверяет расход (мин) — «вспышка» = скорость × это время
PACE_BIG_BURST = 0.2        # «вспышка» за PACE_CHECK_MIN от этой доли лимита и больше считается за 2 паузы, от 2× — за 3…


def pace_factor(cid, spent, limit, now=None, state=None):
    """(множитель ставки, пояснение). spent и limit — ₽ за сегодня. 1.0 — не вмешиваться. state — чтобы учесть паузы
    «по графику» за сегодня: кампания, которая за 5 минут тратит большую долю лимита, после 2 / 4 / 6 / 8 таких пауз идёт
    со ставкой ×0,7 / ×0,55 / ×0,4 / ×0,25 (не ниже минимальной WB) — меньше «вспышки», показы дольше; крупная «вспышка»
    считается за несколько пауз (pace_weight); на следующий день — снова от рекомендации."""
    n = pace_pauses_today(state, cid) if state is not None else 0
    for k, m_ in PACE_PAUSE_STEPS:
        if n >= k:
            low = " (не ниже минимальной WB)" if m_ <= 0.25 else ""
            return m_, f"{n} пауз по графику за день (тратит быстрее спроса) — ставка ×{m_}{low}, чтобы показы шли дольше"
    if not PACING or not limit or spent is None:
        return 1.0, ""
    until = PACING_SKIP_UNTIL.get(str(cid))
    if until and dt.date.today().isoformat() <= until:
        return 1.0, ""
    tgt = pace_target(now)
    if tgt < PACE_MIN_SHARE or spent >= limit * 0.97:
        return 1.0, ""
    frac = spent / limit
    if spent / tgt <= limit * PACE_LIMITED:
        return 1.0, ""                                   # до конца дня денег хватает
    ahead = frac - tgt
    for gap, m in PACE_STEPS:
        if ahead > gap:
            return m, (f"потрачено {frac:.0%} лимита при {tgt:.0%} дневного спроса к этому часу — ставка ×{m}, "
                       "чтобы деньги дошли до вечера")
    return 1.0, ""


def paced(nb, m, mn, step, cur=None, prev_m=1.0):
    """Ставка с поправкой на график расхода (не ниже минимальной WB). Пока расход опережает график, ставка не растёт
    выше текущей (cur) — кроме ослабления поправки за день (была ×0,7, стала ×0,85: prev_m < m)."""
    if nb is None or m >= 1:
        return nb
    if not mn:
        m = max(m, 0.4)                  # минимальная ставка WB неизвестна — глубже ×0,4 не опускаю, чтобы WB не отказал
    res = max(int(mn or 0), int(math.floor(nb * m / step) * step))
    if cur and res > cur and m <= prev_m:
        res = max(int(cur), int(mn or 0))
    return res


def week_free_date(state, cid, week_cap, min_dep=MIN_DEPOSIT):
    """Когда скользящая неделя освободит под пополнение не меньше min_dep (по уже сделанным пополнениям кампании)."""
    today = dt.date.today()
    deps = []
    for t in (state.get("topups") or {}).get(str(cid), []):
        try:
            deps.append((dt.date.fromisoformat(t["date"]), t["sum"]))
        except (KeyError, TypeError, ValueError):
            continue
    for k in range(0, 8):
        d = today + dt.timedelta(days=k)
        if week_cap - sum(s for x, s in deps if 0 <= (d - x).days < 7) >= min_dep:
            return d
    return None


def free_note(state, cid, week_cap, week, min_dep):
    """«; по недельному потолку — с ДД.ММ» — если мешает именно недельный потолок кампании."""
    if week_cap - week >= min_dep:
        return ""
    d = week_free_date(state, cid, week_cap, min_dep)
    return f"; по недельному потолку кампании можно будет с {d:%d.%m}" if d else ""


def topped_week(state, cid, days=7):
    since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    return sum(t["sum"] for t in state.get("topups", {}).get(str(cid), []) if t["date"] >= since)


def topped_cabinet(state, cab, known_ids, days=7):
    """Сколько пополнено за 7 дней по ВСЕМ кампаниям кабинета — и по тем, что уже сняты с правил или на паузе.
    У старых записей кабинета нет — их узнаём по известным кампаниям кабинета (known_ids)."""
    since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    known = {str(c) for c in known_ids}
    known |= {c for c, t in (state.get("topup_rules") or {}).items() if t.get("cabinet") == cab}
    known |= {c for c, m in (state.get("managed") or {}).items() if m.get("cabinet") == cab}
    total = 0
    for cid, recs in (state.get("topups") or {}).items():
        for t in recs:
            if t["date"] >= since and (t.get("cabinet") == cab or (not t.get("cabinet") and cid in known)):
                total += t["sum"]
    return total


START_PENDING_DAYS = 2   # пополнили, а запустить не удалось: столько дней пробовать запустить снова (без пополнения)


def start_pending(state, cid):
    """Дата, когда кампанию пополнили, а запустить не удалось, или None."""
    return (state.get("start_pending") or {}).get(str(cid))


def start_retry(state, cid):
    """Кампания на паузе с деньгами — это «паузу поставили вы», КРОМЕ случая, когда мы её пополнили, а запуск не прошёл
    (сбой WB): тогда её нужно запустить снова, иначе 1 000 ₽ так и лежат в бюджете остановленной кампании."""
    d = start_pending(state, cid)
    return bool(d) and d >= (dt.date.today() - dt.timedelta(days=START_PENDING_DAYS)).isoformat()


def note_broken_topups(state, cid, name):
    """Пополнение, которое оборвалось на середине (процесс остановили во время запроса): деньги могли уйти. Оно уже
    учтено в потолках; сообщаем один раз и помечаем как «неясное»."""
    for t in (state.get("topups") or {}).get(str(cid), []):
        if t.pop("pending", None):
            t["unclear"] = True
            log(f"    ? «{name}»: пополнение на {t['sum']} ₽ {t.get('at') or t['date']} оборвалось на середине — считаю, что "
                "прошло (учтено в потолках). Проверьте бюджет кампании в кабинете WB.")


def live_rule(cab, cid, camps):
    """Правило кампании по RULES прямо сейчас (с DEFAULTS и «*»), или None — кампании в правилах нет."""
    rules = RULES.get(cab) or {}
    today = dt.date.today().isoformat()
    for k, v in rules.items():
        if k == "*" or k.startswith("_") or (v.get("until") and today > v["until"]):
            continue
        if cid in find(camps, k):
            return {**DEFAULTS, **rules.get("*", {}), **v}
    if "*" in rules and cid not in {c for k in rules.get("_except") or [] for c in find(camps, k)}:
        return dict(DEFAULTS, **rules["*"])
    return None


def live_topup(cab, cid, camps):
    """Правила пополнения кампании по RULES в этом файле прямо сейчас. Страж берёт правила из последнего --apply
    автопилота (до 14 ч назад) — уменьшенный вами потолок без этой сверки до того бы не действовал.
    None — кампании в правилах больше нет или пополнение у неё выключено."""
    rules = RULES.get(cab) or {}
    r = live_rule(cab, cid, camps)
    if not r or not r.get("topup_below"):
        return None
    return {"below": r["topup_below"], "amount": r["topup_amount"], "day_cap": r["topup_day_cap"],
            "week_cap": r["topup_week_cap"], "cab_cap": rules.get("_week_budget")}


CPM_LIMIT_FULL = 0.97     # кампания за показы потратила столько от дневного лимита — пауза до утра
CPM_OVERSHOOT = 1.15      # …или по темпу последнего часа к следующей проверке потратит больше лимита × 1,15 — пауза сейчас


def spent_today(token, ids):
    """Потрачено сегодня, ₽: {кампания: сумма}. У кампаний за показы дневного лимита WB нет — держим его сами (пауза),
    поэтому расход нужен и для них: сначала «дневные лимиты» (spentToday, если WB отдаёт его и для показов), иначе
    статистика кампании за сегодня. Не узнали — кампании нет в ответе (тогда ничего не делаем)."""
    res = {}
    if not ids:
        return res
    try:
        for cid, a in get_limits(token, ids).items():
            if a.get("spentToday") is not None:
                res[cid] = num(a["spentToday"]) / 100
    except ApiError:
        pass
    rest = [c for c in ids if c not in res]
    today = dt.date.today().isoformat()
    for part in chunks(rest, 50):
        pace(token, "fullstats", 21)
        try:
            data = call("GET", ADV + "/adv/v3/fullstats", token, what="Статистика за сегодня",
                        params={"ids": ",".join(map(str, part)), "beginDate": today, "endDate": today})
        except ApiError as e:
            log(f"  ! {e} — расход за сегодня у кампаний за показы неизвестен, дневной лимит не проверяю.")
            continue
        for item in data or []:
            s = 0.0
            for day in item.get("days") or []:
                for app in day.get("apps") or []:
                    for n in app.get("nms") or []:
                        s += num(n.get("sum"))
            res[item.get("advertId")] = s
        for cid in part:                       # статистика есть, а кампании в ней нет — сегодня показов не было
            if data is not None and cid not in res:
                res[cid] = 0.0
    return res


def limit_paused(state, cid):
    """Дата, когда кампанию за показы поставили на паузу по дневному лимиту (или None)."""
    return ((state.get("limit_paused") or {}).get(str(cid)) or {}).get("date")


# Свой дневной лимит растягивается по дню (решение пользователя 15.10.2026, вариант 3): утром 15.10 кампании за показы
# тратили 1 000 ₽ меньше чем за час при лимитах 150–600 ₽. Расход сверяется с графиком спроса по часам (PACE_WD/PACE_WE,
# доля дневных заказов к этому часу): обогнал график больше чем на PACE_MARGIN лимита — короткая пауза («по графику»),
# график догнал — запуск. Проверяет каждые 5 мин wb_guard.py --limits (и часовая проверка). Ставку это не меняет.
PACE_SPREAD = True
PACE_MARGIN = 0.08
PACE_NIGHT = (23.5, 7.0)   # с 23:30 до 7:00 такие кампании стоят (пауза «по графику»): ночью заказов ≈9% дневных, а утром
                           # 15.10 «2101 показы» к 7:40 уже потратила 179 из 300 ₽; проверки ночью нет — запуск после 7:00


def pace_night(now=None):
    now = now or dt.datetime.now()
    h = now.hour + now.minute / 60
    return h >= PACE_NIGHT[0] or h < PACE_NIGHT[1]


def pace_pauses_today(state, cid):
    c = (state.get("pace_count") or {}).get(str(cid)) or {}
    return c.get("n", 0) if c.get("date") == dt.date.today().isoformat() else 0


def pace_m_now(state, cid):
    """Поправка ставки по графику, которую сегодня последней поставили автопилот или часовая проверка (1.0 — нет)."""
    p = (state.get("pacing") or {}).get(str(cid)) or {}
    return p.get("m", 1.0) if p.get("date") == dt.date.today().isoformat() else 1.0


def pace_rate(state, cid):
    """Сколько ₽ в минуту кампания тратит сейчас, когда работает (по частым проверкам), или None. Скорость замерена в
    другой час и при другой поправке ставки — пересчитываю: по спросу часа (вечером показов вдвое больше, чем утром) и по
    ставке с запасом: ниже — пропорционально (на деле расход падает сильнее: хуже место — меньше показов), выше — по
    квадрату (дороже показ и больше показов)."""
    r = (state.get("pace_rate") or {}).get(str(cid)) or {}
    if r.get("rate") is None:
        return None
    k = pace_m_now(state, cid) / (r.get("m") or 1.0)
    hk = min(3.0, max(0.25, pace_hour() / r["h"])) if r.get("h") else 1.0
    return round(r["rate"] * (k if k < 1 else k * k) * hk, 2)


def note_spend(state, cid, spent, active):
    """Запомнить замер расхода; если кампания работала с прошлого замера (2–20 мин назад) — обновить скорость расхода."""
    now = dt.datetime.now()
    seen = state.setdefault("cpm_seen", {})
    prev = seen.get(str(cid)) or {}
    try:
        t0 = dt.datetime.fromisoformat(prev.get("at") or "")
    except ValueError:
        t0 = None
    if active and prev.get("active") and t0 and t0.date() == now.date() and spent is not None:
        mins = (now - t0).total_seconds() / 60
        d = spent - (prev.get("spent") or 0)
        if 2 <= mins <= 20 and d > 0:
            old = pace_rate(state, cid)              # уже пересчитана к нынешним часу и ставке
            obs = d / mins
            state.setdefault("pace_rate", {})[str(cid)] = {"rate": round(obs if old is None else 0.5 * old + 0.5 * obs, 2),
                                                           "m": pace_m_now(state, cid), "h": pace_hour(now),
                                                           "at": now.isoformat(timespec="minutes")}
    seen[str(cid)] = {"at": now.isoformat(timespec="minutes"), "spent": spent, "active": bool(active)}


def pace_paused(state, cid):
    """Запись о паузе «по графику расхода» (наша, временная) или None."""
    return (state.get("pace_paused") or {}).get(str(cid))


def pace_spread(spent, limit, now=None):
    """"pause" — расход обогнал график спроса больше чем на PACE_MARGIN лимита; "resume" — график догнал; иначе None.
    Весь лимит потрачен — None: это пауза до утра (cpm_limit_check)."""
    if not PACE_SPREAD or spent is None or not limit or spent >= limit * CPM_LIMIT_FULL:
        return None
    if pace_night(now):
        return "pause"                    # ночью — стоять; запуск после 7:00 по графику
    tgt = pace_target(now)
    if spent > limit * (tgt + PACE_MARGIN):
        return "pause"
    if spent <= limit * tgt:
        return "resume"
    return None


def pace_weight(state, cid, limit):
    """Сколько «пауз» засчитать: кампания, которая за PACE_CHECK_MIN минут съедает большую долю лимита, быстрее доходит до
    сильного снижения ставки (иначе при одной огромной «вспышке» в день ставка не снижалась бы вовсе)."""
    rate = pace_rate(state, cid)
    if not rate or not limit:
        return 1
    return 1 + min(3, int(rate * PACE_CHECK_MIN / limit / PACE_BIG_BURST))


def pace_pause_campaign(token, i, state):
    """Пауза по графику расхода: до тех пор, пока спрос по часам не догонит потраченное (запускает wb_guard.py)."""
    pace(token, "start", 1.1)
    try:
        call("GET", ADV + "/adv/v0/pause", token, params={"id": i["advert_id"]}, what="Пауза кампании")
    except ApiError as e:
        log(f"  ✗ «{i['name']}»: пауза не встала — {e}")
        return False
    state.setdefault("pace_paused", {})[str(i["advert_id"])] = {
        "date": dt.date.today().isoformat(), "at": dt.datetime.now().strftime("%H:%M"),
        "spent": i.get("spent"), "limit": i.get("limit")}
    if not pace_night():                 # ночная пауза — не «вспышка»: в счёт снижения ставки не идёт
        cnt = state.setdefault("pace_count", {})
        c = cnt.get(str(i["advert_id"])) or {}
        cnt[str(i["advert_id"])] = {"date": dt.date.today().isoformat(),
                                    "n": (c.get("n", 0) if c.get("date") == dt.date.today().isoformat() else 0)
                                    + pace_weight(state, i["advert_id"], i.get("limit"))}
    save_state(state)
    log(f"  ⏸ «{i['name']}»: {i.get('why')} — пауза, запущу, когда спрос по часам догонит")
    return True


def cpm_limit_check(cid, spent, limit, state, now=None):
    """(пора ставить на паузу?, пояснение). Темп — по прошлому замеру этого дня (state["cpm_spent"])."""
    now = now or dt.datetime.now()
    if spent is None or not limit:
        return False, ""
    if spent >= limit * CPM_LIMIT_FULL:
        return True, f"потрачено {fm_rub(spent)} из {fm_rub(limit)} ₽ дневного лимита"
    prev = (state.get("cpm_spent") or {}).get(str(cid)) or {}
    try:
        t0 = dt.datetime.fromisoformat(prev.get("at") or "")
    except ValueError:
        t0 = None
    if t0 and t0.date() == now.date() and spent >= (prev.get("spent") or 0):
        hours = (now - t0).total_seconds() / 3600
        if 0.25 <= hours <= 3:
            rate = (spent - (prev.get("spent") or 0)) / hours
            if spent + rate * 1.0 > limit * CPM_OVERSHOOT:
                return True, (f"потрачено {fm_rub(spent)} из {fm_rub(limit)} ₽, тратит ≈{fm_rub(rate)} ₽/ч — до следующей "
                              "проверки ушла бы за лимит")
    return False, ""


def pause_campaign(token, i, state):
    """Пауза по дневному лимиту (кампании за показы). Запрос можно повторять — денег не тратит."""
    pace(token, "start", 1.1)
    try:
        call("GET", ADV + "/adv/v0/pause", token, params={"id": i["advert_id"]}, what="Пауза кампании")
    except ApiError as e:
        log(f"  ✗ «{i['name']}»: пауза не встала — {e}")
        return False
    state.setdefault("limit_paused", {})[str(i["advert_id"])] = {
        "date": dt.date.today().isoformat(), "at": dt.datetime.now().strftime("%H:%M"),
        "spent": i.get("spent"), "limit": i.get("limit")}
    save_state(state)
    log(f"  ⏸ «{i['name']}»: на паузе до утра — {i.get('why')}")
    return True


def on_hold(cid):
    """Кампания в стоп-листе wb_hold.txt (вписал пользователь): не запускать и не пополнять. Файл читается каждый раз —
    правка действует со следующей проверки, без перезапуска."""
    try:
        text = HOLD_FILE.read_text(encoding="utf-8-sig")
    except OSError:
        return False
    return str(cid) in {ln.split("#")[0].strip() for ln in text.splitlines()}


def start_campaign(token, i, state):
    """Запустить кампанию после пополнения. Запуск можно повторять (денег не тратит), поэтому запрос с повторами.
    Не вышло — запомнить: следующий запуск автопилота или часовой проверки попробует снова."""
    pend = state.setdefault("start_pending", {})
    if on_hold(i["advert_id"]):
        for k in ("limit_paused", "pace_paused"):        # пауза больше не наша — её держит пользователь
            (state.get(k) or {}).pop(str(i["advert_id"]), None)
        pend.pop(str(i["advert_id"]), None)
        save_state(state)
        log(f"  ⏹ «{i['name']}»: в стоп-листе {HOLD_FILE.name} — не запускаю (вернуть: убрать номер из файла и запустить "
            "кампанию в кабинете WB).")
        return False
    pace(token, "start", 1.1)
    try:
        call("GET", ADV + "/adv/v0/start", token, params={"id": i["advert_id"]}, what="Запуск кампании")
    except ApiError as e:
        pend.setdefault(str(i["advert_id"]), dt.date.today().isoformat())
        save_state(state)
        log(f"  ✗ «{i['name']}»: не запустилась — {e}. Попробую запустить снова при следующем запуске.")
        return False
    pend.pop(str(i["advert_id"]), None)
    resumed = (state.get("limit_paused") or {}).pop(str(i["advert_id"]), None)
    paced_ = (state.get("pace_paused") or {}).pop(str(i["advert_id"]), None)
    save_state(state)
    log(f"  ✓ «{i['name']}»: запущена" + (" после пополнения" if i["amount"] else
                                          " (новый день — дневной лимит снова доступен)" if resumed else
                                          " (спрос по часам догнал расход)" if paced_ else
                                          " (пополнена раньше, запуск тогда не прошёл)"))
    return True


def no_money(e):
    """Ответ WB «на балансе не хватает денег» (точный текст WB не задокументирован — смотрим по словам)."""
    t = str(e).lower()
    return isinstance(e, ApiError) and e.status in (400, 402, 403, 422) and any(
        w in t for w in ("недостат", "не хватает", "insufficient", "not enough", "баланс", "balance"))


def apply_topups(token, items, state):
    """Пополнить бюджеты. Деньги не возвращаются — отката нет.
      • Перед пополнением бюджет перечитывается: прочитанный при планировании мог устареть на 20–50 мин.
      • Запись о пополнении сохраняется ДО запроса (пометка pending): если процесс оборвётся посреди запроса, потолки
        её всё равно учтут. Отказ WB — запись удаляется; успех — пометка снимается.
      • Запрос не повторяется: если WB не ответил, смотрим, вырос ли бюджет.
      • amount = 0 — кампанию пополнили раньше, а запуск не прошёл: только запустить."""
    ok = 0
    tops = state.setdefault("topups", {})
    broke = set()                       # источники, на которых в этом запуске WB ответил «не хватает денег»

    def drop(cid, rec):
        lst = tops.get(str(cid), [])
        lst[:] = [x for x in lst if x is not rec]
        if not lst:
            tops.pop(str(cid), None)
        save_state(state)

    for i in items:
        cid = i["advert_id"]
        if not i["amount"]:
            if i.get("start"):
                start_campaign(token, i, state)
            continue
        if i["source"] in broke:
            continue                    # денег на этом балансе нет — остальные кампании кабинета не дёргаю
        fresh = get_budgets(token, [cid]).get(cid)
        if fresh is not None and i.get("enough") is not None and fresh >= i["enough"]:
            log(f"  «{i['name']}»: в бюджете уже {fm_rub(fresh)} ₽ (было {fm_rub(i.get('budget_before') or 0)} ₽ при расчёте) — "
                "пополнение не нужно.")
            continue
        before = fresh if fresh is not None else i.get("budget_before")
        rec = {"date": dt.date.today().isoformat(), "sum": i["amount"], "cabinet": i["cabinet"],
               "at": dt.datetime.now().strftime("%d.%m %H:%M"), "pending": True}
        tops.setdefault(str(cid), []).append(rec)
        save_state(state)              # не сохранилось (файл занят) — исключение, деньги не отправлены
        pace(token, "deposit", 1.1)
        try:
            res = call("POST", ADV + "/adv/v1/budget/deposit", token, params={"id": cid}, what="Пополнение бюджета",
                       body={"sum": int(i["amount"]), "type": SOURCE_TYPE[i["source"]], "return": True}, retry=False)
        except ApiError as e:
            if e.status != UNCLEAR and not (isinstance(e.status, int) and e.status >= 500):
                drop(cid, rec)
                log(f"  ✗ «{i['name']}»: пополнение не прошло — {e}")
                if no_money(e):
                    broke.add(i["source"])
                    rest = [x for x in items if x is not i and x["amount"] and x["source"] == i["source"]
                            and items.index(x) > items.index(i)]
                    log(f"  ✗✗ {i['cabinet']}: на «{SOURCE_RU.get(i['source'], i['source'])}» не хватает денег — пополните "
                        f"баланс в кабинете WB. Пополнения кабинета в этом запуске остановлены"
                        + (f" (ждут пополнения ещё кампаний: {len(rest)})" if rest else "") + "; попробую в следующий час.")
                    state.setdefault("no_money", {})[i["cabinet"]] = dt.datetime.now().isoformat(timespec="minutes")
                    save_state(state)
                continue
            # ответ потерялся — повторять нельзя (деньги ушли бы дважды); проверяем бюджет
            time.sleep(5)
            now = get_budgets(token, [cid]).get(cid)
            if now is not None and before is not None and now >= before + i["amount"] * 0.8:
                log(f"  ✓ «{i['name']}»: WB не ответил, но бюджет вырос {fm_rub(before)} → {fm_rub(now)} ₽ — пополнение прошло.")
                res = {"total": now}
            elif now is not None and before is not None:
                drop(cid, rec)
                log(f"  ✗ «{i['name']}»: WB не ответил, бюджет не вырос ({fm_rub(now)} ₽) — пополнение не прошло; "
                    "повторю при следующем запуске.")
                continue
            else:
                rec.pop("pending", None)
                rec["unclear"] = True
                save_state(state)
                log(f"  ? «{i['name']}»: WB не ответил и бюджет проверить не удалось — считаю, что +{i['amount']} ₽ прошло "
                    "(чтобы не пополнить дважды). Проверьте бюджет кампании в кабинете WB.")
                if i.get("start"):
                    start_campaign(token, i, state)
                continue
        rec.pop("pending", None)
        ok += 1
        (state.get("no_money") or {}).pop(i["cabinet"], None)       # деньги на балансе снова есть
        save_state(state)
        total = res.get("total") if isinstance(res, dict) else None
        log(f"  ✓ «{i['name']}»: +{i['amount']} ₽" + (f", бюджет теперь {fm_rub(num(total))} ₽" if total is not None else ""))
        if i.get("start"):
            start_campaign(token, i, state)
    save_state(state)
    return ok


def patch_nms(token, advert_id, add=(), delete=()):
    pace(token, "auction_nms", 1.1)
    data = call("PATCH", ADV + "/adv/v0/auction/nms", token, what="Товары кампании",
                body={"nms": [{"advert_id": advert_id, "nms": {"add": list(add), "delete": list(delete)}}]}) or {}
    res = {"added": [], "deleted": []}
    for it in data.get("nms") or []:
        if it.get("advert_id") == advert_id:
            res["added"] += (it.get("nms") or {}).get("added") or []
            res["deleted"] += (it.get("nms") or {}).get("deleted") or []
    return res


def apply_trim(token, items, reverse=False):
    """Убрать товары из кампаний; при откате — вернуть и поставить прежние ставки (WB ставит минимальную)."""
    by = defaultdict(list)
    for i in items:
        by[i["advert_id"]].append(i)
    ok = 0
    for aid, its in by.items():
        try:
            r = patch_nms(token, aid, **({"add": [i["nm_id"] for i in its]} if reverse else {"delete": [i["nm_id"] for i in its]}))
            ok += len(r["added"] if reverse else r["deleted"])
        except ApiError as e:
            log(f"  ✗ {e}")
            continue
        if reverse:
            bids = []
            for i in its:
                b = i.get("bids_kopecks") or {}
                if i.get("bid_type") == "unified":
                    if b.get("search"):
                        bids.append({"nm_id": i["nm_id"], "bid_kopecks": int(b["search"]), "placement": "combined"})
                else:
                    bids += [{"nm_id": i["nm_id"], "bid_kopecks": int(b[p]), "placement": p}
                             for p in ("search", "recommendations") if b.get(p)]
            for part in chunks(bids, 50):
                pace(token, "patch", 0.3)
                try:
                    call("PATCH", ADV + "/api/advert/v1/bids", token, body={"bids": [{"advert_id": aid, "nm_bids": part}]},
                         what="Ставки товаров")
                except ApiError as e:
                    log(f"  ✗ {e}")
    return ok


def run_apply(cabs, plan, key, state, reverse=False):
    for cab in sorted({i["cabinet"] for part in plan.values() for i in part}):
        if cab not in cabs:
            log(f"  ✗ Нет токена для кабинета «{cab}»")
            continue
        tok = cabs[cab]
        sel = {k: [i for i in v if i["cabinet"] == cab] for k, v in plan.items()}
        res = []
        if sel.get("topup"):
            if reverse:
                log("  Пополнения бюджета не откатываются — деньги уже в бюджетах кампаний.")
            else:
                n_dep = sum(1 for i in sel["topup"] if i["amount"])
                n_ok = apply_topups(tok, sel["topup"], state)
                if n_dep:
                    res.append(f"пополнено бюджетов {n_ok}/{n_dep}")
        if sel.get("trim"):
            res.append(f"{'возвращено' if reverse else 'убрано'} товаров {apply_trim(tok, sel['trim'], reverse)}/{len(sel['trim'])}")
            tr = state.setdefault("trimmed", {})
            for i in sel["trim"]:
                if reverse:
                    tr.pop(pk(i["advert_id"], i["nm_id"]), None)
                else:
                    tr[pk(i["advert_id"], i["nm_id"])] = {**i, "date": dt.date.today().isoformat()}
        if sel.get("untrim"):
            res.append(f"{'снова убрано' if reverse else 'возвращено в кампании'} товаров "
                       f"{apply_trim(tok, sel['untrim'], not reverse)}/{len(sel['untrim'])}")
            tr = state.setdefault("trimmed", {})
            for i in sel["untrim"]:
                if reverse:
                    tr[pk(i["advert_id"], i["nm_id"])] = {**i, "date": dt.date.today().isoformat()}
                else:
                    tr.pop(pk(i["advert_id"], i["nm_id"]), None)
        if sel["bids"]:
            res.append(f"ставки товаров {apply_bids(tok, sel['bids'], key)}/{len(sel['bids'])}")
        if sel["cluster_bids"]:
            res.append(f"ставки запросов {apply_cluster_bids(tok, sel['cluster_bids'], key)}/{len(sel['cluster_bids'])}")
            ret = state.setdefault("cluster_returned", {})
            for i in sel["cluster_bids"]:
                if i.get("returned"):
                    d = ret.setdefault(pk(i["advert_id"], i["nm_id"]), {})
                    if reverse:
                        d.pop(i["query"], None)
                    else:
                        d[i["query"]] = dt.date.today().isoformat()
            save_state(state)
        if sel["minus"]:
            done = apply_minus(tok, sel["minus"], key)
            res.append(f"минус-фразы {len(done)}/{len(sel['minus'])} товаров")
            update_state(state, done, reverse=reverse)
        if sel["limits"]:
            res.append(f"дневные лимиты {apply_limits(tok, sel['limits'], key)}/{len(sel['limits'])}")
        log(f"  {cab}: " + ("; ".join(res) if res else "нечего применять"))


def main():
    cabs = load_tokens()
    state = load_state()
    if "--rollback" in sys.argv:
        # последняя копия, в которой есть что откатывать (в копии только с пополнениями откатывать нечего)
        files, plan = sorted(BACKUP_DIR.glob("bids_*.json")), None
        for f in reversed(files):
            try:
                p_ = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if any(p_.get(k) for k in ("bids", "cluster_bids", "minus", "limits", "trim", "untrim")):
                files, plan = [f], p_
                break
        if not plan:
            sys.exit("Резервных копий с изменениями, которые можно откатить, нет.")
        plan.setdefault("cluster_bids", [])
        plan.setdefault("minus", [])
        plan.setdefault("limits", [])
        plan.setdefault("trim", [])
        plan.setdefault("untrim", [])
        plan["topup"] = []          # пополнения не откатываются
        log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — ОТКАТ по {files[-1].name}")
        run_apply(cabs, plan, "old", state, reverse=True)
        save_state(state)     # список убранных товаров («trimmed») после отката
        files[-1].rename(files[-1].with_suffix(".rolled_back"))     # повторный откат не вернёт то же самое ещё раз
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n\n")
        return

    apply = "--apply" in sys.argv
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — {'ПРИМЕНЕНИЕ' if apply else 'пробный прогон, ничего не меняется'}")
    busy_mark("wb_bids.py")             # сборщик рекомендаций пока ждёт: лимит WB общий
    for cab_, tok_ in cabs.items():
        w_ = token_warning(cab_, tok_)
        if w_:
            log(w_)
    UNIT_LIMITS.update(load_unit_limits())
    if UNIT_LIMITS:
        log(f"Юнит-экономика: лимиты цены заказа для {len(UNIT_LIMITS)} товаров (wb_unit_economics.csv).")
    print("Рекомендации WB отдаёт по одному товару: 5 сразу, дальше раз в 12 секунд (свежие — из прошлого запуска; "
          "пока грузятся остатки и размеры — уже в фоне).")
    plan = {"bids": [], "cluster_bids": [], "minus": [], "limits": [], "trim": [], "untrim": [], "topup": []}
    jobs = []
    for cab, rules in RULES.items():
        if cab not in cabs:
            log(f"\n✗ Кабинет «{cab}» есть в RULES, но нет в wb_tokens.txt")
            continue
        jobs.append((cab, lambda cab=cab, rules=rules: plan_cabinet(cab, cabs[cab], rules, state)))
    results = run_parallel(jobs)
    # кабинет, у которого не загрузилась часть данных, не применяется (ставки были бы посчитаны вслепую), остальные — да
    bad = {c for c, _ in DATA_PROBLEMS}
    for cab, _ in jobs:
        kind, p = results[cab]
        if kind == "err":
            if not isinstance(p, ApiError):
                raise p                 # ошибка в программе, а не ответ WB — остановиться, ничего не применять
            log(f"  ✗ {cab}: {p}")
            FAILED_CABS.add(cab)
            continue
        if apply and cab in bad:
            log(f"\n✗ {cab}: НЕ ПРИМЕНЯЮ — часть данных не загрузилась, ставки были бы посчитаны вслепую:")
            for msg in dict.fromkeys(m for c, m in DATA_PROBLEMS if c == cab):
                log(f"  — {msg}")
            log(f"  В кабинете «{cab}» ничего не изменено; часовая проверка ведёт его по прежним потолкам.")
            FAILED_CABS.add(cab)
            continue
        for k in plan:
            plan[k] += p[k]

    up = sum(1 for c in plan["bids"] if c["new"] > (c["old"] or 0))
    n_minus = sum(len(m["add_auto"]) + len(m["add_route"]) for m in plan["minus"])
    log(f"\nИтого: ставки товаров {len(plan['bids'])} (↑{up} ↓{len(plan['bids']) - up}), "
        f"ставки запросов {len(plan['cluster_bids'])}, новых минус-фраз {n_minus}"
        + (f" (на перепроверку из минуса {n_back})" if (n_back := sum(len(m.get("rem_auto") or []) for m in plan["minus"])) else "")
        + f", дневных лимитов {len(plan['limits'])}, "
        f"убрать товаров с малым остатком {len(plan['trim'])}, вернуть {len(plan['untrim'])}, "
        f"пополнить бюджетов {sum(1 for t in plan['topup'] if t['amount'])} на {sum(t['amount'] for t in plan['topup'])} ₽"
        + (f", запустить снова {sum(1 for t in plan['topup'] if not t['amount'])}" if any(not t['amount'] for t in plan['topup']) else "")
        + ".")
    blind = apply and None in bad          # сбой вне кабинета — не применять ничего
    if apply and not lock_mine():
        log("\n✗ НЕ ПРИМЕНЯЮ: общую блокировку снял другой процесс, пока шёл расчёт (иначе два процесса меняли бы "
            "кабинеты одновременно). Запустите позже ещё раз.")
        blind = True
    elif blind:
        log("\n✗ НЕ ПРИМЕНЯЮ: часть данных не загрузилась, ставки были бы посчитаны вслепую:")
        for msg in dict.fromkeys(m for _, m in DATA_PROBLEMS):
            log(f"  — {msg}")
        log("В кабинетах ничего не изменено. Запустите позже ещё раз.")
    elif apply and any(plan.values()):
        BACKUP_DIR.mkdir(exist_ok=True)
        path = BACKUP_DIR / f"bids_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"Резервная копия: {path.name}")
        prune_backups(BACKUP_DIR, "bids")
        record_day_base(state, plan)
        run_apply(cabs, plan, "new", state)
        log("Откат: python wb_bids.py --rollback")
    elif any(plan.values()):
        if DATA_PROBLEMS:
            log("! Часть данных не загрузилась (см. «!» выше) — --apply сейчас не изменит кабинет(ы): "
                + ", ".join(sorted(c or "все" for c in bad)) + ".")
        log("Это пробный прогон. Применить: python wb_bids.py --apply")
    if apply and not blind:
        # для wb_guard.py. Кабинет, который в этом запуске упал с ошибкой, сохраняет прежние потолки и правила —
        # иначе страж до следующего удачного запуска перестал бы его пополнять и вести ставки.
        old_caps = state.get("caps") or {}
        caps_cab = {k: v for k, v in (old_caps.get("cab") or {}).items()}
        caps_cab.update(CAPS_CAB)
        # потолки кабинета с ошибкой посчитаны не до конца или вслепую — берём прежние (ниже)
        items = {k: v for k, v in CAPS.items() if CAPS_CAB.get(k) not in FAILED_CABS}
        rules_ = dict(TOPUP_RULES)
        if FAILED_CABS:
            for k, v in (old_caps.get("items") or {}).items():
                # кабинет неизвестен (запись до этого обновления) — тоже сохраняю: страж сверяет потолки только с
                # кампаниями своего кабинета, лишняя запись ни на что не влияет
                if k not in items and (old_caps.get("cab") or {}).get(k) in FAILED_CABS | {None}:
                    items[k] = v
                    caps_cab[k] = (old_caps.get("cab") or {}).get(k)
            for k, v in (state.get("topup_rules") or {}).items():
                if k not in rules_ and v.get("cabinet") in FAILED_CABS:
                    rules_[k] = v
            log(f"Для часовой проверки сохраняю прежние потолки кабинетов с ошибкой: {', '.join(sorted(FAILED_CABS))}.")
        # время потолков — по каждому кабинету: у кабинета с ошибкой остаётся прежнее, чтобы страж перестал верить
        # его потолкам через 48 ч, а не вёл их бессрочно
        now_s = dt.datetime.now().isoformat(timespec="seconds")
        cab_at = {c: v for c, v in (old_caps.get("cab_at") or {}).items() if c in FAILED_CABS}
        for c in FAILED_CABS:
            if c not in cab_at and old_caps.get("at"):
                cab_at[c] = old_caps["at"]
        cab_at.update({c: now_s for c, _ in jobs if c not in FAILED_CABS})
        state["caps"] = {"at": now_s, "cab_at": cab_at, "items": items,
                         "cab": {k: v for k, v in caps_cab.items() if k in items}}
        state["topup_rules"] = rules_
        man = state.setdefault("managed", {})
        for cid, cab_ in MANAGED.items():
            man[str(cid)] = {"cabinet": cab_, "seen": dt.date.today().isoformat()}
        prune_state(state)
        save_state(state)     # ставки на начало дня и отсчёт наличия на WB — только при настоящем применении
    write_rec_log()
    save_rec_cache()
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n\n")


if __name__ == "__main__":
    # Блокировку берёт только то, что меняет кабинет (--apply, --rollback); пробный прогон её не трогает.
    if "--apply" in sys.argv or "--rollback" in sys.argv:
        if not lock_acquire("wb_bids.py", wait_min=25):
            log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — ✗ Блокировка занята (работает wb_guard.py или другой wb_bids.py) "
                "больше 25 мин — ничего не меняю, запустите позже.")
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write("\n".join(out) + "\n\n")
            sys.exit(1)
    try:
        main()
    finally:
        lock_release()
