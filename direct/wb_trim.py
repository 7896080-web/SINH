# -*- coding: utf-8 -*-
"""
Убрать из АКТИВНЫХ рекламных кампаний WB товары, которых осталось мало (по умолчанию меньше 15 шт
на складах WB и своём складе вместе): реклама таких товаров быстро упирается в пустой остаток и размеры.

Безопасность:
  • без --apply — пробный прогон: только показывает, что будет убрано, и что эти товары дали рекламе за 7 дней;
  • кампанию целиком не опустошает: если убирать пришлось бы все её товары, кампания не трогается,
    а в отчёте предлагается поставить её на паузу;
  • товары, по которым WB не отдал остаток, не трогаются;
  • --apply сначала сохраняет резервную копию (товары и их ставки) в wb_trim_backups\\;
  • --rollback возвращает товары последнего запуска в кампании и ставит им прежние ставки
    (WB при добавлении ставит минимальную, поэтому ставки восстанавливаются отдельно).

Запуск:  python wb_trim.py                      (пробный прогон, порог 15 шт)
         python wb_trim.py 30                   (порог 30 шт)
         python wb_trim.py --apply              (убрать, с резервной копией)
         python wb_trim.py --rollback           (вернуть последний запуск)
         python wb_trim.py --cabinet "Яворская"
Нужны категории токена: «Продвижение», «Аналитика»; токен с правом изменений.
Результат: вывод на экран + wb_trim_log.txt (дописывается).
"""
import datetime as dt
import json
import sys
from collections import defaultdict

from wb_common import (ADV, HERE, YESTERDAY, ApiError, ad_stats, arg_cabinet, call, chunks, cut, fm, funnel,
                       load_tokens, log, num, pace, _out)

# ---------------- НАСТРОЙКИ ----------------
MIN_STOCK = 15                # убирать товары, у которых в наличии (WB + свой склад) меньше стольких штук (правило 12.10.2026)
COUNT_FBS_STOCK = True        # учитывать свой склад (FBS); False — только склады WB
STATS_DAYS = 7                # за сколько дней показать, что эти товары дали рекламе
# -------------------------------------------

BACKUP_DIR = HERE / "wb_trim_backups"
LOG_FILE = HERE / "wb_trim_log.txt"


def threshold():
    for a in sys.argv[1:]:
        if a.isdigit():
            return int(a)
    return MIN_STOCK


def active_campaigns(token):
    pace(token, "count", 0.3)
    data = call("GET", ADV + "/adv/v1/promotion/count", token, what="Список кампаний") or {}
    ids = [a["advertId"] for g in data.get("adverts") or [] if g.get("status") == 9 for a in g.get("advert_list") or []]
    camps = {}
    for part in chunks(ids, 50):
        pace(token, "adverts", 0.3)
        d = call("GET", ADV + "/api/advert/v2/adverts", token, params={"ids": ",".join(map(str, part))},
                 what="Настройки кампаний") or {}
        for a in d.get("adverts") or []:
            if a.get("id") in ids:
                camps[a["id"]] = a
    return camps


def plan_cabinet(cab, token, limit):
    camps = active_campaigns(token)
    if not camps:
        log("  Активных кампаний нет.")
        return []
    nms = sorted({s.get("nm_id") for a in camps.values() for s in a.get("nm_settings") or [] if s.get("nm_id")})
    info = funnel(token, YESTERDAY, YESTERDAY, nms)
    d1 = YESTERDAY - dt.timedelta(days=STATS_DAYS - 1)
    try:
        _, by_camp = ad_stats(token, list(camps), d1, YESTERDAY)
    except ApiError as e:
        log(f"  ! Статистика рекламы: {e} — покажу без неё.")
        by_camp = {}
    plan = []
    for cid, a in sorted(camps.items(), key=lambda x: str((x[1].get("settings") or {}).get("name"))):
        st = a.get("settings") or {}
        name = st.get("name")
        items = a.get("nm_settings") or []
        drop, keep, unknown = [], [], []
        for s in items:
            nm = s.get("nm_id")
            f = info.get(nm)
            if not f or (f.get("stock_wb") is None and f.get("stock_mp") is None):
                unknown.append(nm)
                continue
            qty = (f.get("stock_wb") or 0) + ((f.get("stock_mp") or 0) if COUNT_FBS_STOCK else 0)
            (drop if qty < limit else keep).append((s, f, qty))
        log(f"\n  ▶ {name} (ID {cid}), {('за клики' if st.get('payment_type') == 'cpc' else 'за показы')}: "
            f"товаров {len(items)}, убрать {len(drop)}, оставить {len(keep)}"
            + (f", остаток неизвестен у {len(unknown)} (не трогаю)" if unknown else ""))
        if not drop:
            continue
        if not keep and not unknown:
            log(f"    ! У всех товаров кампании меньше {limit} шт. — кампанию не опустошаю. "
                "Если реклама ей не нужна, поставьте её на паузу в кабинете WB.")
            for s, f, qty in drop:
                log(f"      {s.get('nm_id'):>11} {cut(f.get('vendor'), 22):22} в наличии {qty:.0f}")
            continue
        for s, f, qty in drop:
            nm = s.get("nm_id")
            v = by_camp.get((cid, nm)) or defaultdict(float)
            log(f"    − {nm:>11} {cut(f.get('vendor'), 22):22} WB {f.get('stock_wb') or 0:.0f} + свой {f.get('stock_mp') or 0:.0f} "
                f"= {qty:.0f} шт; за {STATS_DAYS} дн: расход {fm(v['sum'])} ₽, клики {fm(v['clicks'])}, заказы {fm(v['orders'])}")
            plan.append({"cabinet": cab, "advert_id": cid, "campaign": name, "nm_id": nm, "stock": qty,
                         "bid_type": a.get("bid_type"), "bids_kopecks": s.get("bids_kopecks") or {}})
    return plan


def patch_nms(token, advert_id, add=(), delete=()):
    """Изменить товары кампании. WB отказывает («daily limit validation failed»), пока дневной лимит кампании ниже его
    минимума при нынешних ставках, — тогда, как автопилот, лимит на время правки поднимается до минимума WB."""
    try:
        return _patch_nms(token, advert_id, add, delete)
    except ApiError as e:
        if "daily limit validation failed" not in str(e):
            raise
        import wb_bids as B
        try:
            r = B.nms_with_limit_room(token, advert_id, lambda: _patch_nms(token, advert_id, add, delete))
        except B.ApiError as e2:
            raise ApiError(str(e2), e2.status) from e2
        if r is None:
            raise
        return r


def _patch_nms(token, advert_id, add=(), delete=()):
    pace(token, "auction_nms", 1.1)
    body = {"nms": [{"advert_id": advert_id, "nms": {"add": list(add), "delete": list(delete)}}]}
    data = call("PATCH", ADV + "/adv/v0/auction/nms", token, body=body, what="Товары кампании") or {}
    done = {"added": [], "deleted": []}
    for it in data.get("nms") or []:
        if it.get("advert_id") == advert_id:
            r = it.get("nms") or {}
            done["added"] += r.get("added") or []
            done["deleted"] += r.get("deleted") or []
    return done


def restore_bids(token, items):
    """Вернуть ставки товарам, которые WB добавил обратно с минимальной ставкой."""
    by_c = defaultdict(list)
    for i in items:
        b = i.get("bids_kopecks") or {}
        if i.get("bid_type") == "unified":
            if b.get("search"):
                by_c[i["advert_id"]].append({"nm_id": i["nm_id"], "bid_kopecks": int(b["search"]), "placement": "combined"})
        else:
            for p in ("search", "recommendations"):
                if b.get(p):
                    by_c[i["advert_id"]].append({"nm_id": i["nm_id"], "bid_kopecks": int(b[p]), "placement": p})
    ok = 0
    for aid, nmb in by_c.items():
        for part in chunks(nmb, 50):
            pace(token, "patch", 0.3)
            try:
                call("PATCH", ADV + "/api/advert/v1/bids", token, body={"bids": [{"advert_id": aid, "nm_bids": part}]},
                     what="Ставки товаров")
                ok += len(part)
            except ApiError as e:
                log(f"  ✗ {e}")
    return ok


def write_log():
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write("\n".join(_out) + "\n\n")


def main():
    cabs = load_tokens(arg_cabinet())
    if "--rollback" in sys.argv:
        files = sorted(BACKUP_DIR.glob("trim_*.json"))
        if not files:
            sys.exit("Резервных копий нет.")
        plan = json.loads(files[-1].read_text(encoding="utf-8"))
        log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — ОТКАТ по {files[-1].name}: вернуть {len(plan)} тов. в кампании")
        by = defaultdict(list)
        for i in plan:
            by[(i["cabinet"], i["advert_id"])].append(i)
        for (cab, aid), items in by.items():
            if cab not in cabs:
                log(f"  ✗ Нет токена для «{cab}»")
                continue
            try:
                r = patch_nms(cabs[cab], aid, add=[i["nm_id"] for i in items])
                log(f"  «{items[0]['campaign']}»: возвращено {len(r['added'])} из {len(items)}")
            except ApiError as e:
                log(f"  ✗ «{items[0]['campaign']}»: {e}")
                continue
            log(f"    ставки восстановлены: {restore_bids(cabs[cab], items)}")
        write_log()
        return

    limit = threshold()
    apply = "--apply" in sys.argv
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — {'ПРИМЕНЕНИЕ' if apply else 'пробный прогон, ничего не меняется'}: "
        f"убрать из активных кампаний товары, которых в наличии меньше {limit} шт "
        f"({'склады WB + свой склад' if COUNT_FBS_STOCK else 'только склады WB'})")
    plan = []
    for cab, token in cabs.items():
        log(f"\n══════════ {cab} ══════════")
        try:
            plan += plan_cabinet(cab, token, limit)
        except ApiError as e:
            log(f"  ✗ {cab}: {e}")
    log(f"\nИтого убрать: {len(plan)} тов. из {len({(i['cabinet'], i['advert_id']) for i in plan})} кампаний.")
    if apply and plan:
        BACKUP_DIR.mkdir(exist_ok=True)
        path = BACKUP_DIR / f"trim_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"Резервная копия: {path.name}")
        by = defaultdict(list)
        for i in plan:
            by[(i["cabinet"], i["advert_id"])].append(i)
        for (cab, aid), items in by.items():
            try:
                r = patch_nms(cabs[cab], aid, delete=[i["nm_id"] for i in items])
                log(f"  «{items[0]['campaign']}»: убрано {len(r['deleted'])} из {len(items)}"
                    + ("" if len(r["deleted"]) == len(items) else " — часть WB не принял, проверьте в кабинете"))
            except ApiError as e:
                log(f"  ✗ «{items[0]['campaign']}»: {e}")
        log("Вернуть: python wb_trim.py --rollback")
    elif plan:
        log("Это пробный прогон. Применить: python wb_trim.py --apply")
    write_log()


if __name__ == "__main__":
    if "--apply" in sys.argv or "--rollback" in sys.argv:
        from wb_bids import lock_acquire, lock_release    # общая блокировка с автопилотом и часовой проверкой
        if not lock_acquire("wb_trim.py", wait_min=25):
            sys.exit("✗ Сейчас применяет wb_bids.py или wb_guard.py больше 25 мин — ничего не меняю, запустите позже.")
        try:
            main()
        finally:
            lock_release()
    else:
        main()
