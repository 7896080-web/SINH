"""Что со ВТОРОЙ площадкой копий базы — только чтение.

Запуск:

    C:\\sync_admin\\.venv\\Scripts\\python.exe C:\\sync_admin\\scripts\\probe_backup_mirror.py

Зачем отдельный скрипт. 12.10 на «Диагностике» висела оговорка «копия не
доехала до зеркала», а дальше начинался час ручной работы: узнать, каким
rclone и куда ходит задание, спросить облако про доступ, про папку, сверить с
тем, что сказала последняя попытка. Каждый шаг — своя команда, и собирать её
надо руками, подставляя путь. Ровно на подстановке всё и сломалось: `<remote>`
в примере PowerShell разобрать не может (`<` у него зарезервирован), а
плейсхолдера для него не бывает — то же правило, по которому у
`probe_movements.py` нет позиционных «пустых» аргументов.

Поэтому здесь НЕ НАДО ПОДСТАВЛЯТЬ НИЧЕГО: настройки скрипт берёт из
`rclone_settings()` и `mirror_dir()`, то есть ровно те, которыми ходит суточное
задание. Разойдись они, скрипт отвечал бы про одно облако, а копии уезжали бы в
другое.

И главное, чего не хватало тогда: ВОЗРАСТ последней копии на той стороне. Одна
строка «в облаке последняя копия от 10.10» сразу показывает, один это сорванный
день или вторая площадка пропала давно, — а по оговорке на странице это
неразличимо.

Ничего не меняет, ничего не удаляет и в облако не пишет.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path                                      # noqa: E402

from app.backup import (_parse_moment, _run_rclone, about_unsupported,  # noqa: E402
                        backup_dir, cloud_gist, last_backup, mirror_dir,
                        rclone_settings)
from app.database import SessionLocal                         # noqa: E402
from app.models import WorkerHeartbeat                        # noqa: E402
from app.routers.health import MIRROR_OK_MARKER               # noqa: E402
from app.timeutils import now_utc                             # noqa: E402

# Консоль боевого сервера пишет в cp1251. Свои строки мы держим в её пределах
# (закрыто тестом), но сюда печатаются и ЧУЖИЕ данные: ответ rclone, имена
# файлов, текст оговорки. Один символ, которого в cp1251 нет, уронил бы ВЕСЬ
# вывод посреди строки — и скрипт, который запускают именно когда уже беда,
# ответил бы трассировкой.
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):      # перенаправленный вывод, старый Python
    pass


def _age(moment) -> str:
    """Человеческий возраст отметки: «3 ч», «5 дн»."""
    if moment is None:
        return "никогда"
    delta = now_utc() - moment
    if delta.total_seconds() < 3600:
        return "%d мин" % int(delta.total_seconds() // 60)
    if delta.days < 1:
        return "%d ч" % int(delta.total_seconds() // 3600)
    return "%d дн" % delta.days


def _say_copies(where: str, directory: Path) -> None:
    moment, total = last_backup(directory)
    if total == 0:
        print("  %s: копий нет вовсе" % where)
        return
    print("  %s: копий %d, последняя %s назад" % (where, total, _age(moment)))


def _local() -> None:
    print("ЛОКАЛЬНЫЕ КОПИИ")
    print("  каталог: %s" % backup_dir())
    _say_copies("состояние", backup_dir())


def _folder(db) -> bool:
    """Папка-зеркало. True — задана."""
    print("")
    print("ПАПКА-ЗЕРКАЛО")
    directory = mirror_dir(db)
    if directory is None:
        print("  не задана")
        return False
    print("  путь: %s" % directory)
    if not directory.exists():
        print("  ПАПКИ НЕТ НА ДИСКЕ -- копии туда не уезжают")
        return True
    _say_copies("состояние", directory)
    return True


def _cloud(db) -> bool:
    """Облако через rclone. True — задано."""
    print("")
    print("ОБЛАКО (rclone)")
    settings = rclone_settings(db)
    if settings is None:
        print("  не задано")
        return False
    exe, remote, config = settings
    print("  rclone.exe: %s%s" % (exe, "" if Path(exe).exists() else "   ФАЙЛА НЕТ"))
    print("  конфиг:     %s%s" % (config,
                                  "" if Path(config).exists() else "   ФАЙЛА НЕТ"))
    print("  путь:       %s" % remote)

    # Спрашиваем ТЕМ ЖЕ способом, которым ходит задание: свой вызов rclone
    # однажды разошёлся бы с ним по флагам или по конфигу, и скрипт отвечал бы
    # про не то.
    root = remote.split(":")[0] + ":"
    ok, said = _run_rclone(exe, config, ["about", root])
    if ok:
        print("  доступ к облаку: есть")
    elif about_unsupported(said):
        # Третий исход, и он НЕ отказ: S3 про свободное место не отвечает
        # вовсе. Назови мы это отказом, строка висела бы на исправной системе
        # — а скрипт читают, когда уже беда, и ложная строка в нём съедает
        # доверие ко всем остальным.
        print("  доступ к облаку: не спросить -- этот тип облака (S3) про")
        print("    свободное место не отвечает; смотрите строку про папку ниже")
    else:
        # Корень и папка — РАЗНЫЕ вопросы, и 12.10 именно это и развело разбор:
        # `about` отвечал, а папка — нет. Печатаем оба, иначе «облако не
        # отвечает» и «нет папки» сливаются в одно.
        print("  доступ к облаку: ОТКАЗ")
        print("    %s" % _explain(said, remote))

    ok, said = _run_rclone(exe, config, ["lsjson", remote])
    if not ok:
        print("  папка в облаке: ОТКАЗ")
        print("    %s" % _explain(said, remote))
        return True

    names = _names(said)
    moments = [m for m in (_parse_moment(n) for n in names) if m is not None]
    if not moments:
        print("  папка в облаке: есть, копий в ней нет")
    else:
        print("  папка в облаке: копий %d, последняя %s назад"
              % (len(moments), _age(max(moments))))
    return True


def _names(said: str) -> list:
    import json
    try:
        entries = json.loads(said or "[]")
    except ValueError:
        return []
    return [e.get("Name", "") for e in entries if isinstance(e, dict)]


def _explain(said: str, remote: str) -> str:
    """Причина словами, а под ней — что сказал rclone.

    Разбор ОДИН на скрипт и на само задание (`backup.cloud_gist`). Повтори
    скрипт правило у себя, он однажды объяснил бы отказ иначе, чем отчёт, — и
    человек поверил бы тому из двух, что ближе лежит.
    """
    gist = cloud_gist(said, remote)
    return ("%s\n    %s" % (gist, said)) if gist else said


def _what_the_job_said(db) -> None:
    print("")
    print("ЧТО СКАЗАЛО САМО ЗАДАНИЕ")
    beat = db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == "backup").first()
    if beat is None:
        print("  бэкап не отрабатывал ни разу")
    elif beat.last_error:
        print("  %s (%s назад)" % (beat.last_error, _age(beat.last_run_at)))
    else:
        print("  последняя попытка прошла без оговорок (%s назад)"
              % _age(beat.last_run_at))

    mark = db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == MIRROR_OK_MARKER).first()
    if mark is None:
        # Отметка пишется только при УДАЧНОЙ выгрузке. Её отсутствие значит
        # либо свежую установку, либо что зеркало не срабатывало ни разу с
        # момента обновления, — и выдумывать за неё давность нельзя.
        print("  удачной выгрузки на вторую площадку не было ни разу")
    else:
        print("  последняя удачная выгрузка: %s назад" % _age(mark.last_run_at))


def main() -> int:
    print("ВТОРАЯ ПЛОЩАДКА КОПИЙ -- что с ней сейчас")
    print("")
    db = SessionLocal()
    try:
        _local()
        has_folder = _folder(db)
        has_cloud = _cloud(db)
        _what_the_job_said(db)
        print("")
        if not has_folder and not has_cloud:
            # Это не поломка, а настройка, которой нет. Сказать надо прямо:
            # молчание читалось бы как «вторая площадка в порядке».
            print("ВТОРОЙ ПЛОЩАДКИ НЕТ ВОВСЕ: все копии лежат на том же диске,")
            print("что и база. Отказ этого диска унесёт и базу, и копии разом.")
            print("Задаётся на странице «Уведомления».")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
