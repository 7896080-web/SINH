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
иначе она стала бы «последней» и откат к состоянию до этапа 1 был бы потерян. Повторный --apply, когда этап 1 уже
применялся, — только с --again (по той же причине).

Возраст 45–54 этим скриптом больше не управляется (с 14.10.2026): им занимается direct_age_45_54.py. Иначе любой
повторный --apply возвращал бы −20%, а откат этапа 1 стирал бы значение, поставленное позже.

Откат (сразу, без пробного прогона) возвращает:
  • недельный бюджет — только его: остальная стратегия (тип, цель, ограничения) остаётся как сейчас; если тип
    стратегии с тех пор сменился — бюджет не трогает (верните вручную);
  • площадки — убирает из запрещённых только те, что добавил этап 1; запрещённые позже остаются;
  • корректировки, которые добавил или изменил этап 1 (планшеты, регионы, возраст до 24), — кроме 45–54.
Каждый шаг отдельно: сбой одного не останавливает остальные, в конце — итог.

Токен берётся из настроек AWER CRM (Маркетинг → Подключение к Яндексу).
"""
import copy
import json
import sys
import time
from pathlib import Path

import requests

from yandex_token import crm_config, yandex_token

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
}
# Эти корректировки не трогаем: где есть — остаются как есть, где нет — не добавляются.
# 45–54 ведёт direct_age_45_54.py (06.10 этап 1 поставил −50%, 10.10 смягчено до −20%).
KEEP_AS_IS = {("GENDER_FEMALE", "AGE_55"), (None, "AGE_45_54")}
AGE_SEPARATE = "AGE_45_54"
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

TOKEN, LOGIN = yandex_token(), crm_config().get("direct_client_login", "")
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
    backup = {"campaigns": camps, "bidmodifiers": mods, "added_bidmodifier_ids": [], "changed": [], "deleted": [],
              "unclear": []}
    path = BACKUP_DIR / f"backup_{stamp}.json"

    def save():          # после каждого шага: если скрипт оборвётся, откат знает, что уже сделано
        path.write_text(json.dumps(backup, ensure_ascii=False, indent=1), encoding="utf-8")

    def failed(title, e):
        print(f"  ✗ {title}: {e}")
        if "нет ответа" in str(e):     # запрос мог дойти — запомнить, чтобы проверить руками
            backup["unclear"].append(f"{title}: {e}")
            save()

    save()
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
        failed("campaigns.update", e)

    # 2. Корректировки
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
                backup["deleted"] += d
                save()
            except RuntimeError as e:
                failed(f"{name}: удаление пол/возраст", e)
        if st:
            try:
                report_results(f"{name}: изменение пол/возраст",
                               call("bidmodifiers", "set", {"BidModifiers": [{"Id": m["Id"], "BidModifier": v} for m, v in st]})
                               .get("SetResults", []))
                backup["changed"] += [{"Id": m["Id"], "BidModifier": m["DemographicsAdjustment"]["BidModifier"]} for m, v in st]
                save()
            except RuntimeError as e:
                failed(f"{name}: изменение пол/возраст", e)
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
                    backup["added_bidmodifier_ids"] += r.get("Ids") or []
                save()
            except RuntimeError as e:
                failed(f"{name}: {title}", e)
    save()
    print(f"\nГотово. Добавлено корректировок: {len(backup['added_bidmodifier_ids'])}. Для отката: python direct_stage1.py --rollback")
    if backup["unclear"]:
        print("  ! Директ не ответил на шаги (могли пройти, а откат их не знает) — проверьте в кабинете: "
              + "; ".join(backup["unclear"]))


def rollback():
    files = sorted(BACKUP_DIR.glob("backup_*.json"))
    if not files:
        raise SystemExit("Резервных копий нет.")
    b = json.loads(files[-1].read_text(encoding="utf-8"))
    print(f"Откат по {files[-1]}")
    now_camps, now_mods = get_state()
    by_id = {m.get("Id"): m for m in now_mods}
    done, failed_steps = [], []

    def step(title, fn):
        try:
            fn()
            done.append(title)
        except RuntimeError as e:          # один сбой не останавливает остальные шаги
            print(f"  ✗ {title}: {e}")
            failed_steps.append(title)

    def is_age_separate(mod_id):
        m = by_id.get(mod_id) or {}
        return (m.get("DemographicsAdjustment") or {}).get("Age") == AGE_SEPARATE

    added = [i for i in b.get("added_bidmodifier_ids") or [] if not is_age_separate(i)]
    changed = [x for x in b.get("changed") or [] if not is_age_separate(x["Id"])]
    deleted = [m for m in b.get("deleted") or [] if (m.get("DemographicsAdjustment") or {}).get("Age") != AGE_SEPARATE]
    skipped = (len(b.get("added_bidmodifier_ids") or []) - len(added)) + (len(b.get("changed") or []) - len(changed)) \
        + (len(b.get("deleted") or []) - len(deleted))
    if skipped:
        print(f"  Корректировку 45–54 не трогаю ({skipped} шт.): ею управляет direct_age_45_54.py "
              "(вернуть её прежнее значение — python direct_age_45_54.py --rollback).")
    if added:
        step("удалить добавленные корректировки", lambda: report_results(
            "bidmodifiers.delete", call("bidmodifiers", "delete", {"SelectionCriteria": {"Ids": added}}).get("DeleteResults", [])))
    if changed:
        step("вернуть прежние значения корректировок", lambda: report_results(
            "bidmodifiers.set (возврат значений)", call("bidmodifiers", "set", {"BidModifiers": changed}).get("SetResults", [])))
    for m in deleted:
        d = m["DemographicsAdjustment"]
        item = ({"Gender": d["Gender"]} if d.get("Gender") else {}) | {"Age": d["Age"], "BidModifier": d["BidModifier"]}
        step(f"вернуть удалённую корректировку {d['Age']}", lambda m=m, item=item: report_results(
            "bidmodifiers.add (возврат удалённой)",
            call("bidmodifiers", "add", {"BidModifiers": [{"CampaignId": m["CampaignId"], "DemographicsAdjustments": [item]}]})
            .get("AddResults", [])))
    kinds = lambda s: ((s.get("Search") or {}).get("BiddingStrategyType"), (s.get("Network") or {}).get("BiddingStrategyType"))
    for cid, c in b["campaigns"].items():
        now_c = now_camps.get(int(cid)) or {}
        name = CAMPAIGNS.get(int(cid), cid)
        # площадки: убрать из запрещённых только добавленные этапом 1
        old_sites = ((c.get("ExcludedSites") or {}).get("Items")) or []
        now_sites = ((now_c.get("ExcludedSites") or {}).get("Items")) or []
        keep = [s for s in now_sites if not (s in EXCLUDE and s not in old_sites)]
        item = {"Id": int(cid)}
        if keep != now_sites:
            item["ExcludedSites"] = {"Items": keep} if keep else None      # пустой список в API задаётся как null
            print(f"  {name}: из запрещённых площадок убираю {len(now_sites) - len(keep)} (добавленные этапом 1)")
        # недельный бюджет: только он, в текущей стратегии
        old_s, now_s = network_strategy(c), network_strategy(now_c)
        okey = next((k for k, v in (old_s.get("Network") or {}).items() if isinstance(v, dict)), None)
        nkey = next((k for k, v in (now_s.get("Network") or {}).items() if isinstance(v, dict)), None)
        old_lim = ((old_s.get("Network") or {}).get(okey) or {}).get("WeeklySpendLimit") if okey else None
        if old_s and (kinds(old_s) != kinds(now_s) or okey != nkey):
            print(f"  ! {name}: стратегия с тех пор сменилась ({kinds(old_s)} → {kinds(now_s)}) — бюджет не трогаю "
                  "(иначе сбросится обучение). Верните его вручную, если нужно.")
        elif old_lim and nkey:
            cur_lim = now_s["Network"][nkey].get("WeeklySpendLimit")
            if cur_lim != old_lim:
                new_s = copy.deepcopy(now_s)
                new_s["Network"][nkey]["WeeklySpendLimit"] = old_lim
                item["TextCampaign"] = {"BiddingStrategy": new_s}
                print(f"  {name}: недельный бюджет {(cur_lim or 0) / 1_000_000:,.0f} → {old_lim / 1_000_000:,.0f} ₽ без НДС "
                      "(остальная стратегия — как сейчас)".replace(",", " "))
        if len(item) > 1:
            step(f"{name}: бюджет и площадки", lambda item=item: report_results(
                "campaigns.update", call("campaigns", "update", {"Campaigns": [item]}).get("UpdateResults", [])))
    print(f"\nИтог отката: сделано шагов {len(done)}" + (f"; НЕ сделано: {', '.join(failed_steps)} — "
                                                        "проверьте в кабинете Директа" if failed_steps else "."))
    if b.get("unclear"):
        print("  ! При применении Директ не ответил на шаги: " + "; ".join(b["unclear"])
              + " — если они прошли, откат их не вернул; проверьте в кабинете.")


if __name__ == "__main__":
    if "--rollback" in sys.argv:
        rollback()
        sys.exit()
    camps, mods = get_state()
    show_plan(camps, mods)
    if not has_changes(camps, mods):
        print("\nВсё уже как в настройках этапа 1 — менять нечего. Резервная копия не создаётся.")
    elif "--apply" in sys.argv and sorted(BACKUP_DIR.glob("backup_*.json")) and "--again" not in sys.argv:
        print(f"\nЭтап 1 уже применялся (последняя копия: {sorted(BACKUP_DIR.glob('backup_*.json'))[-1].name}). "
              "Новая копия станет «последней», и откат будет возвращать уже не к состоянию до этапа 1. "
              "Если всё же нужно: python direct_stage1.py --apply --again")
    elif "--apply" in sys.argv:
        apply(camps, mods)
    else:
        print("\nЭто пробный прогон — ничего не изменено. Чтобы применить: python direct_stage1.py --apply")
