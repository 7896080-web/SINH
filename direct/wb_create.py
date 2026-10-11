# -*- coding: utf-8 -*-
"""
Создание рекламных кампаний Wildberries с ручной ставкой:
  • оплата за показы (cpm) — в поиске и/или рекомендациях;
  • оплата за клики (cpc) — в поиске (рекомендации за клики WB пока тестирует у части продавцов).
Новая кампания создаётся в статусе «готова к запуску» с минимальными ставками WB — деньги не тратятся,
пока вы её не запустите.

Примеры:
  Те же товары, что в «27643 клик», но в рекомендациях за показы:
    python wb_create.py --name "27643 рекомендации показы" --pay cpm --where recommendations --from "27643 клик"
  Поиск за показы по тем же товарам (этап 2 — «лучшие запросы»):
    python wb_create.py --name "27643 поиск показы" --pay cpm --where search --from "27643 клик"
  Новые куртки, поиск за клики, по артикулам WB:
    python wb_create.py --name "Куртки 2617 клик" --pay cpc --where search --nms 1454996463,1454996465
  Без --apply — только показывает, что будет создано. С --apply — создаёт.
  --budget 3000 --source balance  — сразу пополнить бюджет кампании (balance — взаиморасчёт, account — счёт, bonus — бонусы).
  --cabinet "Яворская"            — кабинет из wb_tokens.txt (если их несколько).
  --with-no-stock                 — включить товары, которых нет на складах WB (по умолчанию пропускаются).

Запуск, пауза и пополнение бюджета готовой кампании:
    python wb_create.py --start 123456789
    python wb_create.py --pause 123456789
    python wb_create.py --deposit 123456789 --budget 2000 --source balance
"""
import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import requests

ADV = "https://advert-api.wildberries.ru"
ANA = "https://seller-analytics-api.wildberries.ru"
HERE = Path(__file__).resolve().parent
TOKENS_FILE = HERE / "wb_tokens.txt"
SOURCES = {"account": 0, "balance": 1, "bonus": 3}
S = requests.Session()


class ApiError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


def call(method, url, token, params=None, body=None, what=""):
    for attempt in range(5):
        try:
            r = S.request(method, url, headers={"Authorization": token}, params=params, json=body, timeout=120)
        except requests.RequestException as e:
            if attempt < 3:
                time.sleep(5)
                continue
            raise ApiError(f"{what}: нет связи с WB ({e.__class__.__name__})")
        if r.status_code in (200, 204):
            try:
                return r.json() if r.content else None
            except ValueError:
                return r.text
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
        text = r.text[:400].replace("\n", " ")
        hint = " Нужен токен без «Только на чтение» (категории «Продвижение» и «Аналитика»)." if r.status_code in (401, 403) else ""
        raise ApiError(f"{what}: ошибка {r.status_code}.{hint} Ответ WB: {text}", r.status_code)
    raise ApiError(f"{what}: WB не ответил после нескольких попыток")


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
        if line and not line.startswith("#") and "=" in line:
            name, tok = line.split("=", 1)
            tok = tok.strip().strip('"').strip("'")
            if len(tok) >= 20:
                cabs[name.strip()] = tok
    if not cabs:
        sys.exit("В wb_tokens.txt нет токенов.")
    return cabs


def pick_token(cabs, name):
    if name:
        if name not in cabs:
            sys.exit(f"Кабинета «{name}» нет в wb_tokens.txt. Есть: {', '.join(cabs)}")
        return name, cabs[name]
    if len(cabs) > 1:
        sys.exit(f"В wb_tokens.txt несколько кабинетов — укажите --cabinet. Есть: {', '.join(cabs)}")
    return next(iter(cabs.items()))


def all_campaigns(token):
    data = call("GET", ADV + "/adv/v1/promotion/count", token, what="Список кампаний") or {}
    ids = [a["advertId"] for g in data.get("adverts") or [] if g.get("status") in (4, 7, 9, 11)
           for a in g.get("advert_list") or []]
    camps = {}
    for i in range(0, len(ids), 50):
        time.sleep(0.3)
        d = call("GET", ADV + "/api/advert/v2/adverts", token, params={"ids": ",".join(map(str, ids[i:i + 50]))},
                 what="Настройки кампаний") or {}
        for a in d.get("adverts") or []:
            camps[a["id"]] = a
    return camps


def stock_info(token, nms):
    y = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    res = {}
    for i in range(0, len(nms), 100):
        if i:
            time.sleep(21)
        body = {"selectedPeriod": {"start": y, "end": y}, "nmIds": nms[i:i + 100], "brandNames": [], "subjectIds": [],
                "tagIds": [], "skipDeletedNm": False, "limit": 1000, "offset": 0}
        data = call("POST", ANA + "/api/analytics/v3/sales-funnel/products", token, body=body, what="Остатки") or {}
        for p in (data.get("data") or {}).get("products") or []:
            pr = p.get("product") or {}
            res[pr.get("nmId")] = {"wb": (pr.get("stocks") or {}).get("wb"), "mp": (pr.get("stocks") or {}).get("mp"),
                                   "vendor": pr.get("vendorCode"), "title": pr.get("title")}
    return res


def main():
    ap = argparse.ArgumentParser(description="Создание кампаний WB с ручной ставкой")
    ap.add_argument("--name")
    ap.add_argument("--pay", choices=["cpm", "cpc"], help="cpm — за показы, cpc — за клики")
    ap.add_argument("--where", default="search", help="search, recommendations или search,recommendations")
    ap.add_argument("--from", dest="src", help="взять товары из кампании (ID или часть названия)")
    ap.add_argument("--nms", help="артикулы WB через запятую")
    ap.add_argument("--cabinet")
    ap.add_argument("--budget", type=int, help="пополнить бюджет на сумму, ₽")
    ap.add_argument("--source", choices=list(SOURCES), default="balance")
    ap.add_argument("--with-no-stock", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--start", type=int, metavar="ID")
    ap.add_argument("--pause", type=int, metavar="ID")
    ap.add_argument("--deposit", type=int, metavar="ID", help="пополнить бюджет кампании на --budget ₽")
    a = ap.parse_args()

    cab, token = pick_token(load_tokens(), a.cabinet)

    if a.deposit:
        if not a.budget:
            ap.error("для --deposit укажите --budget")
        try:
            res = call("POST", ADV + "/adv/v1/budget/deposit", token, params={"id": a.deposit},
                       body={"sum": int(a.budget), "type": SOURCES[a.source], "return": True}, what="Пополнение бюджета")
            total = (res or {}).get("total") if isinstance(res, dict) else None
            print(f"{cab}: бюджет кампании {a.deposit} пополнен на {a.budget} ₽{f', теперь {total} ₽' if total is not None else ''}.")
        except ApiError as e:
            print(f"✗ {e}")
        return

    if a.start or a.pause:
        cid = a.start or a.pause
        path = "/adv/v0/start" if a.start else "/adv/v0/pause"
        try:
            call("GET", ADV + path, token, params={"id": cid}, what="Запуск" if a.start else "Пауза")
            print(f"{cab}: кампания {cid} {'запущена' if a.start else 'поставлена на паузу'}.")
        except ApiError as e:
            print(f"✗ {e}")
            if a.start:
                print("  Частая причина — пустой бюджет: пополните его (--budget при создании или в кабинете).")
        return

    if not a.name or not a.pay:
        ap.error("нужны --name и --pay (или --start / --pause)")
    where = [w.strip() for w in a.where.split(",") if w.strip()]
    if not where or any(w not in ("search", "recommendations") for w in where):
        ap.error("--where: search, recommendations или search,recommendations")

    camps = all_campaigns(token)
    for cid, c in camps.items():
        if (c.get("settings") or {}).get("name", "").strip().lower() == a.name.strip().lower():
            sys.exit(f"Кампания с названием «{a.name}» уже есть (ID {cid}). Выберите другое название.")

    nms = []
    if a.src:
        found = [cid for cid, c in camps.items()
                 if str(cid) == a.src.strip() or a.src.lower() in (c.get("settings") or {}).get("name", "").lower()]
        if len(found) != 1:
            sys.exit(f"«{a.src}»: найдено кампаний {len(found)} — уточните название или укажите ID.")
        nms += [s.get("nm_id") for s in camps[found[0]].get("nm_settings") or []]
        print(f"Товары из кампании «{camps[found[0]]['settings'].get('name')}»: {len(nms)}")
    if a.nms:
        nms += [int(x) for x in a.nms.replace(" ", "").split(",") if x]
    nms = list(dict.fromkeys(n for n in nms if n))
    if not nms:
        sys.exit("Нет товаров: укажите --from или --nms.")

    try:
        info = stock_info(token, nms)
    except ApiError as e:
        print(f"! Остатки не получены ({e}) — товары без остатка не отфильтрованы.")
        info = {}
    take, skip = [], []
    for nm in nms:
        s = info.get(nm) or {}
        if not a.with_no_stock and s.get("wb") is not None and s["wb"] <= 0:
            skip.append(nm)
        else:
            take.append(nm)
    if len(take) > 50:
        sys.exit(f"В одной кампании WB разрешает не больше 50 товаров, а выбрано {len(take)}. Разделите на несколько.")

    pay_t = "за показы" if a.pay == "cpm" else "за клики"
    where_t = " и ".join({"search": "поиск", "recommendations": "рекомендации"}[w] for w in where)
    print(f"\nКабинет: {cab}\nНовая кампания: «{a.name}» — ручная ставка, {pay_t}, {where_t}")
    if a.pay == "cpc" and "recommendations" in where:
        print("! Рекомендации за клики WB пока открыл не всем — если не разрешено, WB вернёт ошибку.")
    print(f"Товары ({len(take)}):")
    for nm in take:
        s = info.get(nm) or {}
        print(f"  {nm:>11}  {str(s.get('vendor') or ''):22} на WB {s.get('wb', '?')}, свой склад {s.get('mp', '?')}")
    if skip:
        print(f"Пропущены — нет на складах WB ({len(skip)}): {', '.join(map(str, skip))}  (включить: --with-no-stock)")
    if a.budget:
        print(f"Бюджет: пополнить на {a.budget} ₽ из «{a.source}»")
    if not a.apply:
        print("\nЭто пробный прогон — ничего не создано. Создать: добавьте --apply")
        return

    body = {"name": a.name, "nms": take, "bid_type": "manual", "payment_type": a.pay, "placement_types": where}
    try:
        cid = call("POST", ADV + "/adv/v2/seacat/save-ad", token, body=body, what="Создание кампании")
    except ApiError as e:
        sys.exit(f"✗ {e}")
    print(f"\n✓ Создана кампания ID {cid} (готова к запуску, ставки минимальные).")
    if a.budget:
        try:
            res = call("POST", ADV + "/adv/v1/budget/deposit", token, params={"id": cid},
                       body={"sum": int(a.budget), "type": SOURCES[a.source], "return": True}, what="Пополнение бюджета")
            total = (res or {}).get("total") if isinstance(res, dict) else None
            print(f"✓ Бюджет пополнен{f', теперь {total} ₽' if total is not None else ''}.")
        except ApiError as e:
            print(f"✗ {e}")
    print("\nДальше:")
    print(f'  1. Добавьте в RULES файла wb_bids.py строку:  "{a.name}": {{"target": "3-10", "max_cost_per_order": 600}},')
    print("  2. python wb_bids.py --apply   — выставит ставки")
    print(f"  3. python wb_create.py --start {cid}   — запустит кампанию")


if __name__ == "__main__":
    main()
