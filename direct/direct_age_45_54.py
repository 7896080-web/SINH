# -*- coding: utf-8 -*-
"""
Корректировка ставки для возраста 45–54 (оба пола) в кампаниях ЕПК Сети КО и ТО.
Остальные корректировки, бюджеты и площадки не трогает.

С 10.10.2026 стоит −20% (до этого −50%: доля расхода на 45–54 упала с 31% до 3%).
Цель — около 15%. Директ не задаёт долю расхода напрямую, только ставку:
по двум точкам (без корректировки → 31%, −50% → 3%) выходит примерно −20%.

Запуск:
  python direct_age_45_54.py              — пробный прогон: покажет, что изменится
  python direct_age_45_54.py --apply      — применить −20%
  python direct_age_45_54.py -30 --apply  — применить другое значение (например −30%)
                                            Знак обязателен: -30 или +10. Число без знака скрипт не примет,
                                            чтобы «20» случайно не стало +20% вместо −20%.
  python direct_age_45_54.py --rollback   — вернуть прежнее значение

Резервные копии: direct_backups\\age4554_*.json (откат этапа 1 они не затрагивают).
Токен берётся из настроек AWER CRM.
"""
import json
import re
import sys
import time
from pathlib import Path

import requests

from yandex_token import crm_config, yandex_token

CAMPAIGNS = {712220172: "ЕПК Сети КО", 712219913: "ЕПК Сети ТО"}
AGE = "AGE_45_54"
NEW_PERCENT = -20            # −20% → BidModifier 80

for raw in sys.argv[1:]:
    if raw in ("--apply", "--rollback"):
        continue
    a = raw.replace("−", "-").replace("–", "-").replace("—", "-").strip().rstrip("%")   # «−30», «-30%» — тоже
    if re.fullmatch(r"\d+", a):
        raise SystemExit(f"Укажите знак: -{a} (снизить ставку на {a}%) или +{a} (повысить). Число без знака не принимаю.")
    if re.fullmatch(r"[-+]\d+", a):
        NEW_PERCENT = int(a)
    else:
        raise SystemExit(f"Не понял «{raw}». Можно: -30 (снизить на 30%), +10, --apply, --rollback.")
NEW_MODIFIER = 100 + NEW_PERCENT
if not 0 <= NEW_MODIFIER <= 1300:
    raise SystemExit(f"Недопустимое значение {NEW_PERCENT}%: можно от −100% до +1200%.")

TOKEN, LOGIN = yandex_token(), crm_config().get("direct_client_login", "")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept-Language": "ru"}
if LOGIN:
    HEADERS["Client-Login"] = LOGIN
API = "https://api.direct.yandex.com/json/v5/"
BACKUP_DIR = Path(__file__).resolve().with_name("direct_backups")   # рядом со скриптом, откуда бы ни запускали
BACKUP_DIR.mkdir(exist_ok=True)


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


def report(title, results):
    ok = True
    for res in results:
        for e in res.get("Errors") or []:
            ok = False
            print(f"  ✗ {title}: {e.get('Message')} — {e.get('Details')}")
    if ok:
        print(f"  ✓ {title}")
    return ok


def current():
    """{campaign_id: модификатор 45–54 для обоих полов или None}"""
    mods = call("bidmodifiers", "get", {
        "SelectionCriteria": {"CampaignIds": list(CAMPAIGNS), "Levels": ["CAMPAIGN"],
                              "Types": ["DEMOGRAPHICS_ADJUSTMENT"]},
        "FieldNames": ["Id", "CampaignId", "Type"],
        "DemographicsAdjustmentFieldNames": ["Age", "Gender", "BidModifier"],
    }).get("BidModifiers", [])
    found = {cid: None for cid in CAMPAIGNS}
    other = []
    for m in mods:
        d = m.get("DemographicsAdjustment") or {}
        if d.get("Age") == AGE and not d.get("Gender"):
            found[m["CampaignId"]] = m
        elif d.get("Age") == AGE:
            other.append(m)
    return found, other


def pct(v):
    return f"{v - 100:+d}%"


def show(found, other):
    for cid, name in CAMPAIGNS.items():
        m = found.get(cid)
        if m:
            print(f"{name}: 45–54 сейчас {pct(m['DemographicsAdjustment']['BidModifier'])} → будет {pct(NEW_MODIFIER)}")
        else:
            print(f"{name}: корректировки на 45–54 нет → будет добавлена {pct(NEW_MODIFIER)}")
    for m in other:
        d = m["DemographicsAdjustment"]
        g = "мужчины" if d.get("Gender") == "GENDER_MALE" else "женщины"
        print(f"  ! В кампании {CAMPAIGNS.get(m['CampaignId'], m['CampaignId'])} есть отдельная корректировка "
              f"«{g} 45–54» {pct(d['BidModifier'])} — она перекрывает общую, скрипт её не трогает.")


def apply(found):
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = {"before": {str(cid): (m["DemographicsAdjustment"]["BidModifier"] if m else None)
                         for cid, m in found.items()},
              "ids": {str(cid): (m["Id"] if m else None) for cid, m in found.items()},
              "added_ids": []}
    path = BACKUP_DIR / f"age4554_{stamp}.json"
    path.write_text(json.dumps(backup, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nРезервная копия: {path}")

    to_set = [{"Id": m["Id"], "BidModifier": NEW_MODIFIER} for m in found.values()
              if m and m["DemographicsAdjustment"]["BidModifier"] != NEW_MODIFIER]
    if to_set:
        try:
            report(f"изменено на {pct(NEW_MODIFIER)} ({len(to_set)} шт.)",
                   call("bidmodifiers", "set", {"BidModifiers": to_set}).get("SetResults", []))
        except RuntimeError as e:
            print(f"  ✗ изменение: {e} — проверьте в кабинете (откат вернёт прежние значения в любом случае)")
    for cid, m in found.items():
        if m is None:
            try:
                res = call("bidmodifiers", "add", {"BidModifiers": [{
                    "CampaignId": cid, "DemographicsAdjustments": [{"Age": AGE, "BidModifier": NEW_MODIFIER}]}]}
                ).get("AddResults", [])
            except RuntimeError as e:
                print(f"  ✗ {CAMPAIGNS[cid]}: добавление: {e} — если корректировка всё же появилась, откат её не уберёт; "
                      "проверьте в кабинете")
                continue
            if report(f"{CAMPAIGNS[cid]}: добавлено {pct(NEW_MODIFIER)}", res):
                for r in res:
                    backup["added_ids"] += r.get("Ids") or []
            path.write_text(json.dumps(backup, ensure_ascii=False, indent=1), encoding="utf-8")
    path.write_text(json.dumps(backup, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\nПроверка после изменения:")
    found2, _ = current()
    for cid, name in CAMPAIGNS.items():
        m = found2.get(cid)
        print(f"  {name}: 45–54 {pct(m['DemographicsAdjustment']['BidModifier']) if m else 'нет корректировки'}")
    print("\nОткат: python direct_age_45_54.py --rollback")


def rollback():
    files = sorted(BACKUP_DIR.glob("age4554_*.json"))
    if not files:
        raise SystemExit("Резервных копий этого скрипта нет.")
    b = json.loads(files[-1].read_text(encoding="utf-8"))
    print(f"Откат по {files[-1]}")
    if b.get("added_ids"):
        report("удалены добавленные корректировки",
               call("bidmodifiers", "delete", {"SelectionCriteria": {"Ids": b["added_ids"]}}).get("DeleteResults", []))
    items = [{"Id": b["ids"][cid], "BidModifier": v} for cid, v in b["before"].items()
             if v is not None and b["ids"].get(cid)]
    if items:
        report("возвращены прежние значения",
               call("bidmodifiers", "set", {"BidModifiers": items}).get("SetResults", []))
    found, _ = current()
    for cid, name in CAMPAIGNS.items():
        m = found.get(cid)
        print(f"  {name}: 45–54 {pct(m['DemographicsAdjustment']['BidModifier']) if m else 'нет корректировки'}")


if __name__ == "__main__":
    if "--rollback" in sys.argv:
        rollback()
        sys.exit()
    found, other = current()
    show(found, other)
    if all(m and m["DemographicsAdjustment"]["BidModifier"] == NEW_MODIFIER for m in found.values()):
        print(f"\nУже стоит {pct(NEW_MODIFIER)} — менять нечего. Резервная копия не создаётся.")
    elif "--apply" in sys.argv:
        apply(found)
    else:
        print(f"\nЭто пробный прогон — ничего не изменено. Применить: python direct_age_45_54.py {NEW_PERCENT:+d} --apply")
