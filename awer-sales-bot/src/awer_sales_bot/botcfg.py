"""Настройки ботов из админки (страница «Бот MAX»), чтобы владельцу не править файлы руками.

Токены — в .env (секреты только там), ссылки на ботов и владельцы — в базе (kv) и главнее config.yaml, как и
остальные ручные значения админки. Владелец подключается как продавец — одноразовым кодом (QR на странице):
его user_id попадает в max.owners, ему идут сводка, «Кого похвалить» и предупреждения.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from .config import Settings
from .db import DB

ENV_KEYS = {"plan": "MAX_BOT_TOKEN", "training": "MAX_TRAINING_BOT_TOKEN"}
LINK_KEYS = {"plan": "max.bot_link", "training": "max_training.bot_link"}
OWNER_CODE_TTL = timedelta(hours=24)
TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-:.]{10,512}$")  # без пробелов и переводов строки — пишется в .env как есть


def apply(cfg: Settings, db: DB) -> None:
    """Значения из админки поверх config.yaml — при запуске и после сохранения на странице."""
    for bot, key in LINK_KEYS.items():
        v = db.get_kv(key)
        if v is not None:
            (cfg.max if bot == "plan" else cfg.max_training).bot_link = v
    v = db.get_kv("max.owners")
    if v is not None:
        cfg.max.owners = [o["id"] for o in json.loads(v)]


def link(cfg: Settings, bot: str) -> str:
    return cfg.max.bot_link if bot == "plan" else cfg.max_training.bot_link


def set_link(db: DB, cfg: Settings, bot: str, value: str) -> None:
    db.set_kv(LINK_KEYS[bot], value.strip().rstrip("/"))
    apply(cfg, db)


def token(bot: str) -> str:
    return os.environ.get(ENV_KEYS[bot], "")


def mask(tok: str) -> str:
    return f"задан, оканчивается на …{tok[-4:]}" if tok else "не задан"


def set_token(env_path: Path, bot: str, value: str) -> None:
    """Пишет токен в .env (заменяет строку или добавляет) и сразу в окружение процесса."""
    value = value.strip()
    if not TOKEN_RE.match(value):
        raise ValueError("токен выглядит неверно — скопируйте его из business.max.ru целиком, без пробелов")
    key = ENV_KEYS[bot]
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    out = [ln for ln in lines if ln.split("=", 1)[0].strip() != key] + [f"{key}={value}"]
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ[key] = value


# ----------------------------------------------------------------------------- владельцы
def owners(db: DB, cfg: Settings) -> list[dict]:
    """[{id, name, at}] — из базы; пока на странице ничего не меняли — из config.yaml."""
    v = db.get_kv("max.owners")
    return json.loads(v) if v is not None else [{"id": i, "name": "", "at": ""} for i in cfg.max.owners]


def _save_owners(db: DB, cfg: Settings, lst: list[dict]) -> None:
    db.set_kv("max.owners", json.dumps(lst, ensure_ascii=False))
    apply(cfg, db)


def new_owner_code(db: DB) -> str:
    code = "W" + secrets.token_hex(4).upper()  # 9 знаков — не путается с 6-значными кодами продавцов
    db.set_kv("owner_code", f"{code}|{datetime.now().isoformat(timespec='seconds')}")
    return code


def owner_code(db: DB) -> str | None:
    """Действующий код подключения владельца (сутки после выдачи) или None."""
    code, _, at = (db.get_kv("owner_code") or "").partition("|")
    if not code or datetime.now() - datetime.fromisoformat(at) > OWNER_CODE_TTL:
        return None
    return code


def link_owner(db: DB, cfg: Settings, text: str, uid: int, name: str) -> bool:
    code = owner_code(db)
    if not code or text.strip().upper() != code:
        return False
    lst = [o for o in owners(db, cfg) if o["id"] != uid]
    lst.append({"id": uid, "name": name, "at": datetime.now().isoformat(timespec="seconds")})
    _save_owners(db, cfg, lst)
    db.set_kv("owner_code", "")  # код одноразовый
    return True


def remove_owner(db: DB, cfg: Settings, uid: int) -> None:
    _save_owners(db, cfg, [o for o in owners(db, cfg) if o["id"] != uid])
