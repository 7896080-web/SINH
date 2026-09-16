import os
from cryptography.fernet import Fernet

# Ключ шифрования — из переменной окружения, НЕ из кода и НЕ из репозитория.
# Сгенерировать один раз: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
_KEY = os.environ.get("SECRETS_ENCRYPTION_KEY")
if not _KEY:
    raise RuntimeError(
        "Не задана переменная окружения SECRETS_ENCRYPTION_KEY — без неё "
        "API-ключи площадок нельзя ни зашифровать, ни расшифровать."
    )

_fernet = Fernet(_KEY.encode())


def encrypt_value(plain: str) -> str:
    if not plain:
        return ""
    return _fernet.encrypt(plain.encode()).decode()


def decrypt_value(token: str) -> str:
    if not token:
        return ""
    return _fernet.decrypt(token.encode()).decode()


def mask_value(plain: str) -> str:
    """Для отображения в интерфейсе — не показываем ключ целиком."""
    if not plain:
        return ""
    if len(plain) <= 8:
        return "*" * len(plain)
    return plain[:4] + "*" * (len(plain) - 8) + plain[-4:]
