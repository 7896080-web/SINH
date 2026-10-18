"""Учебный контент в базе: уроки, программа новичка, статьи базы знаний, тесты, опросы, чек-листы.

Файлы content/*.yaml — стартовые тексты, которые пишет Claude по скиллу prodazhi-v-magazine. При запуске и по кнопке
в админке они переносятся в таблицу `content`: новые — добавляются, уже взятые из файла — обновляются, а то, что
владелец поправил в админке (origin = admin), файлом больше не перезаписывается. Выключенное (active = 0) не
рассылается, но остаётся в статистике.

Вопросы в админке пишутся простым текстом (parse_questions / format_questions):
    ? Что предложить к брюкам?
    + Ремень в тон обуви          ← верный ответ (в тесте и квизе)
    - Футболку с принтом
    = Почему верно — одна-две фразы
В опросе верного ответа нет: все варианты через «-», строка «~ свой ответ» разрешает ответить своими словами.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import yaml

from .config import Settings
from .db import DB

KINDS = {
    "lesson": "Уроки недели",
    "onboarding": "Программа новичка",
    "article": "База знаний",
    "test": "Тесты",
    "survey": "Опросы",
    "checklist": "Чек-листы",
}
LESSON_TOPICS = {"items": "вещи в чеке", "avg_price": "цена вещи", "discount": "скидки", "returns": "возвраты",
                 "general": "общее"}
ARTICLE_SECTIONS = {"tkani": "Ткани", "uteplitel": "Утеплители и мембраны", "uhod": "Уход за вещами",
                    "svoystva": "Свойства и посадка", "prodazha": "Как рассказывать", "brendy": "Бренды"}
FILES = ("lessons.yaml", "knowledge.yaml", "tasks.yaml")

_synced: dict[str, float] = {}


def _dir(cfg: Settings) -> Path:
    p = Path(cfg.training.lessons_path).parent
    if not p.is_absolute() and not p.exists():
        alt = Path(__file__).resolve().parents[2] / p
        if alt.exists():
            return alt
    return p


def _rows_from_files(cfg: Settings) -> list[dict]:
    out: list[dict] = []
    lp = Path(cfg.training.lessons_path)
    for name in FILES:
        p = lp if name == "lessons.yaml" else _dir(cfg) / name
        if name == "lessons.yaml" and not p.is_absolute() and not p.exists():
            p = _dir(cfg) / lp.name
        if not p.exists():
            continue
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for x in d.get("lessons") or []:
            out.append(dict(id=x["id"], kind="lesson", topic=x.get("topic"), title=x["title"], body=x.get("text"),
                            try_today=x.get("try_today"), data={"quiz": x.get("quiz") or []}))
        for x in d.get("onboarding") or []:
            out.append(dict(id=f"onb-{x['shift']}", kind="onboarding", topic=None, title=x["title"],
                            body=x.get("text"), try_today=x.get("try_today"), data={"shift": x["shift"]}))
        for x in d.get("articles") or []:
            out.append(dict(id=x["id"], kind="article", topic=x.get("section"), title=x["title"], body=x.get("text"),
                            try_today=None, data={"tags": x.get("tags") or []}))
        for kind, key in (("test", "tests"), ("survey", "surveys"), ("checklist", "checklists")):
            for x in d.get(key) or []:
                data = {k: v for k, v in x.items() if k not in ("id", "title", "text")}
                out.append(dict(id=x["id"], kind=kind, topic=None, title=x["title"], body=x.get("text"),
                                try_today=None, data=data))
    return out


def sync_from_files(db: DB, cfg: Settings, force: bool = False) -> int:
    """Переносит content/*.yaml в базу; правки владельца не трогает. Возвращает число добавленных/обновлённых."""
    files = [_dir(cfg) / n for n in FILES] + [Path(cfg.training.lessons_path)]
    stamp = sum(f.stat().st_mtime for f in files if f.exists())
    key = str(db.path)
    if not force and _synced.get(key) == stamp:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    n = 0
    with db.tx() as c:
        for r in _rows_from_files(cfg):
            old = c.execute("SELECT origin, title, body, try_today, data, topic FROM content WHERE id=?",
                            (r["id"],)).fetchone()
            data = json.dumps(r["data"], ensure_ascii=False)
            if old is None:
                c.execute("INSERT INTO content(id,kind,topic,title,body,try_today,data,active,origin,updated_at) "
                          "VALUES(?,?,?,?,?,?,?,1,'yaml',?)",
                          (r["id"], r["kind"], r["topic"], r["title"], r["body"], r["try_today"], data, now))
                n += 1
            elif old["origin"] == "yaml" and (old["title"], old["body"], old["try_today"], old["data"], old["topic"]) != \
                    (r["title"], r["body"], r["try_today"], data, r["topic"]):
                c.execute("UPDATE content SET kind=?, topic=?, title=?, body=?, try_today=?, data=?, updated_at=? "
                          "WHERE id=?", (r["kind"], r["topic"], r["title"], r["body"], r["try_today"], data, now,
                                         r["id"]))
                n += 1
    _synced[key] = stamp
    return n


def items(db: DB, cfg: Settings, kind: str, active_only: bool = True) -> list[dict]:
    sync_from_files(db, cfg)
    sql = "SELECT * FROM content WHERE kind=?" + (" AND active=1" if active_only else "") + " ORDER BY rowid"
    return [_dict(r) for r in db.q(sql, kind)]


def get(db: DB, cfg: Settings, cid: str) -> dict | None:
    sync_from_files(db, cfg)
    r = db.one("SELECT * FROM content WHERE id=?", cid)
    return _dict(r) if r else None


def _dict(r) -> dict:
    d = dict(r)
    d["data"] = json.loads(d["data"] or "{}")
    return d


def save(db: DB, cid: str, kind: str, title: str, body: str | None, try_today: str | None, topic: str | None,
         data: dict, active: bool) -> None:
    """Правка из админки — помечается origin = admin и файлом больше не перезаписывается."""
    with db.tx() as c:
        c.execute("INSERT INTO content(id,kind,topic,title,body,try_today,data,active,origin,updated_at) "
                  "VALUES(?,?,?,?,?,?,?,?,'admin',?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, "
                  "topic=excluded.topic, title=excluded.title, body=excluded.body, try_today=excluded.try_today, "
                  "data=excluded.data, active=excluded.active, origin='admin', updated_at=excluded.updated_at",
                  (cid, kind, topic, title, body, try_today, json.dumps(data, ensure_ascii=False), int(active),
                   datetime.now().isoformat(timespec="seconds")))


def new_id(db: DB, kind: str) -> str:
    n = db.one("SELECT COUNT(*) n FROM content WHERE kind=?", kind)["n"] + 1
    while db.one("SELECT 1 FROM content WHERE id=?", f"{kind}-{n}"):
        n += 1
    return f"{kind}-{n}"


# ----------------------------------------------------------------------------- вопросы простым текстом
def parse_questions(text: str, survey: bool = False) -> list[dict]:
    """Текст из админки → [{q, options, correct, explain, free_text}]. ValueError — с понятным сообщением."""
    out: list[dict] = []
    cur: dict | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        mark, rest = line[0], line[1:].strip()
        if mark == "?":
            cur = {"q": rest, "options": [], "correct": None, "explain": ""}
            if survey:
                cur["free_text"] = False
            out.append(cur)
        elif cur is None:
            raise ValueError(f"Вопрос должен начинаться со строки «? …»: {line}")
        elif mark in "+-":
            if mark == "+":
                if survey:
                    raise ValueError("В опросе нет верного ответа — варианты через «-»")
                cur["correct"] = len(cur["options"])
            cur["options"].append(rest)
        elif mark == "=":
            cur["explain"] = rest
        elif mark == "~" and survey:
            cur["free_text"] = True
        else:
            raise ValueError(f"Непонятная строка: {line} (нужно ? + - = {'~' if survey else ''})")
    for q in out:
        if not q["options"] and not q.get("free_text"):
            raise ValueError(f"У вопроса «{q['q']}» нет вариантов")
        if any(len(o) > 128 for o in q["options"]):
            raise ValueError(f"Вариант длиннее 128 символов в вопросе «{q['q']}» — кнопка не поместится")
        if not survey and q["correct"] is None:
            raise ValueError(f"У вопроса «{q['q']}» не отмечен верный ответ «+»")
        if survey:
            q.pop("correct")
            q.pop("explain")
    return out


def format_questions(questions: list[dict], survey: bool = False) -> str:
    lines = []
    for q in questions:
        lines.append(f"? {q['q']}")
        for i, o in enumerate(q.get("options") or []):
            lines.append(f"{'+' if not survey and i == q.get('correct') else '-'} {o}")
        if survey and q.get("free_text"):
            lines.append("~ свой ответ")
        if q.get("explain"):
            lines.append(f"= {q['explain']}")
        lines.append("")
    return "\n".join(lines).strip()
