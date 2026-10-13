# -*- coding: utf-8 -*-
"""
Минус-фразы WB вручную: добавить исключённые запросы одному товару в одной кампании — с пробным прогоном,
резервной копией и откатом. Нужен в первую очередь, чтобы проверить, принимает ли WB минус-фразы у кампании
за клики через API (в кабинете у таких кампаний этой кнопки нет).

Запуск:
  python wb_minus.py --campaign 36114933 --nm 842037764 --add "куртка женская на осень легкая"
        — пробный прогон: покажет текущий список и что будет записано, ничего не меняет
  ... то же самое + --apply   — записать (сначала резервная копия), потом перечитать у WB и показать результат
  python wb_minus.py --rollback  — вернуть список, который был до последнего --apply (сразу, без пробного прогона)
Несколько фраз — через «;»: --add "фраза 1; фраза 2". Нужна категория токена «Продвижение».

Осторожно: WB при записи ЗАМЕНЯЕТ весь список минус-фраз товара в кампании. Скрипт всегда читает текущий
список и дописывает к нему. Если WB текущий список не вернул: у кампании за клики минус-фраз в кабинете быть
не может, поэтому список считается пустым; у кампании за показы запись в таком случае не делается.
"""
import datetime as dt
import json
import sys
import time

from wb_common import ADV, HERE, ApiError, call, campaigns, load_tokens, log, pace, save_log

BACKUP_DIR = HERE / "wb_minus_backups"


def arg(name):
    if name in sys.argv:
        i = sys.argv.index(name)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    return None


def get_minus(token, cid, nm):
    pace(token, "nq", 0.25)
    data = call("POST", ADV + "/adv/v0/normquery/get-minus", token, what="Минус-фразы",
                body={"items": [{"advert_id": cid, "nm_id": nm}]}) or {}
    for it in data.get("items") or []:
        if it.get("advert_id") == cid and it.get("nm_id") == nm and isinstance(it.get("norm_queries"), list):
            return list(it["norm_queries"])
    return None


def get_excluded(token, cid, nm):
    pace(token, "nq", 0.25)
    try:
        data = call("POST", ADV + "/adv/v0/normquery/list", token, what="Кластеры товара",
                    body={"items": [{"advertId": cid, "nmId": nm}]}) or {}
    except ApiError:
        return None
    for it in data.get("items") or []:
        if it.get("advertId") == cid and it.get("nmId") == nm:
            return list((it.get("normQueries") or {}).get("excluded") or [])
    return None


def set_minus(token, cid, nm, queries):
    pace(token, "nq", 0.25)
    call("POST", ADV + "/adv/v0/normquery/set-minus", token, what="Минус-фразы",
         body={"advert_id": cid, "nm_id": nm, "norm_queries": queries})


def find_campaign(cabs, cid):
    for cab, token in cabs.items():
        camps = campaigns(token, statuses=(9, 11))
        if cid in camps:
            return cab, token, camps[cid]
    return None, None, None


def verify(token, cid, nm, expect):
    time.sleep(3)
    now = get_minus(token, cid, nm)
    exc = get_excluded(token, cid, nm)
    log(f"  Перечитал у WB: минус-фразы {now if now is not None else '— WB список не вернул'}; "
        f"исключённые кластеры {exc if exc is not None else '— нет данных'}.")
    if now is not None and set(now) == set(expect):
        log("  ✓ WB сохранил список.")
        return True
    log("  ! WB не подтвердил список. Возможно, у этого типа кампании минус-фразы не работают — "
        "проверьте в кабинете, а при сомнениях сделайте: python wb_minus.py --rollback")
    return False


def rollback():
    files = sorted(BACKUP_DIR.glob("minus_*.json")) if BACKUP_DIR.exists() else []
    if not files:
        log("Резервных копий нет — откатывать нечего.")
        return
    b = json.loads(files[-1].read_text(encoding="utf-8"))
    cabs = load_tokens(b["cabinet"])
    token = cabs[b["cabinet"]]
    log(f"Откат {files[-1].name}: «{b['name']}» ({b['advert_id']}), товар {b['nm_id']} → "
        f"{b['old'] if b['old'] else 'пустой список'}")
    set_minus(token, b["advert_id"], b["nm_id"], b["old"])
    verify(token, b["advert_id"], b["nm_id"], b["old"])
    files[-1].rename(files[-1].with_suffix(".rolled_back"))


def main():
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — минус-фразы WB вручную")
    if "--rollback" in sys.argv:
        rollback()
        save_log("wb_minus_report.txt")
        return
    try:
        cid, nm = int(arg("--campaign")), int(arg("--nm"))
    except (TypeError, ValueError):
        print(__doc__)
        return
    add = [q.strip().lower() for q in (arg("--add") or "").split(";") if q.strip()]
    cab, token, a = find_campaign(load_tokens(), cid)
    if not a:
        log(f"  Кампания {cid} не найдена среди активных и на паузе ни в одном кабинете.")
        return
    st = a.get("settings") or {}
    pay = st.get("payment_type")
    nms = [s.get("nm_id") for s in a.get("nm_settings") or []]
    log(f"  {cab}: «{st.get('name')}» (ID {cid}) — {'за клики' if pay == 'cpc' else 'за показы'}")
    if nm not in nms:
        log(f"  Товара {nm} в этой кампании нет.")
        return
    cur = get_minus(token, cid, nm)
    if cur is None:
        if pay != "cpc":
            log("  WB не вернул текущий список минус-фраз — записывать вслепую нельзя (запись заменит весь список).")
            return
        log("  WB не вернул текущий список (у кампаний за клики минус-фраз в кабинете нет) — считаю его пустым.")
        cur = []
    log(f"  Сейчас минус-фраз: {len(cur)}" + (f" ({'; '.join(cur[:10])}{' …' if len(cur) > 10 else ''})" if cur else ""))
    new_q = [q for q in add if q not in cur]
    if not new_q:
        log("  Добавлять нечего." if add else "  Укажите фразы: --add \"фраза\".")
        save_log("wb_minus_report.txt")
        return
    for q in new_q:
        log(f"    + «{q}»")
    new = cur + new_q
    if len(new) > 1000:
        log("  ! Больше 1000 фраз — WB не примет.")
        return
    if "--apply" not in sys.argv:
        log("\n  Пробный прогон — ничего не изменено. Чтобы записать, добавьте --apply.")
        save_log("wb_minus_report.txt")
        return
    BACKUP_DIR.mkdir(exist_ok=True)
    path = BACKUP_DIR / f"minus_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps({"cabinet": cab, "advert_id": cid, "nm_id": nm, "name": st.get("name"),
                                "pay": pay, "old": cur, "new": new}, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"  Резервная копия: {path.name}")
    try:
        set_minus(token, cid, nm, new)
        log("  Записано.")
    except ApiError as e:
        log(f"  ✗ WB отказал: {e}")
        path.rename(path.with_suffix(".failed"))
        save_log("wb_minus_report.txt")
        return
    verify(token, cid, nm, new)
    save_log("wb_minus_report.txt")


if __name__ == "__main__":
    main()
