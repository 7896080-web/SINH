"""Страница «Уведомления»: каналы задаёт человек, а не файл на сервере.

Тесты здесь не про то, «сохраняется ли значение» — про то, из-за чего механизм
молча перестал бы работать: значение вернулось из `.env` после того, как его
стёрли; секрет уехал на страницу и затёрся маской; опечатка в числе отключила
дедупликацию; правка не подействовала, потому что настройку прочитали на
импорте.
"""
import pytest

from app import alerts, settings_store
from app.models import AppSetting, AuditLog


# --------------------------------------------------------------------------
# Откуда берётся значение
# --------------------------------------------------------------------------

def test_a_saved_value_wins_over_the_env_file(db, monkeypatch):
    """Забытая строка в `.env` не должна молча отменять правку на странице.

    Иначе человек чинил бы то, что не ломалось: страница показывает одно,
    уведомление уходит по другому.
    """
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "из-файла")
    settings_store.set_value(db, "TELEGRAM_CHAT_ID", "со-страницы")
    db.commit()

    assert settings_store.get(db, "TELEGRAM_CHAT_ID") == "со-страницы"


def test_the_env_file_is_used_while_nothing_is_saved(db, monkeypatch):
    """Установка, где каналы прописаны файлом, обязана работать без действий.

    Обновление не должно выключать уведомления.
    """
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "из-файла")
    assert settings_store.get(db, "TELEGRAM_CHAT_ID") == "из-файла"


def test_clearing_a_field_does_not_bring_the_env_value_back(db, monkeypatch):
    """ГЛАВНЫЙ случай: стёртое поле обязано остаться стёртым.

    Считай мы пустую строку за «не задано», значение из `.env` вернулось бы, и
    тревоги продолжили бы уходить туда, откуда адресата только что убрали, —
    при пустом поле на странице. Немой отказ в самую дорогую сторону.
    """
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "из-файла")
    settings_store.set_value(db, "TELEGRAM_CHAT_ID", "")
    db.commit()

    assert settings_store.get(db, "TELEGRAM_CHAT_ID") == ""
    # Строка обязана остаться: именно её наличие и означает «стёрто осознанно».
    assert db.query(AppSetting).filter(AppSetting.key == "TELEGRAM_CHAT_ID").count() == 1


def test_the_page_says_a_value_came_from_the_env_file(db, monkeypatch):
    """Поле из `.env` правится не здесь — молчать об этом нельзя.

    Человек стёр бы его, увидел заполненным после перезагрузки и решил, что
    интерфейс не работает.
    """
    monkeypatch.setenv("ALERT_SMTP_HOST", "smtp.example")
    cards = settings_store.as_cards(db)
    field = next(f for c in cards for f in c["fields"] if f["name"] == "ALERT_SMTP_HOST")

    assert field["from_env"] is True

    settings_store.set_value(db, "ALERT_SMTP_HOST", "smtp.other")
    db.commit()
    cards = settings_store.as_cards(db)
    field = next(f for c in cards for f in c["fields"] if f["name"] == "ALERT_SMTP_HOST")
    assert field["from_env"] is False


# --------------------------------------------------------------------------
# Секреты
# --------------------------------------------------------------------------

def test_a_secret_is_stored_encrypted_and_never_in_the_open_column(db):
    """Токен бота в открытой колонке — это утечка, которую не видно.

    Заметить её можно только заглянув в базу, а случиться она может один раз и
    навсегда. Колонку выбирает описание поля, а не вызывающий код.
    """
    settings_store.set_value(db, "TELEGRAM_BOT_TOKEN", "123:секрет")
    db.commit()

    row = db.query(AppSetting).filter(AppSetting.key == "TELEGRAM_BOT_TOKEN").one()
    assert row.value is None
    assert row.encrypted_value and "секрет" not in row.encrypted_value
    assert settings_store.get(db, "TELEGRAM_BOT_TOKEN") == "123:секрет"


def test_a_secret_never_reaches_the_page_in_full(db):
    """Страница открыта любому пользователю админки, и через плечо читают так же.

    А в поле ввода маску подставлять нельзя ещё и потому, что первое же
    «Сохранить» записало бы её ВМЕСТО токена.
    """
    settings_store.set_value(db, "TELEGRAM_BOT_TOKEN", "1234567890:ABCDEFGH")
    db.commit()

    field = next(f for c in settings_store.as_cards(db)
                 for f in c["fields"] if f["name"] == "TELEGRAM_BOT_TOKEN")

    assert field["value"] == ""
    assert "ABCDEFGH" not in field["masked"]
    assert field["is_set"] is True


def test_an_unreadable_secret_does_not_break_alerts(db):
    """Сменили `SECRETS_ENCRYPTION_KEY` — канал обязан стать ненастроенным.

    А не уронить задание уведомлений: тогда замолчало бы и всё остальное,
    включая сообщения о настоящих поломках.
    """
    db.add(AppSetting(key="TELEGRAM_BOT_TOKEN", encrypted_value="не-фернет"))
    db.commit()

    assert settings_store.get(db, "TELEGRAM_BOT_TOKEN") == ""
    assert alerts.telegram_configured(db) is False


# --------------------------------------------------------------------------
# Что видят уведомления
# --------------------------------------------------------------------------

def test_a_channel_saved_on_the_page_turns_the_alerts_on(db, monkeypatch):
    """Ради этого всё и затевалось: правка действует без перезапуска службы.

    `.env` читается ОДИН РАЗ на импорте, и про `nssm restart` забывают — а
    забыв, человек уверен, что канал настроен, пока система молчит.
    """
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    assert alerts.configured_channels(db) == []

    settings_store.set_value(db, "TELEGRAM_BOT_TOKEN", "t")
    settings_store.set_value(db, "TELEGRAM_CHAT_ID", "c")
    db.commit()

    assert alerts.configured_channels(db) == ["telegram"]


def test_the_saved_values_are_the_ones_actually_sent_with(db, monkeypatch):
    """Страница и отправка обязаны читать ОДНО И ТО ЖЕ.

    Разойдись они, человек правил бы поле, видел бы его сохранённым, а тревога
    уходила бы по старому адресу.
    """
    settings_store.set_value(db, "TELEGRAM_BOT_TOKEN", "токен-1")
    settings_store.set_value(db, "TELEGRAM_CHAT_ID", "чат-1")
    db.commit()

    seen = {}
    monkeypatch.setattr(alerts, "_send_telegram",
                        lambda cfg, s, b: seen.update(cfg))

    sent, failed = alerts.deliver(db, "тема", "текст")

    assert sent == ["telegram"] and failed == []
    assert seen["TELEGRAM_BOT_TOKEN"] == "токен-1"
    assert seen["TELEGRAM_CHAT_ID"] == "чат-1"


def test_a_nonsense_repeat_interval_does_not_disable_deduplication(db):
    """Ноль в поле «напоминать раз в» значил бы «каждый цикл» — то есть каждые
    пять минут одно и то же.

    Ровно тот поток, от которого дедупликация и защищает: получив пять
    одинаковых сообщений в час, канал отключают, и молчать начинает всё.
    Опечатка в поле не должна уметь снять защиту.
    """
    from datetime import timedelta

    for bad in ("0", "-3", "", "шесть"):
        settings_store.set_value(db, "ALERT_REPEAT_HOURS", bad)
        db.commit()
        assert alerts.repeat_after(db) == timedelta(hours=alerts.DEFAULT_REPEAT_HOURS), bad

    settings_store.set_value(db, "ALERT_REPEAT_HOURS", "2")
    db.commit()
    assert alerts.repeat_after(db) == timedelta(hours=2)


def test_a_nonsense_smtp_port_does_not_lose_the_alarm(db, monkeypatch):
    """Опечатка в порту не должна ронять отправку: берём умолчание."""
    settings_store.set_value(db, "ALERT_SMTP_HOST", "smtp.example")
    settings_store.set_value(db, "ALERT_EMAIL_TO", "a@example")
    settings_store.set_value(db, "ALERT_SMTP_PORT", "пятьсот")
    db.commit()

    ports = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            ports.append(port)

        def starttls(self): pass
        def send_message(self, m): pass
        def quit(self): pass

    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)

    sent, failed = alerts.deliver(db, "тема", "текст")

    assert sent == ["email"], failed
    assert ports == [587]


# --------------------------------------------------------------------------
# Страница
# --------------------------------------------------------------------------

def test_the_page_opens_and_offers_both_channels(logged_in_client, web_db):
    page = logged_in_client.get("/notifications").text

    assert "Telegram" in page
    assert "TELEGRAM_BOT_TOKEN" in page
    assert "ALERT_SMTP_HOST" in page
    assert "ALERT_HEARTBEAT_URL" in page


def test_the_page_says_out_loud_that_no_one_will_be_called(logged_in_client, web_db,
                                                           monkeypatch):
    """Ненастроенный канал выглядит точно так же, как исправный и молчащий.

    Пустая страница обязана читаться как «вас никто не позовёт».
    """
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                 "ALERT_SMTP_HOST", "ALERT_EMAIL_TO"):
        monkeypatch.delenv(name, raising=False)

    page = logged_in_client.get("/notifications").text

    assert "никого не позовёт" in page


def test_saving_through_the_page_works_and_is_logged(logged_in_client, web_db):
    """Кто сменил адресата тревог — вопрос, который однажды зададут."""
    resp = logged_in_client.post("/notifications/save",
                                 data={"name": "TELEGRAM_CHAT_ID", "value": "-100500"},
                                 follow_redirects=True)
    assert resp.status_code == 200

    row = web_db.query(AppSetting).filter(AppSetting.key == "TELEGRAM_CHAT_ID").one()
    assert row.value == "-100500"

    entry = web_db.query(AuditLog).filter(
        AuditLog.action == "alert_setting_changed").one()
    assert "ID чата" in entry.details


def test_a_secret_value_never_lands_in_the_audit_log(logged_in_client, web_db):
    """Журнал действий открыт любому пользователю админки.

    Токену бота там не место — иначе страница, заведённая ради того, чтобы
    убрать секрет из открытого файла, положила бы его в открытый журнал.
    """
    logged_in_client.post("/notifications/save",
                          data={"name": "TELEGRAM_BOT_TOKEN",
                                "value": "1234567890:ОЧЕНЬ-СЕКРЕТНО"},
                          follow_redirects=True)

    entries = web_db.query(AuditLog).all()
    assert entries
    assert all("ОЧЕНЬ-СЕКРЕТНО" not in (e.details or "") for e in entries)


def test_an_unknown_setting_is_refused(logged_in_client, web_db):
    """Имя поля приходит формой — записывать по нему что угодно нельзя."""
    logged_in_client.post("/notifications/save",
                          data={"name": "DATABASE_URL", "value": "чужое"},
                          follow_redirects=True)

    assert web_db.query(AppSetting).count() == 0
    with pytest.raises(KeyError):
        settings_store.set_value(web_db, "DATABASE_URL", "чужое")


def test_clearing_an_env_value_is_a_change_and_reaches_the_log(db, monkeypatch):
    """Стирание поля, пришедшего из `.env`, — это правка, а не пустое действие.

    Читай мы прежнее значение ПОСЛЕ создания строки, оно бралось бы уже из
    свежей пустой строки: эффективное значение сменилось бы с «из файла» на
    пустое, а страница ответила бы «без изменений» и не написала бы в журнал.
    То самое действие, ради которого журнал и нужен (кто перестал получать
    тревоги), прошло бы молча — да ещё и с уверением, что ничего не произошло.
    """
    monkeypatch.setenv("ALERT_EMAIL_TO", "старый@example")

    assert settings_store.set_value(db, "ALERT_EMAIL_TO", "") is True
    db.commit()
    assert settings_store.get(db, "ALERT_EMAIL_TO") == ""

    # А повтор того же действия изменением уже не считается.
    assert settings_store.set_value(db, "ALERT_EMAIL_TO", "") is False


# --------------------------------------------------------------------------
# Зеркало резервных копий
# --------------------------------------------------------------------------

def test_the_mirror_folder_is_taken_from_the_page(db, tmp_path, monkeypatch):
    """Ради этого перенос и делался: правка действует без перезапуска службы."""
    from app import backup

    monkeypatch.delenv("BACKUP_MIRROR_DIR", raising=False)
    assert backup.mirror_dir(db) is None

    settings_store.set_value(db, "BACKUP_MIRROR_DIR", str(tmp_path / "cloud"))
    db.commit()

    assert backup.mirror_dir(db) == tmp_path / "cloud"


def test_clearing_the_mirror_does_not_resurrect_the_env_path(db, tmp_path, monkeypatch):
    """Та же ловушка, что и с каналами, но цена другая.

    Вернись путь из `.env`, копии продолжили бы уезжать в папку, которую человек
    со страницы убрал, — а он при этом видел бы пустое поле и считал, что
    зеркала нет. Ошибка в обе стороны опасна: и «пишем туда, куда не просили»,
    и «думаем, что не пишем».
    """
    from app import backup

    monkeypatch.setenv("BACKUP_MIRROR_DIR", str(tmp_path / "из-файла"))
    settings_store.set_value(db, "BACKUP_MIRROR_DIR", "")
    db.commit()

    assert backup.mirror_dir(db) is None


def test_a_backup_actually_lands_in_the_mirror_set_on_the_page(db, tmp_path, monkeypatch):
    """Проверяем не настройку, а её следствие: файл на второй площадке."""
    from app import backup

    import sqlite3

    source = tmp_path / "src.db"
    conn = sqlite3.connect(source)
    # Таблица именно `products`: копия проверяется не только `integrity_check`,
    # но и чтением товаров — непрочитанная копия это надежда, а не копия.
    conn.execute("CREATE TABLE products (uid_1c TEXT)")
    conn.execute("INSERT INTO products VALUES ('u1')")
    conn.commit()
    conn.close()

    local = tmp_path / "backups"
    cloud = tmp_path / "cloud"
    cloud.mkdir()
    monkeypatch.delenv("BACKUP_MIRROR_DIR", raising=False)
    settings_store.set_value(db, "BACKUP_MIRROR_DIR", str(cloud))
    db.commit()

    result = backup.make_backup(database_url=f"sqlite:///{source}",
                                directory=local, db=db)

    assert result.ok and not result.mirror_error, result.error or result.mirror_error
    copies = list(cloud.glob("sync_admin-*.db"))
    assert len(copies) == 1
    # Временное имя не должно остаться: уборка ищет `sync_admin-*.db` и файлов
    # `.part` не видит — они копились бы вечно.
    assert not list(cloud.glob("*.part"))


def test_a_nonsense_retention_number_does_not_wipe_the_mirror(db, tmp_path):
    """Ноль у `prune` означал бы «оставить ноль копий» — то есть удалить всё.

    Ровно то, ради чего вторая площадка и заводилась. Опечатка в поле не должна
    уметь этого сделать.
    """
    from app import backup

    for bad in ("0", "-5", "", "тридцать"):
        settings_store.set_value(db, "BACKUP_MIRROR_KEEP_DAILY", bad)
        db.commit()
        assert backup._setting_int(db, "BACKUP_MIRROR_KEEP_DAILY",
                                   backup.MIRROR_KEEP_DAILY) == backup.MIRROR_KEEP_DAILY, bad

    settings_store.set_value(db, "BACKUP_MIRROR_KEEP_DAILY", "7")
    db.commit()
    assert backup._setting_int(db, "BACKUP_MIRROR_KEEP_DAILY", 30) == 7


def test_the_manual_script_still_works_when_the_database_is_unreachable(monkeypatch,
                                                                       tmp_path):
    """`scripts/backup_db.py` запускают ИМЕННО когда что-то пошло не так.

    Требовать от него исправной базы значило бы отнять копию в единственный час,
    когда она по-настоящему нужна: саму базу он копирует файлом, минуя
    SQLAlchemy, и снимет её даже с той, которую приложение открыть не может.
    Поэтому сбой на пути к настройке ГЛОТАЕТСЯ, а путь берётся из окружения.
    """
    from app import backup
    import app.database as database

    def boom():
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(database, "SessionLocal", boom)
    monkeypatch.setenv("BACKUP_MIRROR_DIR", str(tmp_path / "из-файла"))

    assert backup.mirror_dir() == tmp_path / "из-файла"


def test_the_page_shows_where_copies_land_and_does_not_let_it_be_changed(
        logged_in_client, web_db):
    """`BACKUP_DIR` полем не выставлен намеренно.

    Указать туда папку облака нельзя — SQLite пишет в целевой файл на месте,
    рядом мелькают `-wal`/`-shm`, а клиент синхронизации держит их открытыми.
    Текстовое поле ровно к этому и приглашает, а последствие отложенное.
    """
    page = logged_in_client.get("/notifications").text

    assert "BACKUP_MIRROR_DIR" in page
    assert 'value="BACKUP_DIR"' not in page

    logged_in_client.post("/notifications/save",
                          data={"name": "BACKUP_DIR", "value": r"C:\cloud"},
                          follow_redirects=True)
    from app.models import AppSetting
    assert web_db.query(AppSetting).filter(
        AppSetting.key == "BACKUP_DIR").count() == 0
