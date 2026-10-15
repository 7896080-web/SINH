"""Имитация WB для долгой модели: кабинеты и кампании как у пользователя на 15.10.2026, расход по часам,
бюджеты (пусто → пауза), дневные лимиты WB (за клики), остатки, размеры, рекомендации (дрейф), запросы, сбои."""
import datetime as _dt
import math
import random
from collections import defaultdict

import os
NOW = [_dt.datetime(2026, 10, 15, 6, 0)]
CPM_BOOST = float(os.environ.get("CPM_BOOST", "1"))   # во сколько раз быстрее тратят кампании за показы (15.10: 1 000 ₽ за час)

# час суток → доля дневного спроса (вечерний пик, как у WB одежды)
PROF = [0.6, 0.4, 0.3, 0.2, 0.2, 0.3, 0.6, 1.2, 2.0, 2.6, 3.0, 3.2, 3.4, 3.4, 3.5, 3.6, 3.8, 4.2, 4.8, 5.4, 5.8, 5.6, 4.2, 2.2]
PSUM = sum(PROF)

#                 кабинет: [(ID, название, оплата, товаров, бюджет, статус, лимит WB ₽ (клики), ставка, просмотров/день на товар)]
CAMPS = {
    "Яворская": [(31049161, "27643 клик от 17.01.2026", "cpc", 17, 1106, 9, 3000, 900, 1800),
                 (31048671, "36446 клик от 17.01.2026", "cpc", 3, 1423, 9, 2000, 900, 2500),
                 (36114933, "Куртка общ от 25.04.2026", "cpc", 10, 980, 9, 1000, 650, 900),
                 (39661494, "ZJYM269002  22.08.2026", "cpc", 4, 2733, 9, 1000, 1757, 500),
                 (39829896, "Кампания от 29.08.2026", "cpc", 6, 3116, 9, 1000, 1757, 500),
                 (39661508, "пух нов 22.08.2026", "cpc", 27, 2074, 9, 1000, 1000, 300),
                 (39829887, "2101 лонг 29.08.2026", "cpc", 4, 1232, 9, 1000, 1163, 500),
                 (40863147, "Кампания от 11.10.2026", "cpc", 13, 1680, 9, 1000, 730, 400)],
    "Ребрик": [(40842596, "Кампания от 10.10.2026", "cpc", 27, 2681, 9, 1000, 830, 250),
               (40769287, "Кампания от 08.10.2026", "cpc", 27, 2836, 9, 1000, 1116, 250),
               (40863019, "Кампания от 11.10.2026", "cpm", 10, 0, 11, 0, 69900, 900),
               (40955628, "Ребрик 2101 показы", "cpm", 4, 140, 9, 0, 39100, 700),
               (40955524, "Ребрик TC26 показы", "cpm", 3, 0, 11, 0, 34300, 700),
               (40863039, "Ребрик куртки показы", "cpm", 20, 500, 11, 0, 30000, 400)],     # пауза пользователя (деньги есть)
    "Караман": [(40842612, "Кампания от 10.10.2026", "cpc", 27, 2618, 9, 1000, 1136, 250),
                (40769334, "Кампания от 08.10.2026", "cpc", 14, 884, 9, 1000, 1116, 250),
                (40955797, "Караман пуховики показы", "cpm", 16, 0, 11, 0, 42300, 700),
                (40769357, "Кампания от 08.10.2026", "cpc", 6, 2839, 9, 1000, 823, 300),
                (40769361, "Кампания от 08.10.2026", "cpc", 4, 1927, 9, 1000, 1346, 300),
                (40769369, "Кампания от 08.10.2026", "cpc", 4, 2266, 9, 1000, 988, 300),
                (40769350, "Караман пуховики клики", "cpc", 16, 800, 11, 1000, 1000, 250)],   # пауза пользователя
}
TOKENS = {"Яворская": "x" * 40 + "YAVORSKAYA", "Ребрик": "x" * 40 + "REBRIKREBRIK", "Караман": "x" * 40 + "KARAMANKARAM"}


class World:
    def __init__(self, seed=1):
        self.rnd = random.Random(seed)
        self.t = NOW[0]
        self.camps = {}       # id → dict
        self.cab_of = {}
        self.nm = {}          # nm → {stock_wb, stock_mp, cr, ctr, price, sizes:{name: share}, size_stock:{}}
        self.daily = defaultdict(lambda: defaultdict(float))       # (date, cid, nm) → views/clicks/sum/orders/atbs
        self.qdaily = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))   # (cid, nm) → (date, query) → ...
        self.orders_nm = defaultdict(lambda: defaultdict(float))    # nm → date → все заказы (с органикой)
        self.deposits = []    # (datetime, cab, cid, sum)
        self.events = []      # (datetime, что) — пауза/запуск/пополнение/ошибки
        self.spent_day = defaultdict(float)                         # (date, cid) → ₽
        self.hour_spend = defaultdict(float)                        # (date, cid, час) → ₽
        self.rec = {}         # (cid, nm) → множитель рекомендации
        self.fault = {"read": 0.0, "deposit_unclear": 0.0, "deposit_fail": 0.0, "start_fail": 0.0}
        self.calls = defaultdict(int)
        nmid = 1000000
        for cab, lst in CAMPS.items():
            for (cid, name, pay, n, bud, st, lim, bid, views) in lst:
                nms = []
                for _ in range(n):
                    nmid += 1
                    low = self.rnd.random() < 0.08
                    self.nm[nmid] = {"wb": self.rnd.choice([0, 0, 5, 20]), "mp": self.rnd.randint(3, 14) if low else self.rnd.randint(25, 300),
                                     "cr": self.rnd.uniform(0.008, 0.05), "ctr": self.rnd.uniform(0.015, 0.045),
                                     "price": self.rnd.choice([3500, 4500, 6500, 9000]), "views": views * self.rnd.uniform(0.4, 1.6),
                                     "sizes": {z: self.rnd.uniform(0.5, 2) for z in ("S", "M", "L", "XL", "XXL")},
                                     "organic": self.rnd.uniform(0.2, 3.0), "buyout": self.rnd.uniform(0.25, 0.6)}
                    nms.append(nmid)
                    self.rec[(cid, nmid)] = self.rnd.uniform(0.8, 1.3)
                self.camps[cid] = {"id": cid, "name": name, "pay": pay, "nms": nms, "budget": float(bud), "status": st,
                                   "limit": lim * 100 if pay == "cpc" else None, "limit_on": pay == "cpc",
                                   "bids": {nm: {"search": bid, "recommendations": bid} for nm in nms},
                                   "unified": cid == 40863019, "minus": defaultdict(list), "cab": cab, "base_bid": bid}
                self.cab_of[cid] = cab
        self.queries = ["куртка мужская", "куртка зимняя мужская", "пуховик мужской", "джемпер мужской", "куртка женская",
                        "куртка детская", "куртка awer", "ветровка"]

    # ---------------- время ----------------
    def advance(self, to):
        """Расход по часам до момента to."""
        while self.t < to:
            nxt = min(to, (self.t.replace(minute=0, second=0, microsecond=0) + _dt.timedelta(hours=1)))
            frac = (nxt - self.t).total_seconds() / 3600
            if frac > 0:
                self._hour(self.t, frac)
            if nxt.date() != self.t.date():
                self._new_day(nxt.date())
            self.t = nxt

    def _new_day(self, d):
        for nm, p in self.nm.items():              # остатки: продажи с органики уже списаны; раз в неделю поставка
            if d.weekday() == 0 and self.rnd.random() < 0.35:
                p["mp"] += self.rnd.randint(20, 120)
                self.events.append((self.t, f"поставка {nm}"))
        for k in list(self.rec):                   # рекомендации WB: дневной дрейф
            self.rec[k] = min(2.2, max(0.5, self.rec[k] * math.exp(self.rnd.gauss(0, 0.07))))

    def _hour(self, t, frac):
        d = t.date().isoformat()
        share = PROF[t.hour] / PSUM * frac
        for nm, p in self.nm.items():              # органика (заказы без рекламы)
            o = p["organic"] * share
            self.orders_nm[nm][d] += o
            self._take(nm, o)
        for k in list(self.rec):
            self.rec[k] = min(2.2, max(0.5, self.rec[k] * math.exp(self.rnd.gauss(0, 0.012 * frac))))
        for cid, c in self.camps.items():
            if c["status"] != 9 or c["budget"] <= 0:
                continue
            if c["pay"] == "cpc" and c["limit_on"] and self.spent_day[(d, cid)] * 100 >= (c["limit"] or 0):
                continue
            for nm in c["nms"]:
                p = self.nm[nm]
                if p["wb"] + p["mp"] <= 0:
                    continue
                bid = c["bids"][nm]["search"]
                rec = self.rec_value(cid, nm, "3-10")
                pos = min(1.6, max(0.05, (bid / rec) ** 1.2)) if rec else 0.5
                views = p["views"] * share * pos * (CPM_BOOST if c["pay"] == "cpm" else 1)
                qs = [q for q in self.queries if q not in c["minus"][nm]]
                if not qs:
                    continue
                views *= len(qs) / len(self.queries)
                clicks = views * p["ctr"]
                spend = clicks * bid / 100 if c["pay"] == "cpc" else views * bid / 100 / 1000
                left = c["budget"]
                if c["pay"] == "cpc" and c["limit_on"]:
                    left = min(left, (c["limit"] or 0) / 100 - self.spent_day[(d, cid)])
                if spend > left:
                    k = max(left, 0) / spend if spend else 0
                    views, clicks, spend = views * k, clicks * k, spend * k
                orders = clicks * p["cr"]
                c["budget"] -= spend
                self.spent_day[(d, cid)] += spend
                self.hour_spend[(d, cid, t.hour)] += spend
                row = self.daily[(d, cid, nm)]
                for kk, v in (("views", views), ("clicks", clicks), ("sum", spend), ("orders", orders), ("atbs", clicks * 0.15),
                              ("sum_price", orders * p["price"]), ("shks", orders)):
                    row[kk] += v
                self.orders_nm[nm][d] += orders
                self._take(nm, orders)
                for i, q in enumerate(qs):           # по запросам: «женская», «детская» — клики без заказов
                    w = 1 / (i + 1)
                    bad = "женск" in q or "детск" in q
                    qr = self.qdaily[(cid, nm)][(d, q)]
                    tot_w = sum(1 / (j + 1) for j in range(len(qs)))
                    qr["views"] += views * w / tot_w
                    qr["clicks"] += clicks * w / tot_w
                    qr["spend"] += spend * w / tot_w
                    qr["orders"] += 0 if bad else orders * w / tot_w * 1.2
                    qr["atbs"] += 0 if bad else clicks * w / tot_w * 0.15
                if c["budget"] <= 0.5:
                    c["budget"] = 0.0
                    c["status"] = 11
                    self.events.append((t, f"WB: пауза — кончился бюджет {cid}"))
                    break

    def _take(self, nm, q):
        p = self.nm[nm]
        a = min(p["mp"], q)
        p["mp"] -= a
        p["wb"] = max(0, p["wb"] - (q - a))

    def rec_value(self, cid, nm, level):
        c = self.camps[cid]
        base = c["base_bid"] * self.rec[(cid, nm)]
        k = {"1-2": 2.1, "3-10": 1.0, "11-34": 0.6}[level]
        step = 100 if c["pay"] == "cpm" else 1
        return int(max(base * k, 415 if c["pay"] == "cpc" else 15000) // step * step)

    def min_bid(self, cid, nm):
        return 415 if self.camps[cid]["pay"] == "cpc" else 15000

    # ---------------- API ----------------
    def call(self, B, method, url, token, params=None, body=None, what="", retry=True):
        self.advance(NOW[0])
        u = url.split(".ru")[1]
        self.calls[u] += 1
        cab = [k for k, v in TOKENS.items() if v == token][0]
        mine = {cid: c for cid, c in self.camps.items() if c["cab"] == cab}
        writes = method in ("PATCH", "PUT", "DELETE") or u in ("/adv/v1/budget/deposit", "/adv/v0/start", "/adv/v0/pause",
                                                                 "/adv/v0/normquery/set-minus")
        if not writes and self.rnd.random() < self.fault["read"]:
            raise B.ApiError(f"{what}: ошибка 502 (имитация)", status=502)
        if u == "/adv/v1/promotion/count":
            g = defaultdict(list)
            for cid, c in mine.items():
                g[c["status"]].append({"advertId": cid, "changeTime": "2026-10-10T00:00:00"})
            return {"adverts": [{"status": s, "advert_list": l} for s, l in g.items()]}
        if u == "/api/advert/v2/adverts":
            res = []
            for cid in map(int, params["ids"].split(",")):
                c = mine.get(cid)
                if not c:
                    continue
                pl = {"search": True, "recommendations": c["pay"] == "cpm" and not c["unified"]}
                res.append({"id": cid, "status": c["status"], "bid_type": "unified" if c["unified"] else "manual",
                            "settings": {"name": c["name"], "payment_type": c["pay"], "placements": pl},
                            "nm_settings": [{"nm_id": nm, "bids_kopecks": dict(c["bids"][nm])} for nm in c["nms"]]})
            return {"adverts": res}
        if u == "/adv/v3/fullstats":
            d1, d2 = _dt.date.fromisoformat(params["beginDate"]), _dt.date.fromisoformat(params["endDate"])
            out = []
            for cid in map(int, params["ids"].split(",")):
                days = []
                x = d1
                while x <= d2:
                    ds = x.isoformat()
                    nms = [{"nmId": nm, **{k: round(v, 2) for k, v in self.daily[(ds, cid, nm)].items()}}
                           for nm in (self.camps[cid]["nms"] if cid in self.camps else []) if (ds, cid, nm) in self.daily]
                    if nms:
                        days.append({"date": ds + "T00:00:00Z", "apps": [{"appType": 1, "nms": nms}]})
                    x += _dt.timedelta(days=1)
                if days:
                    out.append({"advertId": cid, "days": days})
            return out
        if u == "/api/analytics/v3/sales-funnel/products":
            per = body["selectedPeriod"]
            d1, d2 = per["start"], per["end"]
            nms = body.get("nmIds") or [nm for c in mine.values() for nm in c["nms"]]
            res = []
            for nm in nms:
                if nm not in self.nm:
                    continue
                p = self.nm[nm]
                o = sum(v for d, v in self.orders_nm[nm].items() if d1 <= d <= d2)
                hist = 0 if d2 >= self.t.date().isoformat() or d1 < "2026-06-01" else 0
                age = (_dt.date.fromisoformat(d2) - _dt.date.fromisoformat(d1)).days + 1
                if d2 < "2026-10-15":            # история до модели: обычный спрос
                    o = p["organic"] * 1.5 * age
                res.append({"product": {"nmId": nm, "vendorCode": f"v{nm}", "title": f"Товар {nm}", "subjectName": "Куртки",
                                        "stocks": {"wb": round(p["wb"]), "mp": round(p["mp"])}},
                            "statistic": {"selected": {"orderCount": round(o + hist), "buyoutCount": round(o * p["buyout"]),
                                                       "cancelCount": round(o * (1 - p["buyout"])), "openCount": round(o * 30)}}})
            return {"data": {"products": res}}
        if u == "/api/v2/stocks-report/products/sizes":
            nm = body["nmID"]
            p = self.nm[nm]
            tot = sum(p["sizes"].values())
            st = p["wb"] + p["mp"]
            return {"data": {"sizes": [{"name": z, "metrics": {"ordersCount": round(p["organic"] * 30 * w / tot),
                                                               "stockCount": round(st * w / tot) if (nm + i) % 7 else 0}}
                                       for i, (z, w) in enumerate(p["sizes"].items())]}}
        if u == "/api/advert/v1/config":
            return {"minTopUp": 100000, "cpcStep": 1, "cpmStep": 100, "minDailyLimit": 100000}
        if u == "/api/advert/v1/bids/min":
            return {"bids": [{"nm_id": nm, "bids": [{"type": t, "value": self.min_bid(body["advert_id"], nm)}
                                                    for t in ("search", "recommendation", "combined")]} for nm in body["nm_ids"]]}
        if u == "/api/advert/v0/bids/recommendations":
            cid, nm = params["advertId"], params["nmId"]
            if (cid, nm) not in self.rec:
                return {}
            if self.fault.get("rec_format"):
                return {"recommendations": [{"level": "3-10", "bid": self.rec_value(cid, nm, "3-10")}]}
            return {"levels": {"range1To2": {"bidKopecks": self.rec_value(cid, nm, "1-2")},
                               "range3To10": {"bidKopecks": self.rec_value(cid, nm, "3-10")},
                               "range11To34": {"bidKopecks": self.rec_value(cid, nm, "11-34")}},
                    "base": {"top2": {"bidKopecks": self.rec_value(cid, nm, "1-2")},
                             "leadersBid": {"bidKopecks": self.rec_value(cid, nm, "3-10")},
                             "competitiveBid": {"bidKopecks": self.rec_value(cid, nm, "11-34")}}}
        if u == "/api/advert/v0/daily-limits" and method == "GET":
            d = self.t.date().isoformat()
            res = []
            for i in params["advertIds"].split(","):
                c = self.camps.get(int(i))
                if c and c["pay"] == "cpc":
                    res.append({"advertId": int(i), "enabled": c["limit_on"], "dailyLimit": c["limit"],
                                "spentToday": round(self.spent_day[(d, int(i))] * 100), "carryOverEnabled": False,
                                "requiredLimit": 150000})
            return {"adverts": res}
        if u == "/api/advert/v0/daily-limits" and method == "PUT":
            res = []
            for i in body["advertIds"]:
                c = self.camps[i]
                c["limit_on"] = body["enabled"]
                if body["enabled"]:
                    c["limit"] = max(int(body["dailyLimit"]), 100000)
                res.append({"advertId": i, "belowMinLimit": int(body.get("dailyLimit") or 0) < 100000})
            return {"adverts": res}
        if u == "/api/advert/v2/budget":
            return {"adverts": [{"advertId": a, "total": round(self.camps[a]["budget"])} for a in body["advertIds"] if a in mine]}
        if u == "/adv/v1/budget/deposit":
            cid = params["id"]
            if self.rnd.random() < self.fault["deposit_fail"]:
                self.events.append((self.t, f"сбой пополнения (не прошло) {cid}"))
                raise B.ApiError(f"{what}: ошибка 400 (имитация)", status=400)
            s = body["sum"]
            if self.fault.get("balance_empty"):
                self.events.append((self.t, f"пополнение отклонено — нет денег на балансе {cid}"))
                raise B.ApiError(f"{what}: ошибка 400. Ответ WB: Недостаточно средств на балансе", status=400)
            if s < 1000:
                raise B.ApiError(f"{what}: minimum deposit amount is 1000", status=400)
            self.camps[cid]["budget"] += s
            self.deposits.append((self.t, cab, cid, s))
            if self.rnd.random() < self.fault["deposit_unclear"]:
                self.events.append((self.t, f"пополнение прошло, а ответ потерян {cid}"))
                raise B.ApiError(f"{what}: WB не ответил (имитация) — запрос мог пройти", B.UNCLEAR)
            return {"total": round(self.camps[cid]["budget"])}
        if u == "/adv/v0/start":
            c = self.camps[params["id"]]
            if self.rnd.random() < self.fault["start_fail"]:
                raise B.ApiError(f"{what}: ошибка 500 (имитация)", status=500)
            if c["budget"] < 1:
                raise B.ApiError(f"{what}: ошибка 400. Low Budget", status=400)
            c["status"] = 9
            self.events.append((self.t, f"запуск {params['id']}"))
            return None
        if u == "/adv/v0/pause":
            self.camps[params["id"]]["status"] = 11
            self.events.append((self.t, f"пауза {params['id']}"))
            return None
        if u == "/api/advert/v1/bids" and method == "PATCH":
            out = []
            for b in body["bids"]:
                c = self.camps[b["advert_id"]]
                done = []
                for x in b["nm_bids"]:
                    if x["nm_id"] in c["bids"]:
                        pl = "search" if x["placement"] == "combined" else x["placement"]
                        c["bids"][x["nm_id"]][pl] = x["bid_kopecks"]
                        if x["placement"] == "combined":
                            c["bids"][x["nm_id"]]["recommendations"] = x["bid_kopecks"]
                        done.append(x)
                out.append({"advert_id": b["advert_id"], "nm_bids": done})
            return {"bids": out}
        if u == "/adv/v0/auction/nms":
            out = []
            for it in body["nms"]:
                c = self.camps[it["advert_id"]]
                add = [n for n in it["nms"].get("add") or [] if n not in c["nms"]]
                dele = [n for n in it["nms"].get("delete") or [] if n in c["nms"]]
                for n in add:
                    c["nms"].append(n)
                    c["bids"][n] = {"search": self.min_bid(it["advert_id"], n), "recommendations": self.min_bid(it["advert_id"], n)}
                for n in dele:
                    c["nms"].remove(n)
                out.append({"advert_id": it["advert_id"], "nms": {"added": add, "deleted": dele}})
            return {"nms": out}
        if u == "/adv/v0/normquery/stats":
            d1, d2 = body["from"], body["to"]
            out = []
            for it in body["items"]:
                a, n = it["advert_id"], it["nm_id"]
                agg = defaultdict(lambda: defaultdict(float))
                for (d, q), v in self.qdaily[(a, n)].items():
                    if d1 <= d <= d2:
                        for k, x in v.items():
                            agg[q][k] += x
                st = [{"norm_query": q, "views": None if self.camps[a]["pay"] == "cpc" else round(v["views"]),
                       "clicks": round(v["clicks"]), "orders": round(v["orders"]), "atbs": round(v["atbs"]),
                       "spend": round(v["spend"], 2), "avg_pos": 5} for q, v in agg.items()]
                out.append({"advert_id": a, "nm_id": n, "stats": st})
            return {"stats": out}
        if u == "/adv/v0/normquery/get-minus":
            return {"items": [{"advert_id": it["advert_id"], "nm_id": it["nm_id"],
                               "norm_queries": list(self.camps[it["advert_id"]]["minus"][it["nm_id"]])} for it in body["items"]]}
        if u == "/adv/v0/normquery/set-minus":
            c = self.camps[body["advert_id"]]
            if c["pay"] == "cpc":
                raise B.ApiError(f"{what}: ошибка 400 (за клики минус-фраз нет)", status=400)
            if len(body["norm_queries"]) > 1000:
                raise B.ApiError(f"{what}: больше 1000", status=400)
            c["minus"][body["nm_id"]] = list(body["norm_queries"])
            return {}
        if u == "/adv/v0/normquery/list":
            return {"items": [{"advertId": it["advertId"], "nmId": it["nmId"],
                               "normQueries": {"active": [q for q in self.queries if q not in self.camps[it["advertId"]]["minus"][it["nmId"]]],
                                               "excluded": list(self.camps[it["advertId"]]["minus"][it["nmId"]])}} for it in body["items"]]}
        if u in ("/adv/v0/normquery/get-bids",):
            return {"bids": []}
        return {}
