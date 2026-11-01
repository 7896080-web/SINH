# -*- coding: utf-8 -*-
"""
Применить решения пользователя по рекламе WB одним запуском: выключить кампании, вернуть лимиты к правилам, убрать
модели из рекламы. Вписано решение 01.11.2026 (итоги эксперимента 14–27.10 и «лестница» пуховиков):
  1. только «за клики»: кампании за показы выключить во всех кабинетах;
  2. работают только: у Яворской — «27643 клик» 31049161, «36446 клик» 31048671 и кампании курток (куртки, пуховики —
     больше половины товаров кампании); у Ребрика — кампании курток; у Карамана — только куртки 40769334 (третья
     ступень «лестницы», места 11–34; добавлено пользователем 01.11 после применения). Остальное (джемперы, лонгсливы, рубашки… в работе, на паузе и «готовые к
     запуску») — выключить, как переключатель «выкл» на странице «Бюджеты»: работающие — на паузу, все — в стоп-лист
     wb_hold.txt (автопилот и проверки не запускают и не пополняют; деньги в бюджете кампании остаются);
  3. модель 2718 (C26-2818C) убрать из оставшихся кампаний («пух нов» 39661508, Ребрик 40769287 — 21.10 WB не дал);
     кампанию целиком не опустошает; автопилот эти товары больше не возвращает (state["no_return"]).
  «Лестница» (места 1–2 у «пух нов», 3–10 у Ребрика 40769287) — в правилах wb_bids.py, не здесь.
Решение 21.10.2026 (выключить 40955628, 40955797, 40769361; лимиты к правилам; убрать 2718) применено 21.10 18:33 —
см. docs/changelog.md.

Запуск:  python wb_decide.py              — пробный прогон: план, ничего не меняет
         python wb_decide.py --apply      — применить (резервная копия — wb_bids_backups\\decide_*.json)
         python wb_decide.py --rollback   — вернуть по последней копии: товары с прежними ставками, поправки лимитов,
                                            кампании — как были (работавшие включить, стоявшие на паузе — только убрать
                                            из стоп-листа); сразу, без пробного прогона
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
DECISION = "решение 01.11.2026: только за клики, джемперы — 27643 и 36446 Яворской, куртки — «лестница»"
KEEP_ONLY = True               # выключить всё, что не подходит под ONLY_CPC / KEEP_IDS / KEEP_JACKETS (False — только STOP)
ONLY_CPC = True                # кампании за показы — выключить все
KEEP_IDS = {31049161, 31048671, 40769334}      # «27643 клик», «36446 клик» (Яворская); Караман 40769334 — куртки,
                                                # третья ступень «лестницы» (места 11–34, решение 01.11.2026)
KEEP_JACKETS = {"Яворская", "Ребрик"}           # где остаются кампании курток (Караман — без курток, «лестница»)
STOP = {}                      # выключить дополнительно: {кабинет: [номера]}
TO_RULES = {}                  # лимиты/потолки — как в правилах: {кабинет: [номера или "_week_budget"]}
REMOVE_MODELS = {"2718"}       # убрать из рекламы во всех кабинетах (номер модели — как в wb_compare.py)
# ---------------- НАСТРОЙКИ ----------------
TRIM_STATUSES = (9, 11)                     # из каких кампаний убирать: работающие и на паузе (пауза может кончиться)
JACKET_SUBJECTS = {"куртки", "пуховики", "парки", "ветровки", "бомберы", "анораки"}   # предметы WB «курток» (без учёта
                                            # регистра: в настройках кампаний WB пишет их со строчной, в воронке — с прописной)
LOG_FILE = B.HERE / "wb_decide_log.txt"
WHO = "скриптом wb_decide.py"               # пометка в стоп-листе
# -------------------------------------------
log = B.log


def name_of(a, cid):
    return (a.get("settings") or {}).get("name") or str(cid)


def subjects(a, info):
    """{предмет: товаров} кампании — из настроек кампании, нет там — из воронки."""
    out = defaultdict(int)
    for st in a.get("nm_settings") or []:
        sub = (st.get("subject") or {}).get("name") or (info.get(st.get("nm_id")) or {}).get("subject")
        out[sub or "?"] += 1
    return out


def verdict(cab, cid, a, info):
    """(оставить?, почему) по правилу решения."""
    if ONLY_CPC and (a.get("settings") or {}).get("payment_type") != "cpc":
        return False, "за показы"
    if cid in KEEP_IDS:
        return True, "в списке оставляемых"
    sub = subjects(a, info)
    known = sum(n for k, n in sub.items() if k != "?")
    jackets = sum(n for k, n in sub.items() if k.lower() in JACKET_SUBJECTS)
    if known and jackets * 2 >= known:
        return (True, "куртки") if cab in KEEP_JACKETS else (False, "куртки — в этом кабинете без курток («лестница»)")
    return False, "не куртки" if known else "товаров нет"


def ours(cab, cid, camps, state):
    """Может ли кампанию на паузе запустить автопилот или проверка: своё правило, «наша» (state["managed"]), пауза
    по лимиту или графику расхода, отложенный запуск. Остальные стоящие на паузе скрипты не трогают — их не выключаю."""
    r = B.RULES.get(cab) or {}
    if any(cid in B.find(camps, k) for k in r if k != "*" and not str(k).startswith("_")):
        return True
    return (str(cid) in (state.get("managed") or {}) or bool(B.limit_paused(state, cid)) or bool(B.pace_paused(state, cid))
            or str(cid) in (state.get("start_pending") or {}))


def plan_stop(camps_by, info_by, state):
    out = []
    log("\n1. КАМПАНИИ: что остаётся и что выключить (пауза + стоп-лист wb_hold.txt; деньги в бюджете кампании "
        "остаются)")
    extra = {(cab, cid) for cab, ids in STOP.items() for cid in ids}
    for cab, camps in camps_by.items():
        if camps is None:
            log(f"  ✗ {cab}: кабинет не загрузился — пропускаю")
            continue
        log(f"  {cab}:")
        keep, held, kept_idle, idle = [], 0, 0, 0
        for cid, a in sorted(camps.items(), key=lambda x: ({9: 0, 11: 1, 4: 2}.get(x[1].get("_status"), 3), x[0])):
            nm, st = name_of(a, cid), a.get("_status")
            pay = W.PAY.get((a.get("settings") or {}).get("payment_type"), "?")
            if B.on_hold(cid):
                held += 1
                continue
            ok, why = verdict(cab, cid, a, info_by.get(cab) or {}) if KEEP_ONLY else (True, "")
            if (cab, cid) in extra:
                ok, why = False, "указана в STOP"
            sub = ", ".join(f"{k} {n}" for k, n in sorted(subjects(a, info_by.get(cab) or {}).items(),
                                                           key=lambda p: -p[1])[:3])
            line = f"«{C.cut(nm, 30)}» {cid} ({pay}, {W.STATUS.get(st, st)}; {sub or 'без товаров'})"
            if ok:
                if st == 9:
                    keep.append(f"    ✓ оставить: {line} — {why}")
                else:
                    kept_idle += 1
                continue
            if st != 9 and not ours(cab, cid, camps, state) and (cab, cid) not in extra:
                idle += 1                    # стоит и так, скрипты её не запускают — стоп-лист не засоряю
                continue
            log(f"    ⏹ {line} — {why}: " + ("на паузу и в стоп-лист" if st == 9 else "в стоп-лист"))
            out.append({"cabinet": cab, "advert_id": cid, "name": nm, "status_before": st})
        for x in keep:
            log(x)
        if not keep:
            log("    ✓ оставить работающими: ничего")
        notes = [f"уже в стоп-листе — {held}" if held else "",
                 f"подходят под решение, но стоят на паузе (не запускаю) — {kept_idle}" if kept_idle else "",
                 f"не подходят, но стоят на паузе и скрипты их не запускают (не трогаю) — {idle}" if idle else ""]
        if any(notes):
            log("    (" + "; ".join(x for x in notes if x) + ")")
    if not out:
        log("  Выключать нечего.")
    return out


def plan_limits(data):
    """Правки «как в правилах» для страницы «Бюджеты»: (нормализованные правки, строки плана, снимок прежних поправок)."""
    if not TO_RULES:
        return [], [], {"items": {}, "manual": {}, "raised": {}}
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


def plan_trim(cab, camps, skip, state, info):
    """Товары моделей REMOVE_MODELS: (убрать из кампаний, пары «кампания:товар», которые автопилот не должен вернуть)."""
    pool = {cid: a for cid, a in camps.items() if a.get("_status") in TRIM_STATUSES and cid not in skip
            and not B.on_hold(cid)}
    earlier = B.trimmed_items(state, cab)                    # убранные раньше из-за малого остатка
    nms = sorted({s.get("nm_id") for a in pool.values() for s in a.get("nm_settings") or [] if s.get("nm_id")}
                 | {k[1] for k in earlier})
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
    # 1. кампании — как были: работавшие включить (как переключатель «вкл»), стоявшие на паузе — только из стоп-листа
    rules = W.rules_now()
    for i in plan.get("stop") or []:
        if i.get("done") is False or i["cabinet"] not in cabs:
            continue
        if i.get("status_before") != 9:
            try:
                W.hold_edit(i["advert_id"], i["name"], on=True, who=WHO)
                log(f"  ↩ «{i['name']}»: убрана из стоп-листа (до выключения не работала — не запускаю)")
            except OSError as e:
                log(f"  ✗ «{i['name']}»: {e}")
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
        data = W.collect() if TO_RULES else None     # как страница «Бюджеты»: кампании, правила, поправки
        state = B.load_state()
        camps_by, info_by = {}, {}
        for cab, token in cabs.items():
            try:
                camps_by[cab] = B.get_campaigns(token)
            except B.ApiError as e:
                log(f"  ✗ {cab}: список кампаний не загрузился — {e}")
                continue
            nms = sorted({st.get("nm_id") for a in camps_by[cab].values() for st in a.get("nm_settings") or []
                          if st.get("nm_id")} | {k[1] for k in B.trimmed_items(state, cab)})
            try:                             # артикулы продавца и предметы товаров: весь кабинет за раз, недостающие — отдельно
                info_by[cab] = C.funnel(token, C.YESTERDAY, C.YESTERDAY) if nms else {}
                miss = [nm for nm in nms if nm not in info_by[cab]]
                if miss:
                    info_by[cab].update(C.funnel(token, C.YESTERDAY, C.YESTERDAY, miss))
            except C.ApiError as e:
                log(f"  ! {cab}: артикулы продавца не загрузились — {e}; предметы — из настроек кампаний, модель "
                    f"{', '.join(sorted(REMOVE_MODELS))} в этом кабинете не убираю")
        stop = plan_stop(camps_by, info_by, state)
        norm, lines, old = plan_limits(data)
        if REMOVE_MODELS:
            log(f"\n{3 if TO_RULES else 2}. УБРАТЬ ИЗ РЕКЛАМЫ МОДЕЛЬ {', '.join(sorted(REMOVE_MODELS))} (все цвета, все кабинеты, кроме "
                "выключаемых кампаний)")
        skip = {i["advert_id"] for i in stop}
        trim, earlier = [], []
        for cab, camps in camps_by.items():
            if cab not in info_by or not REMOVE_MODELS:
                continue
            t, e = plan_trim(cab, camps, skip, state, info_by[cab])
            trim += t
            earlier += e
        if REMOVE_MODELS and not trim:
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
