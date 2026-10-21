"""Пароль входа: PBKDF2-SHA256 из стандартной библиотеки (без внешних зависимостей).
В `.env` хранится только хеш (`python -m forecast set-password`), не сам пароль."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

ITERATIONS = 390_000
PREFIX = "pbkdf2_sha256"


def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join([PREFIX, str(iterations), base64.b64encode(salt).decode(), base64.b64encode(digest).decode()])


def verify_password(password: str, stored: str) -> bool:
    try:
        prefix, iterations, salt_b64, digest_b64 = stored.split("$")
        if prefix != PREFIX:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)
