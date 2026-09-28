"""Восстановление боевой базы из копии — со всеми шагами, а не четырьмя.

Процедура из четырёх команд (`nssm stop` ×2, `Copy-Item` текущей в сторону,
`Copy-Item` копии поверх, `nssm start` ×2) НЕ ВОССТАНАВЛИВАЕТ НИЧЕГО, и делает
это молча. База живёт в WAL (`app/database.py`), а NSSM при жёстком завершении
службы оставляет рядом `sync_admin.db-wal` и `-shm` со страницами ПРЕЖНЕЙ базы.
SQLite при первом открытии видит спутников, считает их своим журналом и
накатывает поверх подложенной копии. `PRAGMA integrity_check` при этом
отвечает `ok`, размер правдоподобный, приложение поднимается — и человек
уверен, что откатился. Это единственная процедура восстановления в проекте и
единственный выход после оборванной миграции, которую alembic на SQLite не
откатывает.

Второе, что здесь чинится: текущую базу «в сторону» откладывали `Copy-Item`,
то есть файловой копией при живом WAL — ровно тем, что запрещено в CLAUDE.md.
Службы к этому моменту уже остановлены, так что копия скорее всего консистентна,
но «скорее всего» — не то слово, которое хочется слышать про единственный
уцелевший экземпляр заказов, принятых после бэкапа.

Порядок здесь такой:

    1. Убедиться, что службы остановлены. Живая служба держит базу открытой, и
       замена файла под ней — самый надёжный способ получить мусор.
    2. Снять текущую базу штатным механизмом SQLite (`scripts/backup_db.py`),
       а не копированием файла.
    3. Отложить в сторону текущую базу ВМЕСТЕ со спутниками.
    4. Положить копию и проверить её чтением товаров, а не только структуры.

Запуск:

    C:\\sync_admin\\.venv\\Scripts\\python.exe C:\\sync_admin\\scripts\\restore_db.py ^
        C:\\sync_admin\\backups\\sync_admin-ГГГГММДД-ЧЧММСС.db

Ничего не запускает и не останавливает сам: остановить и поднять службы —
решение человека, и делать это за него скрипт не станет.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backup import (  # noqa: E402
    _collapse_journal, _verify, database_path, make_backup,
)

COMPANIONS = ("-wal", "-shm", "-journal")


def _services_running() -> list[str]:
    """Какие из наших служб сейчас живы. Пустой список — можно продолжать.

    Через `sc query`, а не `nssm`: `sc` есть всегда, а ответ разбирается по
    слову STATE. Не нашли службу — считаем остановленной: на машине, где их
    поставили под другими именами, лучше спросить человека, чем упереться.
    """
    alive = []
    for name in ("sync_admin_web", "sync_admin_worker"):
        try:
            out = subprocess.run(["sc", "query", name], capture_output=True,
                                 text=True, timeout=20).stdout
        except Exception:
            continue
        if "RUNNING" in out.upper():
            alive.append(name)
    return alive


def _set_aside(target: Path) -> Path:
    """Отложить базу и ВСЕХ её спутников под `.before-restore`.

    Спутники обязательны: оставленный `-wal` — это и есть тот журнал, который
    накатится поверх подложенной копии. Унести их надо, а не удалить: в них
    может лежать последняя незаписанная транзакция текущей базы.
    """
    kept = target.with_name(target.name + ".before-restore")
    for path in (kept, *(kept.with_name(kept.name + s) for s in COMPANIONS)):
        if path.exists():
            path.unlink()
    shutil.move(str(target), str(kept))
    for suffix in COMPANIONS:
        side = target.with_name(target.name + suffix)
        if side.exists():
            shutil.move(str(side), str(kept.with_name(kept.name + suffix)))
    return kept


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    source = Path(argv[1])
    if not source.is_file():
        print(f"Копия не найдена: {source}")
        return 1

    target = database_path()
    if target is None:
        print("Не удалось понять, где лежит база: DATABASE_URL не файловый SQLite.")
        return 1

    alive = _services_running()
    if alive:
        print("Службы ещё работают: " + ", ".join(alive))
        print("Остановите их и запустите заново:")
        for name in alive:
            print(f"    nssm stop {name}")
        return 1

    problem = _verify(source)
    if problem:
        print(f"Копия негодна, восстанавливать нечем: {problem}")
        return 1

    if target.exists():
        # Штатным механизмом, а не копированием файла: см. docstring.
        print("[*] Снимаю текущую базу в backups перед заменой...")
        result = make_backup()
        if not result.ok:
            print(f"Текущую базу снять НЕ удалось: {result.error}")
            print("Восстановление остановлено: заменять базу, не сохранив нынешнюю,")
            print("значит терять заказы, принятые после копии, без возврата.")
            return 1
        print(f"    снята: {result.path}")
        kept = _set_aside(target)
        print(f"[*] Текущая база отложена: {kept}")
    else:
        # Базы нет, но спутники могли остаться — и накатятся поверх копии.
        for suffix in COMPANIONS:
            side = target.with_name(target.name + suffix)
            if side.exists():
                side.unlink()
                print(f"[*] Убран осиротевший спутник: {side.name}")

    shutil.copy2(str(source), str(target))
    # Копия могла быть в WAL: сводим к одному файлу, чтобы не завести спутников
    # заново тем же действием, от которого только что избавились.
    _collapse_journal(target)

    problem = _verify(target)
    if problem:
        print(f"[X] Восстановленная база не прошла проверку: {problem}")
        print("    Службы НЕ поднимайте. Отложенная база рядом, с суффиксом")
        print("    .before-restore — её можно вернуть на место.")
        return 1

    print(f"[OK] База восстановлена из {source.name}")
    print("     Поднимите службы:")
    print("         nssm start sync_admin_worker")
    print("         nssm start sync_admin_web")
    print("     Заказы, принятые после копии, остались только в отложенной базе")
    print("     (.before-restore) — их придётся сверить с площадками руками.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
