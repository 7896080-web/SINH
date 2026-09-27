"""Боевой и тестовый режимы.

Боевой бот (python -m finance) ведёт настоящий учёт. Тестовый
(python -m finance --test) — отдельный бот в Telegram со своим токеном и
своей папкой данных: в нём можно пробовать скриншоты, выписки, сверку,
правила — и ничего из этого не попадёт в боевые базы. Тестовые данные можно
стереть командой /reset.

Режим задаётся только аргументом командной строки (его ставит служба
systemd), а не переменной в .env: правка настроек не может случайно
превратить боевой бот в тестовый или наоборот.

Границы, которые проверяются при запуске:
- у тестового бота свой токен, и это другой бот (не второй токен того же:
  два процесса на одном боте отнимали бы друг у друга сообщения);
- папка тестовых данных не совпадает с боевой и не лежит внутри неё (и
  наоборот) — стирание тестовых данных не может задеть боевые.
"""

import os
from dataclasses import dataclass

from .users import parse_user_ids

PROD, TEST = "prod", "test"
TEST_MARK = "🧪 ТЕСТ"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RunConfig:
    mode: str
    token: str          # пусто — тестовый бот ещё не настроен
    data_dir: str
    user_ids: list[int]

    @property
    def is_test(self) -> bool:
        return self.mode == TEST

    @property
    def label(self) -> str:
        return TEST_MARK if self.is_test else ""


def bot_id(token: str) -> str:
    """Числовой id бота — часть токена до двоеточия. Перевыпущенный у
    @BotFather токен того же бота начинается так же."""
    return token.strip().split(":", 1)[0]


def same_bot(a: str, b: str) -> bool:
    return bool(a.strip() and b.strip()) and bot_id(a) == bot_id(b)


def test_data_dir(env: dict) -> str:
    explicit = env.get("FINANCE_TEST_DATA_DIR", "").strip()
    if explicit:
        return explicit
    prod = env.get("FINANCE_DATA_DIR", "").strip() or "data"
    return prod.rstrip("/\\") + "-test"


def _overlap(a: str, b: str) -> bool:
    """Одна папка внутри другой (или совпадают). normcase — на Windows регистр
    букв в путях не важен; commonpath — корень диска тоже «содержит» всё."""
    a = os.path.normcase(os.path.realpath(os.path.abspath(a)))
    b = os.path.normcase(os.path.realpath(os.path.abspath(b)))
    try:
        common = os.path.commonpath([a, b])
    except ValueError:  # разные диски Windows
        return False
    return common in (a, b)


def run_config(env: dict, test: bool = False) -> RunConfig:
    prod_dir = env.get("FINANCE_DATA_DIR", "").strip() or "data"
    prod_token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    users = parse_user_ids(env.get("ALLOWED_USER_IDS", ""))
    if not test:
        if not prod_token:
            raise ConfigError("не задан TELEGRAM_BOT_TOKEN")
        return RunConfig(PROD, prod_token, prod_dir, users)

    token = env.get("TELEGRAM_BOT_TOKEN_TEST", "").strip()
    if token and same_bot(token, prod_token):
        raise ConfigError("TELEGRAM_BOT_TOKEN_TEST — это тот же бот, что и боевой. "
                          "Для тестов создайте у @BotFather отдельного бота.")
    data_dir = test_data_dir(env)
    if _overlap(data_dir, prod_dir):
        raise ConfigError(f"папка тестовых данных {data_dir} пересекается с боевой {prod_dir}")
    raw_test_users = env.get("TEST_USER_IDS", "").strip()
    test_users = parse_user_ids(raw_test_users) if raw_test_users else users
    return RunConfig(TEST, token, data_dir, test_users)
