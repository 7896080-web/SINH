"""Шифрование API-ключей площадок.

Ключ — СВОЙ у программы (`REPRICER_SECRETS_KEY`), не ключ sync_admin и не
«Маркировки»: общий ключ связал бы программы, и утечка одного .env раскрыла бы
все базы. Хранится отдельно от копий базы — копия вместе с ключом есть утечка.
"""
import os

from cryptography.fernet import Fernet

_KEY = os.environ.get("REPRICER_SECRETS_KEY")
if not _KEY:
    raise RuntimeError("Не задана переменная окружения REPRICER_SECRETS_KEY")

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
