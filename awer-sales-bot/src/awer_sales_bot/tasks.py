"""Задания в бот «Обучение»: тесты, опросы, чек-листы (контент — content.py, страница админки «Задания»).

Рассылка (`campaigns`): что (тест/опрос/чек-лист из «Обучения»), кому (все, магазин сегодня, смена, новички),
когда (один раз, каждый день, раз в неделю) и в какие часы — «тестовые часы» (`training.test_hours` или свои
у рассылки), чтобы не отвлекать в торговое время. Получают только те, у кого сегодня рабочий день по графику
(нет графика на месяц — все), и только один раз за день по каждой рассылке (`deliveries`).

Ответы — кнопками (callback payload ниже), в опросе можно «Свой ответ» текстом. Рейтинг по тестам и заданиям —
информативный, на ставку и ЗП не влияет (решение владельца 18.10.2026).

payload:  t|<рассылка>|<день>|<вопрос>|<ответ>   — тест
          s|<рассылка>|<день>|<вопрос>|<ответ>   — опрос (ответ -1 — «свой ответ» текстом)
          c|<рассылка>|<день>|<пункт>|x          — чек-лист: отметить пункт; пункт «done» — «Готово»
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from . import content, crews
from . import metrics as M
from .config import Settings
from .db import DB

AUDIENCES = {"all": "все продавцы", "newcomers": "новички (первые рабочие дни)"}
REPEATS = {"once": "один раз", "daily": "каждый день", "weekly": "раз в неделю"}


def hours(cfg: Settings, c=None) -> tuple[time, time]:
    a, b = ((c["hour_from"], c["hour_to"]) if c is not None and c["hour_from"] and c["hour_to"]
            else cfg.training.test_hours.split("-"))
    return datetime.strptime(a.strip(), "%H:%M").time(), datetime.strptime(b.strip(), "%H:%M").time()


def quiet_hours(db: DB, day: date, n: int = 2) -> list[int]:
    """Часы с наименьшими продажами по чекам 1С за 8 недель (подсказка, когда ставить тестовые часы)."""
    h = M.hours(db, day - timedelta(days=56), day)
    return sorted(sorted(h, key=lambda k: h[k])[:n]) if len(h) >= 6 else []


def audience_label(db: DB, a: str) -> str:
    if a.startswith("store:"):
        return f"магазин «{a[6:]}» (кто сегодня там)"
    if a.startswith("crew:"):
        r = db.one("SELECT name FROM crews WHERE id=?", int(a[5:]))
        return f"смена «{r['name'] if r else '?'}»"
    return AUDIENCES.get(a, a)


def recipients(db: DB, cfg: Settings, c, today: date) -> list:
    from .lessons import newcomer_shifts
    out = []
    for s in db.sellers():
        a = c["audience"]
        if a.startswith("store:") and crews.store_on(db, cfg, s["id"], today) != a[6:]:
            continue
        if a.startswith("crew:") and s["crew_id"] != int(a[5:]):
            continue
        if a == "newcomers" and newcomer_shifts(db, cfg, s["id"]) is None:
            continue
        if c["only_working"] and db.scheduled_after(s["id"], today.strftime("%Y-%m"), today - timedelta(days=1)) \
                is not None and not db.one("SELECT 1 FROM schedule WHERE seller_id=? AND day=?", s["id"],
                                           today.isoformat()):
            continue  # график на месяц есть, а сегодня не рабочий день
        out.append(s)
    return out


def due(c, today: date) -> bool:
    start = date.fromisoformat(c["start_day"])
    if not c["active"] or today < start:
        return False
    if c["repeat"] == "once":
        return True  # дальше «один раз» ограничивает deliveries (одна доставка на продавца за всё время)
    if c["repeat"] == "weekly":
        return today.weekday() == (c["weekday"] if c["weekday"] is not None else start.weekday())
    return True


# ----------------------------------------------------------------------------- тексты и кнопки
def _intro(x: dict) -> str:
    kinds = {"test": "📝 Тест", "survey": "💬 Опрос", "checklist": "✅ Чек-лист"}
    head = f"{kinds[x['kind']]}: **{x['title']}**"
    return head + (f"\n\n{x['body']}" if x.get("body") else "")


def question(x: dict, cid: int, day: str, qi: int) -> tuple[str, list]:
    qs = x["data"].get("questions") or []
    q = qs[qi]
    p = "t" if x["kind"] == "test" else "s"
    buttons = [[{"type": "callback", "text": o[:128], "payload": f"{p}|{cid}|{day}|{qi}|{oi}"}]
               for oi, o in enumerate(q.get("options") or [])]
    if x["kind"] == "survey" and q.get("free_text"):
        buttons.append([{"type": "callback", "text": "✍️ Свой ответ", "payload": f"s|{cid}|{day}|{qi}|-1"}])
    return f"Вопрос {qi + 1} из {len(qs)}\n\n{q['q']}", buttons


def checklist(x: dict, cid: int, day: str, done: set[int]) -> tuple[str, list]:
    items = x["data"].get("items") or []
    buttons = [[{"type": "callback", "text": f"{'✅' if i in done else '⬜'} {it}"[:128],
                 "payload": f"c|{cid}|{day}|{i}|x"}] for i, it in enumerate(items)]
    buttons.append([{"type": "callback", "text": "Готово", "payload": f"c|{cid}|{day}|done|x"}])
    return f"{_intro(x)}\n\nОтмечайте пункты по мере выполнения: {len(done)} из {len(items)}.", buttons


def first_messages(x: dict, cid: int, day: str) -> list[tuple[str, list | None]]:
    if x["kind"] == "checklist":
        return [checklist(x, cid, day, set())]
    return [(_intro(x), None), question(x, cid, day, 0)]


# ----------------------------------------------------------------------------- рассылка
def run_due(db: DB, cfg: Settings, sender, now: datetime | None = None) -> int:
    """Разослать задания, у которых сейчас тестовые часы. Повторный вызов в тот же день ничего не дублирует."""
    now = now or datetime.now()
    today, n = now.date(), 0
    for c in db.q("SELECT * FROM campaigns WHERE active=1"):
        a, b = hours(cfg, c)
        if not due(c, today) or not a <= now.time() <= b:
            continue
        x = content.get(db, cfg, c["content_id"])
        if not x or not x["active"]:
            continue
        day = today.isoformat()
        for s in recipients(db, cfg, c, today):
            if db.one("SELECT 1 FROM deliveries WHERE campaign_id=? AND seller_id=? AND (day=? OR ?='once')",
                      c["id"], s["id"], day, c["repeat"]):
                continue
            msgs = first_messages(x, c["id"], day)
            status = None
            for i, (text, buttons) in enumerate(msgs):
                status = sender.to_seller(f"{day}:{c['id']}:{i}", "task", s["id"], text, buttons)
            if status in ("ok", "dry"):
                with db.tx() as t:
                    t.execute("INSERT OR IGNORE INTO deliveries(campaign_id,seller_id,day,sent_at) VALUES(?,?,?,?)",
                              (c["id"], s["id"], day, now.isoformat(timespec="seconds")))
                n += 1
    return n


# ----------------------------------------------------------------------------- ответы
def _delivery(db: DB, cid: int, sid: int, day: str):
    return db.one("SELECT * FROM deliveries WHERE campaign_id=? AND seller_id=? AND day=?", cid, sid, day)


def _finish(db: DB, cid: int, sid: int, day: str, score: float) -> None:
    with db.tx() as c:
        c.execute("UPDATE deliveries SET done_at=?, score=? WHERE campaign_id=? AND seller_id=? AND day=?",
                  (datetime.now().isoformat(timespec="seconds"), score, cid, sid, day))


def _save(db: DB, cid: int, sid: int, day: str, q: int, answer: int | None, text: str | None,
          correct: int | None) -> bool:
    """False — на этот вопрос уже ответили."""
    if db.one("SELECT 1 FROM answers WHERE campaign_id=? AND seller_id=? AND day=? AND q=?", cid, sid, day, q):
        return False
    with db.tx() as c:
        c.execute("INSERT INTO answers(campaign_id,seller_id,day,q,answer,text,correct,at) VALUES(?,?,?,?,?,?,?,?)",
                  (cid, sid, day, q, answer, text, correct, datetime.now().isoformat(timespec="seconds")))
    return True


def _next(db: DB, cfg: Settings, x: dict, cid: int, sid: int, day: str, qi: int) -> list[tuple[str, list | None]]:
    qs = x["data"].get("questions") or []
    if qi + 1 < len(qs):
        return [question(x, cid, day, qi + 1)]
    if x["kind"] == "test":
        r = db.one("SELECT COUNT(*) n, COALESCE(SUM(correct),0) ok FROM answers WHERE campaign_id=? AND seller_id=? "
                   "AND day=?", cid, sid, day)
        _finish(db, cid, sid, day, r["ok"] / r["n"] if r["n"] else 0)
        return [(f"Тест пройден: {r['ok']} из {r['n']}. Спасибо!", None)]
    _finish(db, cid, sid, day, 1.0)
    return [("Спасибо за ответы! Владелец их увидит.", None)]


def on_callback(db: DB, cfg: Settings, sid: int, uid: int, payload: str) -> tuple[str, list, dict | None]:
    """Нажатие кнопки задания → (уведомление, [(сообщение, кнопки)], новое сообщение вместо старого или None)."""
    try:
        kind, cid, day, a, b = payload.split("|")
        cid = int(cid)
    except ValueError:
        return "Кнопка устарела", [], None
    camp = db.one("SELECT * FROM campaigns WHERE id=?", cid)
    x = content.get(db, cfg, camp["content_id"]) if camp else None
    if not x or not _delivery(db, cid, sid, day):
        return "Кнопка устарела", [], None
    if kind == "c":
        items = x["data"].get("items") or []
        if _delivery(db, cid, sid, day)["done_at"]:
            return "Чек-лист уже закрыт", [], None
        if a == "done":
            n = db.one("SELECT COUNT(*) n FROM answers WHERE campaign_id=? AND seller_id=? AND day=?", cid, sid, day)["n"]
            _finish(db, cid, sid, day, n / len(items) if items else 1.0)
            return "Готово", [(f"Чек-лист «{x['title']}»: отмечено {n} из {len(items)}. Спасибо!", None)], None
        i = int(a)
        if not 0 <= i < len(items):
            return "Кнопка устарела", [], None
        _save(db, cid, sid, day, i, 1, None, None)
        done = {r["q"] for r in db.q("SELECT q FROM answers WHERE campaign_id=? AND seller_id=? AND day=?", cid, sid, day)}
        text, buttons = checklist(x, cid, day, done)
        return "Отмечено", [], {"text": text, "attachments": [{"type": "inline_keyboard",
                                                               "payload": {"buttons": buttons}}]}
    qi, oi = int(a), int(b)
    qs = x["data"].get("questions") or []
    if not 0 <= qi < len(qs):
        return "Кнопка устарела", [], None
    q = qs[qi]
    if kind == "s" and oi == -1:
        if db.one("SELECT 1 FROM answers WHERE campaign_id=? AND seller_id=? AND day=? AND q=?", cid, sid, day, qi):
            return "Ответ уже принят", [], None
        db.set_kv(f"await:{uid}", f"{cid}|{day}|{qi}")
        return "Напишите ответ", [("Напишите ответ одним сообщением.", None)], None
    if not 0 <= oi < len(q.get("options") or []):
        return "Кнопка устарела", [], None
    if kind == "t":
        ok = int(oi == q["correct"])
        if not _save(db, cid, sid, day, qi, oi, None, ok):
            return "Ответ уже принят", [], None
        head = "✅ Верно!" if ok else f"Не совсем. Верный ответ: «{q['options'][q['correct']]}»."
        return ("Верно!" if ok else "Ответ принят"), \
            [(f"{head} {q.get('explain', '')}".strip(), None)] + _next(db, cfg, x, cid, sid, day, qi), None
    if not _save(db, cid, sid, day, qi, oi, q["options"][oi], None):
        return "Ответ уже принят", [], None
    return "Ответ принят", _next(db, cfg, x, cid, sid, day, qi), None


def on_text(db: DB, cfg: Settings, sid: int, uid: int, text: str) -> list[tuple[str, list | None]] | None:
    """Свой ответ в опросе (после кнопки «Свой ответ»); None — ответа не ждали."""
    st = db.get_kv(f"await:{uid}")
    if not st:
        return None
    db.set_kv(f"await:{uid}", "")
    cid, day, qi = st.split("|")
    cid, qi = int(cid), int(qi)
    camp = db.one("SELECT * FROM campaigns WHERE id=?", cid)
    x = content.get(db, cfg, camp["content_id"]) if camp else None
    if not x or not _save(db, cid, sid, day, qi, -1, text.strip()[:2000], None):
        return [("Ответ уже принят.", None)]
    return [("Записал, спасибо!", None)] + _next(db, cfg, x, cid, sid, day, qi)


# ----------------------------------------------------------------------------- рейтинг
def rating(db: DB, month: str) -> list[dict]:
    """Рейтинг за месяц — информативный: тесты (средний % верных) и задания (выполнено из полученных)."""
    out = []
    for s in db.sellers(active_only=False):
        r = db.one("SELECT COUNT(*) sent, SUM(done_at IS NOT NULL) done, "
                   "AVG(CASE WHEN c.content_id IN (SELECT id FROM content WHERE kind='test') AND done_at IS NOT NULL "
                   "THEN score END) test FROM deliveries d JOIN campaigns c ON c.id=d.campaign_id "
                   "WHERE d.seller_id=? AND substr(d.day,1,7)=?", s["id"], month)
        q = db.one("SELECT COUNT(*) n, COALESCE(SUM(correct),0) ok FROM quiz_answers WHERE seller_id=? "
                   "AND substr(answered_at,1,7)=?", s["id"], month)
        if not r["sent"] and not q["n"]:
            continue
        parts = [v for v in (r["test"], (q["ok"] / q["n"]) if q["n"] else None,
                             (r["done"] / r["sent"]) if r["sent"] else None) if v is not None]
        out.append(dict(seller_id=s["id"], name=s["name"], sent=r["sent"], done=r["done"] or 0, test=r["test"],
                        quiz=(q["ok"] / q["n"]) if q["n"] else None, score=sum(parts) / len(parts)))
    return sorted(out, key=lambda x: -x["score"])
