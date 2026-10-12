# -*- coding: utf-8 -*-
"""
Часовая «сторожевая» проверка ставок WB (дополнение к автопилоту wb_bids.py).

Если WB поднял минимальную ставку и наша ставка оказалась ниже минимума, товар выпадает из показов до следующего
прогона автопилота. Эта проверка раз в час смотрит только минимальные ставки (несколько запросов, ≈1 минута) и:
  • поднимает ставку до нового минимума, если минимум НЕ выше окупаемого потолка товара
    (потолок — из последнего применения wb_bids.py --apply: цена заказа × конверсия × поправка на выкуп);
  • если минимум выше окупаемого потолка — ставку НЕ поднимает, а пишет в отчёт: реклама этого товара
    по минимальной ставке убыточна, решать вам (оставить, убрать из кампании, поднять допустимую цену заказа).
Больше ничего не меняет: рекомендации, бюджеты, лимиты, минус-фразы — дело wb_bids.py.

Безопасность:
  • без --apply — пробный прогон: только показывает;
  • --apply сохраняет резервную копию в wb_guard_backups\\, --rollback возвращает ставки последнего запуска;
  • пока идёт wb_bids.py (файл wb_bids.running), проверка ничего не делает;
  • потолки старше 2 суток (автопилот давно не применялся) — ничего не меняет, только сообщает.

Запуск:  python wb_guard.py              (пробный прогон)
         python wb_guard.py --apply      (поднять до минимума, где это окупается)
         python wb_guard.py --rollback   (вернуть ставки последнего запуска)
По расписанию — каждый час с --apply (см. wb_schedule.ps1). Токен с категорией «Продвижение».
Результат: вывод на экран + wb_guard_log.txt (дописывается).
"""
import datetime as dt
import json
import sys
from collections import defaultdict

import wb_bids as B

# ---------------- НАСТРОЙКИ ----------------
CAPS_MAX_AGE_HOURS = 48     # потолки из wb_bids.py старше — не доверяю, ничего не меняю
LOCK_MAX_AGE_HOURS = 2      # файл wb_bids.running старше — считаю, что автопилот упал, и работаю
# -------------------------------------------

BACKUP_DIR = B.HERE / "wb_guard_backups"
LOG_FILE = B.HERE / "wb_guard_log.txt"
log = B.log


def rub(k):
    return B.rub(k)


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


def plan_cabinet(cab, token, caps):
    """Ставки ниже минимума WB: поднять (если окупается) или сообщить."""
    by_c = defaultdict(dict)
    for key, zones in caps.items():
        cid, nm = key.split(":")
        by_c[int(cid)][int(nm)] = zones
    camps = B.get_campaigns(token)
    raise_, losing = [], []
    for cid, items in sorted(by_c.items()):
        a = camps.get(cid)
        if not a or a.get("_status") != 9:
            continue                                   # кампания не активна — показов и так нет
        st = a.get("settings") or {}
        name, pay = st.get("name"), st.get("payment_type")
        cur_all = {s.get("nm_id"): s.get("bids_kopecks") or {} for s in a.get("nm_settings") or []}
        zones = sorted({z for zs in items.values() for z in zs})
        for zone in zones:
            nms = [nm for nm, zs in items.items() if zone in zs and nm in cur_all]
            if not nms:
                continue
            mins = B.get_min_bids(token, cid, nms, pay, zone)
            key_cur = "search" if zone == "combined" else zone
            for nm in nms:
                cur, mn, cap = cur_all[nm].get(key_cur), mins.get(nm), items[nm][zone]
                if not mn or not cur or cur >= mn:
                    continue
                it = {"cabinet": cab, "advert_id": cid, "campaign": name, "nm_id": nm, "placement": zone,
                      "old": cur, "new": mn, "cap": cap}
                (raise_ if mn <= cap else losing).append(it)
    return raise_, losing


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
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — проверка минимальных ставок WB "
        f"({'ПРИМЕНЕНИЕ' if apply else 'пробный прогон, ничего не меняется'})")
    if busy():
        log("  Сейчас работает wb_bids.py — проверку пропускаю.")
        write_log()
        return
    caps = (B.load_state().get("caps") or {})
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

    all_raise, all_losing = [], []
    for cab, token in cabs.items():
        try:   # ID кампаний у WB уникальны: в каждом кабинете проверяются только его кампании
            r, l = plan_cabinet(cab, token, caps["items"])
        except B.ApiError as e:
            log(f"  ✗ {cab}: {e}")
            continue
        all_raise += r
        all_losing += l

    if not all_raise and not all_losing:
        log("  Все ставки не ниже минимальных — менять нечего.")
    for it in all_raise:
        log(f"  ↑ «{it['campaign']}» {it['nm_id']}: ставка {rub(it['old'])} ниже минимума WB → {rub(it['new'])} "
            f"(окупаемый потолок {rub(it['cap'])})")
    if all_losing:
        log("  Минимальная ставка WB выше окупаемого потолка — НЕ поднимаю (товар может выпасть из показов):")
        for it in all_losing:
            log(f"    ! «{it['campaign']}» {it['nm_id']}: ставка {rub(it['old'])}, минимум WB {rub(it['new'])}, "
                f"окупается до {rub(it['cap'])}")
        log("    Решить: оставить как есть, убрать товар из кампании или поднять допустимую цену заказа в wb_bids.py.")

    if apply and all_raise:
        BACKUP_DIR.mkdir(exist_ok=True)
        path = BACKUP_DIR / f"guard_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(all_raise, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"  Резервная копия: {path.name}")
        for cab in sorted({i["cabinet"] for i in all_raise}):
            n = B.apply_bids(cabs[cab], [i for i in all_raise if i["cabinet"] == cab], "new")
            log(f"  {cab}: поднято ставок {n} из {sum(1 for i in all_raise if i['cabinet'] == cab)}")
        log("  Откат: python wb_guard.py --rollback")
    elif all_raise:
        log("  Это пробный прогон. Применить: python wb_guard.py --apply")
    write_log()


if __name__ == "__main__":
    main()
