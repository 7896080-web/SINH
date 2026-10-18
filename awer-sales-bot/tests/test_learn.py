"""Обучение в базе: стартовые файлы, правки владельца в админке, вопросы простым текстом."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from awer_sales_bot import content, lessons
from awer_sales_bot.web import create_app
from test_flow import env  # noqa: F401
from test_motivation import LESSONS_YAML

AUTH = ("admin", "pw")
QS = """? Что предложить к брюкам?
- Футболку с принтом
+ Ремень в тон обуви
= Ремень завершает образ

? Сколько дополнений за раз?
+ Одно-два
- Все сразу"""


def test_questions_text_roundtrip_and_errors():
    qs = content.parse_questions(QS)
    assert qs[0] == {"q": "Что предложить к брюкам?", "options": ["Футболку с принтом", "Ремень в тон обуви"],
                     "correct": 1, "explain": "Ремень завершает образ"}
    assert content.parse_questions(content.format_questions(qs)) == qs
    sv = content.parse_questions("? Что мешало продавать?\n- Не было размеров\n- Мало покупателей\n~ свой ответ",
                                 survey=True)
    assert sv == [{"q": "Что мешало продавать?", "options": ["Не было размеров", "Мало покупателей"], "free_text": True}]
    with pytest.raises(ValueError, match="не отмечен верный ответ"):
        content.parse_questions("? Вопрос\n- а\n- б")
    with pytest.raises(ValueError, match="должен начинаться"):
        content.parse_questions("- вариант без вопроса")
    with pytest.raises(ValueError, match="нет верного"):
        content.parse_questions("? В опросе\n+ а", survey=True)


def test_files_seed_db_admin_edit_wins(env, tmp_path):  # noqa: F811
    cfg, db = env
    f = tmp_path / "lessons.yaml"
    f.write_text(LESSONS_YAML, encoding="utf-8")
    cfg.training.lessons_path = f
    assert [x["id"] for x in lessons.load(cfg, db)["lessons"]] == ["obraz-1", "obraz-2", "kontakt-1"]
    assert [x["shift"] for x in lessons.load(cfg, db)["onboarding"]] == [1, 2]

    x = content.get(db, cfg, "obraz-2")
    content.save(db, "obraz-2", "lesson", "Образ 2 (наш стандарт)", "Свой текст", "Своё действие", "items", x["data"], True)
    content.save(db, "kontakt-1", "lesson", "Контакт", "т", "д", "general", {}, False)  # выключен
    f.write_text(LESSONS_YAML.replace("Образ 2", "Образ 2 из файла").replace("Образ 1", "Образ 1 новый"),
                 encoding="utf-8")
    les = {x["id"]: x for x in lessons.load(cfg, db)["lessons"]}
    assert les["obraz-2"]["title"] == "Образ 2 (наш стандарт)"  # правка владельца не перезаписана
    assert les["obraz-1"]["title"] == "Образ 1 новый"  # из файла — обновляется
    assert "kontakt-1" not in les  # выключенный не рассылается


def test_learn_pages(env, tmp_path):  # noqa: F811
    cfg, db = env
    (tmp_path / "lessons.yaml").write_text(LESSONS_YAML, encoding="utf-8")
    cfg.training.lessons_path = tmp_path / "lessons.yaml"
    web = TestClient(create_app(cfg, db))
    page = web.get("/learn", auth=AUTH).text
    assert "Обучение: Уроки недели" in page and "Образ 1" in page and "стартовый файл" in page
    form = web.get("/learn/obraz-1", auth=AUTH).text
    assert "? Что предложить к брюкам?" in form and "+ Ремень" in form

    bad = web.post("/learn/obraz-1", data={"kind": "lesson", "title": "Образ 1", "body": "текст", "topic": "items",
                                           "questions": "? Вопрос\n- а\n- б", "active": "on"}, auth=AUTH)
    assert "не отмечен верный ответ" in bad.text and "? Вопрос" in bad.text  # введённое не потерялось
    ok = web.post("/learn/obraz-1", data={"kind": "lesson", "title": "Образ 1", "body": "текст", "topic": "items",
                                          "questions": QS, "active": "on"}, auth=AUTH)
    assert "Сохранено: Образ 1" in ok.text and "вы (админка)" in ok.text
    assert lessons.by_id(cfg, "obraz-1", db)["quiz"][0]["correct"] == 1

    new = web.get("/learn/new?kind=checklist", auth=AUTH).text
    assert "Пункты (по одному в строке)" in new
    r = web.post("/learn/checklist-1", data={"kind": "checklist", "title": "Открытие магазина",
                                             "items": "Включить свет\nПроверить примерочные", "active": "on"}, auth=AUTH)
    assert "Сохранено: Открытие магазина" in r.text
    assert content.get(db, cfg, "checklist-1")["data"]["items"] == ["Включить свет", "Проверить примерочные"]
    star = web.post("/learn/obraz-1", data={"kind": "lesson", "title": "Образ *1*", "questions": QS}, auth=AUTH)
    assert "Символы * и _" in star.text
    assert date.today()  # страница отвечает без ошибок


def test_repo_content_loads(env):  # noqa: F811
    from awer_sales_bot.config import Settings
    cfg, db = env
    cfg.training.lessons_path = Settings().training.lessons_path  # content/ из репозитория
    n = {k: len(content.items(db, cfg, k)) for k in content.KINDS}
    assert n["lesson"] >= 14 and n["onboarding"] == 10 and n["article"] >= 20
    assert n["test"] == 4 and n["survey"] == 3 and n["checklist"] == 3
    for x in content.items(db, cfg, "article"):
        assert x["topic"] in content.ARTICLE_SECTIONS and len(x["body"]) <= 1500
        assert "*" not in x["body"] and "_" not in x["body"]
    for x in content.items(db, cfg, "test"):
        for q in x["data"]["questions"]:
            assert 0 <= q["correct"] < len(q["options"]) and all(len(o) <= 128 for o in q["options"])
    texts = " ".join((x["body"] or "") + " " + (x["try_today"] or "") for k in ("lesson", "onboarding")
                     for x in content.items(db, cfg, k)).lower()
    assert "к брюкам и джинсам — ремень" not in texts and "к джинсам — ремень" not in texts
