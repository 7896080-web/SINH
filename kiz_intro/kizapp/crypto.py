"""Шифрование токенов ЧЗ и полных кодов. Ключ — свой (`KIZ_SECRETS_KEY`)."""
from cryptography.fernet import Fernet

from kizapp import config

if not config.SECRETS_KEY:
    raise RuntimeError("Не задан KIZ_SECRETS_KEY (.env программы)")
_f = Fernet(config.SECRETS_KEY.encode())


def encrypt(plain: str) -> str:
    return _f.encrypt(plain.encode()).decode() if plain else ""


def decrypt(token: str) -> str:
    return _f.decrypt(token.encode()).decode() if token else ""
