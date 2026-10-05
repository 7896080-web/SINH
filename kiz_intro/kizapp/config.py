"""Настройки из окружения и `.env` в корне программы (рядом с kizapp/).

Свой ключ шифрования (`KIZ_SECRETS_KEY`), своя база, свой порт: с
«Маркировкой» программа не делит ничего (решение заказчика 05.10.2026).
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env(ROOT / ".env")

DATABASE_URL = os.environ.get("KIZ_DATABASE_URL", f"sqlite:///{(ROOT / 'kiz.db').as_posix()}")
SECRETS_KEY = os.environ.get("KIZ_SECRETS_KEY", "")
BACKUP_DIR = Path(os.environ.get("KIZ_BACKUP_DIR", str(ROOT / "backups")))
TRUE_API_URL = os.environ.get("KIZ_TRUE_API_URL", "https://markirovka.crpt.ru/api/v3/true-api").rstrip("/")
CHZ_PROXY = os.environ.get("KIZ_CHZ_PROXY", "")
PORT = int(os.environ.get("KIZ_PORT", "8002"))
