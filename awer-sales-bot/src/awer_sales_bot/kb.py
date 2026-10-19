"""База знаний в боте «Обучение»: разделы и статьи на кнопках, поиск по словам, карточки новинок из 1С.

Статьи — content (kind = article), правятся в админке «Обучение». Кнопки (callback payload):
    k|home            — список разделов
    k|sec|<раздел>    — статьи раздела
    k|art|<id статьи> — текст статьи (просмотр пишется в kb_views — статистика в админке)
Любое сообщение текстом в боте «Обучение» (не команда и не ответ на опрос) — поиск по названиям и словам статей.
"""
from __future__ import annotations

from datetime import date, datetime

from . import content
from .config import Settings
from .db import DB


def _btn(text: str, payload: str) -> dict:
    return {"type": "callback", "text": text[:128], "payload": payload}


def home(db: DB, cfg: Settings) -> tuple[str, list]:
    have = {x["topic"] for x in content.items(db, cfg, "article")}
    rows = [[_btn(t, f"k|sec|{k}")] for k, t in content.ARTICLE_SECTIONS.items() if k in have]
    return ("📖 **База знаний.** Выберите раздел или просто напишите слово: «кашемир», «пуховик», «мембрана».",
            rows)


def section(db: DB, cfg: Settings, key: str) -> tuple[str, list]:
    arts = [x for x in content.items(db, cfg, "article") if x["topic"] == key]
    rows = [[_btn(x["title"], f"k|art|{x['id']}")] for x in arts] + [[_btn("← Разделы", "k|home")]]
    return f"📖 **{content.ARTICLE_SECTIONS.get(key, key)}**", rows


def article(db: DB, cfg: Settings, aid: str, sid: int | None) -> tuple[str, list]:
    x = content.get(db, cfg, aid)
    if not x or x["kind"] != "article" or not x["active"]:
        return "Статья не найдена — возможно, её убрали.", [[_btn("← Разделы", "k|home")]]
    if sid:
        with db.tx() as c:
            c.execute("INSERT INTO kb_views(seller_id, article_id, at) VALUES(?,?,?)",
                      (sid, aid, datetime.now().isoformat(timespec="seconds")))
    return (f"📖 **{x['title']}**\n\n{(x['body'] or '').strip()}",
            [[_btn(f"← {content.ARTICLE_SECTIONS.get(x['topic'], 'Раздел')}", f"k|sec|{x['topic']}")],
             [_btn("Все разделы", "k|home")]])


def search(db: DB, cfg: Settings, text: str, limit: int = 5) -> list[dict]:
    words = [w for w in "".join(ch.lower() if ch.isalnum() else " " for ch in text).split() if len(w) >= 3]
    scored = []
    for x in content.items(db, cfg, "article"):
        tags = [t.lower() for t in x["data"].get("tags") or []]
        title, body = x["title"].lower(), (x["body"] or "").lower()
        score = 0
        for w in words:
            stem = w[:max(4, len(w) - 2)]  # «пуховика» найдёт «пуховик»
            score += 3 * any(t.startswith(stem) for t in tags) + 2 * (stem in title) + (stem in body)
        if score:
            scored.append((score, x))
    return [x for _, x in sorted(scored, key=lambda s: -s[0])[:limit]]


def answer_search(db: DB, cfg: Settings, text: str) -> tuple[str, list]:
    found = search(db, cfg, text)
    if not found:
        msg, rows = home(db, cfg)
        return f"По запросу «{text[:60]}» ничего не нашёл. {msg.split('**', 2)[-1].strip()}", rows
    return f"Нашёл по запросу «{text[:60]}»:", [[_btn(x["title"], f"k|art|{x['id']}")] for x in found]


def on_callback(db: DB, cfg: Settings, payload: str, sid: int | None) -> tuple[str, list]:
    parts = payload.split("|")
    if len(parts) >= 3 and parts[1] == "sec":
        return section(db, cfg, parts[2])
    if len(parts) >= 3 and parts[1] == "art":
        return article(db, cfg, parts[2], sid)
    return home(db, cfg)


# ----------------------------------------------------------------------------- карточки новинок
def arrivals_card(db: DB, cfg: Settings, day: date, limit: int = 10,
                  store: str | None = None) -> tuple[str, list] | None:
    """Новинки за день из 1С: модель, бренд, цвет, размеры — и кнопки на статьи о таких вещах.
    store — только поступления этого магазина (продавцу — его магазин сегодня, аудит П3)."""
    f, a = ("", []) if store is None else (" AND store=?", [store])
    rows = db.q("SELECT item, category, brand, color, GROUP_CONCAT(DISTINCT size) sizes, SUM(qty) q FROM arrivals "
                f"WHERE day=?{f} GROUP BY item, category, brand, color ORDER BY q DESC", day.isoformat(), *a)
    if not rows:
        return None
    lines = []
    for r in rows[:limit]:
        extra = ", ".join(v for v in (r["brand"], r["color"]) if v)
        sizes = ", ".join(s for s in (r["sizes"] or "").split(",") if s and s.upper() not in ("U", "Б/Р"))
        lines.append(f"• {r['item']}" + (f" — {extra}" if extra else "") + (f" (размеры: {sizes})" if sizes else ""))
    more = f"\n…и ещё {len(rows) - limit}" if len(rows) > limit else ""
    arts, seen = [], set()
    for r in rows:
        for x in search(db, cfg, f"{r['category'] or ''} {r['item']}", limit=2):
            if x["id"] not in seen and len(arts) < 3:
                seen.add(x["id"])
                arts.append([_btn(f"📖 {x['title']}", f"k|art|{x['id']}")])
    return (f"🆕 **Новинки {day:%d.%m}**\n\n" + "\n".join(lines) + more +
            "\n\nНайдите их в зале и придумайте к каждой сочетание. Про ткань и утеплитель — на ярлыке и в базе знаний.",
            arts or None)
