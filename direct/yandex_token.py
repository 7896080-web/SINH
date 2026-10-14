# -*- coding: utf-8 -*-
"""
Токены Яндекса для скриптов. В коде скриптов токены не хранятся.

  • OAuth-токен Яндекса (Директ, Метрика, Вебмастер) — из настроек AWER CRM:
    ~/AWER CRM/config.json, ключ yandex_oauth_token (сохраняется в CRM: Маркетинг → Подключение к Яндексу),
    или из переменной окружения YANDEX_OAUTH_TOKEN.
  • Токен Яндекс Доставки — из файла yandex_delivery_token.txt рядом со скриптами
    или из переменной окружения YANDEX_DELIVERY_TOKEN.
"""
import json
import os
import sys
from pathlib import Path

# Вывод в консоль Windows (cp1251) или в файл без UTF-8 не должен обрывать скрипт на ✓ ✗ → ₽ −: непечатаемое
# заменяется «?». Важно для откатов: раньше первый же ✓ после первой записи останавливал откат на середине.
try:
    if sys.stdout is not None and (sys.stdout.encoding or "").lower().replace("-", "") != "utf8":
        sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass

CRM_CONFIG = Path.home() / "AWER CRM" / "config.json"
DELIVERY_FILE = Path(__file__).resolve().with_name("yandex_delivery_token.txt")


def crm_config():
    try:
        return json.loads(CRM_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def yandex_token():
    t = os.environ.get("YANDEX_OAUTH_TOKEN") or crm_config().get("yandex_oauth_token")
    if not t:
        sys.exit("Нет токена Яндекса: сохраните его в AWER CRM (Маркетинг → Подключение к Яндексу) "
                 "или задайте переменную окружения YANDEX_OAUTH_TOKEN.")
    return t


def delivery_token():
    t = os.environ.get("YANDEX_DELIVERY_TOKEN")
    if not t and DELIVERY_FILE.exists():
        t = DELIVERY_FILE.read_text(encoding="utf-8-sig").strip()
    if not t:
        if not DELIVERY_FILE.exists():
            DELIVERY_FILE.write_text("", encoding="utf-8")
        sys.exit(f"Нет токена Яндекс Доставки: вставьте его в {DELIVERY_FILE} "
                 "(ЛК Доставки → Профиль компании → Токен для API).")
    return t
