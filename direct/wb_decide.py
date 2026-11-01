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
  4. (решение 01.11, второе) в кампаниях курток «лестницы» — Яворская «пух нов» 39661508, Ребрик 40769287, Караман
     40769334 (куртки) + 40769350 (пуховики; WB не держит в кампании товары разных предметов, её же — включить) —
     ровно 27 цветов из списка пользователя (2617, 2633, 2717, 2651, 2718), по одному разу: недостающие
     добавить (если в наличии не меньше min_stock), лишние (TC26 у Карамана и т. п.) и повторы — убрать; эти же
     карточки — убрать из других работающих кампаний кабинета; 2718 — вернуть в рекламу (снять state["no_return"]).
  «Лестница» (места 1–2 у «пух нов», 3–10 у Ребрика 40769287, 11–34 у Карамана 40769334) — в правилах wb_bids.py.
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
DECISION = "решение 01.11.2026: только за клики, 27643 и 36446 Яворской, куртки — «лестница» из 27 цветов"
KEEP_ONLY = True               # выключить всё, что не подходит под ONLY_CPC / KEEP_IDS / KEEP_JACKETS (False — только STOP)
ONLY_CPC = True                # кампании за показы — выключить все
KEEP_IDS = {31049161, 31048671, 40769334, 40769350}   # «27643 клик», «36446 клик» (Яворская); Караман 40769334 —
                                                # куртки, 40769350 — пуховики: третья ступень «лестницы» (места 11–34)
KEEP_JACKETS = {"Яворская", "Ребрик"}           # где остаются кампании курток (Караман — без курток, «лестница»)
STOP = {}                      # выключить дополнительно: {кабинет: [номера]}
TO_RULES = {}                  # лимиты/потолки — как в правилах: {кабинет: [номера или "_week_budget"]}
REMOVE_MODELS = set()          # убрать из рекламы во всех кабинетах (номер модели — как в wb_compare.py)
RETURN_MODELS = {"2718"}       # вернуть в рекламу: снять запрет автопилоту возвращать их товары (state["no_return"])
# Кампании курток «лестницы» и ровно какие карточки в них (решение пользователя 01.11.2026: «в рекламе должны быть эти
# модели без дублирования» — в каждом кабинете все 27 цветов, по одному разу; лишние убрать, недостающие добавить, если
# в наличии не меньше min_stock; в других работающих кампаниях кабинета этих карточек быть не должно)
LADDER = {"Яворская": [39661508], "Ребрик": [40769287],
          "Караман": [40769334, 40769350]}   # у Карамана 2617/2633 — «куртки», 2651/2717/2718 — «пуховики»: две кампании
START = {"Караман": [40769350]}           # включить (как переключатель «вкл»): пуховики Карамана — та же ступень, 11–34
LADDER_SET = (
    [f"2617 C24-2317CQ Авер Куртка {c}" for c in ("BLACK", "BRICK RED", "BROWN", "INK BLUE", "NAVY", "OLIVE", "SAND BEIGE")]
    + [f"2633 C24-2233L Авер Куртка {c}" for c in ("BLACK", "GREY", "KHAKI", "OLIVE")]
    + [f"2717 С26-2817VL Авер Куртка {c}" for c in ("BLACK", "BROWN", "NAVY", "SLATE")]
    + [f"2651 C23-2251C Авер Куртка {c}" for c in ("BLACK", "OLIVE", "INK BLUE", "WINE RED", "NAVY", "GREEN", "BROWN",
                                                    "RED")]
    + [f"2718 C26-2818C Авер Куртка {c}" for c in ("BLACK", "BROWN", "DARK SAND", "NAVY")])
# ---------------- НАСТРОЙКИ ----------------
TRIM_STATUSES = (9, 11)                     # из каких кампаний убирать: работающие и на паузе (пауза может кончиться)
JACKET_SUBJECTS = {"куртки", "пуховики", "парки", "ветровки", "бомберы", "анораки"}   # предметы WB «курток» (без учёта
                                            # регистра: в настройках кампаний WB пишет их со строчной, в воронке — с прописной)
LOG_FILE = B.HERE / "wb_decide_log.txt"
WHO = "скриптом wb_decide.py"               # пометка в стоп-листе
MIN_STOCK = B.DEFAULTS.get("min_stock") or 0  # меньше в наличии — в кампанию не добавляю (автопилот всё равно уберёт)
LOOKALIKE = str.maketrans("АВЕКМНОРСТУХ", "ABEKMHOPCTYX")   # «С26» русской и «C26» латинской — одно и то же
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


def norm_vc(v):
    """Артикул продавца для сравнения: без лишних пробелов, без учёта регистра и похожих русских и латинских букв."""
    return " ".join(str(v or "").split()).upper().translate(LOOKALIKE)


def avail(nf):
    """В наличии: склады WB + свой склад (как у автопилота); None — неизвестно."""
    if not nf or (nf.get("stock_wb") is None and nf.get("stock_mp") is None):
        return None
    return (nf.get("stock_wb") or 0) + ((nf.get("stock_mp") or 0) if B.COUNT_FBS_STOCK else 0)


def subject_of(nm, info, sets=None):
    """Предмет товара (строчными): из воронки, нет там — из настроек кампании."""
    sub = (info.get(nm) or {}).get("subject") or ((sets or {}).get(nm, {}).get("subject") or {}).get("name")
    return (sub or "").strip().lower() or None


def plan_ladder(cab, camps, info, skip):
    """Кампании «лестницы» кабинета — ровно к LADDER_SET, каждый цвет один раз; товар — в кампанию своего предмета
    (WB держит в кампании товары одного предмета: «nomenclatures from different category», 01.11.2026 — у Карамана
    2617/2633 — «куртки», 2651/2717/2718 — «пуховики»); эти карточки — убрать из других работающих кампаний кабинета.
    [{кабинет, кампания, add: [артикулы WB], delete: [{nm_id, bids_kopecks}]}]."""
    cids = [c for c in LADDER.get(cab) or [] if c in camps]
    for c in LADDER.get(cab) or []:
        if c not in camps:
            log(f"  ✗ {cab}: кампании {c} нет среди работающих и стоящих на паузе — не трогаю")
    if not cids:
        return []
    want = {norm_vc(v): v for v in LADDER_SET}
    vc_of = {nm: norm_vc((nf or {}).get("vendor")) for nm, nf in info.items()}
    sets = {c: {st["nm_id"]: st for st in camps[c].get("nm_settings") or [] if st.get("nm_id")} for c in cids}
    subj = {}                                        # предмет кампании — по большинству её товаров
    for c in cids:
        cnt = defaultdict(int)
        for nm in sets[c]:
            if subject_of(nm, info, sets[c]):
                cnt[subject_of(nm, info, sets[c])] += 1
        subj[c] = max(cnt, key=cnt.get) if cnt else None
    have, extra, unknown = defaultdict(list), defaultdict(list), defaultdict(list)
    for c in cids:
        for nm in sets[c]:
            vc = vc_of.get(nm)
            if not vc:
                unknown[c].append(nm)                # артикул продавца неизвестен — не трогаю
            elif vc in want:
                have[vc].append((c, nm))
            else:
                extra[c].append(nm)
    dup = defaultdict(list)
    for vc, pairs in have.items():                   # один цвет дважды — оставить карточку с большим остатком
        if len(pairs) > 1:
            pairs.sort(key=lambda p: -(avail(info.get(p[1])) or 0))
            for c, nm in pairs[1:]:
                dup[c].append(nm)
    add, notes = defaultdict(list), []
    for vc, label in want.items():
        if have.get(vc):
            continue
        cands = [nm for nm, v in vc_of.items() if v == vc]
        if not cands:
            notes.append(f"{label} — такой карточки в кабинете нет")
            continue
        nm = max(cands, key=lambda n: avail(info.get(n)) or 0)
        q = avail(info.get(nm))
        if q is not None and q < MIN_STOCK:
            notes.append(f"{label} ({nm}) — в наличии {q:.0f} шт, меньше {MIN_STOCK}")
            continue
        sub = subject_of(nm, info)
        target = [c for c in cids if subj[c] == sub] or ([cids[0]] if len(cids) == 1 and not subj[cids[0]] else [])
        if not target:
            notes.append(f"{label} ({nm}) — предмет «{sub or '?'}», а кампании «лестницы» с таким предметом в кабинете "
                         f"нет (WB не берёт в кампанию товары другого предмета)")
            continue
        add[target[0]].append(nm)
    vend = lambda nm: C.cut((info.get(nm) or {}).get("vendor") or nm, 40)
    total = len(have) + sum(len(v) for v in add.values())
    log(f"  {cab}: цветов из списка сейчас {len(have)} из {len(want)} → станет {total} из {len(want)}")
    ops = []
    for c in cids:
        a = camps[c]
        log(f"  ▶ «{name_of(a, c)}» {c} ({subj[c] or 'предмет неизвестен'}, {W.STATUS.get(a.get('_status'), a.get('_status'))}"
            f"; товаров {len(sets[c])})")
        if add[c]:
            log(f"      добавить {len(add[c])}: " + "; ".join(f"{nm} {vend(nm)}" for nm in add[c]))
        if extra[c]:
            log(f"      убрать не из списка {len(extra[c])}: " + "; ".join(f"{nm} {vend(nm)}" for nm in extra[c]))
        if dup[c]:
            log(f"      убрать повторы {len(dup[c])}: " + "; ".join(f"{nm} {vend(nm)}" for nm in dup[c]))
        if unknown[c]:
            log(f"      артикул продавца не узнал у {len(unknown[c])} тов. — не трогаю: {', '.join(map(str, unknown[c][:6]))}")
        delete = extra[c] + dup[c]
        if len(delete) == len(sets[c]) and not add[c] and delete:
            log("      ! убрать пришлось бы все её товары — кампанию не опустошаю")
            continue
        if add[c] or delete:
            ops.append({"cabinet": cab, "advert_id": c, "campaign": name_of(a, c), "bid_type": a.get("bid_type"),
                        "add": add[c], "delete": [{"nm_id": nm, "bids_kopecks": sets[c][nm].get("bids_kopecks") or {}}
                                                  for nm in delete]})
        if not (add[c] or delete):
            log("      менять нечего")
    for x in notes:
        log(f"      не добавляю: {x}")
    for ocid, oa in sorted(camps.items()):           # те же карточки в других работающих кампаниях кабинета
        if ocid in cids or oa.get("_status") != 9 or B.on_hold(ocid) or ocid in skip:
            continue
        osets = oa.get("nm_settings") or []
        rm = [st for st in osets if vc_of.get(st.get("nm_id")) in want]
        if not rm:
            continue
        if len(rm) == len(osets):
            log(f"  ! «{name_of(oa, ocid)}» {ocid}: все её товары ({len(rm)}) — из списка «лестницы»; кампанию не "
                "опустошаю — выключите её, если она не нужна")
            continue
        log(f"  ▶ «{name_of(oa, ocid)}» {ocid}: убрать повторы карточек «лестницы» {len(rm)}: "
            + "; ".join(f"{st['nm_id']} {vend(st['nm_id'])}" for st in rm))
        ops.append({"cabinet": cab, "advert_id": ocid, "campaign": name_of(oa, ocid), "bid_type": oa.get("bid_type"),
                    "add": [], "delete": [{"nm_id": st["nm_id"], "bids_kopecks": st.get("bids_kopecks") or {}}
                                          for st in rm]})
    return ops


def plan_start(camps_by):
    """START: включить кампании (как переключатель «вкл»): [{кабинет, кампания, статус до}]."""
    out = []
    for cab, ids in START.items():
        for cid in ids:
            a = (camps_by.get(cab) or {}).get(cid)
            if not a:
                log(f"  ✗ {cab} {cid}: кампании нет среди работающих и стоящих на паузе — включить не могу")
                continue
            if a.get("_status") == 9 and not B.on_hold(cid):
                continue
            log(f"  ▶ {cab} «{name_of(a, cid)}» {cid}: включить (сейчас — {W.STATUS.get(a.get('_status'), a.get('_status'))}"
                + (", в стоп-листе" if B.on_hold(cid) else "") + ")")
            out.append({"cabinet": cab, "advert_id": cid, "name": name_of(a, cid), "status_before": a.get("_status"),
                        "held_before": B.on_hold(cid)})
    return out


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
    for op in plan.get("ladder") or []:
        try:
            r = T.patch_nms(cabs[op["cabinet"]], op["advert_id"], add=op["add"],
                            delete=[d["nm_id"] for d in op["delete"]])
        except C.ApiError as e:
            op["added"], op["deleted"] = [], []
            log(f"  ✗ «{op['campaign']}» {op['advert_id']}: товары не изменены — {e}")
            continue
        op["added"], op["deleted"] = r["added"], r["deleted"]
        ok = len(r["added"]) == len(op["add"]) and len(r["deleted"]) == len(op["delete"])
        log(f"  ⇄ «{op['campaign']}» {op['advert_id']}: добавлено {len(r['added'])} из {len(op['add'])}, убрано "
            f"{len(r['deleted'])} из {len(op['delete'])}" + ("" if ok else " — часть WB не принял, проверьте в кабинете"))
    rules_ = W.rules_now()
    for i in plan.get("start") or []:               # включить — после того как товары на месте
        try:
            camps = B.get_campaigns(cabs[i["cabinet"]])
            ok, text = W.switch(cabs[i["cabinet"]], camps, rules_, i["cabinet"], i["advert_id"], True, who=WHO)
            i["done"] = ok
            log(f"  {'▶' if ok else '✗'} {text}")
        except (B.ApiError, OSError, KeyError) as e:
            i["done"] = False
            log(f"  ✗ «{i['name']}»: не включена — {e}")
    state = B.load_state()
    nr = state.setdefault("no_return", {})
    for k in plan.get("no_return_removed") or {}:
        nr.pop(k, None)
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
    for i in plan.get("start") or []:              # включённые — как были: на паузу (и в стоп-лист, если были там)
        if i.get("done") is False or i["cabinet"] not in cabs:
            continue
        st = B.load_state()
        for k in ("pace_paused", "limit_paused", "start_pending"):
            (st.get(k) or {}).pop(str(i["advert_id"]), None)
        B.save_state(st)
        if i.get("held_before"):
            W.hold_edit(i["advert_id"], i["name"], on=False, who=f"{WHO} (откат)")
        try:
            camps = B.get_campaigns(cabs[i["cabinet"]])
            if (camps.get(i["advert_id"]) or {}).get("_status") == 9:
                B.pace(cabs[i["cabinet"]], "start", 1.1)
                B.call("GET", B.ADV + "/adv/v0/pause", cabs[i["cabinet"]], params={"id": i["advert_id"]}, what="Пауза кампании")
            log(f"  ↩ «{i['name']}»: снова на паузе" + (" и в стоп-листе" if i.get("held_before") else ""))
        except B.ApiError as e:
            log(f"  ✗ «{i['name']}»: на паузу не встала — {e}; поставьте её на паузу в кабинете WB")
    for op in plan.get("ladder") or []:            # «лестница»: добавленные — убрать, убранные — вернуть со ставками
        added, deleted = op.get("added"), op.get("deleted")
        if added is None:                              # сбой посреди --apply — откатываю всё, что было в плане
            added, deleted = op["add"], [d["nm_id"] for d in op["delete"]]
        if not added and not deleted or op["cabinet"] not in cabs:
            continue
        try:
            r = T.patch_nms(cabs[op["cabinet"]], op["advert_id"], add=deleted, delete=added)
            back = [{"advert_id": op["advert_id"], "nm_id": d["nm_id"], "bid_type": op.get("bid_type"),
                     "bids_kopecks": d.get("bids_kopecks") or {}} for d in op["delete"] if d["nm_id"] in set(r["added"])]
            log(f"  ↩ «{op['campaign']}» {op['advert_id']}: убрано добавленных {len(r['deleted'])}, возвращено "
                f"{len(r['added'])}, ставок восстановлено {T.restore_bids(cabs[op['cabinet']], back) if back else 0}")
        except C.ApiError as e:
            log(f"  ✗ «{op['campaign']}» {op['advert_id']}: {e}")
    state = B.load_state()
    for k in plan.get("no_return") or []:
        (state.get("no_return") or {}).pop(k, None)
    state.setdefault("no_return", {}).update(plan.get("no_return_removed") or {})
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
        ladder, back = [], {}
        if LADDER:
            log(f"\n{2 + bool(TO_RULES) + bool(REMOVE_MODELS)}. КУРТКИ «ЛЕСТНИЦЫ»: в каждой кампании — ровно "
                f"{len(LADDER_SET)} цветов из списка, по одному разу")
            for cab, camps in camps_by.items():
                if cab in info_by:
                    ladder += plan_ladder(cab, camps, info_by[cab], skip)
                elif cab in LADDER:
                    log(f"  ✗ {cab}: артикулы продавца не загрузились — кампанию «лестницы» не трогаю")
        start = []
        if START:
            log("\n  Включить:")
            start = plan_start(camps_by)
            if not start:
                log("  — включать нечего")
        if RETURN_MODELS:
            ret = {nm for inf in info_by.values() for nm, nf in inf.items() if model_of((nf or {}).get("vendor")) in RETURN_MODELS}
            back = {k: v for k, v in (state.get("no_return") or {}).items() if int(k.split(":")[1]) in ret}
            if back:
                log(f"  Модель {', '.join(sorted(RETURN_MODELS))}: снять запрет автопилоту возвращать её товары в рекламу "
                    f"(после уборки из-за малого остатка), записей: {len(back)}")
        plan = {"decision": DECISION, "at": dt.datetime.now().isoformat(timespec="seconds"), "stop": stop,
                "limits": {"norm": norm, "lines": lines, "old": old}, "trim": trim, "no_return_earlier": earlier,
                "ladder": ladder, "no_return_removed": back, "start": start}
        n_add, n_del = sum(len(o["add"]) for o in ladder), sum(len(o["delete"]) for o in ladder)
        n = len(stop) + len(norm) + len(trim) + len(earlier) + n_add + n_del + len(back) + len(start)
        log(f"\nИтого: выключить {len(stop)} камп.; правок лимитов {len(norm)}; убрать {len(trim)} тов. из "
            f"{len({(i['cabinet'], i['advert_id']) for i in trim})} камп.; «лестница» — добавить {n_add}, убрать {n_del}"
            + (f"; снять запрет на возврат — {len(back)}" if back else "") + (f"; включить {len(start)} камп." if start else ""))
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
