# -*- coding: utf-8 -*-
"""
Корректировка ставки для возраста 45–54 (оба пола) в кампаниях ЕПК Сети КО и ТО.
Остальные корректировки, бюджеты и площадки не трогает.

Сейчас стоит −50% (BidModifier 50) — доля расхода на 45–54 упала с 31% до 3%.
Цель — около 15%. Директ не задаёт долю расхода напрямую, только ставку:
по двум точкам (без корректировки → 31%, −50% → 3%) выходит примерно −20%.

Запуск:
  python direct_age_45_54.py              — пробный прогон: покажет, что изменится
  python direct_age_45_54.py --apply      — применить −20%
  python direct_age_45_54.py -30 --apply  — применить другое значение (например −30%)
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

CAMPAIGNS = {712220172: "ЕПК Сети КО", 712219913: "ЕПК Сети ТО"}
AGE = "AGE_45_54"
NEW_PERCENT = -20            # −20% → BidModifier 80

for a in sys.argv[1:]:
    if re.fullmatch(r"[-+]?\d+", a):
        NEW_PERCENT = int(a)
NEW_MODIFIER = 100 + NEW_PERCENT
if not 0 <= NEW_MODIFIER <= 1300:
    raise SystemExit(f"Недопустимое значение {NEW_PERCENT}%: можно от −100% до +1200%.")

cfg = json.loads((Path.home() / "AWER CRM" / "config.json").read_text(encoding="utf-8"))
TOKEN, LOGIN = cfg.get("yandex_oauth_token", ""), cfg.get("direct_client_login", "")
if not TOKEN:
    raise SystemExit("В CRM не сохранён токен Яндекса (Маркетинг → Подключение к Яндексу).")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept-Language": "ru"}
if LOGIN:
    HEADERS["Client-Login"] = LOGIN
API = "https://api.direct.yandex.com/json/v5/"
BACKUP_DIR = Path("direct_backups")
BACKUP_DIR.mkdir(exist_ok=True)


def call(service, method, params):
    r = requests.post(API + service, headers=HEADERS, json={"method": method, "params": params}, timeout=60)
    data = json.loads(r.content.decode("utf-8"))
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
        report(f"изменено на {pct(NEW_MODIFIER)} ({len(to_set)} шт.)",
               call("bidmodifiers", "set", {"BidModifiers": to_set}).get("SetResults", []))
    for cid, m in found.items():
        if m is None:
            res = call("bidmodifiers", "add", {"BidModifiers": [{
                "CampaignId": cid, "DemographicsAdjustments": [{"Age": AGE, "BidModifier": NEW_MODIFIER}]}]}
            ).get("AddResults", [])
            if report(f"{CAMPAIGNS[cid]}: добавлено {pct(NEW_MODIFIER)}", res):
                for r in res:
                    backup["added_ids"] += r.get("Ids") or []
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
    if "--apply" in sys.argv:
        apply(found)
    else:
        print(f"\nЭто пробный прогон — ничего не изменено. Применить: python direct_age_45_54.py --apply")
