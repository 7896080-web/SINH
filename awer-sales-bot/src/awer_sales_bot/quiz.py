"""Квиз после урока недели и кнопка «Похвалить» у владельца — кнопки MAX (callback).

Кнопка несёт payload: `quiz|<id урока>|<номер вопроса>|<номер ответа>` или `praise|<seller_id>|<YYYY-MM-DD>`.
Payload приходит от клиента — проверяем всё: урок отправлялся этому продавцу, на вопрос ещё не отвечали,
хвалить может только владелец из max.owners. Ответы — в quiz_answers; на ставку не влияют.
"""
from __future__ import annotations

from datetime import date, datetime

from . import achievements, lessons
from .config import Settings
from .db import DB


def question(lesson: dict, qi: int) -> tuple[str, list[list[dict]]]:
    q = lesson["quiz"][qi]
    text = f"❓ Вопрос {qi + 1} из {len(lesson['quiz'])} к уроку «{lesson['title']}»\n\n{q['q']}"
    buttons = [[{"type": "callback", "text": opt[:128], "payload": f"quiz|{lesson['id']}|{qi}|{oi}"}]
               for oi, opt in enumerate(q["options"])]
    return text, buttons


def on_quiz(db: DB, cfg: Settings, sid: int, payload: str) -> tuple[str, list[tuple[str, list | None]]]:
    """Ответ на кнопку квиза → (короткое уведомление, [(сообщение, кнопки)...] продавцу)."""
    try:
        _, lid, qi, oi = payload.split("|")
        qi, oi = int(qi), int(oi)
    except ValueError:
        return "Кнопка устарела", []
    lesson = lessons.by_id(cfg, lid, db)
    if not lesson or lid not in lessons.sent_ids(db, sid) or not 0 <= qi < len(lesson["quiz"]):
        return "Кнопка устарела", []
    q = lesson["quiz"][qi]
    if not 0 <= oi < len(q["options"]):
        return "Кнопка устарела", []
    if db.one("SELECT 1 FROM quiz_answers WHERE seller_id=? AND lesson_id=? AND q=?", sid, lid, qi):
        return "Ответ уже принят", []
    ok = oi == q["correct"]
    with db.tx() as c:
        c.execute("INSERT INTO quiz_answers(seller_id,lesson_id,q,answer,correct,answered_at) VALUES(?,?,?,?,?,?)",
                  (sid, lid, qi, oi, int(ok), datetime.now().isoformat(timespec="seconds")))
    head = "✅ Верно!" if ok else f"Не совсем. Верный ответ: «{q['options'][q['correct']]}»."
    out: list[tuple[str, list | None]] = [(f"{head} {q['explain']}", None)]
    if qi + 1 < len(lesson["quiz"]):
        out.append(question(lesson, qi + 1))
    else:
        r = db.one("SELECT COUNT(*) n, SUM(correct) ok FROM quiz_answers WHERE seller_id=? AND lesson_id=?", sid, lid)
        out.append((f"Квиз пройден: {r['ok']} из {r['n']}. Спасибо! Следующий урок — через неделю.", None))
    return ("Верно!" if ok else "Ответ принят"), out


# ----------------------------------------------------------------------------- похвала от владельца
def praise_buttons(good: list[tuple[int, str, list[str]]], day: date, limit: int = 6) -> list[list[dict]]:
    return [[{"type": "callback", "text": f"👏 Похвалить: {name}"[:128], "payload": f"praise|{sid}|{day.isoformat()}"}]
            for sid, name, _ in good[:limit]]


def on_praise(db: DB, cfg: Settings, payload: str) -> tuple[int, str] | None:
    """Нажатие владельца → (seller_id, текст продавцу) или None, если хвалить не за что / кнопка подделана."""
    try:
        _, sid, d = payload.split("|")
        sid, day = int(sid), date.fromisoformat(d)
    except ValueError:
        return None
    why = next((w for s, _, w in achievements.praise(db, cfg, day) if s == sid), None)
    if not why:
        return None
    return sid, f"👏 Владелец отметил ваш результат за {day:%d.%m}: {'; '.join(why)}. Так держать!"
