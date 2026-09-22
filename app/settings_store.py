"""Настройки, которые задаёт человек со страницы, — с запасным вариантом в `.env`.

Ровно одно правило, из которого следует всё остальное:

    СТРОКА В БАЗЕ ЕСТЬ  → её значение, и точка.
    СТРОКИ В БАЗЕ НЕТ   → смотрим в окружение (`.env`).

Пустая строка в базе — ЭТО ЗНАЧЕНИЕ, а не отсутствие значения. Разница не
теоретическая: пока каналы жили только в `.env`, там мог остаться токен; человек
открывает страницу, стирает поле, жмёт «Сохранить» — и, считай мы пустоту за
«ничего не задано», значение из `.env` вернулось бы обратно. Канал продолжал бы
слать туда, откуда его только что убрали, а страница показывала бы пустое поле.
Немой отказ, причём в ту сторону, где ошибиться дороже всего: тревоги уходят
человеку, который их больше не ждёт.

Обратный порядок (окружение важнее базы) тоже разбирали и отвергли: тогда
забытая строка в `.env` молча отменяла бы правку на странице, и человек чинил бы
то, что не ломалось.

Зачем `.env` вообще оставлен. Установка, где каналы уже прописаны файлом,
продолжает работать без единого действия — обновление не должно выключать
уведомления. И ещё: строка в базе недоступна, пока база не поднялась, а
`ALERT_HEARTBEAT_URL` нужен ровно тогда, когда что-то пошло не так.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.crypto import encrypt_value, decrypt_value, mask_value
from app.models import AppSetting


@dataclass(frozen=True)
class Field:
    """Описание одной настройки.

    `secret` решает, в какую колонку `AppSetting` ляжет значение и покажем ли мы
    его на странице целиком. Решает ИМЕННО описание, а не вызывающий код:
    разойдись они, токен однажды уехал бы в открытую колонку — молча, один раз и
    навсегда, потому что заметить это можно только заглянув в базу.
    """
    name: str
    label: str
    secret: bool = False
    hint: str = ""
    placeholder: str = ""


# Каналы уведомлений. Имена совпадают с переменными окружения намеренно: одно
# имя на оба пути, иначе пришлось бы держать таблицу соответствий и однажды её
# рассинхронизировать.
TELEGRAM_FIELDS = [
    Field("TELEGRAM_BOT_TOKEN", "Токен бота", secret=True,
          placeholder="8123456789:AAH...",
          hint="Выдаёт @BotFather по команде /newbot — строка целиком, вместе с двоеточием."),
    Field("TELEGRAM_CHAT_ID", "ID чата",
          placeholder="123456789",
          hint="Напишите боту что-нибудь, затем откройте "
               "https://api.telegram.org/bot&lt;ТОКЕН&gt;/getUpdates и возьмите chat.id. "
               "У группы он отрицательный — минус обязателен."),
]

EMAIL_FIELDS = [
    Field("ALERT_SMTP_HOST", "SMTP-сервер", placeholder="smtp.yandex.ru",
          hint="Без него канал почты выключен, сколько бы ни было заполнено остальное."),
    Field("ALERT_EMAIL_TO", "Кому слать", placeholder="you@example.com",
          hint="Несколько адресов — через запятую."),
    Field("ALERT_SMTP_PORT", "Порт", placeholder="587",
          hint="465 — SSL, 587 — STARTTLS. По умолчанию 587."),
    Field("ALERT_SMTP_USER", "Логин", placeholder="sync-admin@example.com",
          hint="Пусто — подключаемся без авторизации (внутренний релей)."),
    Field("ALERT_SMTP_PASSWORD", "Пароль", secret=True,
          hint="У Яндекса, Gmail и Mail.ru нужен пароль приложения, а не пароль от почты."),
    Field("ALERT_EMAIL_FROM", "От кого", placeholder="совпадает с логином",
          hint="Необязательно. Пусто — возьмём логин, а если и его нет, адрес получателя."),
    Field("ALERT_SMTP_TLS", "STARTTLS", placeholder="1",
          hint="«0» отключает STARTTLS. Нужно только для внутреннего релея без шифрования."),
]

COMMON_FIELDS = [
    Field("ALERT_BASE_URL", "Адрес системы для ссылок",
          placeholder="http://127.0.0.1:8000",
          hint="Тот адрес, по которому вы сами заходите. По умолчанию локальный — "
               "с телефона такая ссылка не откроется."),
    Field("ALERT_HEARTBEAT_URL", "Пинг внешнего сторожа",
          placeholder="https://hc-ping.com/...",
          hint="Воркер дёргает его на каждом прогоне. Перестал — внешний сервис "
               "сообщает сам. Это единственное, что переживает смерть воркера."),
    Field("ALERT_REPEAT_HOURS", "Напоминать раз в, часов", placeholder="6",
          hint="Через сколько повторить сообщение о той же никуда не девшейся поломке."),
]

# Зеркало копий базы. Здесь, а не в `.env`, ровно по той же причине, что и
# каналы: файл читается один раз на старте, и правка без перезапуска службы не
# действует — а человек при этом уверен, что вторая площадка у него есть.
#
# `BACKUP_DIR` (куда копия снимается СНАЧАЛА) полем НЕ выставлен намеренно.
# Указать туда папку облака нельзя: `Connection.backup()` пишет прямо в целевой
# файл, рядом мелькают `-wal`/`-shm`, а клиент синхронизации держит файлы
# открытыми и мешает уборке. Текстовое поле ровно к этому и приглашает, а
# последствие отложенное — копии как будто есть. Страница показывает этот путь
# только для чтения.
BACKUP_FIELDS = [
    Field("BACKUP_MIRROR_DIR", "Папка зеркала",
          placeholder=r"C:\YandexDisk\sync_admin_backups",
          hint="Папка синхронизации облака, сетевая шара или второй диск. "
               "Пусто — зеркала нет, и все копии лежат на том же диске, что и база."),
    Field("BACKUP_MIRROR_KEEP_DAILY", "Хранить ежедневных", placeholder="30",
          hint="Сколько последних календарных дней держать в зеркале."),
    Field("BACKUP_MIRROR_KEEP_WEEKLY", "Хранить недельных", placeholder="12",
          hint="Плюс по одной копии на неделю — они ловят порчу данных, "
               "замеченную поздно."),
]

ALL_FIELDS = TELEGRAM_FIELDS + EMAIL_FIELDS + COMMON_FIELDS + BACKUP_FIELDS
BY_NAME = {f.name: f for f in ALL_FIELDS}


def _row(db: Session, name: str) -> AppSetting | None:
    return db.query(AppSetting).filter(AppSetting.key == name).first()


def get(db: Session, name: str) -> str:
    """Значение настройки: база, если строка есть, иначе окружение.

    Отсутствие строки и пустая строка различаются намеренно — см. модуль.
    """
    field = BY_NAME.get(name)
    row = _row(db, name)
    if row is not None:
        if field is not None and field.secret:
            # Битый шифротекст (сменили `SECRETS_ENCRYPTION_KEY`, правили базу
            # руками) не роняет уведомления: канал просто окажется ненастроенным
            # и скажет об этом, как о любом незаполненном поле.
            try:
                return decrypt_value(row.encrypted_value or "")
            except Exception:                        # noqa: BLE001
                return ""
        return (row.value or "").strip()
    return (os.environ.get(name) or "").strip()


def get_many(db: Session, names: list[str]) -> dict[str, str]:
    return {name: get(db, name) for name in names}


def set_value(db: Session, name: str, value: str) -> bool:
    """Записать настройку. Возвращает True, если значение изменилось.

    Не коммитит: вызывающий пишет в журнал действий тем же коммитом, что и саму
    правку, — иначе одно из двух могло бы уцелеть без другого.
    """
    if name not in BY_NAME:
        raise KeyError(name)
    field = BY_NAME[name]
    value = (value or "").strip()

    # Прежнее значение читаем ДО создания строки, иначе оно читалось бы уже из
    # свежей пустой строки, а не из окружения. Цена ровно одна и неприятная:
    # человек стирает поле, пришедшее из `.env`, — эффективное значение меняется
    # с «из файла» на пустое, а страница отвечает «без изменений» и ничего не
    # пишет в журнал. То самое действие, ради которого журнал и нужен, прошло бы
    # молча, да ещё и с уверением, что ничего не произошло.
    was = get(db, name)

    row = _row(db, name)
    if row is None:
        row = AppSetting(key=name)
        db.add(row)
        # Без flush следующий `get` в том же запросе строки не увидит:
        # сессия живёт с `autoflush=False`.
        db.flush()

    if field.secret:
        row.encrypted_value = encrypt_value(value) if value else None
    else:
        row.value = value
    return was != value


def as_cards(db: Session) -> list[dict]:
    """Поля для страницы, сгруппированные по каналам.

    Секрет наружу не отдаём даже своему шаблону — только маску: страница
    открыта любому пользователю админки, а через плечо читают ровно так же,
    как с экрана.
    """
    groups = [
        ("telegram", "Telegram", TELEGRAM_FIELDS),
        ("email", "Почта", EMAIL_FIELDS),
        ("common", "Общие настройки", COMMON_FIELDS),
        ("backup", "Зеркало резервных копий", BACKUP_FIELDS),
    ]
    cards = []
    for key, title, fields in groups:
        rows = []
        for field in fields:
            plain = get(db, field.name)
            row = _row(db, field.name)
            rows.append({
                "name": field.name, "label": field.label, "secret": field.secret,
                "hint": field.hint, "placeholder": field.placeholder,
                "value": "" if field.secret else plain,
                "masked": mask_value(plain) if field.secret else plain,
                "is_set": bool(plain),
                # Откуда взялось значение — человеку это важнее, чем кажется:
                # поле, заполненное из `.env`, правится не здесь.
                "from_env": row is None and bool(plain),
                "updated_at": row.updated_at if row is not None else None,
            })
        cards.append({"key": key, "title": title, "fields": rows})
    return cards
