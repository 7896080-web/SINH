# -*- coding: utf-8 -*-
"""
Этап 1 аудита через API Яндекс Директа:
  1) недельный бюджет стратегии в каждой кампании;
  2) запрещённые площадки (ВК/ОК, погода) — добавляются к уже существующему списку;
  3) корректировки ставок: планшеты, возраст 45–54, регионы.

Запуск:
  python direct_stage1.py            — пробный прогон: только показывает, что будет изменено
  python direct_stage1.py --apply    — применить (перед этим сохраняется резервная копия)
  python direct_stage1.py --rollback — вернуть как было по последней резервной копии
Если менять нечего, --apply ничего не делает и новую резервную копию не создаёт —
иначе она стала бы «последней» и откат к состоянию до этапа 1 был бы потерян.
Откат не трогает стратегию, если её тип с тех пор сменился (иначе сбросится обучение).

Токен берётся из настроек AWER CRM (Маркетинг → Подключение к Яндексу).
"""
import json
import sys
import time
from pathlib import Path

import requests

# ---------------- НАСТРОЙКИ ----------------
CAMPAIGNS = {
    712220172: "ЕПК Сети КО",
    712219913: "ЕПК Сети ТО",
}
# Недельный бюджет, рубли БЕЗ НДС (суммы в API Директа — без НДС)
WEEKLY_BUDGET = {
    712220172: 38000,
    712219913: 32000,
}
EXCLUDE = [
    "com.vkontakte.android", "com.vk.vkclient", "vk.com", "ok.ru", "ru.ok.android",
    "m.pogoda.yandex.ru", "pogoda.yandex.ru", "gismeteo.ru",
    "ru.yandex.mobile.weather", "ru.yandex.weatherplugin",
]
TABLET_MODIFIER = 0          # 0 = −100%
# Итоговый набор корректировок по полу и возрасту (одинаковый для обеих кампаний).
# Ключ: (пол или None = оба пола, возраст). Всё, чего нет в этом списке, удаляется.
DEMOGRAPHICS = {
    (None, "AGE_0_17"): 0,              # −100%, как было
    (None, "AGE_18_24"): 0,             # −100%, как было
    (None, "AGE_45_54"): 80,            # −20% (06.10 было −50%, 10.10 смягчено direct_age_45_54.py)
}
# Эти корректировки не трогаем: где есть — остаются как есть, где нет — не добавляются.
KEEP_AS_IS = {("GENDER_FEMALE", "AGE_55")}
# Регионы (id геобазы Яндекса): 100 = без изменений, 70 = −30%, 50 = −50%.
# Более конкретный регион перекрывает общий, поэтому Москве явно ставим 100.
REGION_MODIFIERS = {
    3: 70,       # Центральный ФО
    1: 70,       # Москва и Московская область
    213: 100,    # Москва — без изменений
    17: 50,      # Северо-Западный ФО
    10174: 70,   # Санкт-Петербург и Ленинградская область
    40: 70,      # Приволжский ФО
    52: 50,      # Уральский ФО
}
# -------------------------------------------

cfg = json.loads((Path.home() / "AWER CRM" / "config.json").read_text(encoding="utf-8"))
TOKEN, LOGIN = cfg.get("yandex_oauth_token", ""), cfg.get("direct_client_login", "")
if not TOKEN:
    raise SystemExit("В CRM не сохранён токен Яндекса (Маркетинг → Подключение к Яндексу).")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept-Language": "ru"}
if LOGIN:
    HEADERS["Client-Login"] = LOGIN
API = "https://api.direct.yandex.com/json/v5/"
BACKUP_DIR = Path(__file__).resolve().with_name("direct_backups")   # рядом со скриптом, откуда бы ни запускали
BACKUP_DIR.mkdir(exist_ok=True)
IDS = list(CAMPAIGNS)


def call(service, method, params):
    try:
        r = requests.post(API + service, headers=HEADERS, json={"method": method, "params": params}, timeout=60)
        data = json.loads(r.content.decode("utf-8"))
    except (requests.RequestException, ValueError) as e:
        raise RuntimeError(f"{service}.{method}: нет ответа от Директа ({e.__class__.__name__})")
    if "error" in data:
        e = data["error"]
        raise RuntimeError(f"{service}.{method}: {e.get('error_code')} {e.get('error_string')} — {e.get('error_detail')}")
    return data["result"]


def report_results(title, results):
    for i, res in enumerate(results):
        errs = res.get("Errors") or []
        warns = res.get("Warnings") or []
        if errs:
            for e in errs:
                print(f"  ✗ {title} [{i}]: {e.get('Message')} — {e.get('Details')}")
        else:
            print(f"  ✓ {title} [{i}]" + (f" (предупреждение: {warns[0].get('Details')})" if warns else ""))


def get_state():
    res = call("campaigns", "get", {
        "SelectionCriteria": {"Ids": IDS},
        "FieldNames": ["Id", "Name", "ExcludedSites"],
        "TextCampaignFieldNames": ["BiddingStrategy"],
    })
    camps = {c["Id"]: c for c in res.get("Campaigns", [])}
    params = {
        "SelectionCriteria": {"CampaignIds": IDS, "Levels": ["CAMPAIGN"]},
        "FieldNames": ["Id", "CampaignId", "Type", "Level"],
        "MobileAdjustmentFieldNames": ["BidModifier"],
        "DesktopAdjustmentFieldNames": ["BidModifier"],
        "TabletAdjustmentFieldNames": ["BidModifier"],
        "RegionalAdjustmentFieldNames": ["RegionId", "BidModifier"],
        "DemographicsAdjustmentFieldNames": ["Age", "Gender", "BidModifier"],
        "RetargetingAdjustmentFieldNames": ["RetargetingConditionId", "BidModifier"],
        "DesktopOnlyAdjustmentFieldNames": ["BidModifier"],
    }
    try:
        mods = call("bidmodifiers", "get", params).get("BidModifiers", [])
    except RuntimeError:
        params.pop("DesktopOnlyAdjustmentFieldNames")
        mods = call("bidmodifiers", "get", params).get("BidModifiers", [])
    return camps, mods


def demo_key(m):
    d = m.get("DemographicsAdjustment") or {}
    return (d.get("Gender"), d.get("Age"))


def demo_plan(cid, mods):
    """Что сделать с корректировками пол/возраст: (удалить, изменить, добавить)."""
    existing = {demo_key(m): m for m in mods
                if m.get("CampaignId") == cid and m.get("Type") == "DEMOGRAPHICS_ADJUSTMENT"}
    to_delete = [m for k, m in existing.items() if k not in DEMOGRAPHICS and k not in KEEP_AS_IS]
    to_set = [(m, DEMOGRAPHICS[k]) for k, m in existing.items()
              if k in DEMOGRAPHICS and m["DemographicsAdjustment"]["BidModifier"] != DEMOGRAPHICS[k]]
    to_add = [(k, v) for k, v in DEMOGRAPHICS.items() if k not in existing]
    return to_delete, to_set, to_add


def demo_label(k):
    g = {"GENDER_MALE": "мужчины ", "GENDER_FEMALE": "женщины ", None: ""}[k[0]]
    return g + k[1].replace("AGE_", "").replace("_", "–")


def network_strategy(c):
    return ((c.get("TextCampaign") or {}).get("BiddingStrategy") or {})


def show_plan(camps, mods):
    for cid, name in CAMPAIGNS.items():
        c = camps.get(cid)
        if not c:
            print(f"\n{name} ({cid}): кампания не найдена!")
            continue
        strat = network_strategy(c)
        net = strat.get("Network") or {}
        stype = net.get("BiddingStrategyType")
        params = next((v for k, v in net.items() if isinstance(v, dict)), {}) or {}
        cur = params.get("WeeklySpendLimit")
        cur_rub = cur / 1_000_000 if cur else None
        excluded = ((c.get("ExcludedSites") or {}).get("Items")) or []
        add_sites = [s for s in EXCLUDE if s not in excluded]
        print(f"\n=== {name} ({cid}) ===")
        print(f"Стратегия в сетях: {stype}")
        print(f"Недельный бюджет: {cur_rub if cur_rub is not None else 'не задан'} → {WEEKLY_BUDGET[cid]} ₽ без НДС")
        print(f"Запрещённых площадок сейчас: {len(excluded)}; добавится: {len(add_sites)} → {', '.join(add_sites) or 'ничего'}")
        existing = [m for m in mods if m.get("CampaignId") == cid]
        print(f"Корректировок на уровне кампании сейчас: {len(existing)}")
        for m in existing:
            body = {k: v for k, v in m.items() if k not in ("Id", "CampaignId", "Type", "Level")}
            print(f"   • {m.get('Type')}: {json.dumps(body, ensure_ascii=False)}")
        d, st, ad = demo_plan(cid, mods)
        for m in d:
            print(f"   − удалить: {demo_label(demo_key(m))} ({m['DemographicsAdjustment']['BidModifier'] - 100:+d}%)")
        for m, v in st:
            print(f"   ~ изменить: {demo_label(demo_key(m))} {m['DemographicsAdjustment']['BidModifier'] - 100:+d}% → {v - 100:+d}%")
        for k, v in ad:
            print(f"   + добавить: {demo_label(k)} {v - 100:+d}%")
        have = {m.get("Type") for m in existing}
        if "TABLET_ADJUSTMENT" not in have:
            print(f"   + добавить: планшеты {TABLET_MODIFIER - 100:+d}%")
        if "REGIONAL_ADJUSTMENT" not in have:
            print("   + добавить регионы: " + ", ".join(f"{r}={v - 100:+d}%" for r, v in REGION_MODIFIERS.items()))


def has_changes(camps, mods):
    for cid in CAMPAIGNS:
        c = camps.get(cid)
        if not c:
            continue
        net = network_strategy(c).get("Network") or {}
        params = next((v for k, v in net.items() if isinstance(v, dict)), None)
        if params is not None and params.get("WeeklySpendLimit") != int(WEEKLY_BUDGET[cid] * 1_000_000):
            return True
        excluded = ((c.get("ExcludedSites") or {}).get("Items")) or []
        if any(s not in excluded for s in EXCLUDE):
            return True
        if any(demo_plan(cid, mods)):
            return True
        have = {m.get("Type") for m in mods if m.get("CampaignId") == cid}
        if "TABLET_ADJUSTMENT" not in have or "REGIONAL_ADJUSTMENT" not in have:
            return True
    return False


def apply(camps, mods):
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = {"campaigns": camps, "bidmodifiers": mods, "added_bidmodifier_ids": []}
    path = BACKUP_DIR / f"backup_{stamp}.json"
    path.write_text(json.dumps(backup, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nРезервная копия: {path}")

    # 1. Бюджет + запрещённые площадки
    items = []
    for cid in CAMPAIGNS:
        c = camps.get(cid)
        if not c:
            print(f"  ! {CAMPAIGNS[cid]}: кампания не найдена — пропускаю")
            continue
        strat = json.loads(json.dumps(network_strategy(c)))   # копия
        net = strat.get("Network") or {}
        key = next((k for k, v in net.items() if isinstance(v, dict)), None)
        if key:
            net[key]["WeeklySpendLimit"] = int(WEEKLY_BUDGET[cid] * 1_000_000)
        excluded = ((c.get("ExcludedSites") or {}).get("Items")) or []
        item = {"Id": cid, "ExcludedSites": {"Items": excluded + [s for s in EXCLUDE if s not in excluded]}}
        if key:
            item["TextCampaign"] = {"BiddingStrategy": strat}
        else:
            print(f"  ! {CAMPAIGNS[cid]}: не нашёл параметры стратегии, бюджет не меняю")
        items.append(item)
    print("\nКампании: бюджет и запрещённые площадки")
    try:
        report_results("campaigns.update", call("campaigns", "update", {"Campaigns": items}).get("UpdateResults", []))
    except RuntimeError as e:
        print(f"  ✗ {e}")

    # 2. Корректировки
    added, changed, deleted = [], [], []
    for cid in CAMPAIGNS:
        if cid not in camps:
            continue
        name = CAMPAIGNS[cid]
        d, st, ad = demo_plan(cid, mods)
        if d:
            try:
                report_results(f"{name}: удаление пол/возраст",
                               call("bidmodifiers", "delete", {"SelectionCriteria": {"Ids": [m["Id"] for m in d]}})
                               .get("DeleteResults", []))
                deleted += d
            except RuntimeError as e:
                print(f"  ✗ {name}: удаление: {e}")
        if st:
            try:
                report_results(f"{name}: изменение пол/возраст",
                               call("bidmodifiers", "set", {"BidModifiers": [{"Id": m["Id"], "BidModifier": v} for m, v in st]})
                               .get("SetResults", []))
                changed += [{"Id": m["Id"], "BidModifier": m["DemographicsAdjustment"]["BidModifier"]} for m, v in st]
            except RuntimeError as e:
                print(f"  ✗ {name}: изменение: {e}")
        have = {m.get("Type") for m in mods if m.get("CampaignId") == cid}
        groups = []
        if ad:
            groups.append(("пол/возраст", {"CampaignId": cid, "DemographicsAdjustments": [
                ({"Gender": k[0]} if k[0] else {}) | {"Age": k[1], "BidModifier": v} for k, v in ad]}))
        if "TABLET_ADJUSTMENT" not in have:
            groups.append(("планшеты", {"CampaignId": cid, "TabletAdjustment": {"BidModifier": TABLET_MODIFIER}}))
        if "REGIONAL_ADJUSTMENT" not in have:
            groups.append(("регионы", {"CampaignId": cid, "RegionalAdjustments": [
                {"RegionId": r, "BidModifier": v} for r, v in REGION_MODIFIERS.items()]}))
        for title, item in groups:
            try:
                res = call("bidmodifiers", "add", {"BidModifiers": [item]}).get("AddResults", [])
                report_results(f"{name}: {title}", res)
                for r in res:
                    added += r.get("Ids") or []
            except RuntimeError as e:
                print(f"  ✗ {name}: {title}: {e}")
    backup["changed"] = changed
    backup["deleted"] = deleted
    backup["added_bidmodifier_ids"] = added
    path.write_text(json.dumps(backup, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nГотово. Добавлено корректировок: {len(added)}. Для отката: python direct_stage1.py --rollback")


def rollback():
    files = sorted(BACKUP_DIR.glob("backup_*.json"))
    if not files:
        raise SystemExit("Резервных копий нет.")
    b = json.loads(files[-1].read_text(encoding="utf-8"))
    print(f"Откат по {files[-1]}")
    if b.get("added_bidmodifier_ids"):
        report_results("bidmodifiers.delete",
                       call("bidmodifiers", "delete", {"SelectionCriteria": {"Ids": b["added_bidmodifier_ids"]}})
                       .get("DeleteResults", []))
    if b.get("changed"):
        report_results("bidmodifiers.set (возврат значений)",
                       call("bidmodifiers", "set", {"BidModifiers": b["changed"]}).get("SetResults", []))
    for m in b.get("deleted") or []:
        d = m["DemographicsAdjustment"]
        item = ({"Gender": d["Gender"]} if d.get("Gender") else {}) | {"Age": d["Age"], "BidModifier": d["BidModifier"]}
        report_results("bidmodifiers.add (возврат удалённой)",
                       call("bidmodifiers", "add", {"BidModifiers": [{"CampaignId": m["CampaignId"],
                                                                       "DemographicsAdjustments": [item]}]})
                       .get("AddResults", []))
    now_camps, _ = get_state()
    items = []
    for cid, c in b["campaigns"].items():
        old_sites = ((c.get("ExcludedSites") or {}).get("Items")) or []
        # пустой список в API задаётся как null
        item = {"Id": int(cid), "ExcludedSites": {"Items": old_sites} if old_sites else None}
        strat = network_strategy(c)
        now = network_strategy(now_camps.get(int(cid)) or {})
        kinds = lambda s: ((s.get("Search") or {}).get("BiddingStrategyType"), (s.get("Network") or {}).get("BiddingStrategyType"))
        if strat and kinds(strat) == kinds(now):
            item["TextCampaign"] = {"BiddingStrategy": strat}
        elif strat:
            print(f"  ! {cid}: стратегия с тех пор сменилась ({kinds(strat)} → {kinds(now)}) — её не трогаю, "
                  "иначе сбросится обучение. Бюджет верните вручную, если нужно.")
        items.append(item)
    report_results("campaigns.update", call("campaigns", "update", {"Campaigns": items}).get("UpdateResults", []))


if __name__ == "__main__":
    if "--rollback" in sys.argv:
        rollback()
        sys.exit()
    camps, mods = get_state()
    show_plan(camps, mods)
    if not has_changes(camps, mods):
        print("\nВсё уже как в настройках этапа 1 — менять нечего. Резервная копия не создаётся.")
    elif "--apply" in sys.argv:
        apply(camps, mods)
    else:
        print("\nЭто пробный прогон — ничего не изменено. Чтобы применить: python direct_stage1.py --apply")
