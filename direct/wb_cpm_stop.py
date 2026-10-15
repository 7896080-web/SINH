# -*- coding: utf-8 -*-
"""
Промежуточная проверка эксперимента Ребрик/Караман (решение пользователя 15.10.2026: «проверка 20.10, если показы
без заказов — остановить»).

Берёт кампании за показы Ребрика и Карамана из RULES wb_bids.py и смотрит их статистику с начала эксперимента (14.10)
по вчера. Кампания потратила не меньше JUDGE_SPEND ₽ и не принесла ни одного заказа с рекламы — остановить:
  1. вписать в стоп-лист wb_hold.txt — иначе частая проверка лимита (wb_guard.py --limits) утром запустила бы её снова,
     а автопилот и часовая проверка пополнили бы;
  2. поставить на паузу, если она сейчас работает.
Деньги, оставшиеся в бюджете кампании, остаются в ней (WB их не возвращает). Для сравнения показаны кампании за клики
тех же кабинетов: сколько стоил заказ у них.

Запуск:  python wb_cpm_stop.py               — пробный прогон: таблица и план, ничего не меняет
         python wb_cpm_stop.py --apply       — остановить (с CHECK_DATE; резервная копия в wb_bids_backups)
         python wb_cpm_stop.py --rollback    — вернуть последнюю остановку: убрать из стоп-листа, вернуть кампании под
                                               управление (запускает их частая проверка лимита по графику расхода)
Откат возвращает только последний --apply и применяется сразу, без пробного прогона.
Нужна категория токена «Продвижение». Журнал — wb_cpm_stop_log.txt.
"""
import datetime as dt
import json
import sys
from collections import defaultdict

import wb_bids as B

# ---------------- НАСТРОЙКИ ----------------
CABINETS = ("Ребрик", "Караман")      # кабинеты эксперимента
FROM = dt.date(2026, 10, 14)           # начало эксперимента — с этого дня считаем заказы
CHECK_DATE = dt.date(2026, 10, 20)     # раньше этого дня --apply не останавливает (решение пользователя — проверка 20.10)
JUDGE_SPEND = 1000                     # меньше потратила (₽) — судить рано, не трогаю
MIN_ORDERS = 1                         # меньше стольких заказов с рекламы — остановить («показы без заказов»)
LOG_FILE = B.HERE / "wb_cpm_stop_log.txt"
# -------------------------------------------
log = B.log


def stats(token, ids, d1, d2):
    """{кампания: {views, clicks, sum, orders, sum_price}} за d1..d2 (окнами по 31 день)."""
    res = defaultdict(lambda: defaultdict(float))
    a = d1
    while a <= d2:
        b = min(d2, a + dt.timedelta(days=30))
        for part in B.chunks(ids, 50):
            B.pace(token, "fullstats", 21)
            data = B.call("GET", B.ADV + "/adv/v3/fullstats", token,
                          params={"ids": ",".join(map(str, part)), "beginDate": a.isoformat(), "endDate": b.isoformat()},
                          what="Статистика кампаний")
            for item in data or []:
                aid = item.get("advertId")
                for day in item.get("days") or []:
                    for app in day.get("apps") or []:
                        for n in app.get("nms") or []:
                            for k in ("views", "clicks", "sum", "orders", "sum_price"):
                                res[aid][k] += B.num(n.get(k))
        a = b + dt.timedelta(days=1)
    return res


def plan_cabinet(cab, token, d2):
    """(к остановке, строки таблицы) по кабинету."""
    camps = B.get_campaigns(token)
    ids = [int(k) for k in (B.RULES.get(cab) or {}) if str(k).isdigit() and int(k) in camps]
    pay = {cid: (camps[cid].get("settings") or {}).get("payment_type") for cid in ids}
    cpm = [cid for cid in ids if pay[cid] == "cpm"]
    if not cpm:
        log(f"\n{cab}: кампаний за показы из RULES нет.")
        return [], []
    st = stats(token, ids, FROM, d2)
    cpc_sum = sum(st[c]["sum"] for c in ids if pay[c] == "cpc")
    cpc_ord = sum(st[c]["orders"] for c in ids if pay[c] == "cpc")
    log(f"\n{'═' * 20} {cab} {'═' * 20}")
    log(f"  За клики (для сравнения): {B.fm_rub(cpc_sum)} ₽, заказов {cpc_ord:.0f}"
        + (f", заказ ≈{B.fm_rub(cpc_sum / cpc_ord)} ₽" if cpc_ord else ""))
    log(f"  {'кампания за показы':34}{'потрачено':>11}{'клики':>7}{'клик':>8}{'заказы':>8}{'заказ':>9}  решение")
    stop, rows = [], []
    for cid in cpm:
        s = st[cid]
        name = (camps[cid].get("settings") or {}).get("name") or str(cid)
        status = camps[cid].get("_status")
        if B.on_hold(cid):
            why = "уже в стоп-листе — не трогаю"
        elif s["sum"] < JUDGE_SPEND:
            why = f"потратила меньше {JUDGE_SPEND} ₽ — судить рано, оставляю"
        elif s["orders"] < MIN_ORDERS:
            why = "ни одного заказа — ОСТАНОВИТЬ"
            stop.append({"cabinet": cab, "advert_id": cid, "name": name, "status_before": status,
                         "spent": round(s["sum"], 2), "orders": s["orders"]})
        else:
            cpo = s["sum"] / s["orders"]
            ref = cpc_sum / cpc_ord if cpc_ord else None
            why = "есть заказы — оставляю (решение по итогам 27.10)" + (
                f"; заказ в {cpo / ref:.1f} раза дороже, чем за клики" if ref and cpo > ref * 1.5 else "")
        log(f"  {B.cut(name, 24):24}{cid:>10}{B.fm_rub(s['sum']):>11}"
            f"{s['clicks']:>7.0f}{(B.fm_rub(s['sum'] / s['clicks']) if s['clicks'] else '—'):>8}{s['orders']:>8.0f}"
            f"{(B.fm_rub(s['sum'] / s['orders']) if s['orders'] else '—'):>9}  {why}")
        rows.append({"cabinet": cab, "advert_id": cid, "name": name, "why": why})
    return stop, rows


def hold_add(items):
    """Дописать номера в стоп-лист (строки файла, бывшие до этого, не трогаю)."""
    try:
        text = B.HOLD_FILE.read_text(encoding="utf-8-sig")
    except OSError:
        text = "# Стоп-лист: номера кампаний WB (по одному в строке), которые скрипты не запускают и не пополняют.\n"
    if text and not text.endswith("\n"):
        text += "\n"
    for i in items:
        text += (f"{i['advert_id']}   # «{i['name']}» — {dt.date.today():%d.%m.%Y} wb_cpm_stop.py: "
                 f"{B.fm_rub(i['spent'])} ₽ с {FROM:%d.%m} без заказов\n")
    B.HOLD_FILE.write_text(text, encoding="utf-8")


def hold_remove(ids):
    try:
        lines = B.HOLD_FILE.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    ids = {str(i) for i in ids}
    keep = [ln for ln in lines if ln.split("#")[0].strip() not in ids]
    B.HOLD_FILE.write_text("\n".join(keep) + "\n", encoding="utf-8")


def write_log():
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(B.out) + "\n\n")
    except OSError:
        pass


def apply(cabs, stop):
    state = B.load_state()
    saved = {str(i["advert_id"]): {k: (state.get(k) or {}).get(str(i["advert_id"]))
                                   for k in ("limit_paused", "pace_paused")} for i in stop}
    B.BACKUP_DIR.mkdir(exist_ok=True)
    bk = B.BACKUP_DIR / f"cpmstop_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    bk.write_text(json.dumps({"items": stop, "state": saved}, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"\nРезервная копия: {bk.name}")
    hold_add(stop)                      # сначала стоп-лист: даже если пауза не встанет, скрипты её не запустят
    for i in stop:
        for k in ("limit_paused", "pace_paused", "start_pending"):
            (state.get(k) or {}).pop(str(i["advert_id"]), None)
        if i["status_before"] == 9:
            B.pace(cabs[i["cabinet"]], "start", 1.1)
            try:
                B.call("GET", B.ADV + "/adv/v0/pause", cabs[i["cabinet"]], params={"id": i["advert_id"]},
                       what="Пауза кампании")
                log(f"  ⏹ «{i['name']}»: в стоп-листе, поставлена на паузу.")
            except B.ApiError as e:
                log(f"  ✗ «{i['name']}»: в стоп-листе, но пауза не встала — {e}. Поставьте на паузу в кабинете WB: "
                    "скрипты её больше не запустят и не пополнят.")
        else:
            log(f"  ⏹ «{i['name']}»: уже стояла на паузе — в стоп-листе, скрипты её больше не запустят и не пополнят.")
    B.save_state(state)


def rollback(cabs):
    files = sorted(B.BACKUP_DIR.glob("cpmstop_*.json"))
    if not files:
        sys.exit("Резервных копий остановки нет.")
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    items = data.get("items") or []
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — ОТКАТ по {files[-1].name} (применяется сразу): "
        + ", ".join(f"«{i['name']}»" for i in items))
    hold_remove([i["advert_id"] for i in items])
    state = B.load_state()
    for i in items:
        cid = str(i["advert_id"])
        # кампания снова «наша»: частая проверка лимита запустит её по графику расхода (с утра — после 7:00)
        state.setdefault("pace_paused", {})[cid] = {"date": (dt.date.today() - dt.timedelta(days=1)).isoformat(),
                                                   "at": "откат остановки", "spent": None, "limit": None}
        log(f"  ↩ «{i['name']}»: убрана из стоп-листа — запустит проверка лимита по графику расхода.")
    B.save_state(state)


def main():
    cabs = {k: v for k, v in B.load_tokens().items() if k in CABINETS}
    if not cabs:
        sys.exit(f"В wb_tokens.txt нет кабинетов {', '.join(CABINETS)}.")
    if "--rollback" in sys.argv:
        if not B.lock_acquire("wb_cpm_stop.py --rollback", wait_min=5):
            sys.exit("Сейчас работает автопилот или проверка больше 5 мин — запустите позже.")
        try:
            rollback(cabs)
        finally:
            B.lock_release()
            write_log()
        return
    do = "--apply" in sys.argv
    d2 = dt.date.today() - dt.timedelta(days=1)
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — проверка кампаний за показы ({'ПРИМЕНЕНИЕ' if do else 'пробный прогон'}); "
        f"заказы с {FROM:%d.%m} по {d2:%d.%m} ({(d2 - FROM).days + 1} дн.)")
    if do and dt.date.today() < CHECK_DATE:
        sys.exit(f"Проверка назначена на {CHECK_DATE:%d.%m} — до этого --apply ничего не останавливает. "
                 "Без --apply можно смотреть таблицу.")
    stop = []
    for cab, token in cabs.items():
        try:
            s, _ = plan_cabinet(cab, token, d2)
        except B.ApiError as e:
            log(f"\n{cab}: ! {e} — кабинет пропущен, ничего не останавливаю.")
            continue
        stop += s
    if not stop:
        log("\nОстанавливать нечего.")
    elif not do:
        log(f"\nПлан: остановить {len(stop)}: " + ", ".join(f"«{i['name']}»" for i in stop)
            + ". Применить: python wb_cpm_stop.py --apply")
    else:
        if not B.lock_acquire("wb_cpm_stop.py", wait_min=5):
            log("Сейчас работает автопилот или проверка больше 5 мин — ничего не сделано, запустите позже.")
        else:
            try:
                apply(cabs, stop)
            finally:
                B.lock_release()
    if do:
        write_log()


if __name__ == "__main__":
    main()
