"""Долгая модель работы автопилота WB (только для проверки кода, к WB не обращается):
   python tools/sim/sim.py <папка для прогона> <дней> [--seed N]     (sim2.py — сценарий 2: пустой баланс,
   пробуждение компьютера, смена формата рекомендаций). Отчёт — <папка>/_report.txt.
Виртуальные часы; расписание как в wb_schedule.ps1 (сборщик каждые 15 мин 7:00–23:45, страж в :40 7:40–23:40,
автопилот 8:00/13:00/18:00 + wb_budget после утреннего); сбои и «выключенный компьютер» по сценарию.
Проверки после каждого шага — по «правде» мира (world.py), а не по журналам скриптов."""
import datetime as _dt
import importlib
import io
import json
import os
import shutil
import sys
import time as _time
import traceback
import types
from collections import defaultdict
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = os.path.abspath(sys.argv[1])
DAYS = int(sys.argv[2])
SEED = int(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else 1
SRC = os.path.abspath(os.path.join(HERE, "..", ".."))   # папка со скриптами (direct)
os.makedirs(RUN, exist_ok=True)
for f in ("wb_bids.py", "wb_guard.py", "wb_collector.py", "wb_budget.py", "wb_common.py", "wb_run.py", "wb_pacing.py"):
    if os.path.exists(os.path.join(SRC, f)):
        shutil.copy(os.path.join(SRC, f), RUN)
os.chdir(RUN)
sys.path.insert(0, RUN)
sys.path.insert(0, HERE)
import world as Wm

# ---------------- виртуальные часы ----------------
NOW = Wm.NOW


class FDateTime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        n = NOW[0]
        return cls(n.year, n.month, n.day, n.hour, n.minute, n.second, n.microsecond)

    @classmethod
    def today(cls):
        return cls.now()


class FDate(_dt.date):
    @classmethod
    def today(cls):
        n = NOW[0]
        return cls(n.year, n.month, n.day)


fdt = types.ModuleType("datetime")
fdt.__dict__.update(_dt.__dict__)
fdt.datetime, fdt.date = FDateTime, FDate
ftime = types.ModuleType("time")
ftime.__dict__.update(_time.__dict__)
ftime.time = lambda: NOW[0].timestamp()


def _sleep(s):
    NOW[0] = NOW[0] + _dt.timedelta(seconds=max(s, 0))


ftime.sleep = _sleep

W = Wm.World(SEED)
MODE = ["check"]
COLLECTOR_BROKEN = [False]
USER_PAUSED = {}                 # кампания → (с, по) — пауза пользователя (автопилот не должен запускать)
OUT_DIR = os.path.join(RUN, "_out")
os.makedirs(OUT_DIR, exist_ok=True)
FIND = defaultdict(list)         # вид → [(время, текст)]
RUNS = []                        # (время, что, сек, исключение?)
B = G = C = BU = R = None


def note(kind, text):
    FIND[kind].append((NOW[0].strftime("%d.%m %H:%M"), text))


def mods():
    """Каждый запуск — «новый процесс»: модули перечитываются, затем подменяются часы и WB."""
    global B, G, C, BU, R
    for name in ("wb_bids", "wb_guard", "wb_collector", "wb_budget", "wb_run"):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)
    B, G, C, BU, R = (sys.modules[n] for n in ("wb_bids", "wb_guard", "wb_collector", "wb_budget", "wb_run"))
    for m in (B, G, C, BU):
        m.dt = fdt
        m.time = ftime
    B.call = lambda method, url, token, params=None, body=None, what="", retry=True: W.call(B, method, url, token, params, body, what, retry)
    B.pace = lambda *a, **k: None
    B.pace_burst = lambda *a, **k: None
    B.progress = lambda *a, **k: None
    B.load_tokens = lambda *a: dict(Wm.TOKENS)
    B.PARALLEL = False
    B.start_rec_prefetch = lambda *a, **k: None
    B.COLLECTOR_MODE = MODE[0]
    G.T0[0] = ftime.time()
    if COLLECTOR_BROKEN[0]:
        def boom(*a, **k):
            raise RuntimeError("сборщик сломан (имитация)")
        C.collect_cabinet = boom


def run(what, fn_name, argv):
    mods()
    R.rotate()
    mod = {"bids": B, "guard": G, "collector": C, "budget": BU}[what]
    sys.argv = [mod.__file__] + argv
    buf = io.StringIO()
    t0 = _time.time()
    err = None
    try:
        with redirect_stdout(buf):
            if what == "bids" and "--apply" in argv:          # как в if __name__ == "__main__" у wb_bids.py
                if not B.lock_acquire("wb_bids.py", wait_min=25):
                    print("✗ Блокировка занята — ничего не меняю")
                    raise SystemExit(1)
                try:
                    mod.main()
                finally:
                    B.lock_release()
            else:
                mod.main()
    except SystemExit as e:
        if e.code not in (None, 0):
            err = f"SystemExit({e.code})"
    except Exception:                 # noqa: BLE001
        err = traceback.format_exc()
    sec = _time.time() - t0
    out = buf.getvalue()
    RUNS.append((NOW[0], what, sec, bool(err)))
    with open(os.path.join(OUT_DIR, f"{NOW[0]:%m%d_%H%M}_{what}.txt"), "w", encoding="utf-8") as f:
        f.write(out + ("\n\n" + err if err else ""))
    if err:
        note("сбой скрипта", f"{what}: {err.strip().splitlines()[-1]}")
    for line in out.splitlines():
        if "Traceback" in line:
            note("трассировка в выводе", f"{what}: {line[:150]}")
    # «процесс завершился»: его отметки — снять (как сделала бы ОС); оставленная блокировка — находка
    for fn, kind in (("wb_bids.running", "блокировка осталась"), ("wb_collector.running", "замок сборщика остался")):
        if os.path.exists(fn):
            note(kind, f"{what}: {fn} после выхода")
            os.remove(fn)
    if os.path.exists("wb_api.busy"):
        os.remove("wb_api.busy")
    check_after(what, out)
    return out


# ---------------- проверки по «правде» мира ----------------
def rule_of(cab, cid):
    rules = B.RULES.get(cab) or {}
    r = rules.get(str(cid))
    if r is None and "*" in rules:
        r = rules["*"]
    if r is None:
        return None
    if r.get("until") and NOW[0].date().isoformat() > r["until"]:
        r = rules.get("*")
        if r is None:
            return None
    return {**B.DEFAULTS, **rules.get("*", {}), **r}


DEP_SEEN = [0]


def check_after(what, out):
    # пополнения: потолки в сутки / неделю, кабинет
    for (t, cab, cid, s) in W.deposits[DEP_SEEN[0]:]:
        r = rule_of(cab, cid) or {}
        d = t.date()
        day = sum(x[3] for x in W.deposits if x[2] == cid and x[0].date() == d)
        week = sum(x[3] for x in W.deposits if x[2] == cid and (d - x[0].date()).days < 7 and x[0] <= t)
        cabw = sum(x[3] for x in W.deposits if x[1] == cab and (d - x[0].date()).days < 7 and x[0] <= t)
        if r.get("topup_day_cap") and day > max(r["topup_day_cap"], 1000):
            note("потолок пополнений в сутки", f"{cab} {cid}: за {d:%d.%m} {day:.0f} ₽ > {max(r['topup_day_cap'], 1000)}")
        if r.get("topup_week_cap") and week > r["topup_week_cap"]:
            note("потолок пополнений в неделю", f"{cab} {cid}: за 7 дн. {week:.0f} ₽ > {r['topup_week_cap']}")
        wb = (B.RULES.get(cab) or {}).get("_week_budget")
        if wb and cabw > wb:
            note("недельный бюджет кабинета", f"{cab}: за 7 дн. {cabw:.0f} ₽ > {wb}")
        same = [x for x in W.deposits if x[2] == cid and abs((x[0] - t).total_seconds()) < 1800]
        if len(same) > 1 and same[0] is not same[-1] and (t, cab, cid, s) == same[-1]:
            note("два пополнения за полчаса", f"{cab} {cid}: {[f'{x[0]:%H:%M} {x[3]}' for x in same]}")
    DEP_SEEN[0] = len(W.deposits)
    if what in ("bids", "guard"):
        for cid, c in W.camps.items():
            r = rule_of(c["cab"], cid)
            if not r or (c["status"] not in (9, 11)):
                continue
            if str(cid) not in (B.RULES.get(c["cab"]) or {}) and c["status"] != 9 and cid not in managed_seen:
                continue
            cap = (r["max_bid_cpc"] if c["pay"] == "cpc" else r["max_bid_cpm"]) * 100
            for nm, b in c["bids"].items():
                for pl, v in b.items():
                    if pl == "recommendations" and c["pay"] == "cpc":
                        continue
                    if v > cap + 1 and W.min_bid(cid, nm) <= cap:
                        note("ставка выше потолка", f"{c['cab']} {cid} {nm} {pl}: {v / 100:.2f} > {cap / 100:.0f}")
    if what == "bids" and "--apply" in sys.argv:
        for cid, c in W.camps.items():
            r = rule_of(c["cab"], cid)
            if not r or not r.get("min_stock") or c["status"] not in (9,) or str(cid) not in (B.RULES.get(c["cab"]) or {}) and c["cab"] != "Яворская":
                continue
            low = [nm for nm in c["nms"] if round(W.nm[nm]["wb"]) + round(W.nm[nm]["mp"]) < r["min_stock"]]
            if low and len(low) < len(c["nms"]):
                note("малый остаток не убран", f"{c['cab']} {cid}: {len(low)} тов. < {r['min_stock']} шт")


managed_seen = set()
HOURS = defaultdict(lambda: defaultdict(int))   # кампания → {состояние: часов с 8 до 24}
STUCK = defaultdict(int)          # кампания → часов подряд «на паузе с деньгами»
STARVE = defaultdict(int)         # кампания → часов подряд «без денег, хотя потолки позволяют»
DAYSPEND = defaultdict(float)


def hourly_watch():
    h = NOW[0]
    for cid, c in W.camps.items():
        r = rule_of(c["cab"], cid)
        if not r or not r.get("topup_below"):
            continue
        up = USER_PAUSED.get(cid)
        if up and up[0] <= h < up[1]:
            if c["status"] == 9:
                note("запущена кампания, остановленная пользователем", f"{c['cab']} {cid}")
            continue
        if cid in (40863039, 40769350):     # паузы пользователя с начала
            if c["status"] == 9:
                note("запущена кампания, остановленная пользователем", f"{c['cab']} {cid}")
            continue
        if 8 <= h.hour <= 23 and str(cid) in (B.RULES.get(c["cab"]) or {}):
            lp = c["status"] == 11 and c["budget"] >= 100
            nomoney = c["budget"] < 100 and (c["status"] == 11 or True)
            HOURS[cid]["всего"] += 1
            if lp:
                HOURS[cid]["пауза по своему лимиту"] += 1
            elif nomoney:
                d = h.date()
                day = sum(x[3] for x in W.deposits if x[2] == cid and x[0].date() == d)
                week = sum(x[3] for x in W.deposits if x[2] == cid and (d - x[0].date()).days < 7)
                cabw = sum(x[3] for x in W.deposits if x[1] == c["cab"] and (d - x[0].date()).days < 7)
                wb = (B.RULES.get(c["cab"]) or {}).get("_week_budget") or 10 ** 9
                why = ("недельный потолок кампании" if r["topup_week_cap"] - week < 1000 else
                       "недельный бюджет кабинета" if wb - cabw < 1000 else
                       "потолок в сутки" if max(r["topup_day_cap"], 1000) - day < 1000 else "потолки позволяли")
                HOURS[cid]["без денег: " + why] += 1
        if c["status"] == 11 and c["budget"] >= 100:
            STUCK[cid] += 1
            if STUCK[cid] == 26:
                note("пауза с деньгами дольше суток", f"{c['cab']} {cid} «{c['name']}», бюджет {c['budget']:.0f} ₽")
        else:
            STUCK[cid] = 0
        if c["status"] == 11 and c["budget"] < 100 and 9 <= h.hour <= 22:
            d = h.date()
            day = sum(x[3] for x in W.deposits if x[2] == cid and x[0].date() == d)
            week = sum(x[3] for x in W.deposits if x[2] == cid and (d - x[0].date()).days < 7)
            cabw = sum(x[3] for x in W.deposits if x[1] == c["cab"] and (d - x[0].date()).days < 7)
            wb = (B.RULES.get(c["cab"]) or {}).get("_week_budget") or 10 ** 9
            room = min(max(r["topup_day_cap"], 1000) - day, r["topup_week_cap"] - week, wb - cabw)
            if room >= 1000:
                STARVE[cid] += 1
                if STARVE[cid] == 3:
                    note("стоит без денег, хотя потолки позволяют", f"{c['cab']} {cid} «{c['name']}» ({h:%d.%m %H:%M})")
            else:
                STARVE[cid] = 0
        else:
            STARVE[cid] = 0


def own_limit_report():
    """Свой дневной лимит (показы; клики с лимитом < 1000): расход за день против лимита."""
    res = []
    days = sorted({d for (d, cid) in W.spent_day})
    for cid, c in W.camps.items():
        r = rule_of(c["cab"], cid)
        if not r or not r.get("daily_limit"):
            continue
        own = c["pay"] == "cpm" or r["daily_limit"] < 1000
        if not own:
            continue
        for d in days[:-1]:
            s = W.spent_day.get((d, cid), 0)
            if s > 0:
                res.append((cid, c["cab"], d, s, r["daily_limit"]))
    return res


# ---------------- расписание и сценарий ----------------
def scenario(t):
    d = t.strftime("%m-%d")
    off = (d == "10-19" and t.hour < 12) or ("10-31" <= d <= "11-03" and not (d == "10-31" and t.hour < 18) and not (d == "11-03" and t.hour >= 7))
    return {"off": off}


def main():
    start = _dt.datetime(2026, 10, 15, 6, 0)
    NOW[0] = start
    mods()
    W.t = start
    end = start + _dt.timedelta(days=DAYS)
    ev_count = defaultdict(int)
    t = start
    last_budget = None
    while t < end:
        NOW[0] = t
        W.advance(t)
        # сценарий
        if t == _dt.datetime(2026, 10, 16, 7, 0):
            MODE[0] = "use"
            note("сценарий", "COLLECTOR_MODE = use")
        if t.strftime("%m-%d %H:%M") == "10-21 00:00":
            COLLECTOR_BROKEN[0] = True
            note("сценарий", "сборщик сломан на сутки")
        if t.strftime("%m-%d %H:%M") == "10-22 00:00":
            COLLECTOR_BROKEN[0] = False
        if t.strftime("%m-%d %H:%M") == "10-23 00:00":
            W.fault.update(read=0.03, deposit_unclear=0.15, deposit_fail=0.05, start_fail=0.10)
            note("сценарий", "день сбоев WB")
        if t.strftime("%m-%d %H:%M") == "10-24 00:00":
            W.fault.update(read=0.0, deposit_unclear=0.0, deposit_fail=0.0, start_fail=0.0)
        if t.strftime("%m-%d %H:%M") == "10-22 12:00":
            W.camps[31048671]["status"] = 11
            USER_PAUSED[31048671] = (t, _dt.datetime(2026, 10, 25, 12, 0))
            note("сценарий", "пользователь поставил на паузу 31048671 (деньги есть) до 25.10 12:00")
        if t.strftime("%m-%d %H:%M") == "10-25 12:00":
            W.camps[31048671]["status"] = 9
        sc = scenario(t)
        hm = t.strftime("%H:%M")
        if t.minute == 0:
            hourly_watch()
        if not sc["off"]:
            if t.hour >= 7 and t.minute in (0, 15, 30, 45):
                run("collector", "main", [])
                ev_count["collector"] += 1
            if hm in ("08:00", "13:00", "18:00"):
                NOW[0] = t + _dt.timedelta(seconds=30)
                run("bids", "main", ["--apply"])
                ev_count["bids"] += 1
                for cid, c in W.camps.items():
                    if c["status"] == 9:
                        managed_seen.add(cid)
                if hm == "08:00" and (last_budget is None or (t - last_budget).total_seconds() > 20 * 3600):
                    run("budget", "main", [])
                    last_budget = t
            if t.hour >= 7 and t.minute == 40:
                NOW[0] = t + _dt.timedelta(seconds=30)
                run("guard", "main", ["--apply"])
                ev_count["guard"] += 1
        if t.hour == 0 and t.minute == 0:
            sizes = {f: os.path.getsize(f) for f in os.listdir(".") if os.path.isfile(f) and not f.endswith(".py")}
            print(f"{t:%d.%m} запусков: {dict(ev_count)}; файлов {len(sizes)}, крупные: "
                  + ", ".join(f"{k} {v // 1024} КБ" for k, v in sorted(sizes.items(), key=lambda x: -x[1])[:4]), flush=True)
        nxt = t + _dt.timedelta(minutes=5)
        if NOW[0] >= nxt:                 # запуск затянулся — пропущенные события не догоняем (как IgnoreNew)
            n = NOW[0] + _dt.timedelta(minutes=5)
            nxt = n.replace(minute=(n.minute // 5) * 5, second=0, microsecond=0)
            note("запуск дольше 5 мин", f"{t:%d.%m %H:%M} → часы {NOW[0]:%H:%M}")
        t = nxt
    NOW[0] = end
    W.advance(end)
    report(ev_count)


def report(ev_count):
    rep = []
    rep.append(f"Модель {DAYS} дн. с 15.10.2026, seed {SEED}. Запусков: {dict(ev_count)}")
    by = defaultdict(list)
    for (t, what, sec, err) in RUNS:
        by[what].append(sec)
    for k, v in by.items():
        first, last = v[:5], v[-5:]
        rep.append(f"  {k}: {len(v)} запусков, сек (модель): первые {sum(first) / len(first):.2f}, последние {sum(last) / len(last):.2f}, макс {max(v):.2f}")
    rep.append("\nНАХОДКИ:")
    for kind, items in sorted(FIND.items(), key=lambda x: -len(x[1])):
        rep.append(f"  [{len(items)}] {kind}")
        for t, x in items[:6]:
            rep.append(f"      {t}  {x}")
    rep.append("\nДЕНЬГИ (пополнения по кабинетам по неделям):")
    wk = defaultdict(lambda: defaultdict(float))
    for (t, cab, cid, s) in W.deposits:
        wk[cab][t.isocalendar()[1]] += s
    for cab, v in wk.items():
        rep.append(f"  {cab}: " + ", ".join(f"нед.{k}: {x:.0f}" for k, x in sorted(v.items())) + f"; потолок кабинета {(B.RULES.get(cab) or {}).get('_week_budget')}")
    rep.append("\nЧАСЫ С 8 ДО 24 (кампании из RULES): без денег — почему; пауза по своему лимиту")
    for cid, hh in sorted(HOURS.items(), key=lambda x: (W.camps[x[0]]["cab"], x[0])):
        c = W.camps[cid]
        rest = {k: v for k, v in hh.items() if k != "всего" and v}
        rep.append(f"  {c['cab']:9} {cid} {c['name'][:24]:24} из {hh['всего']} ч: " + (", ".join(f"{k} {v}" for k, v in sorted(rest.items())) or "всё время работала"))
    rep.append("\nСВОЙ ДНЕВНОЙ ЛИМИТ (расход за сутки / лимит):")
    lim = own_limit_report()
    agg = defaultdict(list)
    for cid, cab, d, s, l in lim:
        agg[(cab, cid, l)].append(s / l)
    for (cab, cid, l), xs in sorted(agg.items()):
        rep.append(f"  {cab} {cid} лимит {l}: дней {len(xs)}, среднее {sum(xs) / len(xs):.2f}, макс {max(xs):.2f}, >1.2: {sum(1 for x in xs if x > 1.2)}")
    import re
    warn = defaultdict(lambda: [0, ""])
    for fn in sorted(os.listdir(OUT_DIR)):
        what = fn.split("_", 2)[-1][:-4]
        for line in open(os.path.join(OUT_DIR, fn), encoding="utf-8"):
            ls = line.strip()
            if ls.startswith(("!", "✗", "? ", "⏱")) or " ✗ " in ls or ls.startswith("✓✓") or "ВНИМАНИЕ" in ls:
                k = what + ": " + re.sub(r"\d[\d\s.,:]*", "N", ls)[:140]
                warn[k][0] += 1
                warn[k][1] = warn[k][1] or (fn[:9] + " " + ls[:200])
    rep.append("\nПРЕДУПРЕЖДЕНИЯ СКРИПТОВ (сколько раз, пример):")
    for k, (n, ex) in sorted(warn.items(), key=lambda x: -x[1][0])[:40]:
        rep.append(f"  {n:5}  {k}\n         пример: {ex}")
    rep.append("\nФАЙЛЫ в конце:")
    for f in sorted(os.listdir(".")):
        if os.path.isfile(f) and not f.endswith(".py"):
            rep.append(f"  {f}: {os.path.getsize(f) // 1024} КБ")
        elif os.path.isdir(f) and f not in ("_out", "__pycache__"):
            rep.append(f"  {f}/: {len(os.listdir(f))} файлов")
    rep.append("\nСОБЫТИЯ МИРА (последние 15): " + "; ".join(f"{t:%d.%m %H:%M} {x}" for t, x in W.events[-15:]))
    rep.append(f"ВЫЗОВЫ WB: {dict(sorted(W.calls.items(), key=lambda x: -x[1])[:8])}")
    txt = "\n".join(rep)
    open(os.path.join(RUN, "_report.txt"), "w", encoding="utf-8").write(txt)
    print(txt)


if __name__ == "__main__":
    main()
