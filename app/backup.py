"""Резервная копия базы — то единственное, чего у боевого сервера не было.

Аудит 21.09: `deploy/backup.sh` в репозитории есть, но это Linux-скрипт из
другого варианта развёртывания, а в `deploy/README_WINDOWS.md` слова «бэкап»
нет вовсе. То есть боевую базу не копировал никто.

В файле лежит всё, что не восстанавливается ниоткуда: соответствие баркодов
товарам (собиралось руками), история проведённых заказов, задания 1С с их
ответами, настройки кабинетов. 1С этого не знает, площадки — тем более. Потеря
файла означает не «откатимся на вчера», а ручную настройку каталога заново.

**Копируем штатным механизмом SQLite, а не файловой копией.** При живом WAL
часть транзакций лежит в `-wal`, и копия одного `sync_admin.db` может не
открыться вовсе или открыться без последних часов работы. `Connection.backup()`
делает согласованный снимок НА ЖИВОЙ БАЗЕ, не останавливая службы: он читает
страницы порциями и повторяет те, что изменились по ходу. Именно поэтому здесь
нет ни остановки служб, ни предупреждений про «делайте ночью».

Копия проверяется сразу после создания (`PRAGMA integrity_check` плюс чтение
таблицы товаров). Непроверенная копия — это не копия, а надежда: о том, что она
битая, узнают в тот единственный день, когда она нужна.
"""

from __future__ import annotations

import logging
import os
import re
import json
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.timeutils import now_utc

logger = logging.getLogger("sync_worker")

# Куда складывать. Отдельный каталог, а не рядом с базой: копия рядом с
# оригиналом не переживает ровно того случая, ради которого делается, —
# потери диска или каталога целиком.
BACKUP_DIR_ENV = "BACKUP_DIR"
DEFAULT_BACKUP_DIR = r"C:\sync_admin\backups"

# Вторая площадка для копий: папка синхронизации облака (Яндекс.Диск и такие
# же), сетевая шара, второй физический диск. По умолчанию ПУСТО — зеркала нет.
#
# Зачем отдельная папка, а не просто `BACKUP_DIR` внутрь облака. Копия
# снимается `Connection.backup()` СРАЗУ в целевой файл, без временного имени:
# клиент синхронизации увидел бы файл растущим и залил недописанную базу, а
# `_collapse_journal` успел бы ещё и создать рядом `-wal`/`-shm`, которые тут же
# удаляются. В облаке остался бы мусор, а какое-то время — и битая копия под
# правильным именем. Плюс клиент держит файлы открытыми, мешая `prune`.
#
# Поэтому порядок такой: снять локально, ПРОВЕРИТЬ, и только потом положить в
# зеркало — целиком и через временное имя с переименованием, чтобы под финальным
# именем файл появился уже полным.
MIRROR_DIR_ENV = "BACKUP_MIRROR_DIR"
# Сколько копий держать в зеркале. Облако обычно просторнее локального диска
# (1 ТБ против сотни гигабайт), поэтому по умолчанию глубже: 30 ежедневных и 12
# недельных — квартал. Но это ВТОРАЯ ПЛОЩАДКА, а не глубина хранения: зеркало
# повторяет то, что есть, и удаление здесь идёт по своему правилу, а не следом
# за локальным.
MIRROR_KEEP_DAILY = 30
MIRROR_KEEP_WEEKLY = 12

# --- Вторая площадка через rclone -----------------------------------------
#
# Нужна там, где папки-зеркала взять неоткуда: на арендованном сервере нет ни
# второго диска, ни соседней машины, а клиент облачной синхронизации либо не
# ставится на серверную ОС (Яндекс.Диск), либо требует живой сессии
# пользователя — то есть молча перестаёт работать после выхода из RDP.
#
# rclone решает ровно это: один исполняемый файл, никакой сессии, и — главное —
# ПОСЛЕ ВЫГРУЗКИ МОЖНО СПРОСИТЬ, ДОЕХАЛО ЛИ. У папки синхронизации этого нет
# принципиально: мы кладём файл и видим только то, что он лежит в папке.
#
# Токен облака живёт в конфиге САМОГО rclone и к нам не попадает: его выдаёт
# `rclone config` в диалоге с человеком, rclone же его и обновляет. Хранить у
# себя то, чем умеет управлять он, значит однажды разойтись с ним.
RCLONE_REMOTE_ENV = "BACKUP_RCLONE_REMOTE"
RCLONE_EXE_ENV = "BACKUP_RCLONE_EXE"
RCLONE_CONFIG_ENV = "BACKUP_RCLONE_CONFIG"
DEFAULT_RCLONE_EXE = r"C:\sync_admin\tools\rclone.exe"
# Конфиг задаётся ЯВНО, и это не придирка. `rclone config` пишет его в профиль
# ТОГО, кто запустил, — обычно администратора; служба же работает под своей
# учётной записью и этого файла не увидит вовсе. Человек настроил, проверил
# руками, всё сошлось — а суточная выгрузка молчит. Тот же класс отказа, что и
# «клиенту нужна живая сессия», поэтому путь общий и лежит рядом с приложением.
DEFAULT_RCLONE_CONFIG = r"C:\sync_admin\rclone.conf"
# Выгрузка идёт в фоновом задании, и повиснуть на ней нельзя: воркер в это время
# не принимает заказы. Пятнадцати минут хватает на сотни мегабайт по любому
# разумному каналу, а всё, что дольше, — это уже не медленная сеть, а зависание.
RCLONE_TIMEOUT = 900

# Сколько храним. Ежедневных две недели — этого хватает, чтобы заметить порчу
# данных, которую видно не сразу (перепутанный мэппинг, неверный импорт).
# Плюс по одной копии на неделю за два месяца: они ловят то, что заметили
# поздно, и стоят копейки против цены «восстанавливать нечего».
KEEP_DAILY = 14
KEEP_WEEKLY = 8

NAME_PREFIX = "sync_admin-"
NAME_RE = re.compile(r"^sync_admin-(\d{8})-(\d{6})\.db$")


@dataclass
class BackupResult:
    path: str
    size_bytes: int
    checked: bool
    removed: int = 0
    error: str = ""
    # Куда отложен негодный файл, если попытка сорвалась. Он остаётся на диске
    # для разбора, но под именем, которое уборка и `last_backup` за копию не
    # считают.
    path_kept: str = ""
    # Что пошло не так при зеркалировании. Пустая строка — либо зеркало не
    # настроено, либо копия туда доехала. САМ БЭКАП этим не портится: локальная
    # копия уже снята и проверена, и объявить её неудачной из-за недоступной
    # сетевой папки значило бы поднять тревогу о том, чего не случилось.
    mirror_error: str = ""

    @property
    def ok(self) -> bool:
        return self.checked and not self.error


def backup_dir() -> Path:
    return Path(os.environ.get(BACKUP_DIR_ENV) or DEFAULT_BACKUP_DIR)


def database_path(database_url: str | None = None) -> Path | None:
    """Путь к файлу SQLite из DATABASE_URL. None — база не файловая.

    Бэкапить умеем только SQLite: у PostgreSQL свой механизм, и делать вид,
    что мы его прикрыли, опаснее, чем честно ничего не делать.
    """
    url = database_url if database_url is not None else os.environ.get("DATABASE_URL", "")
    if not url.startswith("sqlite"):
        return None
    tail = url.split("///", 1)[-1]
    if not tail or tail == ":memory:":
        return None
    return Path(tail)


def _set_aside(target: Path) -> str:
    """Убрать негодный файл из-под имени копии, не удаляя его.

    `last_backup()` и `prune()` различают копии ПО ИМЕНИ, и оборванный файл
    правильного вида проходил у них за полноценную копию: задание при следующем
    запуске видело «свежая копия уже есть», писало зелёный heartbeat и не
    пробовало снова двадцать часов, а находка отчёта молчала двое суток. Копии
    нет — и все три механизма контроля утверждают обратное.

    Удалять нельзя: по этому файлу разбираются, что именно пошло не так (кончился
    диск, права, повреждение). Достаточно вывести его из-под шаблона имён.
    """
    if not target.exists():
        _remove_companions(target)
        return ""
    bad = target.with_suffix(target.suffix + ".bad")
    try:
        if bad.exists():
            bad.unlink()
        target.replace(bad)
    except OSError:
        # Не переименовалось — беда меньшая, чем потерянная копия, но соврать об
        # этом нельзя: возвращаем пустую строку, ошибка и так уже в результате.
        return ""
    _remove_companions(target)
    return str(bad)


def _remove_companions(target: Path) -> None:
    """Убрать спутников файла: `-wal`, `-shm`, `-journal`.

    `-journal` добавлен к списку после аудита: он остаётся от ОБОРВАННОЙ копии, а
    уборка его не видела — такие файлы копились бы вечно.
    """
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            target.with_name(target.name + suffix).unlink()
        except OSError:
            pass


def _collapse_journal(path: Path) -> None:
    """Свести копию к одному файлу: журнал внутрь, спутники убрать.

    Ошибку глотаем намеренно и с последствиями: копия уже снята и целостна,
    а спутники — вопрос опрятности. Уронить из-за них снятый бэкап значило бы
    потерять важное ради второстепенного. Если спутники всё же остались, их
    уберёт `prune` вместе с самой копией.
    """
    try:
        con = sqlite3.connect(path)
        try:
            con.execute("PRAGMA journal_mode=DELETE")
            con.commit()
        finally:
            con.close()
    except Exception as e:
        logger.warning("бэкап: журнал копии не свёрнут (%s): %s", path, e)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            try:
                sidecar.unlink()
            except OSError:
                pass


def _verify(path: Path) -> str:
    """Проверка копии. Пустая строка — всё в порядке, иначе причина.

    `integrity_check` проверяет структуру, а чтение товаров — что данные на
    месте: битый, но структурно целый файл бывает, и он выглядит нормально до
    первого обращения.
    """
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            status = con.execute("PRAGMA integrity_check").fetchone()
            if not status or status[0] != "ok":
                return f"integrity_check: {status[0] if status else 'нет ответа'}"
            con.execute("SELECT COUNT(*) FROM products").fetchone()
        finally:
            con.close()
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return ""


def _parse_moment(name: str) -> datetime | None:
    match = NAME_RE.match(name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def names_to_drop(names: list[str], keep_daily: int = KEEP_DAILY,
                  keep_weekly: int = KEEP_WEEKLY) -> list[str]:
    """Какие копии лишние. Правило ОДНО на все площадки — и это принципиально.

    Локальная папка и облако чистятся разными механизмами (`unlink` против
    вызова rclone), но решать, что удалить, обязаны одинаково: разойдись они, в
    облаке лежало бы не то, что обещано человеку, и узнал бы он об этом ровно в
    тот день, когда полез восстанавливаться.

    ПО ОДНОЙ НА КАЛЕНДАРНЫЙ ДЕНЬ, а не «первые keep_daily файлов».

    Было второе, и обещание «вернуться на любой из недавних дней» не
    выполнялось: стоило копиям пойти чаще раза в сутки — а `scripts/backup_db.py`
    прямо предлагается для планировщика Windows и, в отличие от задания, никакого
    `BACKUP_MIN_GAP` не проверяет, — и четырнадцать «ежедневных» превращались в
    четырнадцать ПОСЛЕДНИХ ЧАСОВ. Замер: 24 почасовых копии за сутки плюс 60
    суточных → после уборки осталось 15 последних часов и провал в неделю сразу
    за ними. Порчу данных (перепутанный мэппинг, неверный импорт) замечают через
    день-два, и восстанавливать было бы не из чего.

    Имя, которое не разбирается, НЕ попадает в ответ никогда: и в каталоге, и в
    облаке может лежать копия, положенная человеком руками.
    """
    parsed = []
    for name in names:
        moment = _parse_moment(name)
        if moment is not None:
            parsed.append((moment, name))
    parsed.sort(reverse=True)

    keep: set[str] = set()
    days_seen: set = set()
    rest: list[tuple] = []
    for moment, name in parsed:
        day = moment.date()
        if day not in days_seen and len(days_seen) < keep_daily:
            days_seen.add(day)
            keep.add(name)
        else:
            rest.append((moment, name))

    weeks_seen: set[tuple] = set()
    for moment, name in rest:
        week = moment.isocalendar()[:2]
        if week not in weeks_seen and len(weeks_seen) < keep_weekly:
            weeks_seen.add(week)
            keep.add(name)

    return [name for _, name in parsed if name not in keep]


def prune(directory: Path, keep_daily: int = KEEP_DAILY,
          keep_weekly: int = KEEP_WEEKLY) -> int:
    """Удалить лишние копии. Возвращает сколько удалено.

    Правило двухслойное намеренно. Последние `keep_daily` копий — по одной на
    день, чтобы вернуться на любой из недавних дней. Дальше — по одной на
    календарную неделю: порчу данных замечают и через месяц, а хранить тридцать
    ежедневных копий ради этого незачем.

    Файл с неразборчивым именем не трогаем никогда: в каталоге бэкапов может
    лежать копия, положенная человеком руками, и удалить её значит сделать ровно
    то, от чего этот модуль защищает.
    """
    files = []
    for path in directory.glob(f"{NAME_PREFIX}*.db"):
        moment = _parse_moment(path.name)
        if moment is not None:
            files.append((moment, path))
    files.sort(reverse=True)

    drop = set(names_to_drop([p.name for _, p in files], keep_daily, keep_weekly))

    removed = 0
    for _, path in files:
        if path.name in drop:
            try:
                path.unlink()
                removed += 1
            except OSError as e:
                logger.warning("бэкап: не удалось удалить %s: %s", path, e)
                continue
            # Спутники, если вдруг остались от старых копий: сами по себе они
            # под шаблон имени не подходят и иначе лежали бы вечно. `-journal`
            # остаётся от ОБОРВАННОЙ копии — его не видели вовсе.
            _remove_companions(path)
    return removed


def _copy_database(source: Path, target: Path) -> None:
    """Снять копию живой базы штатным механизмом SQLite.

    ОДНИМ шагом, а не порциями. Было `pages=1000`, и посылка под этим была
    неверной: «длинная блокировка на живой базе нам не нужна». В WAL читатель
    писателя не блокирует вовсе, так что порции не покупали ничего — а стоили
    сходимости. `Connection.backup()` при чужой записи между шагами начинает
    копирование ЗАНОВО, и на живой базе это означает, что копия может не сняться
    никогда: замер на базе в 150 МБ при внешнем писателе дал десять коммитов в
    секунду → 33,5 с и 1448 перезапусков, двадцать и сто коммитов в секунду → не
    завершилось за минуту вовсе. Задание при этом не падает: оно крутится на
    100% CPU, занимает поток планировщика, heartbeat не обновляет, а `-wal`
    источника растёт всё это время, потому что длинный читатель не даёт его
    чекпойнтить.

    Частота такая в системе есть: сама чистка даёт около тридцати пяти коммитов
    в секунду и стартует через четыре минуты после бэкапа, а приём заказов
    коммитит на каждый заказ.

    Одним шагом на том же стенде — 1,0 с при любой нагрузке; проверено и на
    109 МБ при ста коммитах в секунду: 1,5 с.
    """
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _setting(db, name: str, default: str = "") -> str:
    """Значение настройки: страница «Уведомления», иначе `.env`, иначе умолчание.

    `db` передаёт тот, у кого сессия уже есть (суточное задание). Её нет у
    `scripts/backup_db.py` — и это не оплошность вызывающего, а его смысл: этот
    скрипт запускают руками ИМЕННО ТОГДА, когда что-то идёт не так, и требовать
    от него исправной базы значило бы отнять копию в единственный час, когда она
    по-настоящему нужна. Саму базу он копирует файлом через `sqlite3`, минуя
    SQLAlchemy, поэтому снять копию он может и с той базы, которую приложение
    открыть не в состоянии (нет `SECRETS_ENCRYPTION_KEY`, побит пул, занят порт).

    Отсюда двойная страховка: сессию заводим свою, а любую неудачу на этом пути
    ГЛОТАЕМ и читаем окружение. Хуже недоступной настройки только копия, которая
    из-за неё не снялась.
    """
    if db is not None:
        try:
            from app import settings_store

            return settings_store.get(db, name)
        except Exception:                            # noqa: BLE001 — см. докстринг
            pass
    else:
        try:
            from app import settings_store
            from app.database import SessionLocal

            own = SessionLocal()
            try:
                return settings_store.get(own, name)
            finally:
                own.close()
        except Exception:                            # noqa: BLE001 — см. докстринг
            pass
    return (os.environ.get(name) or default).strip()


def _setting_int(db, name: str, default: int) -> int:
    """Число из настройки. Мусор и ноль — это умолчание, а не «не хранить».

    Опечатка в поле не должна уметь стереть зеркало целиком: ноль у `prune`
    означал бы «оставить ноль копий», то есть удалить всё, ради чего вторая
    площадка и заводилась.
    """
    try:
        value = int(_setting(db, name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def mirror_dir(db=None) -> Path | None:
    """Папка зеркала, если настроена."""
    raw = _setting(db, MIRROR_DIR_ENV)
    return Path(raw) if raw else None


def mirror_backup(source: Path, directory: Path | None = None, db=None) -> str:
    """Положить ГОТОВУЮ копию на вторую площадку. Пустая строка — всё хорошо.

    Копируем через временное имя и переименовываем: под финальным именем файл
    обязан появиться уже целым. Клиент облачной синхронизации смотрит за папкой
    и заливает всё, что в ней меняется, — увидев файл растущим, он отправил бы в
    облако недописанную базу. Переименование в пределах одной папки атомарно, и
    клиент видит сразу готовый файл.

    Имя временного файла НЕ похоже на копию (`.part`): уборка ищет
    `sync_admin-*.db`, и оборванный кусок под правильным именем прошёл бы у неё
    и у `last_backup` за полноценную копию — ровно тот дефект, из-за которого
    сорвавшаяся попытка откладывается в `.bad`.

    Ошибку возвращаем, а не бросаем: локальная копия к этому моменту снята и
    проверена, и объявлять бэкап неудачным из-за недоступной сетевой папки
    нельзя — это подняло бы тревогу о том, чего не случилось. Но и глотать
    молча тоже нельзя: зеркало, о котором мы думаем, что оно есть, хуже
    отсутствующего.
    """
    # `db` передаём дальше ОБЯЗАТЕЛЬНО. Забыв его здесь, мы получили бы самый
    # неприятный из возможных исходов: папку, заданную на странице, зеркало не
    # видит, путь берётся из окружения, а при пустом окружении функция
    # возвращает «всё хорошо» — бэкап зелёный, `mirror_error` пуст, находка
    # отчёта молчит, и второй площадки просто нет. Поймано тестом.
    directory = directory or mirror_dir(db)
    if directory is None:
        return ""
    # Зеркало, указывающее на каталог самих копий, — НЕ зеркало.
    #
    # Отказ диска унесёт обе «площадки» разом, то есть защита, ради которой всё
    # заводилось, не работает вовсе. А выглядит это исправнее исправного:
    # копия действительно «доезжает» (файл переписывается сам в себя), ошибки
    # нет, `mirror_error` пуст, находка отчёта молчит, скрипт бодро печатает
    # «зеркало: <путь>». Поймать глазами можно, только сличив два пути в разных
    # разделах страницы, — а кто их сличает, тот и не ошибается.
    #
    # Поэтому отказ, и с объяснением: молча проигнорировать настройку нельзя,
    # человек её задал и считает работающей.
    try:
        same = directory.resolve() == source.parent.resolve()
    except OSError:                                  # noqa: BLE001
        same = False
    if same:
        return ("папка: указана на каталог самих копий "
                f"({directory}) — это не вторая площадка: отказ диска унесёт "
                "и базу, и обе копии. Укажите другой диск, сетевую папку или "
                "облако, либо очистите поле")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / source.name
        staging = directory / (source.name + ".part")
        if staging.exists():
            staging.unlink()
        shutil.copyfile(source, staging)
        os.replace(staging, target)
    except Exception as e:                           # noqa: BLE001 — см. docstring
        logger.warning("зеркало бэкапа недоступно (%s): %s", directory, e)
        # Площадок ДВЕ, и каждая жалоба обязана называть свою. Иначе одна
        # строка «копия не доехала до зеркала» читается как «второй площадки
        # нет вовсе» — при том, что вторая могла сработать. Облако свои
        # сообщения подписывает так же.
        return f"папка: {type(e).__name__}: {e}"[:200]

    try:
        prune(directory,
              keep_daily=_setting_int(db, "BACKUP_MIRROR_KEEP_DAILY", MIRROR_KEEP_DAILY),
              keep_weekly=_setting_int(db, "BACKUP_MIRROR_KEEP_WEEKLY", MIRROR_KEEP_WEEKLY))
    except Exception as e:                           # noqa: BLE001
        # Копия УЖЕ в зеркале — это главное. Неубранные старые файлы место
        # занимают, но ничего не ломают, а уронить из-за них доставленную копию
        # значило бы потерять важное ради второстепенного.
        logger.warning("зеркало: старые копии не убраны (%s): %s", directory, e)
    return ""


# ---------------------------------------------------------------------------
# Вторая площадка через rclone
# ---------------------------------------------------------------------------

def rclone_settings(db=None) -> tuple[str, str, str] | None:
    """(исполняемый файл, удалённый путь, конфиг) или None, если не настроено.

    Ключевое здесь — `remote`: строка вида `yandex:sync_admin_backups`, где
    `yandex` заведён человеком через `rclone config`. Ни токена, ни пароля мы не
    видим и не храним.
    """
    remote = _setting(db, RCLONE_REMOTE_ENV)
    if not remote:
        return None
    exe = _setting(db, RCLONE_EXE_ENV) or DEFAULT_RCLONE_EXE
    config = _setting(db, RCLONE_CONFIG_ENV) or DEFAULT_RCLONE_CONFIG
    return exe, remote.rstrip("/"), config


def _run_rclone(exe: str, config: str, args: list[str]) -> tuple[bool, str]:
    """Позвать rclone. (получилось, что сказал).

    Вывод обрезаем: он уезжает в `last_error` успешной отметки, а её читает и
    «Диагностика», и находка отчёта — простыня там вытеснила бы всё остальное.
    """
    command = [exe, "--config", config, "--log-level", "ERROR"] + args
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=RCLONE_TIMEOUT)
    except FileNotFoundError:
        return False, f"rclone не найден: {exe}"
    except subprocess.TimeoutExpired:
        return False, f"rclone не ответил за {RCLONE_TIMEOUT} с"
    except Exception as e:                           # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    if done.returncode != 0:
        said = (done.stderr or done.stdout or "").strip().replace("\n", "; ")
        return False, f"rclone вернул {done.returncode}: {said[:300]}"
    return True, (done.stdout or "")


def _remote_listing(exe: str, config: str, remote: str) -> tuple[list[dict], str]:
    ok, said = _run_rclone(exe, config, ["lsjson", remote])
    if not ok:
        return [], said
    try:
        return json.loads(said or "[]"), ""
    except ValueError as e:
        return [], f"ответ rclone не разобран: {e}"


def upload_to_remote(source: Path, db=None) -> str:
    """Выгрузить ГОТОВУЮ копию в облако. Пустая строка — всё хорошо.

    И СРАЗУ СПРОСИТЬ, ДОЕХАЛА ЛИ. Это главное отличие от папки синхронизации и
    единственная причина возиться с rclone: там мы кладём файл и знаем ровно
    одно — что он лежит в папке. Доехал ли он в облако, не видно ниоткуда, и
    отсутствие второй площадки выглядит точно так же, как её наличие. Здесь
    после выгрузки читается список удалённой папки и сверяется размер: копия,
    которую мы не прочитали на той стороне, — это надежда, а не копия. Ровно то
    же правило, по которому локальная копия проверяется `integrity_check` и
    чтением товаров.

    Ошибку возвращаем, а не бросаем, и складываем её туда же, куда ошибку
    папки-зеркала: локальная копия к этому моменту снята и проверена, и
    объявлять бэкап неудачным из-за недоступного облака нельзя. Читатель у этого
    текста уже есть — `report._check_backup_mirror`.
    """
    settings = rclone_settings(db)
    if settings is None:
        return ""
    exe, remote, config = settings

    ok, said = _run_rclone(exe, config,
                           ["copyto", str(source), f"{remote}/{source.name}"])
    if not ok:
        return f"облако: {said}"

    entries, problem = _remote_listing(exe, config, remote)
    if problem:
        # Выгрузка прошла, а проверить не вышло. Это НЕ успех: сказать «копия в
        # облаке», не увидев её там, значит вернуться ровно к тому, от чего
        # уходили.
        return f"облако: копия отправлена, но проверить не удалось — {problem}"

    sizes = {e.get("Name"): e.get("Size") for e in entries if isinstance(e, dict)}
    if source.name not in sizes:
        return f"облако: копии {source.name} там нет, хотя выгрузка прошла без ошибки"
    expected = source.stat().st_size
    if sizes[source.name] != expected:
        return (f"облако: размер не сошёлся — там {sizes[source.name]} байт, "
                f"у нас {expected}")

    # Уборка в облаке — тем же правилом, что и локально. Сбой уборки выгрузку НЕ
    # роняет: копия уже доставлена и проверена, а лишние старые файлы занимают
    # место, но ничего не ломают.
    drop = names_to_drop(list(sizes),
                         keep_daily=_setting_int(db, "BACKUP_MIRROR_KEEP_DAILY",
                                                 MIRROR_KEEP_DAILY),
                         keep_weekly=_setting_int(db, "BACKUP_MIRROR_KEEP_WEEKLY",
                                                  MIRROR_KEEP_WEEKLY))
    for name in drop:
        gone, why = _run_rclone(exe, config, ["deletefile", f"{remote}/{name}"])
        if not gone:
            logger.warning("облако: не удалось удалить %s: %s", name, why)
    return ""


def make_backup(database_url: str | None = None,
                directory: Path | None = None, db=None) -> BackupResult:
    """Снять копию, проверить её и подчистить старые.

    Битая копия НЕ удаляется и остаётся на диске: она может пригодиться для
    разбора, а главное — её присутствие вместе с ошибкой в журнале честнее, чем
    пустой каталог, по которому не понять, была попытка или нет. Но лежит она под
    расширением `.bad` (`_set_aside`): под именем копии она проходила за
    полноценную у `last_backup` и у задания, и одна сорвавшаяся попытка делала
    мониторинг зелёным на сутки при отсутствующей копии.
    """
    source = database_path(database_url)
    if source is None:
        return BackupResult(path="", size_bytes=0, checked=False,
                            error="база не SQLite — копию снимать нечем")
    if not source.exists():
        return BackupResult(path="", size_bytes=0, checked=False,
                            error=f"файла базы нет: {source}")

    directory = directory or backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{NAME_PREFIX}{now_utc():%Y%m%d-%H%M%S}.db"

    try:
        _copy_database(source, target)
    except Exception as e:
        # Недоснятую копию УБИРАЕМ ИЗ-ПОД ИМЕНИ КОПИИ. Она остаётся на диске для
        # разбора, но под расширением `.bad`, и это принципиально: `last_backup`
        # читает только ИМЕНА, поэтому оборванный файл правильного вида выглядел
        # свежей копией — и `job_backup` при следующем запуске видел «копия уже
        # есть», писал зелёный heartbeat и не пробовал снова двадцать часов, а
        # находка отчёта молчала двое суток. Копии нет, а все три механизма
        # контроля говорят, что всё хорошо.
        return BackupResult(path=str(target), size_bytes=0, checked=False,
                            error=f"копирование не удалось: {type(e).__name__}: {e}",
                            path_kept=_set_aside(target))

    # Копия обязана быть ОДНИМ файлом. `backup()` переносит и режим журнала, то
    # есть копия тоже оказывается в WAL, и рядом с ней появляются `-wal` и
    # `-shm`. 21.09 на бою так и вышло: три файла вместо одного. Беда не в
    # красоте — уборка старых копий ищет `sync_admin-*.db` и спутников не видит,
    # они копились бы вечно; а копия, которую унесли без спутников, у читателя
    # вызвала бы вопросы на ровном месте. `journal_mode=DELETE` дописывает
    # журнал в сам файл и спутников удаляет.
    _collapse_journal(target)

    problem = _verify(target)
    size = target.stat().st_size if target.exists() else 0
    if problem:
        return BackupResult(path=str(target), size_bytes=size, checked=False,
                            error=f"копия не прошла проверку: {problem}",
                            path_kept=_set_aside(target))

    removed = prune(directory)
    # Зеркало — ПОСЛЕ проверки: в облако уезжает только копия, которую мы уже
    # прочитали и признали целой. Непроверенная копия на второй площадке — это
    # две надежды вместо одной.
    # Две площадки независимы: папка может быть настроена, облако нет, и
    # наоборот. Ошибки складываем в ОДНО поле — у него уже есть читатель
    # (`report._check_backup_mirror`), и заводить второе значило бы завести
    # находку без читателя, то есть ровно тот дефект, который чинили весь день.
    problems = [p for p in (mirror_backup(target, db=db),
                            upload_to_remote(target, db=db)) if p]
    mirror_error = "; ".join(problems)
    return BackupResult(path=str(target), size_bytes=size, checked=True,
                        removed=removed, mirror_error=mirror_error)


def last_backup(directory: Path | None = None) -> tuple[datetime | None, int]:
    """(время последней копии, сколько копий всего). Для /health и отчёта."""
    directory = directory or backup_dir()
    if not directory.exists():
        return None, 0
    moments = [m for m in (_parse_moment(p.name)
                           for p in directory.glob(f"{NAME_PREFIX}*.db"))
               if m is not None]
    return (max(moments) if moments else None), len(moments)
