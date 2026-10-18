# Бот в мессенджере MAX

## Создание бота

1. Создать бота на платформе MAX для партнёров/разработчиков (dev.max.ru → «Чат-боты»): нужна организация
   (ИП/ООО) и верификация. Получить **токен** → `MAX_BOT_TOKEN` в `.env`.
2. Ссылка на бота вида `https://max.ru/<имя_бота>` → `max.bot_link` в `config.yaml`.
3. Проверка: `python -m awer_sales_bot bot` — в логе строка `MAX bot: {...}` с данными бота (метод `/me`).

## ПРОВЕРИТЬ по актуальной документации (dev.max.ru/docs-api)

API молодой и менялся. В коде (`max_api.py`, `bot.py`):
* базовый адрес `https://platform-api.max.ru`;
* токен: заголовок `Authorization: <token>` (`max.auth: header`) — либо query `access_token` (`max.auth: query`);
* отправка: `POST /messages?user_id=<id>` с телом `{"text": ..., "format": "markdown"}`;
* получение событий: `GET /updates?marker=&timeout=&types=message_created,bot_started` → `{"updates": [...], "marker": N}`;
* поля событий: `bot_started.user.user_id`, `bot_started.payload` (код из ссылки `?start=`),
  `message_created.message.sender.user_id`, `message_created.message.body.text`.
Если формат отличается — поправить `_extract()` в `bot.py` и `MaxClient`.

## Привязка продавцов

1. Админка → «Продавцы» → «новый код» (или `awer-bot invite "Фамилия Имя"`).
2. Продавцу — ссылка `https://max.ru/<бот>?start=<КОД>` или сам код: открыть бота и отправить код.
3. В админке появится ✅ и user_id. Владелец/управляющий: записать свой user_id в `max.owners`
   (узнать: написать боту любое сообщение и посмотреть `message_log` / лог).

Команды: `/plan` — состояние на сейчас, `/help`; владельцу — `/svodka`.

## Расписание

`schedule.daily_send_time` (09:30): импорт папки входящих → если за вчера есть выгрузки по всем магазинам из
`stores` — рассылка; иначе повтор каждые `retry_minutes` до `deadline_time`, затем рассылка по тому, что есть,
и предупреждение владельцу. 1-го числа дополнительно: итоги прошлого месяца, фиксация и рассылка нового плана.
Повторной отправки за тот же день нет (`message_log`).
