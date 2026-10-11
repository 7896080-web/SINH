# -*- coding: utf-8 -*-
"""
Перенаправление рекламы на куртки AWER:
  A) КО: объявления групп курток ведут не на общий каталог, а на коллекции AWER
     (/catalog/kurtki → /collections/kurtki-awer, /catalog/puhoviki → /collections/awer-kurtki-puhoviki);
     UTM-метки в ссылках сохраняются.
  B) ТО, «Мужские куртки - Новая ЦА»: показывать из фида только AWER и Black Vinyl (без Dauntless).
     Делается, если фильтр товаров группы доступен через API: добавляется новый фильтр с условием
     по бренду, старый — приостанавливается (не удаляется).

Запуск:
  python direct_awer_jackets.py            — пробный прогон (ничего не меняет)
  python direct_awer_jackets.py --apply    — применить (с резервной копией)
  python direct_awer_jackets.py --rollback — вернуть ссылки и фильтры как было
"""
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

KO, TO = 712220172, 712219913
HREF_MAP = {  # группа КО -> (какие старые пути менять, новый путь)
    # объявление «на осень и весну» — на демисезонные куртки AWER (7 моделей на биопухе)
    "Мужские куртки - Новая ЦА": (["/catalog/kurtki", "/collections/kurtki-awer"], "/collections/kurtki-awer-demisezonnye"),
    "Мужские зимние куртки - Новая ЦА": (["/catalog/puhoviki"], "/collections/awer-kurtki-puhoviki"),
}
TO_GROUP = "Мужские куртки - Новая ЦА"
KEEP_BRANDS = ["AWER", "Awer", "Black Vinyil", "Black Vinyl"]

cfg = json.loads((Path.home() / "AWER CRM" / "config.json").read_text(encoding="utf-8"))
H = {"Authorization": f"Bearer {cfg['yandex_oauth_token']}", "Accept-Language": "ru"}
if cfg.get("direct_client_login"):
    H["Client-Login"] = cfg["direct_client_login"]
API = "https://api.direct.yandex.com/json/v5/"
BACKUP = Path("direct_backups")
BACKUP.mkdir(exist_ok=True)


def call(service, method, params):
    r = requests.post(API + service, headers=H, json={"method": method, "params": params}, timeout=60)
    data = json.loads(r.content.decode("utf-8"))
    if "error" in data:
        e = data["error"]
        raise RuntimeError(f"{service}.{method}: {e.get('error_code')} {e.get('error_string')} — {e.get('error_detail')}")
    return data["result"]


def results(title, res):
    for r in res:
        errs = r.get("Errors") or []
        print(f"  {'✗' if errs else '✓'} {title}" + (f": {errs[0].get('Message')} — {errs[0].get('Details')}" if errs else ""))


def groups_by_name(cid):
    gs = call("adgroups", "get", {"SelectionCriteria": {"CampaignIds": [cid]},
                                  "FieldNames": ["Id", "Name", "CampaignId"]}).get("AdGroups", [])
    out = {}
    for g in gs:
        out.setdefault(g["Name"], []).append(g["Id"])
    return out


def new_href(href, old_paths, new_path):
    p = urlsplit(href)
    if p.path.rstrip("/") not in old_paths:
        return None
    return urlunsplit((p.scheme, p.netloc, new_path, p.query, p.fragment))


# ---------- сбор плана ----------
def plan():
    ko_groups = groups_by_name(KO)
    href_changes = []
    print("=== A. Ссылки объявлений КО ===")
    for gname, (old, new) in HREF_MAP.items():
        ids = ko_groups.get(gname, [])
        if not ids:
            print(f"Группа «{gname}» не найдена")
            continue
        ads = call("ads", "get", {"SelectionCriteria": {"AdGroupIds": ids},
                                  "FieldNames": ["Id", "AdGroupId", "Type", "State", "Status"],
                                  "TextAdFieldNames": ["Title", "Href"]}).get("Ads", [])
        for a in ads:
            href = (a.get("TextAd") or {}).get("Href")
            title = (a.get("TextAd") or {}).get("Title")
            if not href:
                print(f"  объявление {a['Id']} ({a.get('Type')}) — ссылка через API недоступна, менять в интерфейсе")
                continue
            nh = new_href(href, old, new)
            if nh:
                href_changes.append({"Id": a["Id"], "old": href, "new": nh})
                print(f"  {a['Id']} «{title}»: {urlsplit(href).path} → {new}")
            else:
                print(f"  {a['Id']} «{title}»: ведёт на {urlsplit(href).path} — не трогаю")

    print("\n=== B. Фильтр товаров в группе ТО «Мужские куртки - Новая ЦА» ===")
    to_ids = groups_by_name(TO).get(TO_GROUP, [])
    targets = []
    if to_ids:
        try:
            targets = call("dynamicfeedadtargets", "get", {
                "SelectionCriteria": {"AdGroupIds": to_ids},
                "FieldNames": ["Id", "AdGroupId", "Name", "Conditions", "State", "AvailableItemsOnly"],
            }).get("DynamicFeedAdTargets", [])
        except RuntimeError as e:
            print(f"  Фильтры через API недоступны ({e}).")
    active = [t for t in targets if t.get("State") != "SUSPENDED"]
    if not active:
        print("  Фильтров товаров в API не найдено — для этой группы фильтр по бренду ставится в интерфейсе:")
        print("  группа → товары из фида → фильтр «Бренд» (vendor): AWER, Awer, Black Vinyil.")
    for t in active:
        conds = t.get("Conditions") or []
        has_vendor = any(c.get("Operand") == "vendor" for c in conds)
        print(f"  фильтр {t['Id']} «{t.get('Name')}»: условия {json.dumps(conds, ensure_ascii=False)}"
              + (" — уже есть условие по бренду, не трогаю" if has_vendor else " → добавлю условие по бренду"))
    target_changes = [t for t in active if not any(c.get("Operand") == "vendor" for c in (t.get("Conditions") or []))]
    return href_changes, target_changes


def apply(href_changes, target_changes):
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = {"href_changes": href_changes, "suspended_targets": [], "added_targets": []}
    path = BACKUP / f"awer_jackets_{stamp}.json"
    if href_changes:
        print("\nМеняю ссылки объявлений КО")
        results("ads.update", call("ads", "update", {"Ads": [{"Id": c["Id"], "TextAd": {"Href": c["new"]}}
                                                            for c in href_changes]}).get("UpdateResults", []))
    for t in target_changes:
        conds = (t.get("Conditions") or []) + [{"Operand": "vendor", "Operator": "EQUALS_ANY", "Arguments": KEEP_BRANDS}]
        item = {"AdGroupId": t["AdGroupId"], "Name": (t.get("Name") or "Фильтр") + " — AWER и Black Vinyl",
                "Conditions": conds, "AvailableItemsOnly": t.get("AvailableItemsOnly", "YES")}
        print(f"\nДобавляю фильтр по бренду для группы {t['AdGroupId']}")
        res = call("dynamicfeedadtargets", "add", {"DynamicFeedAdTargets": [item]}).get("AddResults", [])
        results("dynamicfeedadtargets.add", res)
        new_ids = [r["Id"] for r in res if r.get("Id")]
        if new_ids:
            backup["added_targets"] += new_ids
            results("dynamicfeedadtargets.suspend (старый)",
                    call("dynamicfeedadtargets", "suspend", {"SelectionCriteria": {"Ids": [t["Id"]]}}).get("SuspendResults", []))
            backup["suspended_targets"].append(t["Id"])
    path.write_text(json.dumps(backup, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nГотово. Резервная копия: {path}. Откат: python direct_awer_jackets.py --rollback")


def rollback():
    files = sorted(BACKUP.glob("awer_jackets_*.json"))
    if not files:
        raise SystemExit("Резервных копий нет.")
    b = json.loads(files[-1].read_text(encoding="utf-8"))
    print(f"Откат по {files[-1]}")
    if b.get("href_changes"):
        results("ads.update (старые ссылки)", call("ads", "update", {"Ads": [{"Id": c["Id"], "TextAd": {"Href": c["old"]}}
                                                                         for c in b["href_changes"]]}).get("UpdateResults", []))
    if b.get("suspended_targets"):
        results("dynamicfeedadtargets.resume", call("dynamicfeedadtargets", "resume",
                                                   {"SelectionCriteria": {"Ids": b["suspended_targets"]}}).get("ResumeResults", []))
    if b.get("added_targets"):
        results("dynamicfeedadtargets.delete", call("dynamicfeedadtargets", "delete",
                                                   {"SelectionCriteria": {"Ids": b["added_targets"]}}).get("DeleteResults", []))


if __name__ == "__main__":
    if "--rollback" in sys.argv:
        rollback()
        sys.exit()
    hc, tc = plan()
    if "--apply" in sys.argv:
        apply(hc, tc)
    else:
        print("\nЭто пробный прогон — ничего не изменено. Чтобы применить: python direct_awer_jackets.py --apply")
