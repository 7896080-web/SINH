"""Страница «Бот MAX»: токены в .env, ссылка на бота сама, владелец по одноразовому коду, кто из продавцов подключён."""
import os

from fastapi.testclient import TestClient

from awer_sales_bot import bot, botcfg
from awer_sales_bot.max_api import MaxAuthError
from awer_sales_bot.web import create_app
from test_flow import env  # noqa: F401
from test_motivation import FakeMax

AUTH = ("admin", "pw")


class FakeClient:
    def __init__(self, token):
        self.token = token

    def me(self):
        if self.token.startswith("bad"):
            raise MaxAuthError("401", 401)
        return {"user_id": 1, "first_name": "AWER План продаж", "username": "id9102000000_bot"}


class FakeRunner:
    running, started, restarted = False, 0, 0

    def start(self):
        self.started += 1
        self.running = True
        return True

    def restart(self):
        self.restarted += 1


def app(env, tmp_path, monkeypatch, runner=None):  # noqa: F811
    cfg, db = env
    cfg._env_path = tmp_path / ".env"
    cfg._env_path.write_text("ADMIN_PASSWORD=pw\n", encoding="utf-8")
    for k in botcfg.ENV_KEYS.values():
        monkeypatch.delenv(k, raising=False)
    return TestClient(create_app(cfg, db, runner=runner, max_client=FakeClient))


def test_token_saved_to_env_link_filled_bot_started(env, tmp_path, monkeypatch):  # noqa: F811
    cfg, db = env
    runner = FakeRunner()
    web = app(env, tmp_path, monkeypatch, runner)
    page = web.get("/bot", auth=AUTH).text
    assert "Бот MAX" in page and "не задан" in page and "Владелец ещё не подключён" in page

    tok = "f9LHodD0cOKdm8nGq6uk3ZqJ0MbV2XqA"
    r = web.post("/bot", data={"action": "save", "token_plan": tok, "link_plan": "", "token_training": "",
                               "link_training": ""}, auth=AUTH)
    env_text = cfg.env_path.read_text(encoding="utf-8")
    assert f"MAX_BOT_TOKEN={tok}" in env_text and "ADMIN_PASSWORD=pw" in env_text  # остальное в .env не тронуто
    assert os.environ["MAX_BOT_TOKEN"] == tok and runner.started == 1
    assert cfg.max.bot_link == "https://max.ru/id9102000000_bot"  # ссылка — из ника бота, руками не вводить
    assert "Бот запущен" in r.text and "на связи: AWER План продаж" in r.text
    assert tok not in r.text and "…XqA" not in r.text and "…2XqA" in r.text  # токен целиком не показываем

    web.post("/bot", data={"action": "save", "token_plan": "bad token\nX=1", "link_plan": cfg.max.bot_link,
                           "token_training": "", "link_training": ""}, auth=AUTH)
    assert "X=1" not in cfg.env_path.read_text(encoding="utf-8")  # перевод строки в .env не пролезет

    restarted = web.post("/bot", data={"action": "restart"}, auth=AUTH)
    assert runner.restarted == 1 and "перезапускается" in restarted.text


def test_owner_connects_by_code(env, tmp_path, monkeypatch):  # noqa: F811
    cfg, db = env
    web = app(env, tmp_path, monkeypatch)
    botcfg.set_link(db, cfg, "plan", "https://max.ru/id9102000000_bot")
    page = web.post("/bot", data={"action": "owner_code"}, auth=AUTH).text
    code = botcfg.owner_code(db)
    assert code and code in page and f"start={code}" in page and "<svg" in page

    m = FakeMax()
    bot.handle(db, cfg, m, {"update_type": "bot_started", "user": {"user_id": 900, "first_name": "Ольга",
                                                                    "last_name": "Хозяйкина"}, "payload": code})
    assert cfg.max.owners == [900] and "владелец" in m.sent[0][1]
    assert botcfg.owner_code(db) is None  # код одноразовый
    bot.handle(db, cfg, m, {"update_type": "bot_started", "user": {"user_id": 901}, "payload": code})
    assert cfg.max.owners == [900]
    assert "Ольга Хозяйкина" in web.get("/bot", auth=AUTH).text

    botcfg.apply(cfg.model_copy(), db)  # после перезапуска владелец берётся из базы
    fresh = cfg.model_copy()
    fresh.max.owners = []
    botcfg.apply(fresh, db)
    assert fresh.max.owners == [900]

    web.post("/bot", data={"action": "owner_del:900"}, auth=AUTH)
    assert cfg.max.owners == []


def test_seller_identified_by_max_profile(env, tmp_path, monkeypatch):  # noqa: F811
    cfg, db = env
    web = app(env, tmp_path, monkeypatch)
    sid = db.add_seller("Новикова Мария", "Мария")
    m = FakeMax()
    bot.handle(db, cfg, m, {"update_type": "bot_started", "payload": db.new_invite(sid),
                            "user": {"user_id": 501, "first_name": "Мария", "last_name": "Новикова"}})
    s = db.seller(sid)
    assert s["max_user_id"] == 501 and s["max_name"] == "Мария Новикова"
    assert "(в MAX: Мария Новикова)" in web.get("/sellers", auth=AUTH).text
    page = web.get("/bot", auth=AUTH).text
    assert "Подключены к «Плану»" in page and "Новикова Мария" not in page.split("Ещё не подключены")[-1]
