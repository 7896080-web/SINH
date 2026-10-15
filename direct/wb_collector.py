# -*- coding: utf-8 -*-
"""
Сборщик рекомендованных ставок и размеров WB → база wb_rec.db (решение пользователя 15.10.2026).

Зачем: WB отдаёт рекомендованную ставку по одному товару, 5 запросов в минуту на кабинет, а размеры — по одному товару
раз в 20 секунд. Автопилот (wb_bids.py) и часовая проверка (wb_guard.py) раньше спрашивали всё сами и ждали:
утренний автопилот шёл ~28 мин. Сборщик спрашивает понемногу каждые 15 минут и складывает ответы в базу — скрипты
берут готовое (COLLECTOR_MODE = "use" в wb_bids.py), а часовая проверка успевает сверить с рекомендацией все товары.

Только ЧИТАЕТ WB — в рекламе ничего не меняет (поэтому пробного прогона и отката нет).

Что делает за один проход (не дольше RUN_MIN минут, кабинеты — одновременно):
  1. Кампании кабинета: активные, а на паузе — только те, что есть в RULES wb_bids.py (паузу из-за денег автопилот
     снимет). Товары без остатка (WB + свой склад) пропускает — автопилот им рекомендацию не спрашивает.
  2. Рекомендации: сначала те, что в базе старше всех (или их нет), и сначала активные кампании; моложе
     REC_FRESH_MIN минут — не переспрашивает. Круг по всем товарам Яворской (~83) — около 17 минут.
  3. Размеры (параллельно — у WB это другой лимит): товары с заказами за 30 дней, раз в SIZES_EVERY_H часа
     (WB обновляет их раз в 2 часа).
  4. Пока автопилот или проверка спрашивают WB сами (файл wb_api.busy), сборщик ждёт — лимит у них общий.
  5. История рекомендаций (места 1–2, 3–10, 11–34) — таблица rec_hist: строка, когда рекомендация изменилась, но не
     чаще раза в час на товар; хранится HIST_KEEP_DAYS дней.

Запуск:  python wb_collector.py            (один проход)
         python wb_collector.py --status   (что в базе: свежесть по кабинетам и сверка с автопилотом)
По расписанию — каждые 15 мин с 7:00 до 24:00 (wb_schedule.ps1). Токен: категории «Продвижение» и «Аналитика».
Результат: wb_rec.db (база), wb_collector_log.txt (по строке на кабинет за проход).
"""
import csv
import datetime as dt
import json
import os
import sys
import threading
import time

import wb_bids as B

# ---------------- НАСТРОЙКИ ----------------
RUN_MIN = 13              # один проход — не дольше (запуск каждые 15 мин; wb_run останавливает через 14)
REC_FRESH_MIN = 12        # рекомендацию моложе — не переспрашиваю
SIZES_EVERY_H = 2         # размеры обновляю не чаще (WB сам обновляет их раз в 2 часа)
HIST_KEEP_DAYS = 60       # сколько дней хранить историю рекомендаций
HIST_EVERY_MIN = 55       # в историю — не чаще раза в час на товар (иначе ~13 тыс. строк в сутки, ~50 МБ за 60 дней)
FRESH_SHOW_MIN = 30       # в отчёте «свежая» рекомендация — моложе стольких минут
BUSY_MAX_AGE_MIN = 70     # отметка «спрашиваю WB» старше — процесс, видимо, упал: не жду
# -------------------------------------------
LOG_FILE = B.HERE / "wb_collector_log.txt"
LOCK_FILE = B.HERE / "wb_collector.running"
LOG_MAX_MB = 2


class Db:
    """Одно соединение на все потоки, запись по очереди (как DbQueue в wb_positions.py)."""

    def __init__(self):
        self.con = B.collector_connect()
        self.lock = threading.Lock()

    def q(self, sql, args=()):
        with self.lock:
            return self.con.execute(sql, args).fetchall()

    def save_rec(self, cab, aid, nm, pay, data):
        at = dt.datetime.now().isoformat(timespec="seconds")
        lv = B.rec_levels(data, pay)
        with self.lock:
            prev = self.con.execute("SELECT r12, r310, r1134, at FROM rec_hist WHERE advert_id=? AND nm=? ORDER BY at DESC "
                                    "LIMIT 1", (aid, nm)).fetchone()
            self.con.execute("INSERT INTO rec (advert_id, nm, cab, pay, at, data) VALUES (?, ?, ?, ?, ?, ?) "
                             "ON CONFLICT (advert_id, nm) DO UPDATE SET cab=excluded.cab, pay=excluded.pay, "
                             "at=excluded.at, data=excluded.data",
                             (aid, nm, cab, pay, at, json.dumps(data, ensure_ascii=False)))
            if prev is None or (tuple(prev[:3]) != lv and (B.age_min(prev[3]) or 0) >= HIST_EVERY_MIN):   # изменения, раз в час
                self.con.execute("INSERT INTO rec_hist (at, cab, advert_id, nm, pay, r12, r310, r1134) "
                                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (at, cab, aid, nm, pay, *lv))
            self.con.commit()

    def save_sizes(self, cab, nm, sizes):
        at = dt.datetime.now().isoformat(timespec="seconds")
        with self.lock:
            self.con.execute("INSERT INTO sizes (nm, cab, at, data) VALUES (?, ?, ?, ?) ON CONFLICT (nm) DO UPDATE SET "
                             "cab=excluded.cab, at=excluded.at, data=excluded.data",
                             (nm, cab, at, json.dumps(sizes, ensure_ascii=False)))
            self.con.commit()

    def save_run(self, cab, started, info):
        with self.lock:
            self.con.execute("INSERT INTO runs (cab, started, finished, info) VALUES (?, ?, ?, ?) ON CONFLICT (cab) "
                             "DO UPDATE SET started=excluded.started, finished=excluded.finished, info=excluded.info",
                             (cab, started, dt.datetime.now().isoformat(timespec="seconds"),
                              json.dumps(info, ensure_ascii=False)))
            self.con.commit()

    def cleanup(self):
        edge = (dt.datetime.now() - dt.timedelta(days=HIST_KEEP_DAYS)).isoformat(timespec="seconds")
        old = (dt.datetime.now() - dt.timedelta(days=3)).isoformat(timespec="seconds")
        with self.lock:
            self.con.execute("DELETE FROM rec_hist WHERE at < ?", (edge,))
            self.con.execute("DELETE FROM rec WHERE at < ?", (old,))      # товары, которых в кампаниях больше нет
            self.con.execute("DELETE FROM sizes WHERE at < ?", (old,))
            self.con.commit()


def age(at):
    """Минут с момента at; записи нет — бесконечно много (0 — «только что», а не «нет записи»)."""
    a = B.age_min(at)
    return float("inf") if a is None else a


def busy_other():
    """Автопилот или страж сейчас спрашивают WB (их отметка свежая и процесс жив) → (кто, с какого времени) или None."""
    try:
        d = json.loads(B.BUSY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    a = B.age_min(d.get("at"))
    if a is None or a > BUSY_MAX_AGE_MIN or not B.pid_alive(d.get("pid")) or d.get("pid") == os.getpid():
        return None
    return d.get("owner") or "?", d.get("at")


def wait_if_busy(deadline, waited):
    """Ждать, пока автопилот или страж спрашивают WB. waited — [секунды] для отчёта. False — проход пора заканчивать."""
    while time.time() < deadline:
        if not busy_other():
            return True
        t = time.time()
        time.sleep(min(15, max(deadline - time.time(), 0)))
        waited[0] += time.time() - t
    return False


def lock_take():
    """Свой замок: предыдущий проход ещё идёт (компьютер спал, WB тормозил) — второй не начинаю."""
    for _ in range(2):
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps({"pid": os.getpid(), "at": dt.datetime.now().isoformat(timespec="seconds")}))
            return True
        except FileExistsError:
            try:
                d = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                d = {}
            a = B.age_min(d.get("at"))
            if B.pid_alive(d.get("pid")) and a is not None and a < RUN_MIN * 3:
                return False
            try:
                LOCK_FILE.unlink()                   # прежний проход упал или его остановили — замок ничей
            except OSError:
                return False
        except OSError:
            return False
    return False


def lock_free():
    try:
        if json.loads(LOCK_FILE.read_text(encoding="utf-8")).get("pid") == os.getpid():
            LOCK_FILE.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def explicit_ids(cab, camps):
    """Кампании из RULES wb_bids.py этого кабинета (по номеру или названию)."""
    res = set()
    for k in (B.RULES.get(cab) or {}):
        if k != "*" and not str(k).startswith("_"):
            ids = B.find(camps, k)
            if len(ids) == 1:
                res |= set(ids)
    return res


def collect_cabinet(cab, token, db, deadline):
    B._tl.cab = cab
    started = dt.datetime.now().isoformat(timespec="seconds")
    camps = B.get_campaigns(token)
    mine = explicit_ids(cab, camps)
    use = {cid: a for cid, a in camps.items() if a.get("_status") == 9 or (a.get("_status") == 11 and cid in mine)}
    nms = sorted({s.get("nm_id") for a in use.values() for s in a.get("nm_settings") or [] if s.get("nm_id")})
    stock = B.get_stock(token, nms) if nms else {}
    stock = stock or {}

    def no_stock(nm):
        q = B.available(stock.get(nm))
        return q is not None and q <= 0

    have = {(aid, nm): at for aid, nm, at in db.q("SELECT advert_id, nm, at FROM rec WHERE cab=?", (cab,))}
    pairs = []
    for cid, a in use.items():
        pay = (a.get("settings") or {}).get("payment_type") or "cpm"
        for s in a.get("nm_settings") or []:
            nm = s.get("nm_id")
            if nm and not no_stock(nm):
                pairs.append((cid, nm, pay, a.get("_status")))
    # сначала то, чего нет или что старше всех; при равенстве — активные кампании
    pairs.sort(key=lambda p: (have.get((p[0], p[1])) or "", 0 if p[3] == 9 else 1))
    todo = [p for p in pairs if age(have.get((p[0], p[1]))) >= REC_FRESH_MIN]

    size_have = {nm: at for nm, at in db.q("SELECT nm, at FROM sizes WHERE cab=?", (cab,))}
    size_nms = [nm for nm in nms if not no_stock(nm) and ((stock.get(nm) or {}).get("orders") or 0) >= B.SIZE_MIN_ORDERS]
    size_todo = sorted((nm for nm in size_nms if age(size_have.get(nm)) >= SIZES_EVERY_H * 60),
                       key=lambda nm: size_have.get(nm) or "")

    st = {"rec_ok": 0, "rec_err": 0, "size_ok": 0, "size_err": 0, "waited": [0.0], "err": ""}

    def recs():
        B._tl.buf, B._tl.session, B._tl.cab = [], B.requests.Session(), cab
        for aid, nm, pay, _ in todo:
            if not wait_if_busy(deadline, st["waited"]):
                break
            B.pace_burst(token, "rec", B.REC_INTERVAL, B.REC_BURST)
            if time.time() >= deadline:
                break
            B.REC_COUNT[cab] = B.REC_COUNT.get(cab, 0) + 1      # для строки «идёт работа» при ручном запуске
            try:
                data = B.fetch_rec_raw(token, aid, nm)
            except B.ApiError as e:
                st["rec_err"] += 1
                st["err"] = st["err"] or str(e)
                if e.status in (401, 403):
                    break
                continue
            if data:
                db.save_rec(cab, aid, nm, pay, data)
                st["rec_ok"] += 1

    def sizes():
        B._tl.buf, B._tl.session, B._tl.cab = [], B.requests.Session(), cab
        for nm in size_todo:
            if not wait_if_busy(deadline, [0.0]):
                break
            B.pace(token, "sizes", 20.5)
            if time.time() >= deadline:
                break
            try:
                db.save_sizes(cab, nm, B.fetch_sizes(token, nm))
                st["size_ok"] += 1
            except B.ApiError as e:
                st["size_err"] += 1
                st["err"] = st["err"] or str(e)
                if e.status in (401, 403):
                    break

    th = [threading.Thread(target=recs, daemon=True), threading.Thread(target=sizes, daemon=True)]
    for t in th:
        t.start()
    for t in th:
        while t.is_alive():
            t.join(1)
    now_have = {(aid, nm): at for aid, nm, at in db.q("SELECT advert_id, nm, at FROM rec WHERE cab=?", (cab,))}
    fresh = sum(1 for p in pairs if age(now_have.get((p[0], p[1]))) < FRESH_SHOW_MIN)
    exp = B.token_expires(token)
    info = {"pairs": len(pairs), "fresh": fresh, "rec_ok": st["rec_ok"], "rec_err": st["rec_err"],
            "token_exp": exp.isoformat(timespec="minutes") if exp else None,
            "sizes_total": len(size_nms), "size_ok": st["size_ok"], "size_err": st["size_err"],
            "waited_min": round(st["waited"][0] / 60, 1), "err": st["err"][:200]}
    db.save_run(cab, started, info)
    B.log(f"  {cab}: рекомендаций спрошено {st['rec_ok']}" + (f" (ошибок {st['rec_err']})" if st["rec_err"] else "")
          + f", свежих (моложе {FRESH_SHOW_MIN} мин) {fresh} из {len(pairs)}; размеров {st['size_ok']}"
          + (f" (ошибок {st['size_err']})" if st["size_err"] else "") + f" из {len(size_nms)} тов."
          + (f"; ждал автопилот/проверку {info['waited_min']} мин" if info["waited_min"] else "")
          + (f"; ! {st['err'][:120]}" if st["err"] else ""))
    return info


def write_log():
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_MB * 1024 * 1024:
            old = LOG_FILE.with_name(LOG_FILE.stem + ".old.txt")
            if old.exists():
                old.unlink()
            LOG_FILE.rename(old)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(B.out) + "\n")
    except OSError:
        pass


def status():
    if not B.COLLECTOR_DB.exists():
        sys.exit(f"Базы {B.COLLECTOR_DB.name} ещё нет — сборщик не запускался.")
    db = Db()
    print(f"Сборщик рекомендаций и размеров WB. Режим автопилота и проверки: COLLECTOR_MODE = \"{B.COLLECTOR_MODE}\"")
    for cab, started, finished, info in db.q("SELECT cab, started, finished, info FROM runs ORDER BY cab"):
        i = json.loads(info or "{}")
        ages = [B.age_min(at) for (at,) in db.q("SELECT at FROM rec WHERE cab=?", (cab,))]
        ages = sorted(a for a in ages if a is not None)
        print(f"  {cab}: последний проход {finished[11:16]} ({B.age_min(finished):.0f} мин назад); "
              f"рекомендаций в базе {len(ages)}, моложе {FRESH_SHOW_MIN} мин — {sum(1 for a in ages if a < FRESH_SHOW_MIN)}"
              + (f", самая старая {ages[-1]:.0f} мин" if ages else "")
              + f"; размеры: {i.get('sizes_total', 0)} тов." + (f"; ! {i['err'][:100]}" if i.get("err") else ""))
    n = db.q("SELECT COUNT(*) FROM rec_hist")[0][0]
    print(f"  История рекомендаций: {n} записей.")
    if B.CHECK_FILE.exists():
        with open(B.CHECK_FILE, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f, delimiter=";"))
        edge = (dt.datetime.now() - dt.timedelta(hours=24)).isoformat(timespec="seconds")
        rows = [r for r in rows if r.get("время", "") >= edge]
        for kind in ("рекомендация", "размеры"):
            k = [r for r in rows if r.get("что") == kind]
            if k:
                same = sum(1 for r in k if r.get("совпало") == "да")
                young = [r for r in k if float((r.get("возраст записи сборщика, мин") or "99").replace(",", ".")) <= 15]
                ys = sum(1 for r in young if r.get("совпало") == "да")
                print(f"  Сверка за сутки ({kind}): совпало {same} из {len(k)}"
                      + (f"; где запись сборщика моложе 15 мин — {ys} из {len(young)}" if young else "")
                      + " (расхождения — в wb_rec_check.csv; рекомендации WB меняются и в течение часа)")


def main():
    if "--status" in sys.argv:
        status()
        return
    if not lock_take():
        print("Предыдущий проход сборщика ещё идёт — этот пропускаю.")
        return
    try:
        cabs = B.load_tokens()
        t0 = time.time()
        deadline = t0 + RUN_MIN * 60
        B.log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — сборщик рекомендаций и размеров WB (только чтение)")
        db = Db()
        jobs = [(cab, lambda cab=cab, token=token: collect_cabinet(cab, token, db, deadline)) for cab, token in cabs.items()]
        for cab, (kind, res) in B.run_parallel(jobs).items():
            if kind == "err":
                B.log(f"  ✗ {cab}: {res}")
        db.cleanup()
        B.log(f"  Проход занял {(time.time() - t0) / 60:.1f} мин.")
        write_log()
    finally:
        lock_free()


if __name__ == "__main__":
    main()
