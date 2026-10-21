# -*- coding: utf-8 -*-
"""
Применить решения пользователя по рекламе WB одним запуском: выключить кампании, вернуть лимиты к правилам, убрать
модели из рекламы. Вписано решение 21.10.2026 по сравнению трёх кабинетов (wb_compare.py) — «да, все пункты»:
  1. выключить Ребрик «2101 показы» 40955628 (4 046 ₽ → 1 заказ), Караман «пуховики показы» 40955797 (16 131 ₽ → 2),
     Караман 40769361 (джемпер ZJYM269002, за клики: 1 409 ₽ без заказов) — как переключатель «выкл» на странице
     «Бюджеты»: пауза и стоп-лист wb_hold.txt (автопилот и проверки не запускают и не пополняют; деньги в бюджете
     кампании остаются);
  2. вернуть к правилам (снять ручные правки страницы «Бюджеты» от 16.10): Яворская «пух нов» 39661508 — лимит
     1 000 ₽; Караман 40769334 — 300 ₽; недельный бюджет Карамана — 16 200 ₽;
  3. убрать модель 2718 (C26-2818C, все цвета) из рекламы во всех кабинетах: ≈8 200 ₽ за 14 дней без единого заказа.
     Кампанию целиком не опустошает (если все её товары — эта модель, только предупреждает). Автопилот эти товары больше
     не возвращает, даже когда остаток пополнится (state["no_return"] в wb_bids_state.json).

Запуск:  python wb_decide.py              — пробный прогон: план, ничего не меняет
         python wb_decide.py --apply      — применить (резервная копия — wb_bids_backups\\decide_*.json)
         python wb_decide.py --rollback   — вернуть по последней копии: товары с прежними ставками, поправки лимитов,
                                            включить кампании (сразу, без пробного прогона)
Откат возвращает только последний --apply; из поправок лимитов — только те, что менял этот скрипт.
Нужны категории токена: «Продвижение», «Аналитика». Журнал — wb_decide_log.txt.
"""
import copy
import datetime as dt
import json
import os
import sys
from collections import defaultdict

import wb_bids as B
import wb_common as C
import wb_trim as T
import wb_web as W
from wb_compare import model_of

# ---------------- РЕШЕНИЕ (что делать) ----------------
DECISION = "решение 21.10.2026 по сравнению кабинетов"
STOP = {"Ребрик": [40955628], "Караман": [40955797, 40769361]}           # выключить: пауза + стоп-лист
TO_RULES = {"Яворская": [39661508], "Караман": [40769334, "_week_budget"]}   # лимиты/потолки — как в правилах
REMOVE_MODELS = {"2718"}                    # убрать из рекламы во всех кабинетах (номер модели — как в wb_compare.py)
# ---------------- НАСТРОЙКИ ----------------
TRIM_STATUSES = (9, 11)                     # из каких кампаний убирать: работающие и на паузе (пауза может кончиться)
LOG_FILE = B.HERE / "wb_decide_log.txt"
WHO = "скриптом wb_decide.py"               # пометка в стоп-листе
# -------------------------------------------
log = B.log


def name_of(a, cid):
    return (a.get("settings") or {}).get("name") or str(cid)


def plan_stop(camps_by):
    out = []
    log("\n1. ВЫКЛЮЧИТЬ КАМПАНИИ (пауза + стоп-лист wb_hold.txt; деньги в бюджете кампании остаются)")
    for cab, ids in STOP.items():
        camps = camps_by.get(cab)
        for cid in ids:
            if camps is None:
                log(f"  ✗ {cab} {cid}: кабинет не загрузился — пропускаю")
                continue
            a = camps.get(cid)
            if not a:
                log(f"  — {cab} {cid}: нет среди работающих и стоящих на паузе (завершена?) — делать нечего")
                continue
            nm, st = name_of(a, cid), a.get("_status")
            pay = W.PAY.get((a.get("settings") or {}).get("payment_type"), "?")
            if B.on_hold(cid):
                log(f"  — {cab} «{nm}» {cid} ({pay}): уже в стоп-листе — не трогаю")
                continue
            log(f"  ⏹ {cab} «{nm}» {cid} ({pay}, {W.STATUS.get(st, st)}): "
                + ("поставить на паузу и в стоп-лист" if st == 9 else "уже на паузе — вписать в стоп-лист"))
            out.append({"cabinet": cab, "advert_id": cid, "name": nm, "status_before": st})
    if not out:
        log("  Выключать нечего.")
    return out


def plan_limits(data):
    """Правки «как в правилах» для страницы «Бюджеты»: (нормализованные правки, снимок прежних поправок)."""
    log("\n2. ЛИМИТЫ И ПОТОЛКИ — КАК В ПРАВИЛАХ (снять ручные поправки)")
    by_cab = {c["cab"]: c for c in data["cabinets"]}
    changes, errors = [], []
    for cab, ids in TO_RULES.items():
        c = by_cab.get(cab)
        if c is None or c.get("error"):
            errors.append(f"{cab}: кабинет не загрузился — лимиты не трогаю")
            continue
        for cid in ids:
            if cid == "_week_budget":
                if c["week_budget"]["base"] is not None and c["week_budget"]["cur"] != c["week_budget"]["base"]:
                    changes.append({"cab": cab, "id": "_week_budget", "field": "_week_budget",
                                    "value": c["week_budget"]["base"]})
                continue
            row = next((x for x in c["camps"] if str(x["id"]) == str(cid)), None)
            if row is None:
                errors.append(f"{cab} {cid}: не под управлением автопилота — лимиты не трогаю")
                continue
            for f in W.FIELDS:
                b = row["base"].get(f)
                if b is not None and row["cur"].get(f) != b and (f == "daily_limit" or row["topup"]):
                    changes.append({"cab": cab, "id": row["id"], "field": f, "value": b})
    lines, warns, errs, norm = W.plan(data, changes)
    for x in lines:
        log(f"  {x}")
    for x in warns:
        log(f"  ! {x}")
    for x in errors + errs:
        log(f"  ✗ {x}")
    if not norm:
        log("  Менять нечего — уже как в правилах.")
    ov = W.load_json(W.OVERRIDES, {"items": {}, "raised": {}})
    old = {"items": {}, "manual": {}, "raised": {}}
    for n in norm:
        cab, key = n["cab"], str(n["id"])
        mkey = "_week_budget:" + cab if key == "_week_budget" else key
        old["items"].setdefault(cab, {})[key] = copy.deepcopy(((ov.get("items") or {}).get(cab) or {}).get(key))
        old["manual"][mkey] = copy.deepcopy((ov.get("manual") or {}).get(mkey))
        if key != "_week_budget":
            old["raised"][key] = copy.deepcopy((ov.get("raised") or {}).get(key))
    return norm, lines, old


def plan_trim(cab, token, camps, skip, state):
    """Товары моделей REMOVE_MODELS: (убрать из кампаний, пары «кампания:товар», которые автопилот не должен вернуть)."""
    pool = {cid: a for cid, a in camps.items() if a.get("_status") in TRIM_STATUSES and cid not in skip
            and not B.on_hold(cid)}
    earlier = B.trimmed_items(state, cab)                    # убранные раньше из-за малого остатка
    nms = sorted({s.get("nm_id") for a in pool.values() for s in a.get("nm_settings") or [] if s.get("nm_id")}
                 | {k[1] for k in earlier})
    info = C.funnel(token, C.YESTERDAY, C.YESTERDAY, nms) if nms else {}
    vendor = {nm: (info.get(nm) or {}).get("vendor") for nm in nms}
    hit = {nm for nm, v in vendor.items() if model_of(v) in REMOVE_MODELS}
    items = []
    for cid, a in sorted(pool.items()):
        sets = a.get("nm_settings") or []
        rm = [s for s in sets if s.get("nm_id") in hit]
        if not rm:
            continue
        pay = W.PAY.get((a.get("settings") or {}).get("payment_type"), "?")
        head = f"  ▶ {cab} «{name_of(a, cid)}» {cid} ({pay}, {W.STATUS.get(a.get('_status'), a.get('_status'))})"
        arts = "; ".join(f"{s['nm_id']} {C.cut(vendor.get(s['nm_id']), 34)}" for s in rm)
        if len(rm) == len(sets):
            log(f"{head}: все её товары ({len(rm)}) — эта модель; кампанию не опустошаю — выключите её, если реклама "
                f"не нужна. {arts}")
            continue
        log(f"{head}: убрать {len(rm)} из {len(sets)} — {arts}")
        items += [{"cabinet": cab, "advert_id": cid, "campaign": name_of(a, cid), "nm_id": s["nm_id"],
                   "vendor": vendor.get(s["nm_id"]), "bid_type": a.get("bid_type"),
                   "bids_kopecks": s.get("bids_kopecks") or {}} for s in rm]
    back = sorted(B.pk(*k) for k in earlier if k[1] in hit)
    if back:
        log(f"  {cab}: убранные раньше из-за малого остатка товары этой модели ({len(back)}) автопилот тоже больше не "
            "вернёт в рекламу")
    return items, back


def write_backup(path, plan):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def apply(cabs, camps_by, data, plan):
    B.BACKUP_DIR.mkdir(exist_ok=True)
    bk = B.BACKUP_DIR / f"decide_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    write_backup(bk, plan)                   # до любых изменений: откат найдёт, что вернуть, даже после сбоя
    log(f"\nРезервная копия: {bk.name}")
    rules = W.rules_now()
    for i in plan["stop"]:
        try:
            ok, text = W.switch(cabs[i["cabinet"]], camps_by[i["cabinet"]], rules, i["cabinet"], i["advert_id"], False,
                                who=WHO)
            i["done"] = True                 # в стоп-листе в любом случае (пауза могла не встать — сказано в тексте)
            log(f"  {'⏹' if ok else '✗'} {text}")
        except (B.ApiError, OSError) as e:
            i["done"] = False
            log(f"  ✗ «{i['name']}»: {e}")
    if plan["limits"]["norm"]:
        try:
            name = W.apply_changes(data, plan["limits"]["norm"])
            plan["limits"]["done"] = True
            W.write_log([f"ЗАПИСАНО ({WHO}, {DECISION}), резервная копия {name}"] + plan["limits"]["lines"])
            log(f"  ✓ Лимиты и потолки — как в правилах (копия прежних — wb_budget_backups\\{name}); действуют со "
                "следующей проверки.")
        except OSError as e:
            plan["limits"]["done"] = False
            log(f"  ✗ Лимиты не записаны: {e}")
    by = defaultdict(list)
    for i in plan["trim"]:
        by[(i["cabinet"], i["advert_id"])].append(i)
    for (cab, aid), items in by.items():
        try:
            r = T.patch_nms(cabs[cab], aid, delete=[i["nm_id"] for i in items])
        except C.ApiError as e:
            log(f"  ✗ «{items[0]['campaign']}»: товары не убраны — {e}")
            for i in items:
                i["done"] = False
            continue
        gone = set(r["deleted"])
        for i in items:
            i["done"] = i["nm_id"] in gone
        log(f"  − «{items[0]['campaign']}» {aid}: убрано {len(gone)} из {len(items)}"
            + ("" if len(gone) == len(items) else " — часть WB не принял, проверьте в кабинете"))
    state = B.load_state()
    nr = state.setdefault("no_return", {})
    keys = [B.pk(i["advert_id"], i["nm_id"]) for i in plan["trim"] if i.get("done")] + plan["no_return_earlier"]
    for k in keys:
        nr[k] = {"date": dt.date.today().isoformat(), "why": f"{DECISION}: модель {', '.join(sorted(REMOVE_MODELS))}"}
    plan["no_return"] = keys
    B.save_state(state)
    write_backup(bk, plan)
    log(f"\nГотово. Вернуть всё: python wb_decide.py --rollback (сразу, без пробного прогона).")


def rollback(cabs):
    files = sorted(B.BACKUP_DIR.glob("decide_*.json")) if B.BACKUP_DIR.exists() else []
    if not files:
        sys.exit("Резервных копий wb_decide.py нет — откатывать нечего.")
    plan = json.loads(files[-1].read_text(encoding="utf-8"))
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — ОТКАТ по {files[-1].name} (применяется сразу)")
    # 3. товары — обратно, с прежними ставками (WB добавляет с минимальной)
    by = defaultdict(list)
    for i in plan.get("trim") or []:
        if i.get("done") is not False:
            by[(i["cabinet"], i["advert_id"])].append(i)
    for (cab, aid), items in by.items():
        if cab not in cabs:
            log(f"  ✗ Нет токена «{cab}» — товары «{items[0]['campaign']}» не возвращены")
            continue
        try:
            r = T.patch_nms(cabs[cab], aid, add=[i["nm_id"] for i in items])
            back = [i for i in items if i["nm_id"] in set(r["added"])]
            log(f"  ↩ «{items[0]['campaign']}»: возвращено {len(back)} из {len(items)}, ставок восстановлено "
                f"{T.restore_bids(cabs[cab], back)}")
        except C.ApiError as e:
            log(f"  ✗ «{items[0]['campaign']}»: {e}")
    state = B.load_state()
    for k in plan.get("no_return") or []:
        (state.get("no_return") or {}).pop(k, None)
    B.save_state(state)
    # 2. поправки лимитов — как были до --apply (только те, что менял скрипт)
    lim = plan.get("limits") or {}
    if lim.get("norm") and lim.get("done") is not False:
        ov = W.load_json(W.OVERRIDES, {"items": {}, "raised": {}})
        W.BACKUPS.mkdir(exist_ok=True)
        bk = W.BACKUPS / f"overrides_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
        bk.write_text(json.dumps(ov, ensure_ascii=False, indent=1), encoding="utf-8")
        items = ov.setdefault("items", {})
        for cab, keys in (lim["old"].get("items") or {}).items():
            it = items.setdefault(cab, {})
            for k, v in keys.items():
                if v is None:
                    it.pop(k, None)
                else:
                    it[k] = v
            if not it:
                items.pop(cab, None)
        for part in ("manual", "raised"):
            d = ov.setdefault(part, {})
            for k, v in (lim["old"].get(part) or {}).items():
                if v is None:
                    d.pop(k, None)
                else:
                    d[k] = v
        ov["at"] = dt.datetime.now().isoformat(timespec="seconds")
        tmp = W.OVERRIDES.with_suffix(".tmp")
        tmp.write_text(json.dumps(ov, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, W.OVERRIDES)
        W.write_log([f"ОТКАТ ({WHO}), резервная копия {bk.name}"] + [f"возвращено: {x}" for x in lim.get("lines") or []])
        log(f"  ↩ Поправки лимитов — как до {files[-1].name} (копия нынешних — wb_budget_backups\\{bk.name})")
    # 1. кампании — включить (как переключатель «вкл»)
    rules = W.rules_now()
    for i in plan.get("stop") or []:
        if i.get("done") is False or i["cabinet"] not in cabs:
            continue
        try:
            camps = B.get_campaigns(cabs[i["cabinet"]])
            if i["advert_id"] not in camps:
                W.hold_edit(i["advert_id"], i["name"], on=True, who=WHO)
                log(f"  ↩ «{i['name']}»: убрана из стоп-листа (среди работающих и на паузе её нет — запустите в кабинете WB)")
                continue
            ok, text = W.switch(cabs[i["cabinet"]], camps, rules, i["cabinet"], i["advert_id"], True,
                                who=f"{WHO} (откат)")
            log(f"  {'↩' if ok else '✗'} {text}")
        except (B.ApiError, OSError) as e:
            log(f"  ✗ «{i['name']}»: {e}")
    files[-1].rename(files[-1].with_suffix(".rolled_back"))


def write_log():
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(B.out) + "\n\n")
    except OSError:
        pass


def main():
    cabs = B.load_tokens()
    if "--rollback" in sys.argv:
        if not B.lock_acquire("wb_decide.py --rollback", wait_min=25):
            sys.exit("Сейчас работает автопилот или проверка больше 25 мин — ничего не сделано, запустите позже.")
        try:
            rollback(cabs)
        finally:
            B.lock_release()
            write_log()
        return
    do = "--apply" in sys.argv
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — {DECISION} ({'ПРИМЕНЕНИЕ' if do else 'пробный прогон, ничего не меняется'})")
    if do and not B.lock_acquire("wb_decide.py", wait_min=25):
        sys.exit("Сейчас работает автопилот или проверка больше 25 мин — ничего не сделано, запустите позже.")
    try:
        data = W.collect()                   # как страница «Бюджеты»: кампании, правила, поправки
        camps_by = {}
        for cab, token in cabs.items():
            try:
                camps_by[cab] = B.get_campaigns(token)
            except B.ApiError as e:
                log(f"  ✗ {cab}: список кампаний не загрузился — {e}")
        stop = plan_stop(camps_by)
        norm, lines, old = plan_limits(data)
        log(f"\n3. УБРАТЬ ИЗ РЕКЛАМЫ МОДЕЛЬ {', '.join(sorted(REMOVE_MODELS))} (все цвета, все кабинеты)")
        state = B.load_state()
        skip = {i["advert_id"] for i in stop}
        trim, earlier = [], []
        for cab, camps in camps_by.items():
            try:
                t, e = plan_trim(cab, cabs[cab], camps, skip, state)
            except C.ApiError as e:
                log(f"  ✗ {cab}: артикулы продавца не загрузились — {e}; модель в этом кабинете не убираю")
                continue
            trim += t
            earlier += e
        if not trim:
            log("  Убирать нечего.")
        plan = {"decision": DECISION, "at": dt.datetime.now().isoformat(timespec="seconds"), "stop": stop,
                "limits": {"norm": norm, "lines": lines, "old": old}, "trim": trim, "no_return_earlier": earlier}
        n = len(stop) + len(norm) + len(trim) + len(earlier)
        log(f"\nИтого: выключить {len(stop)} камп.; правок лимитов {len(norm)}; убрать {len(trim)} тов. из "
            f"{len({(i['cabinet'], i['advert_id']) for i in trim})} камп.")
        if not n:
            log("Делать нечего.")
        elif not do:
            log("Это пробный прогон. Применить: python wb_decide.py --apply")
        else:
            apply(cabs, camps_by, data, plan)
    finally:
        if do:
            B.lock_release()
            write_log()


if __name__ == "__main__":
    main()
