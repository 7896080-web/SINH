"""Программа «Маркировка и поставки».

При боевом запуске (Windows-службы, где нет `source .env`) переменные
окружения берутся из `.env` в корне программы — до того, как их прочитают
`database.py` и `crypto.py` на импорте. Под pytest окружение не трогаем: его
задаёт `tests/conftest.py`. Уже заданное окружение всегда в приоритете.
"""
import sys
from pathlib import Path

if "pytest" not in sys.modules:
    try:
        from dotenv import load_dotenv

        # utf-8-sig — PowerShell 5.1 пишет .env с BOM; без этого первая
        # переменная файла не распознаётся.
        load_dotenv(Path(__file__).resolve().parent.parent / ".env",
                    override=False, encoding="utf-8-sig")
    except Exception:
        pass
