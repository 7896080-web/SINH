"""Служебные команды для установщика под Windows (deploy/windows/*.ps1).

    python -m finance.windows init-env C:\\FinanceBot   — создать/дополнить .env
    python -m finance.windows cert C:\\FinanceBot\\settings — сертификат страницы настроек
    python -m finance.windows missing C:\\FinanceBot     — каких настроек не хватает

Всё, что пишет файлы, сделано здесь, а не в PowerShell: PowerShell 5 пишет
текст в «своей» кодировке, а .env должен быть в UTF-8.
"""

import datetime
import os
import shutil
import sys

from .setup_web import read_env, write_env

REQUIRED = ("TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY")
CERT_DAYS = 730
RENEW_BEFORE_DAYS = 30


def init_env(app: str, template: str | None = None) -> str:
    """Создать .env из шаблона (если его ещё нет) и прописать пути Windows.
    Уже вписанные токены и id не трогаются."""
    app = os.path.abspath(app)
    env_path = os.path.join(app, ".env")
    template = template or os.path.join(app, ".env.example")
    if not os.path.exists(env_path):
        if os.path.exists(template):
            shutil.copyfile(template, env_path)
        else:
            open(env_path, "w", encoding="utf-8").close()
    env = read_env(env_path)
    updates = {}
    for key, folder in (("FINANCE_DATA_DIR", "data"), ("FINANCE_TEST_DATA_DIR", "data-test")):
        value = env.get(key, "")
        if not value or value.startswith("/"):  # пусто или путь из шаблона для Linux
            updates[key] = os.path.join(app, folder)
    if updates:
        write_env(env_path, updates)
    return env_path


def missing(app: str) -> list[str]:
    env = read_env(os.path.join(os.path.abspath(app), ".env"))
    return [key for key in REQUIRED if not env.get(key)]


def make_cert(folder: str, now: datetime.datetime | None = None) -> bool:
    """Самоподписанный сертификат для страницы настроек. True — создан новый,
    False — прежний ещё годен."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    now = now or datetime.datetime.now(datetime.timezone.utc)
    cert_path = os.path.join(folder, "cert.pem")
    key_path = os.path.join(folder, "key.pem")
    if os.path.exists(cert_path) and os.path.exists(key_path):
        with open(cert_path, "rb") as fh:
            old = x509.load_pem_x509_certificate(fh.read())
        if old.not_valid_after_utc - now > datetime.timedelta(days=RENEW_BEFORE_DAYS):
            return False
    os.makedirs(folder, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "finance-bot-settings")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=CERT_DAYS))
            .sign(key, hashes.SHA256()))
    with open(key_path, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM,
                                   serialization.PrivateFormat.TraditionalOpenSSL,
                                   serialization.NoEncryption()))
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    return True


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] not in ("init-env", "cert", "missing"):
        print(__doc__)
        return 2
    cmd, path = argv
    if cmd == "init-env":
        print(init_env(path))
    elif cmd == "cert":
        print("Создан сертификат страницы (самоподписанный, на 2 года)." if make_cert(path)
              else "Сертификат страницы ещё действует.")
    else:
        print(" ".join(missing(path)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
