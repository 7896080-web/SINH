"""Резервная копия базы — самостоятельный запуск.

Ту же копию снимает воркер раз в сутки (`scheduler.job_backup`). Этот скрипт
нужен для двух случаев: снять копию прямо сейчас, руками, перед накатом или
рискованной правкой, — и повесить её в планировщик заданий Windows, если не
полагаться на то, что воркер жив.

    C:\\sync_admin\\.venv\\Scripts\\python.exe C:\\sync_admin\\scripts\\backup_db.py

Ничего не останавливает: копия снимается на живой базе штатным механизмом
SQLite и сразу проверяется. Возвращает 0 при успехе и 1 при любой неудаче —
планировщик Windows по этому коду покажет задание упавшим.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backup import backup_dir, last_backup, make_backup  # noqa: E402


def main() -> int:
    result = make_backup()
    if not result.ok:
        print(f"БЭКАП НЕ СНЯТ: {result.error}")
        return 1
    moment, total = last_backup()
    print(f"копия: {result.path}")
    print(f"размер: {result.size_bytes / 1024 / 1024:.1f} МБ")
    print(f"удалено старых: {result.removed}")
    print(f"всего копий в {backup_dir()}: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
