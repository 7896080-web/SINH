# -*- coding: utf-8 -*-
"""
Токены Яндекса для скриптов. В коде скриптов токены не хранятся.

  • OAuth-токен Яндекса (Директ, Метрика, Вебмастер) — из настроек AWER CRM:
    ~/AWER CRM/config.json, ключ yandex_oauth_token (сохраняется в CRM: Маркетинг → Подключение к Яндексу),
    на сервере — из crm_config.json рядом со скриптами (его собирает deploy/export_for_server.ps1),
    или из переменной окружения YANDEX_OAUTH_TOKEN.
  • Токен Яндекс Доставки — из файла yandex_delivery_token.txt рядом со скриптами
    или из переменной окружения YANDEX_DELIVERY_TOKEN.
"""
import json
import os
import sys
from pathlib import Path

# Переезд на сервер (deploy/export_for_server.ps1 положил MOVED_TO_SERVER.txt): здесь скрипты больше не запускаются —
# два компьютера меняли бы ставки и пополняли бюджеты одновременно, а лимиты WB у них общие.
if (Path(__file__).resolve().parent / "MOVED_TO_SERVER.txt").exists():
    sys.exit("Скрипты перенесены на сервер — запускайте их там (RDP, C:\\direct). Здесь не запускаю: два компьютера "
             "меняли бы рекламу одновременно. Подробно — MOVED_TO_SERVER.txt рядом со скриптами.")

# Вывод в консоль Windows (cp1251) или в файл без UTF-8 не должен обрывать скрипт на ✓ ✗ → ₽ −: непечатаемое
# заменяется «?». Важно для откатов: раньше первый же ✓ после первой записи останавливал откат на середине.
try:
    if sys.stdout is not None and (sys.stdout.encoding or "").lower().replace("-", "") != "utf8":
        sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass

CRM_CONFIG = Path.home() / "AWER CRM" / "config.json"
# На сервере AWER CRM нет: при переезде deploy/export_for_server.ps1 кладёт сюда только нужные этим скриптам настройки
# CRM (токен Яндекса, логин Директа, счётчик Метрики, соинвест) — без остальных ключей CRM.
LOCAL_CONFIG = Path(__file__).resolve().with_name("crm_config.json")
DELIVERY_FILE = Path(__file__).resolve().with_name("yandex_delivery_token.txt")


def crm_config():
    for p in (CRM_CONFIG, LOCAL_CONFIG):
        try:
            return json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
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
