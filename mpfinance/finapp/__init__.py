"""Программа «Финансы площадок» — отчёты WB / Lamoda / Яндекс KIT в единой форме,
юнит-экономика, сверка, загрузка отчётов о продажах в 1С, дашборды.

Отдельная программа рядом с sync_admin и «Репрайсером» (как они, — своя база,
свой вход, свои ключи площадок, своя служба Windows). С 1С говорит той же
обработкой «ОбменССайтом», но СВОИМИ файлами: задания `task_fin_*.txt`, ответы
в `results\\finance`. Базу sync_admin только читает — для сверки отчёта с
принятыми заказами (`syncdb.py`), ничего в неё не пишет.

При запуске службой (без `source .env`) переменные окружения берутся из `.env`
в корне программы — до того, как их прочитают `database.py` и `crypto.py`.
Под pytest окружение не трогаем: его задаёт `tests/conftest.py`.
"""
import sys
from pathlib import Path

if "pytest" not in sys.modules:
    try:
        from dotenv import load_dotenv

        # utf-8-sig — PowerShell 5.1 пишет .env с BOM.
        load_dotenv(Path(__file__).resolve().parent.parent / ".env",
                    override=False, encoding="utf-8-sig")
    except Exception:
        pass
