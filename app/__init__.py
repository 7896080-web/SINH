"""Пакет приложения.

При боевом запуске (в т.ч. как Windows-службы, где нет `source .env`)
подхватываем переменные окружения из файла `.env` в корне проекта — до того,
как их прочитают database.py/crypto.py на импорте.

Под pytest окружение НЕ трогаем: там переменные задаёт conftest.py, а .env
проекта не должен влиять на тесты. `override=False` — уже заданное окружение
всегда в приоритете.
"""
import sys
from pathlib import Path

if "pytest" not in sys.modules:
    try:
        from dotenv import load_dotenv

        # encoding="utf-8-sig" — терпим BOM в .env (PowerShell 5.1 пишет файлы
        # с BOM; без этого load_dotenv не распознаёт первую переменную).
        load_dotenv(Path(__file__).resolve().parent.parent / ".env",
                    override=False, encoding="utf-8-sig")
    except Exception:
        # Отсутствие python-dotenv или .env не должно ронять импорт приложения.
        pass
