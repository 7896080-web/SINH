"""Уроки недели и программа новичка — из content/lessons.yaml (правит владелец; методика и правила текста —
скилл .claude/skills/prodazhi-v-magazine).

Урок недели выбирается по теме цели недели (goals.METRICS): первый ещё не отправленный урок темы, потом —
из общих (general), когда всё пройдено — самый давний урок темы. Новичок (нет истории из ведомостей и не больше
`training.onboarding_shifts` смен) вместо урока недели получает уроки программы новичка — по одному к каждой
следующей смене. Какие уроки ушли, помнит таблица lessons_sent.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml

from .config import Settings
from .db import DB

_cache: dict[Path, tuple[float, dict]] = {}


def _path(cfg: Settings) -> Path:
    p = Path(cfg.training.lessons_path)
    if not p.is_absolute() and not p.exists():  # запуск не из папки проекта (тесты, служба с другим каталогом)
        alt = Path(__file__).resolve().parents[2] / p
        if alt.exists():
            return alt
    return p


def load(cfg: Settings, db: DB | None = None) -> dict:
    """{'lessons': [...], 'onboarding': [...]}.

    С базой — из таблицы content (стартовые тексты из файла + правки владельца в админке «Обучение», только
    включённые); без базы — прямо из файла (проверка файла в тестах)."""
    if db is not None:
        from . import content
        les = [dict(id=x["id"], topic=x["topic"], title=x["title"], text=x["body"] or "",
                    try_today=x["try_today"] or "", quiz=x["data"].get("quiz") or [])
               for x in content.items(db, cfg, "lesson")]
        onb = sorted((dict(shift=x["data"]["shift"], title=x["title"], text=x["body"] or "",
                           try_today=x["try_today"] or "") for x in content.items(db, cfg, "onboarding")),
                     key=lambda x: x["shift"])
        return {"lessons": les, "onboarding": onb}
    p = _path(cfg)
    if not p.exists():
        return {"lessons": [], "onboarding": []}
    mtime = p.stat().st_mtime
    hit = _cache.get(p)
    if hit and hit[0] == mtime:
        return hit[1]
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out = {"lessons": data.get("lessons") or [], "onboarding": sorted(data.get("onboarding") or [],
                                                                       key=lambda x: x["shift"])}
    _cache[p] = (mtime, out)
    return out


def by_id(cfg: Settings, lesson_id: str, db: DB | None = None) -> dict | None:
    return next((x for x in load(cfg, db)["lessons"] if x["id"] == lesson_id), None)


def sent_ids(db: DB, sid: int) -> set[str]:
    return {r["lesson_id"] for r in db.q("SELECT lesson_id FROM lessons_sent WHERE seller_id=?", sid)}


def mark_sent(db: DB, sid: int, lesson_id: str) -> None:
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO lessons_sent(seller_id,lesson_id,sent_at) VALUES(?,?,?)",
                  (sid, lesson_id, datetime.now().isoformat(timespec="seconds")))


def pick(db: DB, cfg: Settings, sid: int, topic: str) -> dict | None:
    lessons = load(cfg, db)["lessons"]
    sent = sent_ids(db, sid)
    for t in (topic, "general"):
        for x in lessons:
            if x["topic"] == t and x["id"] not in sent:
                return x
    ids = {x["id"] for x in lessons if x["topic"] == topic}
    for r in db.q("SELECT lesson_id FROM lessons_sent WHERE seller_id=? ORDER BY sent_at, lesson_id", sid):
        if r["lesson_id"] in ids:
            return by_id(cfg, r["lesson_id"], db)
    return None


def text(lesson: dict, header: str = "Урок недели") -> str:
    return (f"📚 **{header}: {lesson['title']}**\n\n{lesson['text'].strip()}\n\n"
            f"**Попробуйте сегодня:** {lesson['try_today'].strip()}")


# ----------------------------------------------------------------------------- программа новичка
def newcomer_shifts(db: DB, cfg: Settings, sid: int) -> int | None:
    """Сколько смен отработал новичок (0…N) или None, если продавец не новичок."""
    if db.one("SELECT 1 FROM monthly_history WHERE seller_id=?", sid):
        return None
    n = db.one("SELECT COUNT(DISTINCT day) n FROM sales_lines WHERE seller_id=?", sid)["n"]
    return n if n <= cfg.training.onboarding_shifts else None


def next_onboarding(db: DB, cfg: Settings, sid: int) -> dict | None:
    """Ближайший неотправленный урок программы к следующей смене (не дальше смены n+1)."""
    n = newcomer_shifts(db, cfg, sid)
    if n is None:
        return None
    sent = sent_ids(db, sid)
    for x in load(cfg, db)["onboarding"]:
        if x["shift"] <= n + 1 and f"onb-{x['shift']}" not in sent:
            return x
    return None


def onboarding_text(lesson: dict, total: int) -> str:
    return text(lesson, f"Программа новичка ({lesson['shift']} из {total})")  # в заголовке урока уже «Рабочий день N»
