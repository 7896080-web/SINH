# -*- coding: utf-8 -*-
"""
Проверка товарных фидов в Яндекс Директе:
  1) какие фиды загружены, их статус, дата последнего обновления, число товаров;
  2) какой фид используют группы объявлений в каждой кампании;
  3) скачивает каждый фид по ссылке и ищет старые ошибки в названиях:
     «Vinyil», размер в названии, приписку AWER к чужим брендам.

Запуск: python direct_feeds.py  -> вывод + direct_feeds.txt
"""
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from yandex_token import yandex_token, crm_config

CAMPAIGNS = {712220172: "ЕПК Сети КО", 712219913: "ЕПК Сети ТО", 714517819: "ЕПК Сети ТО (новый фид)"}
PARTNER_BRANDS = ["dauntless", "black vinyl", "black vinyil", "mcl", "konko", "nero", "tony montana"]

URL_ARGS = [a for a in sys.argv[1:] if a.startswith("http")]

cfg = crm_config()
HEADERS = {"Authorization": f"Bearer {yandex_token()}", "Accept-Language": "ru"}
if cfg.get("direct_client_login"):
    HEADERS["Client-Login"] = cfg["direct_client_login"]
API = "https://api.direct.yandex.com/json/v5/"
out = []


def log(s=""):
    print(s)
    out.append(str(s))


def call(service, method, params):
    r = requests.post(API + service, headers=HEADERS, json={"method": method, "params": params}, timeout=60)
    data = json.loads(r.content.decode("utf-8"))
    if "error" in data:
        e = data["error"]
        raise RuntimeError(f"{service}.{method}: {e.get('error_code')} {e.get('error_string')} — {e.get('error_detail')}")
    return data["result"]


if URL_ARGS:
    feeds_from_args = [{"Id": f"по ссылке {i + 1}", "UrlFeed": {"Url": u}} for i, u in enumerate(URL_ARGS)]
# 1. Группы объявлений и их фиды (feeds.get требует Id фидов — берём их из групп)
log("=== Группы объявлений и их фиды ===")
groups = []
for params in ({"SelectionCriteria": {"CampaignIds": list(CAMPAIGNS)}, "FieldNames": ["Id", "Name", "CampaignId", "Type", "Status"],
                "TextAdGroupFeedParamsFieldNames": ["FeedId", "FeedCategoryIds"]},
               {"SelectionCriteria": {"CampaignIds": list(CAMPAIGNS)}, "FieldNames": ["Id", "Name", "CampaignId", "Type", "Status"]}):
    try:
        groups = call("adgroups", "get", params).get("AdGroups", [])
        break
    except RuntimeError as e:
        log(f"(запрос групп не прошёл: {e})")
feed_ids = set(int(a) for a in sys.argv[1:] if a.isdigit())
for g in sorted(groups, key=lambda g: (g["CampaignId"], g["Name"])):
    fp = g.get("TextAdGroupFeedParams") or {}
    if fp.get("FeedId"):
        feed_ids.add(fp["FeedId"])
    log(f"{CAMPAIGNS.get(g['CampaignId'], g['CampaignId'])} | {g['Name']} | {g.get('Type')} | {g.get('Status')} | "
        f"фид: {fp.get('FeedId', '—')}")

# 2. Фиды
feeds = []
if not feed_ids:
    log("\nВ группах не нашлось ссылок на фиды. Посмотрите номер фида в Директе (Библиотека → Фиды) "
        "и запустите так: python direct_feeds.py НОМЕР_ФИДА")
else:
    for fields in (["Id", "Name", "BusinessType", "SourceType", "UpdatedAt", "CampaignIds", "NumberOfItems", "Status"],
                   ["Id", "Name", "Status"]):
        try:
            feeds = call("feeds", "get", {"SelectionCriteria": {"Ids": sorted(feed_ids)}, "FieldNames": fields,
                                          "UrlFeedFieldNames": ["Url"], "FileFeedFieldNames": ["Filename"]}).get("Feeds", [])
            break
        except RuntimeError as e:
            log(f"(запрос фидов не прошёл: {e})")
if URL_ARGS:
    feeds = feeds_from_args
log(f"\nФидов найдено: {len(feeds)}")
for f in feeds:
    url = (f.get("UrlFeed") or {}).get("Url") or (f.get("FileFeed") or {}).get("Filename") or "—"
    log(f"\n• Фид {f['Id']} «{f.get('Name')}»")
    log(f"  статус: {f.get('Status')}, обновлён: {f.get('UpdatedAt')}, товаров: {f.get('NumberOfItems')}")
    log(f"  источник: {url}")
    if f.get("CampaignIds"):
        log("  используется в кампаниях: " + ", ".join(CAMPAIGNS.get(c, str(c)) for c in f["CampaignIds"]))

# 3. Содержимое фидов
size_re = re.compile(r"(\b(4[4-9]|5\d|6[0-4])\b.{0,3}(р\.|р-р|размер)|(р\.|р-р|размер)\s*\d{2})", re.I)
for f in feeds:
    url = (f.get("UrlFeed") or {}).get("Url")
    if not url:
        continue
    log(f"\n=== Проверка содержимого фида {f['Id']} ===")
    try:
        r = requests.get(url, timeout=60)
        root = ET.fromstring(r.content)
    except Exception as e:  # noqa: BLE001
        log(f"Не удалось скачать или разобрать: {e}")
        continue
    offers = root.findall(".//offer")
    names = []
    for o in offers:
        name = (o.findtext("name") or o.findtext("model") or "").strip()
        vendor = (o.findtext("vendor") or "").strip()
        names.append((name, vendor))
    log(f"Товаров в файле: {len(offers)}")
    vinyil = [n for n, v in names if "vinyil" in (n + " " + v).lower()]
    sized = [n for n, v in names if size_re.search(n)]
    awer_partner = [n for n, v in names if "awer" in n.lower() and any(b in n.lower() for b in PARTNER_BRANDS if b != "awer")]
    vendors = sorted({v for n, v in names if v})
    log(f"«Vinyil» в названии или бренде: {len(vinyil)}" + (f" — например: {vinyil[0]}" if vinyil else " ✓"))
    log(f"Размер в названии: {len(sized)}" + (f" — например: {sized[0]}" if sized else " ✓"))
    log(f"AWER рядом с чужим брендом: {len(awer_partner)}" + (f" — например: {awer_partner[0]}" if awer_partner else " ✓"))
    log(f"Бренды (vendor): {', '.join(vendors) if vendors else 'не указаны'}")
    bv = [n for n, v in names if "vinyl" in (n + " " + v).lower() or "vinyil" in (n + " " + v).lower()]
    if bv:
        log("Названия товаров Black Vinyl в фиде (до 8):")
        for n in bv[:8]:
            log(f"   {n}")

Path("direct_feeds.txt").write_text("\n".join(out), encoding="utf-8")
print("\nСохранено в direct_feeds.txt")
