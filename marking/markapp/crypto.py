"""Шифрование секретов (API-ключи, токены ЧЗ, полные коды).

Ключ — СВОЙ у программы (`MARKING_SECRETS_KEY`), не ключ sync_admin: общий
ключ связал бы две программы, и утечка одного .env раскрыла бы обе базы.
Хранится отдельно от резервных копий — копия вместе с ключом есть утечка.
"""
import os

from cryptography.fernet import Fernet

_KEY = os.environ.get("MARKING_SECRETS_KEY")
if not _KEY:
    raise RuntimeError("Не задана переменная окружения MARKING_SECRETS_KEY")

_fernet = Fernet(_KEY.encode())


def encrypt_value(plain: str) -> str:
    return _fernet.encrypt(plain.encode()).decode() if plain else ""


def decrypt_value(token: str) -> str:
    return _fernet.decrypt(token.encode()).decode() if token else ""


def mask_value(plain: str) -> str:
    if not plain:
        return ""
    if len(plain) <= 8:
        return "*" * len(plain)
    return plain[:4] + "*" * (len(plain) - 8) + plain[-4:]
