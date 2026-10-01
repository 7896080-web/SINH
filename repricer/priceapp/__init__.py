"""Программа «Репрайсер» — цены на WB/Ozon/Kit от себестоимости 1С.

Отдельная программа рядом с sync_admin и «Маркировкой» (корень репозитория):
своя база, свой вход, свои ключи площадок. Стоит на офисном компьютере, с 1С
говорит файлами `task_price_*.txt` по SFTP до сервера (как «Маркировка»).

При запуске ярлыком (без `source .env`) переменные окружения берутся из `.env`
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
