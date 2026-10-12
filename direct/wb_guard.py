# -*- coding: utf-8 -*-
"""
Часовая проверка ставок WB (дополнение к автопилоту wb_bids.py, который идёт 3 раза в день).

За час рекомендованная ставка WB может заметно измениться. При оплате за клики WB списывает ровно нашу ставку:
если для нужных мест теперь хватает 10 ₽, а у нас стоит 20 — каждый клик вдвое дороже, чем нужно. И наоборот:
если WB поднял минимальную ставку выше нашей — товар выпадает из показов.

Раз в час проверка:
  1. Смотрит минимальные ставки WB. Ставка ниже минимума → поднимает до минимума, только если минимум НЕ выше
     окупаемого потолка товара; если выше — не трогает и пишет в отчёт (решать вам).
  2. Для самых кликабельных товаров (до REC_MAX штук, по кликам за 14 дней) смотрит рекомендованную ставку WB
     и двигает нашу за ней — по тем же правилам, что автопилот:
       • не выше окупаемого потолка (цена заказа × конверсия × поправка на выкуп — из последнего wb_bids.py --apply);
       • снижение — сразу; повышение — сразу, но только до потолка (решение 12.10.2026);
       • не ниже минимальной ставки WB;
       • товарам «без ходовых размеров» и «много кликов без заказов» — не повышает;
       • товары, которым автопилот сознательно держит минимальную ставку (нет в наличии и т. п.), не трогает.
Бюджеты, лимиты, минус-фразы, ставки по запросам — только wb_bids.py.

Безопасность:
  • без --apply — пробный прогон: только показывает;
  • --apply сохраняет резервную копию в wb_guard_backups\\, --rollback возвращает ставки последнего запуска;
  • пока идёт wb_bids.py (файл wb_bids.running), проверка ничего не делает;
  • потолки старше 2 суток (автопилот давно не применялся) — ничего не меняет, только сообщает.

Запуск:  python wb_guard.py              (пробный прогон)
         python wb_guard.py --apply      (применить)
         python wb_guard.py --rollback   (вернуть ставки последнего запуска)
По расписанию — каждый час с --apply (см. wb_schedule.ps1). Токен с категорией «Продвижение».
Время: WB отдаёт рекомендации раз в 12 с — при REC_MAX = 40 проверка идёт ≈9 минут.
Результат: вывод на экран + wb_guard_log.txt (дописывается).
"""
import datetime as dt
import json
import sys
from collections import defaultdict

import wb_bids as B

# ---------------- НАСТРОЙКИ ----------------
REC_MAX = 40                # сколько товаров за проверку сверять с рекомендацией WB (самые кликабельные)
CAPS_MAX_AGE_HOURS = 48     # потолки из wb_bids.py старше — не доверяю, ничего не меняю
LOCK_MAX_AGE_HOURS = 2      # файл wb_bids.running старше — считаю, что автопилот упал, и работаю
# -------------------------------------------

BACKUP_DIR = B.HERE / "wb_guard_backups"
LOG_FILE = B.HERE / "wb_guard_log.txt"
log = B.log


def busy():
    if not B.LOCK_FILE.exists():
        return False
    try:
        t = dt.datetime.fromisoformat(B.LOCK_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    return dt.datetime.now() - t < dt.timedelta(hours=LOCK_MAX_AGE_HOURS)


def write_log():
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write("\n".join(B.out) + "\n\n")


def rec_for(token, cid, nm, pay, target):
    data = B.get_rec(token, cid, nm)
    rec = B.rec_cpc(data, B.CPC_LEVEL[target]) if pay == "cpc" else B.rec_cpm(data, B.CPM_LEVEL[target])
    if rec is None and data and target != "3-10":    # нет уровня для мест 1–2 — как автопилот, берём 3–10
        rec = B.rec_cpc(data, B.CPC_LEVEL["3-10"]) if pay == "cpc" else B.rec_cpm(data, B.CPM_LEVEL["3-10"])
    return rec


def plan_cabinet(cab, token, caps, state, budget):
    """Возвращает (изменения, «минимум выше окупаемого»). budget — сколько ещё товаров можно сверить с рекомендацией."""
    by_c = defaultdict(dict)
    for key, zones in caps.items():
        cid, nm = key.split(":")
        by_c[int(cid)][int(nm)] = zones
    camps = B.get_campaigns(token)
    cand = []                          # (кампания, товар, зона, данные кампании) — только активные кампании
    for cid, items in by_c.items():
        a = camps.get(cid)
        if a and a.get("_status") == 9:
            for nm, zones in items.items():
                for zone, c in zones.items():
                    cand.append((cid, nm, zone, c if isinstance(c, dict) else {"cap": c, "fixed": True}, a))
    # минимальные ставки — по всем товарам (дёшево: один запрос на кампанию и зону)
    mins = {}
    for cid, zone in sorted({(x[0], x[2]) for x in cand}):
        a = camps[cid]
        nms = [x[1] for x in cand if x[0] == cid and x[2] == zone]
        for nm, v in B.get_min_bids(token, cid, nms, (a.get("settings") or {}).get("payment_type"), zone).items():
            mins[(cid, nm, zone)] = v
    # рекомендации — самым кликабельным, не «зафиксированным» правилом
    rec_ok = {(x[0], x[1], x[2]) for x in sorted((x for x in cand if not x[3].get("fixed")),
                                                  key=lambda x: -(x[3].get("clicks") or 0))[:budget]}
    changes, losing, checked = [], [], 0
    for cid, nm, zone, c, a in sorted(cand, key=lambda x: (x[0], -(x[3].get("clicks") or 0))):
        st = a.get("settings") or {}
        cur_all = {s.get("nm_id"): s.get("bids_kopecks") or {} for s in a.get("nm_settings") or []}
        if nm not in cur_all:
            continue                                   # товар убрали из кампании
        cur = cur_all[nm].get("search" if zone == "combined" else zone)
        mn, cap = mins.get((cid, nm, zone)), c["cap"]
        if not cur:
            continue
        base = {"cabinet": cab, "advert_id": cid, "campaign": st.get("name"), "nm_id": nm, "placement": zone,
                "old": cur, "cap": cap}
        if mn and mn > cap:                            # окупаемо только ниже минимума WB
            if cur < mn:
                losing.append({**base, "new": mn})
            continue
        if (cid, nm, zone) in rec_ok:
            checked += 1
            try:
                rec = rec_for(token, cid, nm, c.get("pay") or st.get("payment_type"), c.get("target") or "3-10")
            except B.ApiError as e:
                log(f"  ! {e}")
                rec = None
            if rec:
                step = c.get("step") or 1
                nb, why = B.new_bid(cur, rec, cap, mn or 0, step, c.get("max_raise"),
                                    B.day_base(state, "bids", cid, nm, zone), c.get("max_drop"))
                if c.get("no_raise") and nb > cur:
                    nb, why = cur, "не повышаю (мало размеров или много кликов без заказов)"
                if B.changed(nb, cur, step):
                    changes.append({**base, "new": nb, "rec": rec, "why": why})
                continue
        if mn and cur < mn:                            # рекомендацию не смотрели — только минимум
            changes.append({**base, "new": mn, "rec": None, "why": "ниже минимума WB → минимальная"})
    return changes, losing, checked


def main():
    cabs = B.load_tokens()
    if "--rollback" in sys.argv:
        files = sorted(BACKUP_DIR.glob("guard_*.json"))
        if not files:
            sys.exit("Резервных копий нет.")
        items = json.loads(files[-1].read_text(encoding="utf-8"))
        log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — ОТКАТ по {files[-1].name}: вернуть {len(items)} ставок "
            "(применяется сразу; если автопилот после этого уже менял ставки — они будут перезаписаны)")
        for cab in sorted({i["cabinet"] for i in items}):
            if cab in cabs:
                n = B.apply_bids(cabs[cab], [i for i in items if i["cabinet"] == cab], "old")
                log(f"  {cab}: возвращено ставок {n}")
        write_log()
        return

    apply = "--apply" in sys.argv
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — часовая проверка ставок WB "
        f"({'ПРИМЕНЕНИЕ' if apply else 'пробный прогон, ничего не меняется'})")
    if busy():
        log("  Сейчас работает wb_bids.py — проверку пропускаю.")
        write_log()
        return
    state = B.load_state()
    caps = state.get("caps") or {}
    try:
        age = dt.datetime.now() - dt.datetime.fromisoformat(caps.get("at") or "")
    except ValueError:
        age = None
    if not caps.get("items") or age is None:
        log("  Нет окупаемых потолков: автопилот ещё ни разу не применялся (python wb_bids.py --apply). Ничего не меняю.")
        write_log()
        return
    if age > dt.timedelta(hours=CAPS_MAX_AGE_HOURS):
        log(f"  Потолки устарели: автопилот применялся {age.days} дн. назад. Ничего не меняю — запустите wb_bids.py.")
        write_log()
        return

    changes, losing, budget = [], [], REC_MAX
    for cab, token in cabs.items():
        try:   # ID кампаний у WB уникальны: в каждом кабинете проверяются только его кампании
            ch, lo, n = plan_cabinet(cab, token, caps["items"], state, budget)
        except B.ApiError as e:
            log(f"  ✗ {cab}: {e}")
            continue
        changes += ch
        losing += lo
        budget = max(budget - n, 0)
    print(" " * 60, end="\r")
    log(f"  Сверено с рекомендацией WB: {REC_MAX - budget} тов.; изменений ставок: {len(changes)}.")
    for it in changes:
        arrow = "↑" if it["new"] > it["old"] else "↓"
        log(f"  {arrow} «{it['campaign']}» {it['nm_id']}: {B.rub(it['old'])} → {B.rub(it['new'])}"
            + (f" (рекоменд. WB {B.rub(it['rec'])}, потолок {B.rub(it['cap'])})" if it.get("rec") else
               f" (потолок {B.rub(it['cap'])})") + f" — {it['why']}")
    if losing:
        log("  Минимальная ставка WB выше окупаемого потолка — НЕ поднимаю (товар может выпасть из показов):")
        for it in losing:
            log(f"    ! «{it['campaign']}» {it['nm_id']}: ставка {B.rub(it['old'])}, минимум WB {B.rub(it['new'])}, "
                f"окупается до {B.rub(it['cap'])}")
        log("    Решить: оставить как есть, убрать товар из кампании или поднять допустимую цену заказа в wb_bids.py.")

    if apply and changes:
        BACKUP_DIR.mkdir(exist_ok=True)
        path = BACKUP_DIR / f"guard_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(changes, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"  Резервная копия: {path.name}")
        B.record_day_base(state, {"bids": changes, "cluster_bids": []})   # ставка на начало дня (если в правилах вернут ограничения в сутки)
        for cab in sorted({i["cabinet"] for i in changes}):
            n = B.apply_bids(cabs[cab], [i for i in changes if i["cabinet"] == cab], "new")
            log(f"  {cab}: изменено ставок {n} из {sum(1 for i in changes if i['cabinet'] == cab)}")
        B.save_state(state)
        log("  Откат: python wb_guard.py --rollback")
    elif changes:
        log("  Это пробный прогон. Применить: python wb_guard.py --apply")
    write_log()


if __name__ == "__main__":
    main()
