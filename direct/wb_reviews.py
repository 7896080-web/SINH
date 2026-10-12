# -*- coding: utf-8 -*-
"""
Отзывы Wildberries — что не нравится покупателям: размер, цвет, качество. Только чтение, ничего не отвечает и не меняет.

Зачем: 30–50% отказов при получении у джемперов — обычно размер «не тот» или вещь вживую не такая, как на фото.
Отзывы показывают, что именно: WB сам отмечает «маломерит / большемерит», а в тексте видно про цвет и качество.

Что показывает (по каждому кабинету, за последние N дней, по умолчанию 180):
  1. По артикулам (цветам): отзывов, средняя оценка, доля плохих (1–3), маломерит / большемерит,
     сколько отзывов от тех, кто отказался или вернул, и темы жалоб из текста (цвет, размер, качество, ткань).
  2. По размерам: где чаще пишут «маломерит», где «большемерит» — подсказка для карточки и таблицы размеров.
  3. Последние плохие отзывы (1–3) с текстом — по 3 на артикул, для самых проблемных.
Имена покупателей не выводятся.

Запуск:  python wb_reviews.py                       (все товары, 180 дней)
         python wb_reviews.py 365                   (за год)
         python wb_reviews.py --models 27643,36446  (только эти модели — текст в артикуле продавца)
         python wb_reviews.py --cabinet "Яворская"
Нужна категория токена «Вопросы и отзывы». Работает 1–2 минуты; можно запускать одновременно с wb_bids.py.
Результат: вывод на экран + wb_reviews_report.txt, wb_reviews.csv (все отзывы периода)
"""
import datetime as dt
import sys
from collections import Counter, defaultdict

from wb_common import ApiError, arg_cabinet, call, cut, load_tokens, log, pace, save_log, write_csv

# ---------------- НАСТРОЙКИ ----------------
DEFAULT_DAYS = 180       # отзывов мало — смотрим полгода
MIN_REVIEWS = 5          # артикул / размер с меньшим числом отзывов в таблицы не выводить
TOP = 20                 # сколько артикулов в таблице
BAD_PER_ART = 3          # сколько плохих отзывов показать на артикул
BAD_ARTS = 6             # для скольких самых проблемных артикулов показывать тексты
# Темы жалоб: слово в тексте (в нижнем регистре) → тема. Ищется в тексте, «недостатках» и «достоинствах» не ищется.
THEMES = {
    "цвет": ["цвет", "оттен", "на фото", "на фотограф", "не как на", "не такой как", "темнее", "светлее", "ярче", "бледн"],
    "размер": ["маломер", "большемер", "мал ", "мала", "мало ", "велик", "широк", "узк", "длинн", "коротк", "размер",
               "в плечах", "рукав"],
    "качество": ["катышк", "тонк", "колюч", "кусач", "шов", "швы", "нитк", "брак", "дыр", "затяжк", "качеств",
                 "линяет", "полинял", "сел после", "села после", "растянул"],
    "ткань": ["синтет", "состав", "акрил", "полиэстер", "шерст", "хлоп", "ткань"],
}
# -------------------------------------------

FB = "https://feedbacks-api.wildberries.ru"
STATUS = {"buyout": "выкупил", "rejected": "отказался", "returned": "вернул", "notSpecified": ""}


def days():
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a.isdigit() and not (i > 0 and args[i - 1] in ("--models", "--cabinet")):
            return int(a)
    return DEFAULT_DAYS


def models():
    if "--models" in sys.argv:
        i = sys.argv.index("--models")
        if i + 1 < len(sys.argv):
            return [m.strip().lower() for m in sys.argv[i + 1].split(",") if m.strip()]
    return []


DAYS = days()


def fetch(token, d1):
    """Все отзывы с даты d1: обработанные и необработанные."""
    res, start = [], int(dt.datetime.combine(d1, dt.time()).timestamp())
    for answered in ("true", "false"):
        skip = 0
        while True:
            pace(token, "feedbacks", 0.4)
            data = call("GET", FB + "/api/v1/feedbacks", token, what="Отзывы",
                        params={"isAnswered": answered, "take": 5000, "skip": skip, "order": "dateDesc",
                                "dateFrom": start}) or {}
            part = (data.get("data") or {}).get("feedbacks") or []
            res += part
            if len(part) < 5000 or skip + 5000 > 195000:
                break
            skip += 5000
    seen, uniq = set(), []
    for f in res:
        if f.get("id") not in seen:
            seen.add(f.get("id"))
            uniq.append(f)
    return uniq


def themes(f):
    t = f" {(f.get('text') or '')} {(f.get('cons') or '')} ".lower()
    return [k for k, words in THEMES.items() if any(w in t for w in words)]


def run_cabinet(cab, token, mods):
    log(f"\n══════════ {cab} ══════════")
    d1 = dt.date.today() - dt.timedelta(days=DAYS)
    print("  отзывы...")
    fbs = fetch(token, d1)
    rows = []
    for f in fbs:
        pd = f.get("productDetails") or {}
        art = str(pd.get("supplierArticle") or "")
        if mods and not any(m in art.lower() for m in mods):
            continue
        rows.append({"f": f, "art": art or str(pd.get("nmId")), "nm": pd.get("nmId"), "size": str(pd.get("size") or "?"),
                     "val": f.get("productValuation") or 0, "match": (f.get("matchingSize") or "").strip().lower(),
                     "status": f.get("orderStatus") or "", "themes": themes(f)})
    log(f"  Период: с {d1:%d.%m.%Y} ({DAYS} дн.)" + (f", модели: {', '.join(mods)}" if mods else ", все товары")
        + f". Отзывов: {len(rows)}.")
    if not rows:
        return []
    avg = sum(r["val"] for r in rows) / len(rows)
    bad = [r for r in rows if r["val"] and r["val"] <= 3]
    log(f"  Средняя оценка {avg:.2f}, плохих (1–3) {len(bad)} ({len(bad) / len(rows):.0%}).")
    ms = Counter(r["match"] for r in rows if r["match"])
    sized = sum(ms.values())
    if sized:
        log(f"  Размер по отметкам покупателей: в размер {ms.get('ок', 0) + ms.get('ok', 0)}, "
            f"маломерит {ms.get('smaller', 0)} ({ms.get('smaller', 0) / sized:.0%}), "
            f"большемерит {ms.get('bigger', 0)} ({ms.get('bigger', 0) / sized:.0%}) — из {sized} отметивших.")
    th = Counter(t for r in bad for t in r["themes"])
    if th:
        log("  Темы плохих отзывов: " + ", ".join(f"{k} — {n}" for k, n in th.most_common()))
    tags = Counter(t for r in rows for t in (r["f"].get("bables") or []))
    if tags:
        log("  Метки покупателей (WB): " + ", ".join(f"«{k}» {n}" for k, n in tags.most_common(8)))

    # 1. по артикулам
    by = defaultdict(list)
    for r in rows:
        by[r["art"]].append(r)
    arts = [(a, rs) for a, rs in by.items() if len(rs) >= MIN_REVIEWS]
    score = lambda rs: sum(1 for r in rs if r["val"] <= 3) / len(rs)
    arts.sort(key=lambda x: -score(x[1]))
    log(f"\n  1. Артикулы (где отзывов ≥ {MIN_REVIEWS}; сначала с наибольшей долей плохих):")
    log(f"    {'артикул продавца':26}{'отз.':>5}{'ср.':>5}{'плох.':>6}{'малом.':>7}{'больш.':>7}{'отказ/возвр':>12}"
        f"  {'темы плохих отзывов'}")
    for a, rs in arts[:TOP]:
        m = Counter(r["match"] for r in rs if r["match"])
        mt = sum(m.values()) or 1
        rej = sum(1 for r in rs if r["status"] in ("rejected", "returned"))
        t = Counter(x for r in rs if r["val"] <= 3 for x in r["themes"])
        log(f"    {cut(a, 26):26}{len(rs):>5}{sum(r['val'] for r in rs) / len(rs):>5.1f}{score(rs):>6.0%}"
            f"{m.get('smaller', 0) / mt:>7.0%}{m.get('bigger', 0) / mt:>7.0%}{rej:>12}  "
            + ", ".join(f"{k} {n}" for k, n in t.most_common(3)))
    small = [a for a, rs in by.items() if len(rs) < MIN_REVIEWS]
    if small:
        log(f"    (ещё {len(small)} артикулов с отзывами меньше {MIN_REVIEWS} — в таблице не показаны)")

    # 2. по размерам
    bys = defaultdict(Counter)
    for r in rows:
        if r["match"]:
            bys[r["size"]][r["match"]] += 1
    sizes = [(s, c) for s, c in bys.items() if sum(c.values()) >= MIN_REVIEWS]
    if sizes:
        log(f"\n  2. Размеры — что отмечают покупатели (где отметок ≥ {MIN_REVIEWS}):")
        log(f"    {'размер':10}{'отметок':>9}{'в размер':>10}{'маломерит':>11}{'большемерит':>13}  вывод")
        for s, c in sorted(sizes, key=lambda x: x[0]):
            n = sum(c.values())
            sm, bg = c.get("smaller", 0) / n, c.get("bigger", 0) / n
            hint = ("маломерит — советовать размер больше" if sm >= 0.3 and sm > bg else
                    "большемерит — советовать размер меньше" if bg >= 0.3 and bg > sm else "")
            log(f"    {s:10}{n:>9}{(c.get('ок', 0) + c.get('ok', 0)) / n:>10.0%}{sm:>11.0%}{bg:>13.0%}  {hint}")
        log("    30% и больше одинаковых отметок — повод добавить подсказку в карточку и проверить таблицу размеров.")

    # 3. тексты плохих отзывов
    worst = [a for a, rs in arts if any(r["val"] <= 3 for r in rs)][:BAD_ARTS]
    if worst:
        log(f"\n  3. Последние плохие отзывы (1–3) — по {BAD_PER_ART} на артикул:")
        for a in worst:
            log(f"    ▶ {a}")
            rs = sorted((r for r in by[a] if r["val"] <= 3), key=lambda r: str(r["f"].get("createdDate")), reverse=True)
            for r in rs[:BAD_PER_ART]:
                f = r["f"]
                txt = " / ".join(x for x in (f.get("text"), f.get("cons") and "минусы: " + f["cons"]) if x)
                extra = ", ".join(x for x in (f"размер {r['size']}",
                                              {"smaller": "маломерит", "bigger": "большемерит"}.get(r["match"], ""),
                                              STATUS.get(r["status"], "")) if x)
                log(f"      {str(f.get('createdDate') or '')[:10]} ★{r['val']} ({extra}): {cut(txt.replace(chr(10), ' '), 220) or '(без текста)'}")

    out = []
    for r in rows:
        f = r["f"]
        out.append({"кабинет": cab, "дата": str(f.get("createdDate") or "")[:10], "артикул WB": r["nm"],
                    "артикул продавца": r["art"], "цвет": f.get("color") or "", "размер": r["size"], "оценка": r["val"],
                    "размер по отзыву": {"smaller": "маломерит", "bigger": "большемерит", "ок": "в размер",
                                         "ok": "в размер"}.get(r["match"], ""),
                    "заказ": STATUS.get(r["status"], r["status"]), "темы": ", ".join(r["themes"]),
                    "текст": (f.get("text") or "").replace("\n", " "), "достоинства": (f.get("pros") or "").replace("\n", " "),
                    "недостатки": (f.get("cons") or "").replace("\n", " ")})
    return out


def main():
    mods = models()
    cabs = load_tokens(arg_cabinet())
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — отзывы WB за {DAYS} дн. (только отчёт, ничего не отвечает и не меняет)")
    allrows = []
    for cab, token in cabs.items():
        try:
            allrows += run_cabinet(cab, token, mods)
        except ApiError as e:
            log(f"  ✗ {cab}: {e}")
    write_csv("wb_reviews.csv", allrows, header=list(allrows[0]) if allrows else None)
    save_log("wb_reviews_report.txt")
    print("\nСохранено: wb_reviews_report.txt, wb_reviews.csv")


if __name__ == "__main__":
    main()
