# -*- coding: utf-8 -*-
"""
Автопилот рекламы Wildberries: ставки, ставки на поисковые кластеры, минус-фразы, дневные лимиты.
Трогает только кампании из RULES ("*" — все активные, кроме «_except»). Без --apply ничего не меняет.

Что делает для каждой кампании из RULES ("*" — все активные):
  • Ставки товаров — подтягивает к рекомендованной WB для нужных позиций, но не выше окупаемой:
      за клики:  допустимая цена заказа × конверсия товара (клик → заказ);
      за показы: допустимая цена заказа × конверсия × CTR × 1000.
    Растёт не больше чем на 30% в сутки (от ставки при первом запуске за день, сколько бы запусков
    ни было — ставка «на начало дня» хранится в wb_bids_state.json), снижается сразу.
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

Запуск:
  python wb_bids.py              — пробный прогон, ничего не меняет
  python wb_bids.py --apply      — применить (с резервной копией)
  python wb_bids.py --rollback   — вернуть всё, что изменил последний запуск

Токены — из wb_tokens.txt. Нужен токен БЕЗ «Только на чтение», категории «Продвижение» и «Аналитика».
"""
import datetime as dt
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

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
#   Дневной лимит WB даёт только кампаниям за клики; у кампаний за показы он пропускается.
RULES = {
    "Яворская": {
        "*": {"target": "3-10", "max_cost_per_order": 600, "daily_limit": 1000, "auto_minus": True,
              "topup_below": 500, "topup_amount": 1000, "topup_week_cap": 7000, "topup_day_cap": 1000,
              "topup_source": "balance"},
        "_except": [],
        # Дневной лимит (решение 12.10.2026): 1 000 ₽ в сутки на кампанию, у «27643 клик» и «36446 клик» — 2 000 ₽.
        # ── Главные кампании (решение 12.10.2026): потолок пополнений 2 000 ₽ в сутки и 14 000 ₽ в неделю; если встали на паузу
        #    из-за пустого бюджета — после пополнения запустить. Остальное — как в "*".
        "31049161": {"daily_limit": 2000, "topup_week_cap": 14000, "topup_day_cap": 2000, "start_after_topup": True},   # «27643 клик»
        "31048671": {"daily_limit": 2000, "topup_week_cap": 14000, "topup_day_cap": 2000, "start_after_topup": True},   # «36446 клик»
        # ── Новинки (запуск, решение 12.10.2026): цель — места 1–2, допустимый заказ вдвое выше обычного,
        #    запросы не минусуем, пока набираются продажи и отзывы. С 03.11 — общие правила "*".
        "39661494": {"target": "1-2", "max_cost_per_order": 1200, "auto_minus": False,
                     "until": "2026-11-02"},   # ZJYM269002, за клики
        "39829896": {"target": "1-2", "max_cost_per_order": 1200, "auto_minus": False,
                     "until": "2026-11-02"},   # 256250 («Кампания от 29.08.2026»), за клики

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
}
DEFAULTS = {
    "target": "3-10",
    "max_cost_per_order": 600,
    "max_cost_per_order_default": 600,
    "max_bid_cpc": 60,          # потолок ставки за клик, ₽
    "max_bid_cpm": 800,         # потолок ставки за 1000 показов, ₽
    "max_raise_pct": 30,        # максимум роста ставки в сутки, % (от ставки при первом запуске за день)
    "stop_clicks": 150,         # столько кликов за 14 дней без единого заказа — ставку не повышать и в отчёт
                                # «проверить карточку» (фото, цена, размеры, отзывы); None — выключено
    "min_stock": 15,            # товар, которого в наличии меньше (WB + свой склад), убрать из кампании (None — не убирать).
                                # Правило пользователя от 12.10.2026. Кампанию целиком не опустошает. Откат возвращает товар
    "max_drop_pct": 30,         # максимум снижения ставки в сутки, % (None — без ограничения). Нет в наличии или
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
CLUSTER_DAYS = 30                   # статистика запросов
PRIOR_CR, K_CLICKS = 0.03, 100      # конверсия по умолчанию и её вес
PRIOR_CTR, K_VIEWS = 0.03, 1000     # CTR в поиске по умолчанию и его вес
# ===================================================================

ADV = "https://advert-api.wildberries.ru"
ANA = "https://seller-analytics-api.wildberries.ru"
HERE = Path(__file__).resolve().parent
TOKENS_FILE = HERE / "wb_tokens.txt"
BACKUP_DIR = HERE / "wb_bids_backups"
STATE_FILE = HERE / "wb_bids_state.json"
LOG_FILE = HERE / "wb_bids_log.txt"
CPC_LEVEL = {"1-2": "range1To2", "3-10": "range3To10", "11-34": "range11To34"}
CPM_LEVEL = {"1-2": "top2", "3-10": "leadersBid", "11-34": "competitiveBid"}
REACH = {"1-2": "reachMax", "3-10": "reachMedium", "11-34": "reachMin"}

S = requests.Session()
out = []


def log(s=""):
    print(s)
    out.append(str(s))


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


def call(method, url, token, params=None, body=None, what=""):
    for attempt in range(6):
        try:
            r = S.request(method, url, headers={"Authorization": token}, params=params, json=body, timeout=120)
        except requests.RequestException as e:
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
                 if k != "_except" and dict(DEFAULTS, **rule)["max_cost_per_order"] == "auto"}
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


def mcpo(cab, rule, nm):
    """Допустимая цена заказа для товара: число из правила или лимит из юнит-экономики."""
    v = rule["max_cost_per_order"]
    if v == "auto":
        return UNIT_LIMITS.get((cab, nm), rule["max_cost_per_order_default"])
    return v


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


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"minus_auto": {}, "minus_route": {}, "cluster_returned": {}}


def pk(cid, nm):
    return f"{cid}:{nm}"


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


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
    return [cid for cid, a in camps.items()
            if str(cid) == key or key.lower() in str((a.get("settings") or {}).get("name", "")).lower()]


# Сбои загрузки данных, без которых ставки считать нельзя. Если список не пуст, --apply ничего не меняет.
DATA_PROBLEMS = []


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
            DATA_PROBLEMS.append(f"статистика кампаний не загрузилась ({e})")
            continue
        for item in data or []:
            aid = item.get("advertId")
            for day in item.get("days") or []:
                for app in day.get("apps") or []:
                    for n in app.get("nms") or []:
                        for k in ("views", "clicks", "orders", "sum"):
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
                DATA_PROBLEMS.append(f"статистика запросов не загрузилась ({e})")
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
        DATA_PROBLEMS.append(f"остатки не загрузились ({e})")
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
    y = dt.date.today() - dt.timedelta(days=1)
    todo_n = sum(1 for nm in nm_ids if not fresh(cache.get(str(nm))))
    if todo_n:
        print(f"  Размеры: запрашиваю {todo_n} тов. (WB — раз в 20 с, ≈{todo_n * 20 // 60 + 1} мин; дальше 6 ч из кэша)…", flush=True)
    k = 0
    for nm in nm_ids:
        e = cache.get(str(nm))
        if not fresh(e):
            k += 1
            print(f"    размеры {k}/{todo_n}", end="\r", flush=True)
            pace(token, "sizes", 20.5)
            body = {"nmID": nm, "currentPeriod": {"start": (y - dt.timedelta(days=SIZE_DAYS - 1)).isoformat(),
                                                  "end": y.isoformat()},
                    "stockType": "" if COUNT_FBS_STOCK else "wb", "orderBy": {"field": "ordersCount", "mode": "desc"},
                    "includeOffice": False}
            try:
                data = call("POST", ANA + "/api/v2/stocks-report/products/sizes", token, body=body,
                            what=f"Размеры {nm}") or {}
            except ApiError as ex:
                failed += 1
                if failed == 1:
                    log(f"  ! {ex} — размеры не учтены.")
                continue
            sizes = []
            for z in ((data.get("data") or {}).get("sizes") or []):
                m = z.get("metrics") or {}
                sizes.append({"name": str(z.get("name") or z.get("chrtID")), "orders": num(m.get("ordersCount")),
                              "stock": num(m.get("stockCount"))})
            e = cache[str(nm)] = {"at": now.isoformat(timespec="seconds"), "sizes": sizes}
        res[nm] = e["sizes"]
    if todo_n:
        print(" " * 60, end="\r", flush=True)
    if failed:
        log(f"  ! Размеры не получены для {failed} тов. — для них правило размеров не применяется.")
    try:
        SIZE_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
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


def get_rec(token, aid, nm):
    if (aid, nm) not in _rec_cache:
        print(f"    рекомендации WB: {len(_rec_cache) + 1}-й товар…", end="\r", flush=True)
        pace(token, "rec", 12.5)
        data = call("GET", ADV + "/api/advert/v0/bids/recommendations", token,
                    params={"nmId": nm, "advertId": aid}, what=f"Рекомендованные ставки {nm}") or {}
        print(" " * 60, end="\r", flush=True)
        if not _rec_cache:   # первый ответ за запуск сохраняем как образец (токенов в нём нет)
            try:
                REC_SAMPLE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            except OSError:
                pass
        _rec_cache[(aid, nm)] = data if isinstance(data, dict) else {}
    return _rec_cache[(aid, nm)]


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
        top = max(current or 0, int(base * (1 + max_raise / 100)))
        if new > top:
            new = top
            reason += f", рост ограничен +{max_raise}% в сутки"
        if max_drop is not None and current:
            floor_ = min(current, int(math.ceil(base * (1 - max_drop / 100) / step) * step))
            if new < floor_:
                new = floor_
                reason += f", снижение ограничено −{max_drop}% в сутки"
    new = int(math.floor(new / step) * step)
    if mn and new < mn:
        new = mn
        reason = "окупаемая ставка ниже минимальной WB → минимальная" if cap < mn else reason
    return new, reason


def changed(new, cur, step):
    return new is not None and cur is not None and abs(new - cur) >= max(step, cur * 0.03)


def plan_cabinet(cab, token, rules, state):
    log(f"\n══════════ {cab} ══════════")
    plan = {"bids": [], "cluster_bids": [], "minus": [], "limits": [], "trim": [], "topup": []}
    camps = get_campaigns(token)
    todo = {}
    explicit = {}
    for k, v in rules.items():
        if k in ("*", "_except"):
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

    involved = set(todo) | {r.get("clusters_from_id") for r in todo.values()} | {r.get("route_from_id") for r in todo.values()}
    involved.discard(None)
    # статистику WB отдаёт только для статусов 7, 9, 11; «готовая к запуску» (4) в пакете могла бы сорвать весь запрос
    stats = nm_stats(token, [c for c in involved if camps[c].get("_status") != 4])
    nms_of = {cid: [s.get("nm_id") for s in camps[cid].get("nm_settings") or []] for cid in involved}
    stock = get_stock(token, sorted({nm for cid in involved for nm in nms_of[cid]}))
    def no_stock(nm):
        q = available((stock or {}).get(nm))
        return q is not None and q <= 0

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
    steps = {"cpc": int(cfg.get("cpcStep") or 100), "cpm": int(cfg.get("cpmStep") or 100)}

    # статистика запросов нужна для auto_minus, cluster_bids и источников "top"
    need_cl = set()
    for cid, r in todo.items():
        if r["auto_minus"] or r["cluster_bids"]:
            need_cl.add(cid)
        if r.get("clusters_from_id"):
            need_cl.add(r["clusters_from_id"])
    cl = cluster_stats(token, [(cid, nm) for cid in need_cl for nm in nms_of[cid]])

    # порядок: сначала кампании со ставками на запросы, потом остальные (для route_from)
    order = sorted(todo, key=lambda c: 0 if todo[c]["cluster_bids"] else 1)
    planned_cluster = defaultdict(set)   # (cid, nm) → запросы, на которые будут стоять ставки
    check_card = []                      # товары с кликами без заказов — проверить карточку
    returned_now = defaultdict(set)      # (cid, nm) → запросы, которые в этом запуске возвращаются в клики

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
        nms = nms_of[cid]
        c_clicks = sum(v["clicks"] for (x, _), v in stats.items() if x == cid)
        c_orders = sum(v["orders"] for (x, _), v in stats.items() if x == cid)
        c_views = sum(v["views"] for (x, _), v in stats.items() if x == cid)
        camp_cr = smooth(c_orders, c_clicks, PRIOR_CR, K_CLICKS)
        camp_ctr = smooth(c_clicks, c_views, PRIOR_CTR, K_VIEWS)
        log(f"\n  ▶ {name} (ID {cid}) — {'единая' if unified else 'ручная'} ставка, "
            f"{'за клики' if pay == 'cpc' else 'за показы'}, цель {target}, "
            f"{'заказ до ' + str(r['max_cost_per_order']) + ' ₽' if r['max_cost_per_order'] != 'auto' else 'цена заказа — из юнит-экономики'}, "
            f"конверсия {camp_cr * 100:.1f}%" + (f"; режим запуска новинки до {r['_until']}" if r.get("_until") else ""))

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
                    try:
                        data = get_rec(token, cid, nm)
                        rec = rec_cpc(data, CPC_LEVEL[target]) if pay == "cpc" else rec_cpm(data, CPM_LEVEL[target])
                        if rec is None and data and target != "3-10":   # нет уровня для мест 1–2 — берём 3–10
                            rec = rec_cpc(data, CPC_LEVEL["3-10"]) if pay == "cpc" else rec_cpm(data, CPM_LEVEL["3-10"])
                            if rec is not None:
                                log(f"      ({nm}: WB не дал ставку для мест {target}, беру для 3–10)")
                        if rec is None and data:
                            log(f"      ! {nm}: не понял ответ WB с рекомендацией (образец — {REC_SAMPLE.name}), ставку не трогаю.")
                            DATA_PROBLEMS.append(f"непонятный формат рекомендаций WB — см. {REC_SAMPLE.name}")
                    except ApiError as e:
                        log(f"      ! {e}")
                    nb, reason = new_bid(current, rec, cap, mn, step, r["max_raise_pct"],
                                         day_base(state, "bids", cid, nm, placement), r.get("max_drop_pct"))
                    if size_short(nm) == "hold" and current and nb and nb > current:
                        nb, reason = current, f"мало ходовых размеров (закрыто {cover[nm][0]:.0%} спроса) — не повышаю"
                    if r.get("stop_clicks") and v["clicks"] >= r["stop_clicks"] and not v["orders"]:
                        check_card.append(f"{nm} ({cut(s_info.get('vendor'), 20)}) в «{name}»: {int(v['clicks'])} кликов "
                                          f"за {STATS_DAYS} дн без заказов")
                        if current and nb and nb > current:
                            nb, reason = current, f"{int(v['clicks'])} кликов без заказов — не повышаю, проверить карточку"
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
        if r["auto_minus"] or r.get("route_from_id"):
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
                log("    Минус-фразы: WB не вернул по этой кампании ни одного списка — у неё нет минус-фраз или её тип их "
                    "не поддерживает. Автоминус здесь не работает; чтобы включить его, добавьте в кабинете WB одну "
                    "минус-фразу любому товару этой кампании (так скрипт убедится, что WB отдаёт списки).")
            for nm in (nms if camp_known else []):
                key = pk(cid, nm)
                current = list(cur_minus.get((cid, nm), []))
                cur_set = set(current)
                add_auto, add_route, rem_route = [], [], []
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
                    gated = sd is None or sd < MINUS_AFTER_RESTOCK_DAYS or size_short(nm) is not None
                    cs = cl.get((cid, nm), {})
                    bad = sorted(((q, s) for q, s in cs.items()
                                  if s["orders"] == 0 and (s["clicks"] >= MINUS_MIN_CLICKS or negative(q))
                                  and (s["clicks"] * cr_ref >= MINUS_EXPECTED or negative(q)) and q not in cur_set
                                  and not protected(q) and q not in planned_cluster.get((cid, nm), set())
                                  and q not in route_want.get(nm, set())),
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
                if add_auto or add_route or rem_route:
                    new = [q for q in current if q not in rem_route] + add_route + add_auto
                    if len(new) > 1000:
                        log(f"    ! У товара {nm} больше 1000 минус-фраз — новые не добавляю.")
                        continue
                    plan["minus"].append({"cabinet": cab, "advert_id": cid, "nm_id": nm, "old": current, "new": new,
                                          "add_auto": add_auto, "add_route": add_route, "rem_route": rem_route,
                                          "stats": {q: cl.get((cid, nm), {}).get(q) for q in add_auto}})
            items = [m for m in plan["minus"] if m["advert_id"] == cid]
            if items:
                log("    Минус-фразы:")
                for m in items:
                    for q in m["add_auto"]:
                        s = m["stats"].get(q) or {}
                        log(f"      + {m['nm_id']:>11} {cut(q, 40):40} {int(s.get('clicks', 0))} кл., 0 заказов, "
                            f"{s.get('spend', 0):,.0f} ₽".replace(",", " "))
                    for q in m["add_route"]:
                        log(f"      + {m['nm_id']:>11} {cut(q, 40):40} передан в «{r['route_from']}»")
                    for q in m["rem_route"]:
                        log(f"      − {m['nm_id']:>11} {cut(q, 40):40} возвращён (в «{r['route_from']}» ставки больше нет)")
            elif cur_minus is not None:
                log("    Минус-фразы: добавлять нечего.")
            for note in held_notes:
                log(note)

        # ---- дневной лимит ----
        if r["daily_limit"]:
            if pay != "cpc":
                log("    ! Дневной лимит WB даёт только кампаниям с оплатой за клики — пропускаю.")
            else:
                try:
                    lim = get_limits(token, [cid]).get(cid) or {}
                except ApiError as e:
                    log(f"    ! {e}")
                    lim = None
                if lim is not None:
                    want = int(r["daily_limit"] * 100)
                    min_dl = int(cfg.get("minDailyLimit") or 0)
                    if want < min_dl:
                        log(f"    ! Лимит {r['daily_limit']} ₽ меньше минимального WB ({rub(min_dl)} ₽) — ставлю минимальный.")
                        want = min_dl
                    req = lim.get("requiredLimit")
                    note = f" WB рекомендует не меньше {rub(req)} ₽ при текущих ставках." if req and want < req else ""
                    same = lim.get("enabled") and lim.get("dailyLimit") == want and bool(lim.get("carryOverEnabled")) == bool(r["carry_over"])
                    log(f"    Дневной лимит: сейчас {rub(lim.get('dailyLimit')) if lim.get('enabled') else 'выключен'}"
                        f"{', потрачено сегодня ' + rub(lim.get('spentToday')) if lim.get('spentToday') is not None else ''}"
                        f" → {'без изменений' if same else rub(want) + ' ₽'}.{note}")
                    if not same:
                        plan["limits"].append({"cabinet": cab, "advert_id": cid,
                                               "old": {"enabled": bool(lim.get("enabled")), "dailyLimit": lim.get("dailyLimit"),
                                                       "carryOverEnabled": bool(lim.get("carryOverEnabled"))},
                                               "new": {"enabled": True, "dailyLimit": want, "carryOverEnabled": bool(r["carry_over"])}})
        # ---- автопополнение бюджета ----
        if r.get("topup_below"):
            bud = get_budgets(token, [cid]).get(cid)
            if bud is None:
                log("    ! Бюджет кампании не получен — пополнение пропускаю.")
            elif bud >= r["topup_below"]:
                log(f"    Бюджет: {fm_rub(bud)} ₽ — пополнять не нужно (порог {r['topup_below']} ₽).")
            else:
                week = topped_week(state, cid)
                today_ = topped_week(state, cid, days=1)
                amount = min(r["topup_amount"], r["topup_day_cap"] - today_, r["topup_week_cap"] - week)
                if amount < 100:     # меньше 100 ₽ не пополняем — дневной или недельный потолок исчерпан
                    log(f"    ! Бюджет {fm_rub(bud)} ₽ ниже порога, но потолок исчерпан: сегодня пополнено на {fm_rub(today_)} "
                        f"из {r['topup_day_cap']} ₽, за 7 дней на {fm_rub(week)} из {r['topup_week_cap']} ₽ — не пополняю.")
                else:
                    start = bool(r.get("start_after_topup")) and a.get("_status") == 11
                    log(f"    Бюджет: {fm_rub(bud)} ₽ < {r['topup_below']} ₽ → пополнить на {amount} ₽ из «{SOURCE_RU[r['topup_source']]}» "
                        f"(сегодня уже {fm_rub(today_)} из {r['topup_day_cap']} ₽, за 7 дней {fm_rub(week)} из {r['topup_week_cap']} ₽)" + (" и запустить кампанию" if start else "") + ".")
                    plan["topup"].append({"cabinet": cab, "advert_id": cid, "name": name, "amount": amount,
                                          "source": r["topup_source"], "budget_before": bud, "start": start})
    if check_card:
        log("\n  Проверить карточку (много кликов, ни одного заказа — дело скорее в фото, цене, размерах или отзывах, "
            "а не в ставке; ставку таким товарам не повышаю):")
        for c in check_card:
            log(f"    • {c}")
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
            log(f"  ✗ {e}")
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
        if not reverse:
            auto |= set(m["add_auto"])
            route = (route | set(m["add_route"])) - set(m["rem_route"])
        else:
            auto -= set(m["add_auto"])
            route = (route - set(m["add_route"])) | set(m["rem_route"])
        state["minus_auto"][key] = sorted(auto)
        state["minus_route"][key] = sorted(route)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


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


def topped_week(state, cid, days=7):
    since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    return sum(t["sum"] for t in state.get("topups", {}).get(str(cid), []) if t["date"] >= since)


def apply_topups(token, items, state):
    """Пополнить бюджеты. Деньги не возвращаются — отката нет. Записывает пополнения в состояние (для потолка)."""
    ok = 0
    for i in items:
        pace(token, "deposit", 1.1)
        try:
            res = call("POST", ADV + "/adv/v1/budget/deposit", token, params={"id": i["advert_id"]}, what="Пополнение бюджета",
                       body={"sum": int(i["amount"]), "type": SOURCE_TYPE[i["source"]], "return": True})
        except ApiError as e:
            log(f"  ✗ «{i['name']}»: пополнение не прошло — {e}")
            continue
        ok += 1
        state.setdefault("topups", {}).setdefault(str(i["advert_id"]), []).append(
            {"date": dt.date.today().isoformat(), "sum": i["amount"]})
        total = res.get("total") if isinstance(res, dict) else None
        log(f"  ✓ «{i['name']}»: +{i['amount']} ₽" + (f", бюджет теперь {fm_rub(num(total))} ₽" if total is not None else ""))
        if i.get("start"):
            pace(token, "start", 1.1)
            try:
                call("GET", ADV + "/adv/v0/start", token, params={"id": i["advert_id"]}, what="Запуск кампании")
                log(f"  ✓ «{i['name']}»: запущена после пополнения")
            except ApiError as e:
                log(f"  ✗ «{i['name']}»: не запустилась — {e}")
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
                res.append(f"пополнено бюджетов {apply_topups(tok, sel['topup'], state)}/{len(sel['topup'])}")
        if sel.get("trim"):
            res.append(f"{'возвращено' if reverse else 'убрано'} товаров {apply_trim(tok, sel['trim'], reverse)}/{len(sel['trim'])}")
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
            STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
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
        files = sorted(BACKUP_DIR.glob("bids_*.json"))
        if not files:
            sys.exit("Резервных копий нет.")
        plan = json.loads(files[-1].read_text(encoding="utf-8"))
        plan.setdefault("cluster_bids", [])
        plan.setdefault("minus", [])
        plan.setdefault("limits", [])
        plan.setdefault("trim", [])
        plan["topup"] = []          # пополнения не откатываются
        log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — ОТКАТ по {files[-1].name}")
        run_apply(cabs, plan, "old", state, reverse=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n\n")
        return

    apply = "--apply" in sys.argv
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — {'ПРИМЕНЕНИЕ' if apply else 'пробный прогон, ничего не меняется'}")
    UNIT_LIMITS.update(load_unit_limits())
    if UNIT_LIMITS:
        log(f"Юнит-экономика: лимиты цены заказа для {len(UNIT_LIMITS)} товаров (wb_unit_economics.csv).")
    print("Рекомендации WB отдаёт по одному товару раз в 12 секунд — на кампанию уходит несколько минут.")
    plan = {"bids": [], "cluster_bids": [], "minus": [], "limits": [], "trim": [], "topup": []}
    for cab, rules in RULES.items():
        if cab not in cabs:
            log(f"\n✗ Кабинет «{cab}» есть в RULES, но нет в wb_tokens.txt")
            continue
        try:
            p = plan_cabinet(cab, cabs[cab], rules, state)
        except ApiError as e:
            log(f"  ✗ {cab}: {e}")
            continue
        for k in plan:
            plan[k] += p[k]

    up = sum(1 for c in plan["bids"] if c["new"] > (c["old"] or 0))
    n_minus = sum(len(m["add_auto"]) + len(m["add_route"]) for m in plan["minus"])
    log(f"\nИтого: ставки товаров {len(plan['bids'])} (↑{up} ↓{len(plan['bids']) - up}), "
        f"ставки запросов {len(plan['cluster_bids'])}, новых минус-фраз {n_minus}, дневных лимитов {len(plan['limits'])}, "
        f"убрать товаров с малым остатком {len(plan['trim'])}, "
        f"пополнить бюджетов {len(plan['topup'])} на {sum(t['amount'] for t in plan['topup'])} ₽.")
    if apply and DATA_PROBLEMS:
        log("\n✗ НЕ ПРИМЕНЯЮ: часть данных не загрузилась, ставки были бы посчитаны вслепую:")
        for msg in dict.fromkeys(DATA_PROBLEMS):
            log(f"  — {msg}")
        log("В кабинетах ничего не изменено. Запустите позже ещё раз.")
    elif apply and any(plan.values()):
        BACKUP_DIR.mkdir(exist_ok=True)
        path = BACKUP_DIR / f"bids_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"Резервная копия: {path.name}")
        record_day_base(state, plan)
        run_apply(cabs, plan, "new", state)
        log("Откат: python wb_bids.py --rollback")
    elif any(plan.values()):
        if DATA_PROBLEMS:
            log("! Часть данных не загрузилась (см. «!» выше) — --apply сейчас откажется применять.")
        log("Это пробный прогон. Применить: python wb_bids.py --apply")
    if apply and not DATA_PROBLEMS:
        save_state(state)     # ставки на начало дня и отсчёт наличия на WB — только при настоящем применении
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n\n")


if __name__ == "__main__":
    main()
