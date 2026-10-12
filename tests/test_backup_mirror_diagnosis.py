"""Отказ второй площадки обязан быть ЧИТАЕМЫМ и НАЗВАННЫМ.

12.10 на «Диагностике» висела оговорка:

    копия не доехала до зеркала: облако: rclone вернул 1: ERROR : Attempt 1/3
    failed with 1 errors and: [410 - GoneError] Gone (Р РµСЃСѓСЂСЃ Р±РѕР»СЊС€Рµ
    РЅРµРґРѕСЃС‚СѓРїРµРЅ.)

Два дефекта в одной строке, и оба наши.

ПЕРВЫЙ — мозаика. Это «Ресурс больше недоступен», прочитанное не той
кодировкой: `subprocess.run(text=True)` декодирует по локальной кодировке
Windows (cp1251), а rclone пишет UTF-8. Вред не косметический: строка режется
по 300 символов, а мозаика занимает ВДВОЕ больше места, то есть вытесняет
собой то самое, ради чего её читают.

ВТОРОЙ — сырой код наверх. 410 прочитали как «папку удалили» и собрались
создавать её заново; папка была на месте, а у Яндекс.Диска шёл сбой, о котором
они сами писали на своей странице. Структурное действие в ответ на отказ,
который проходит сам, — ровно то, за что 06.10 заплатили тремя кабинетами WB.

Отсюда же третий сюжет файла: один сорвавшийся день и неделя без второй
площадки — разные вещи, а выглядели одинаково.
"""
import subprocess
from datetime import timedelta

import pytest

from app import backup, report
from app.models import WorkerHeartbeat
from app.routers.health import MIRROR_OK_MARKER, snapshot
from app.timeutils import now_utc

# Кодировка, которой `text=True` читает вывод на БОЕВОЙ машине: Windows с
# русской локалью. Здесь Python взял бы UTF-8 — и дефекта не было бы видно
# вовсе, то есть проверка на этой машине обязана подставлять ту.
WINDOWS_LOCALE = "cp1251"

# Дословно то, что ответил Яндекс.Диск 12.10.
GONE = "ERROR : [410 - GoneError] Gone (Ресурс больше недоступен.)"


class Rclone:
    """Подставной rclone: отдаёт БАЙТЫ и декодирует их тем, о чём попросили.

    Это и есть предмет проверки. Обычная заглушка, возвращающая готовую
    строку, согласилась бы с любой кодировкой и молчала бы ровно про тот
    дефект, ради которого написана.
    """

    def __init__(self, stderr="", stdout="", code=1):
        self.stderr = stderr.encode("utf-8")
        self.stdout = stdout.encode("utf-8")
        self.code = code
        self.calls = []
        self.waited = []                 # сколько секунд ждали каждую команду
        self.kwargs = {}

    def __call__(self, command, capture_output=True, timeout=None,
                 encoding=None, errors=None, text=False):
        self.calls.append(command)
        self.waited.append(timeout)
        self.kwargs = {"encoding": encoding, "errors": errors, "text": text}
        enc = encoding or (WINDOWS_LOCALE if text else None)
        if enc is None:                      # байтовый режим — нам не нужен
            return subprocess.CompletedProcess(command, self.code,
                                               self.stdout, self.stderr)
        how = errors or "strict"
        return subprocess.CompletedProcess(
            command, self.code,
            self.stdout.decode(enc, how), self.stderr.decode(enc, how))

    def subcommands(self):
        return [c[c.index("--log-level") + 2] for c in self.calls]


@pytest.fixture()
def copy(tmp_path):
    path = tmp_path / "sync_admin-20261012-120000.db"
    path.write_bytes(b"x" * 1024)
    return path


@pytest.fixture()
def configured(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKUP_RCLONE_REMOTE", "yandex:sync_admin_backups")
    monkeypatch.setenv("BACKUP_RCLONE_EXE", str(tmp_path / "rclone.exe"))
    monkeypatch.setenv("BACKUP_RCLONE_CONFIG", str(tmp_path / "rclone.conf"))


# --------------------------------------------------------------- кодировка

def test_the_cloud_answer_stays_readable(copy, configured, monkeypatch):
    """Главное следствие: причину можно прочитать глазами.

    Проверяем ТЕКСТ, который доедет до «Диагностики», а не то, какие аргументы
    переданы: предмет правки — то, что увидит человек.
    """
    monkeypatch.setattr(subprocess, "run", Rclone(stderr=GONE))

    problem = backup.upload_to_remote(copy)

    assert "Ресурс больше недоступен" in problem, (
        "ответ облака пришёл мозаикой — значит вывод снова читается локальной "
        "кодировкой Windows, а не UTF-8:\n" + problem)


def test_an_odd_byte_does_not_lose_the_whole_message(copy, configured, monkeypatch):
    """В выводе чужая строка, и упасть на ней значило бы потерять всё.

    `errors="replace"` ставит «?» вместо символа — несравнимо лучше, чем
    `UnicodeDecodeError` вместо сообщения. То же правило, по которому скрипты
    глушат ошибку кодирования на выводе.
    """
    class Broken(Rclone):
        def __call__(self, command, **kw):
            self.stderr = b"\xff\xfe " + "сбой".encode("utf-8")
            return super().__call__(command, **kw)

    monkeypatch.setattr(subprocess, "run", Broken(stderr=""))

    problem = backup.upload_to_remote(copy)

    assert "сбой" in problem


def test_the_fake_would_actually_catch_the_mojibake():
    """Проверка на проверку: заглушка, согласная на любую кодировку, молчит
    так же, как исправный код.

    Если «Ресурс» читается и через cp1251, то тест выше не значит ничего.
    """
    as_windows_would = "Ресурс больше недоступен.".encode("utf-8").decode(WINDOWS_LOCALE)
    assert "Ресурс" not in as_windows_would


# ------------------------------------------------------------- что это значит

@pytest.mark.parametrize("said, expected", [
    ("[410 - GoneError] Gone (Ресурс больше недоступен.)", "отвечает отказом (410)"),
    ("HTTP error 404 (404 Not Found)", "там нет"),
    ("googleapi: Error 403: Forbidden", "не пускает"),
    ("status code 503", "отвечает отказом (503)"),
    ("[507 - InsufficientStorage]", "кончилось место"),
])
def test_a_known_refusal_is_named(said, expected):
    """Отказ называется своими словами: чинятся они совершенно по-разному."""
    assert expected in backup.cloud_gist(said, "yandex:backups")


def test_a_transient_refusal_says_it_passes_by_itself():
    """410 — отказ платформы, и человеку надо сказать именно это.

    Прочитай он его как «папку удалили», он пойдёт создавать папку поверх
    живой — то самое структурное действие в ответ на проходящий отказ.
    """
    gist = backup.cloud_gist(GONE, "yandex:sync_admin_backups")

    assert "проходит само" in gist
    assert "на месте ли папка" in gist, (
        "у отказа платформы есть и второе прочтение, и умолчать о нём нельзя")


def test_an_unknown_refusal_is_not_explained_away():
    """Разбирать за облако то, чего не понимаем, мы не беремся.

    Выдуманное объяснение хуже сырого текста: по нему пойдут чинить не то.
    """
    assert backup.cloud_gist("какая-то новая беда", "yandex:backups") == ""


def test_the_raw_answer_survives_the_explanation(copy, configured, monkeypatch):
    """Причина словами И что сказал rclone — обе части обязательны.

    Без причины сырой код читается как поломка настройки; без сырого текста
    нечем проверить, что причину разобрали верно.
    """
    monkeypatch.setattr(subprocess, "run", Rclone(stderr=GONE))

    problem = backup.upload_to_remote(copy)

    assert "отвечает отказом (410)" in problem
    assert "GoneError" in problem


def test_a_platform_outage_does_not_make_us_create_anything(copy, configured,
                                                            monkeypatch):
    """И САМОЕ ГЛАВНОЕ: на проходящий отказ мы не делаем НИЧЕГО.

    Соблазн прямой — «папки нет, создадим». 12.10 папка была на месте, а 410
    приходил и на ЧТЕНИЕ: создай мы её тогда, разбирались бы потом ещё и с
    этим. Проверяем по следствию — в облако не ушло ни одной команды, кроме
    самой выгрузки.
    """
    fake = Rclone(stderr=GONE)
    monkeypatch.setattr(subprocess, "run", fake)

    backup.upload_to_remote(copy)

    assert fake.subcommands() == ["copyto"], (
        "в облако ушла лишняя команда: отказ платформы чинить структурно нельзя")


# ------------------------------------------------------------ сколько уже так

def _broken_mirror(db):
    db.add(WorkerHeartbeat(worker_name="backup", last_run_at=now_utc(),
                           last_success=True,
                           last_error="копия не доехала до зеркала: облако: 410"))


def test_a_single_bad_day_is_only_a_warning(db):
    """Сбой облака проходит сам — будить из-за него нельзя.

    Разбавь мы критичное таким, оно перестало бы значить «разбирать
    немедленно», и канал обесценился бы весь.
    """
    _broken_mirror(db)
    db.add(WorkerHeartbeat(worker_name=MIRROR_OK_MARKER, last_success=True,
                           last_run_at=now_utc() - timedelta(days=1)))
    db.commit()

    finding = report._check_backup_mirror(db)

    assert finding.level == report.WARNING
    assert "1 дн" in finding.title, finding.title


def test_a_week_without_a_second_site_is_critical(db):
    """Трое суток — уже не сбой, а состояние.

    Всё это время единственная копия лежит на том же диске, что и база, то
    есть защита, ради которой зеркало заводилось, не работает.
    """
    _broken_mirror(db)
    db.add(WorkerHeartbeat(worker_name=MIRROR_OK_MARKER, last_success=True,
                           last_run_at=now_utc() - timedelta(days=7)))
    db.commit()

    assert report._check_backup_mirror(db).level == report.CRITICAL


def test_without_the_mark_we_say_so_instead_of_guessing(db):
    """Отметки нет — давности нет, и выдумывать её нельзя.

    Объявить критичным то, про что мы не знаем даже давности, значит один раз
    напугать и навсегда приучить не читать.
    """
    _broken_mirror(db)
    db.commit()

    finding = report._check_backup_mirror(db)

    assert finding.level == report.WARNING
    assert "ни разу" in finding.title


def test_a_working_mirror_is_still_quiet(db):
    """Молчание на исправной системе — обязательное свойство отчёта."""
    db.add(WorkerHeartbeat(worker_name="backup", last_run_at=now_utc(),
                           last_success=True, last_error=None))
    db.add(WorkerHeartbeat(worker_name=MIRROR_OK_MARKER, last_success=True,
                           last_run_at=now_utc() - timedelta(days=30)))
    db.commit()

    assert report._check_backup_mirror(db) is None, (
        "давняя отметка не должна поднимать находку сама по себе: оговорки нет "
        "— значит зеркало работает")


# ------------------------------------------------------------------- /health

def test_the_mark_is_not_a_worker(web_db):
    """Отметка НЕ проверяется на протухание, и это главное про неё.

    Пишется она раз в сутки и только на удаче, а умолчание `/health` — десять
    минут. Попади она в общий список, мониторинг краснел бы через десять минут
    после первой же удачной выгрузки и держался бы красным сутки. Ровно тот
    дефект, про который в `CLAUDE.md` написано «добавил задание — впиши ему
    срок протухания»; здесь он лечится не сроком, а тем, что это не задание.
    """
    web_db.add(WorkerHeartbeat(worker_name="dispatch", last_run_at=now_utc(),
                               last_success=True))
    web_db.add(WorkerHeartbeat(worker_name=MIRROR_OK_MARKER, last_success=True,
                               last_run_at=now_utc() - timedelta(days=30)))
    web_db.commit()

    body, code = snapshot(web_db)

    assert code == 200, body
    assert MIRROR_OK_MARKER not in [w["worker"] for w in body["workers"]]


# ------------------------------------------------------- кто ставит отметку

def _run_job_backup(db, monkeypatch, *, mirror_error, configured=True):
    """Прогнать `job_backup` с подставным бэкапом и вернуть отметки."""
    from app.workers import scheduler

    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
    monkeypatch.setattr(scheduler, "last_backup", lambda: (None, 0))
    monkeypatch.setattr(scheduler, "second_site_configured", lambda _db: configured)
    monkeypatch.setattr(
        scheduler, "make_backup",
        lambda db=None: backup.BackupResult(path="/tmp/c.db", size_bytes=10,
                                            checked=True,
                                            mirror_error=mirror_error))
    scheduler.job_backup()
    return {hb.worker_name: hb for hb in db.query(WorkerHeartbeat).all()}


def test_a_delivered_copy_stamps_the_mark(db, monkeypatch):
    """Удачная выгрузка двигает отметку — без неё возраст считать не из чего."""
    marks = _run_job_backup(db, monkeypatch, mirror_error="")

    assert MIRROR_OK_MARKER in marks


def test_a_failed_upload_leaves_the_mark_alone(db, monkeypatch):
    """И это главное свойство отметки: на неудаче она НЕ двигается.

    Двинь мы её на каждом прогоне, ответ «сколько суток второй площадки нет»
    стирался бы ровно тем прогоном, который его и должен был дать: истории у
    отметки нет, она хранит одно состояние.
    """
    was = now_utc() - timedelta(days=9)
    db.add(WorkerHeartbeat(worker_name=MIRROR_OK_MARKER, last_success=True,
                           last_run_at=was))
    db.commit()

    marks = _run_job_backup(db, monkeypatch, mirror_error="облако: 410")

    assert marks[MIRROR_OK_MARKER].last_run_at == was
    assert marks["backup"].last_success is True, (
        "недоступное зеркало не делает бэкап неудачным: копия снята и проверена")
    assert "не доехала" in marks["backup"].last_error


def test_without_a_second_site_nothing_is_stamped(db, monkeypatch):
    """Площадки нет вовсе — отметка утверждала бы наличие того, чего нет.

    `mirror_error` при ненастроенной второй площадке тоже пуст, то есть по
    нему одному эти два случая неразличимы.
    """
    marks = _run_job_backup(db, monkeypatch, mirror_error="", configured=False)

    assert MIRROR_OK_MARKER not in marks


# --------------------------------------------------------------- сколько ждём

def test_the_upload_gets_its_own_patience(copy, configured, monkeypatch):
    """Выгрузке дают часы, листингу — минуты. Один предел на обе беды не годится.

    12.10 замер по WebDAV Яндекс.Диска: 111 КиБ/с, то есть копия в 305 МБ
    уезжает 46 минут ровно и без срывов. Прежние 900 с на все команды разом
    обрывали её на одиннадцати процентах, и выглядело это как «rclone не
    ответил» — то есть как зависание, которого не было.

    Обратную сторону тоже нельзя потерять: дай мы четыре часа листингу,
    повисший `lsjson` держал бы суточное задание полдня.
    """
    fake = Rclone(stdout="[]", code=0)
    monkeypatch.setattr(subprocess, "run", fake)

    backup.upload_to_remote(copy)

    waited = dict(zip(fake.subcommands(), fake.waited))
    assert waited["copyto"] == backup.RCLONE_UPLOAD_TIMEOUT
    assert waited["lsjson"] == backup.RCLONE_TIMEOUT
    assert backup.RCLONE_UPLOAD_TIMEOUT > backup.RCLONE_TIMEOUT * 4


def test_the_upload_limit_has_room_for_a_growing_database():
    """Запас считается ОТ ЗАМЕРА, а не «46 минут плюс чуть-чуть».

    База растёт примерно на 7 МБ в сутки — это минута выгрузки в день. Предел
    впритык купил бы ровно то же падение через полгода, и причину пришлось бы
    искать заново.
    """
    measured_seconds = 305 * 1024 / 111        # 305 МБ на 111 КиБ/с
    assert backup.RCLONE_UPLOAD_TIMEOUT > measured_seconds * 3


def test_a_hung_connection_is_cut_by_rclone_itself(copy, configured, monkeypatch):
    """Долгий предел НЕ должен означать четыре часа ожидания мертвеца.

    Снаружи мы видим только, что процесс жив; идут ли байты, знает сам rclone.
    Поэтому у него свои пределы на простой и на соединение — без них четыре
    часа терпения превратились бы в четыре часа висящего суточного задания.
    """
    fake = Rclone(stdout="[]", code=0)
    monkeypatch.setattr(subprocess, "run", fake)

    backup.upload_to_remote(copy)

    command = fake.calls[0]
    assert "--timeout" in command, command
    assert "--contimeout" in command, command
    assert command[command.index("--timeout") + 1].endswith("s")


def test_the_refusal_names_the_limit_that_was_hit(copy, configured, monkeypatch):
    """«Не ответил за 900 с» при пределе в четыре часа — неправда в сообщении.

    Человек читает его на «Диагностике» и по нему решает, поднимать предел или
    искать зависание.
    """
    def hang(*a, **kw):
        raise subprocess.TimeoutExpired("rclone", kw.get("timeout"))

    monkeypatch.setattr(subprocess, "run", hang)

    problem = backup.upload_to_remote(copy)

    assert str(backup.RCLONE_UPLOAD_TIMEOUT) in problem, problem


def test_the_cloud_is_named_once(copy, configured, monkeypatch):
    """«облако: облако отвечает отказом» читается как сбой самого сообщения.

    Площадку подписывает тот, кто её знает, а причина начинается с существа
    дела. 12.10 на бою это и вышло: слово задвоилось в первой же оговорке.
    """
    monkeypatch.setattr(subprocess, "run", Rclone(stderr=GONE))

    problem = backup.upload_to_remote(copy)

    assert problem.count("облако") == 1, problem
