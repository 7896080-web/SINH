import enum
import uuid
from datetime import datetime
from app.timeutils import now_utc

from sqlalchemy import (
    Column, String, Boolean, Integer, DateTime, Date, Enum, ForeignKey,
    Index, UniqueConstraint, Text,
)
from sqlalchemy.orm import relationship

from app.database import Base


class Platform(str, enum.Enum):
    wb = "wb"
    ozon = "ozon"
    kit = "kit"


class ProposalSource(str, enum.Enum):
    manual = "manual"
    catalog_detected = "catalog_detected"


class AnomalyReason(str, enum.Enum):
    order_on_disabled = "order_on_disabled"
    missing_barcode = "missing_barcode"


class AnomalyStatus(str, enum.Enum):
    new = "new"
    resolved = "resolved"


# ---------------------------------------------------------------------------
# Пользователи админки (страница входа)
# ---------------------------------------------------------------------------

class UserRole(str, enum.Enum):
    """Роль определяет, какие страницы видно. Список — в `app/access.py`.

    `admin` — всё, как было до появления ролей; им и остаются все учётные
    записи, заведённые раньше (миграция проставляет его явно: молча отнять у
    живого человека доступ хуже, чем дать лишний).

    `warehouse` — склад: только раздел возвратов. Он принимает вещи и решает их
    судьбу, и этого ему достаточно; на остальных страницах есть кнопки, которые
    в один клик двигают боевые остатки по всему каталогу.
    """
    admin = "admin"
    warehouse = "warehouse"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=now_utc)
    # Защита от подбора пароля — раздел про ограничение попыток входа.
    failed_login_attempts = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime, nullable=True)
    role = Column(Enum(UserRole), default=UserRole.admin, nullable=False)


# ---------------------------------------------------------------------------
# Кабинеты площадок — "единица подключения". У WB их может быть несколько
# (разные ИП/ООО с отдельными токенами), у Ozon/Kit обычно один, но модель
# одинакова для всех трёх — не завязываемся на то, что площадка = кабинет.
# ---------------------------------------------------------------------------

class PlatformAccount(Base):
    __tablename__ = "platform_accounts"

    id = Column(Integer, primary_key=True)
    platform = Column(Enum(Platform), nullable=False)
    name = Column(String(128), nullable=False)  # например "ИП Яворская"
    warehouse_id = Column(String(128), nullable=True)  # склад на площадке для push_stock
    is_active = Column(Boolean, default=True, nullable=False)
    # Трансляция остатков на этот кабинет включена (ручной переключатель, отдельно
    # от is_active/автовыключателя). Выключено — диспетчер ПРОПУСКАЕТ кабинет
    # (пауза рассылки: остатки на площадке не трогаем), приём заказов продолжается.
    # UI даёт переключать по площадке (все кабинеты площадки) и по всем сразу.
    dispatch_enabled = Column(Boolean, default=True, nullable=False)
    # Возвращать на витрину карточку, которую площадка спрятала за нулевой
    # остаток, когда на неё уходит ненулевой. Нужно для Kit: при настройке
    # «скрывать товары без остатка» распроданная карточка переходит в HIDDEN и
    # САМА оттуда не возвращается — приход остатка её статус не меняет, и товар
    # остаётся невидимым, сколько ни шли остаток.
    #
    # По умолчанию ВЫКЛЮЧЕНО и включается руками по кабинету: публикация — это
    # действие наружу, а карточку мог спрятать и человек (снял с продажи,
    # спорный товар, не сезон). Отличить одно от другого площадка не даёт:
    # статус `HIDDEN` один на оба случая.
    publish_hidden_on_stock = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=now_utc)

    # Автоматический выключатель (раздел про стабильность): растёт при
    # подряд идущих сбоях опроса заказов, сбрасывается при успехе. При
    # достижении порога кабинет сам отключается — чтобы не долбить
    # сломанные/просроченные ключи бесконечно и не засорять очередь.
    consecutive_failures = Column(Integer, default=0, nullable=False)
    last_error = Column(Text, nullable=True)

    # Результат последней ручной или автоматической проверки подключения
    last_connection_check_at = Column(DateTime, nullable=True)
    last_connection_ok = Column(Boolean, nullable=True)
    last_connection_message = Column(Text, nullable=True)

    credentials = relationship("ApiCredential", back_populates="account", cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# API-ключи кабинетов (страница "API-ключи")
# Значения хранятся зашифрованными на прикладном уровне (см. app/crypto.py)
# ---------------------------------------------------------------------------

class ApiCredential(Base):
    __tablename__ = "api_credentials"
    __table_args__ = (UniqueConstraint("account_id", "field_name", name="uq_account_field"),)

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("platform_accounts.id"), nullable=False)
    field_name = Column(String(64), nullable=False)   # например: token / client_id / api_key
    field_label = Column(String(128), nullable=False)  # человекочитаемая подпись для формы
    encrypted_value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=now_utc, onupdate=now_utc)

    account = relationship("PlatformAccount", back_populates="credentials")


# ---------------------------------------------------------------------------
# Товар — локальное зеркало номенклатуры старой базы (без ссылок, только УИД)
# ---------------------------------------------------------------------------

class Product(Base):
    __tablename__ = "products"

    uid_1c = Column(String(36), primary_key=True)  # УникальныйИдентификатор() старой базы
    article = Column(String(64), index=True)
    name = Column(String(255), index=True)
    # Размер и цвет размер-цвет SKU (из выгрузки 1С: размер = характеристика,
    # цвет = реквизит номенклатуры hiЦвет). Только для отображения оператору.
    size = Column(String(32))
    color = Column(String(128))
    stock_on_hand = Column(Integer, default=0)      # оперативный остаток (раздел 7 спецификации)
    # Резерв: сколько штук физического остатка держать у себя и НЕ отдавать на
    # площадки. На каждую площадку уходит max(0, остаток − резерв). Общий на все
    # каналы (остаток физически один — ЦС), задаётся на товар (размер-цвет SKU).
    reserve = Column(Integer, default=0, nullable=False)
    # Ручной «передаваемый остаток» (override). Если задан — на площадки уходит
    # ровно это значение (не ниже 0), а не расчёт «остаток − резерв − порог».
    # Ведёт себя как ручной запас: ЗАКАЗЫ вычитаются ИЗ НЕГО, отмена возвращает
    # (см. order_poller). Фоновое обновление остатка из 1С его НЕ трогает.
    # NULL = считать автоматически (сбрасывает оператор вручную).
    transmit_override = Column(Integer, nullable=True)
    # Порог трансляции — фиксированная зависимость «ЦС − порог». Вычисляется как
    # (остаток ЦС на дату) − (доступно на дату) и МОЖЕТ БЫТЬ ОТРИЦАТЕЛЬНЫМ. Если
    # задан — на площадки уходит max(0, stock_on_hand − broadcast_offset). В отличие
    # от transmit_override НЕ накапливается заказами/сверкой (они двигают только сам
    # остаток stock_on_hand), поэтому НЕ дрейфует. Приоритетнее transmit_override.
    # NULL = порог не задан.
    broadcast_offset = Column(Integer, nullable=True)
    # Трансляция SKU на площадки (одна на товар, не по площадкам). Выключена —
    # на площадки уходит 0 (товар снимается с продажи). ПО УМОЛЧАНИЮ ВЫКЛЮЧЕНА:
    # новый SKU не льёт остаток на площадки, пока оператор явно не включит его
    # (безопасный дефолт — ничего не уезжает случайно).
    broadcast_enabled = Column(Boolean, default=False, nullable=False)
    # --- Расчёт порога от даты ---------------------------------------------
    # Порог перестал быть числом, которое оператор вбивает руками: он выводится
    # из трёх величин на выбранную дату
    #     порог = остаток ЦС на дату − (факт на дату − бронь)
    # и записывается в broadcast_offset выше. Хранить эти три величины
    # обязательно: без них порог нельзя пересчитать при смене брони, а бронь
    # меняют чаще всего остального — иначе новое значение брони молча ни на что
    # не повлияло бы, потому что старое уже зашито в порог.
    #
    # offset_base_date   — дата, на которую считаем.
    # offset_base_stock  — что 1С показала на эту дату (NULL = ответа ещё нет;
    #                      товара не было в выгрузке = 0, это не то же самое).
    # fact_at_date       — сколько лежало на складе НА САМОМ ДЕЛЕ, руками.
    #                      NULL = оператор не вводил, берём offset_base_stock,
    #                      и тогда порог равен просто брони.
    #
    # Все три NULL — порог живёт по-старому: как число, введённое руками, или
    # не задан вовсе. Так продолжают работать товары, настроенные до этой правки.
    offset_base_date = Column(Date, nullable=True)
    offset_base_stock = Column(Integer, nullable=True)
    fact_at_date = Column(Integer, nullable=True)
    # Когда по товару в последний раз прошла АКТУАЛИЗАЦИЯ: с базовой даты подняты
    # реальные заказы площадок и по ним созданы перемещения в 1С. До этого момента
    # остаток ЦС отражает склад без учёта отгрузок на маркетплейсы, и включать
    # трансляцию рано — уедет завышенное число. Отдельно от порога намеренно:
    # порог посчитан и остаток актуализирован — разные состояния, и оператор
    # должен видеть, какое из них достигнуто.
    # МЁРТВАЯ КОЛОНКА, оставленная ради того, чтобы её не завели заново.
    # Хранила «порог, который надо удержать, когда 1С ответит на новую дату»:
    # при сдвиге даты назад под сохранённый порог подбирался ФАКТ, а если снимка
    # на новую дату ещё не было, подбирать было не от чего, и намерение лежало
    # здесь до ответа 1С. Задачи больше нет — расхождение хранится на товаре и
    # смену даты переживает само, так что и удерживать нечего. Значения погашены
    # миграцией `930c4c4db5e9`; саму колонку не роняем, потому что `DROP COLUMN`
    # на SQLite перестраивает таблицу в 152 тысячи строк при живых службах.
    offset_pinned = Column(Integer, nullable=True)
    # РАСХОЖДЕНИЕ УЧЁТА СО СКЛАДОМ: `учёт 1С − сколько лежит на самом деле`.
    #
    # Это главное число всего механизма порога, и до 23.09 оно нигде не
    # хранилось: выводилось на лету из пары «остаток на дату − факт на дату», то
    # есть носителем выступала ДАТА. Отсюда весь класс бед — сменили дату, факт
    # справедливо стёрся, расхождение исчезло, порог схлопнулся до брони, и
    # наружу поехал остаток, завышенный ровно на расхождение. Латали это
    # `pin_offset`, который подбирал факт под сохранённый порог, — но подобранный
    # факт оператор видит как измерение, которого не делал, и «поправляет»
    # обратно на учётное число. Ровно так 23.09 и схлопнулись пороги у 62
    # товаров.
    #
    # Теперь расхождение — СВОЙСТВО ТОВАРА, а не даты:
    #
    #     порог = расхождение + бронь
    #
    # Дату можно двигать куда угодно, расхождение не шелохнётся. Оно постоянно
    # по смыслу: учёт врёт на одну и ту же величину, пока склад не пересчитали
    # заново. Знак значим и обе стороны законны — плюс значит «в 1С числится
    # больше, чем лежит», минус — «на складе больше, чем знает 1С» (на бою
    # такие есть, до −213).
    #
    # NULL — расхождение НЕ ИЗМЕРЯЛИ. Это не то же самое, что ноль: ноль значит
    # «склад сошёлся с учётом», и поставить его можно только явно.
    stock_discrepancy = Column(Integer, nullable=True)
    recalc_done_at = Column(DateTime, nullable=True)
    # Кабинеты, заказы которых расчёт РЕАЛЬНО прочитал (id через запятую).
    # «Актуализирован» — свойство пары товар+кабинет, а не одного товара: расчёт
    # поднимает заказы только с отмеченных кабинетов, и для неотмеченного его
    # остаток ничем не подтверждён. Без этого списка галочку можно было
    # поставить на кабинет, которого расчёт не касался, и через 45 секунд туда
    # уезжал остаток, не сверенный с его продажами. NULL — расчёт был до
    # появления этой колонки: считаем, что не покрыт никто, и требуем пересчёт.
    recalc_account_ids = Column(Text, nullable=True)
    # «Включить трансляцию, как только расчёт закончится» — просьба, а не
    # состояние. Ставит её импорт Excel: оператор отмечает в файле дату, факт,
    # кабинеты и «Трансляция = Да» одной строкой, а расчёт идёт минутами и
    # заканчивается уже без него. Гейт при этом не ослаблен: включает
    # `broadcast_gate.apply_pending_broadcast` ровно тогда, когда включила бы и
    # страница. Снимается при включении и при любом выключении трансляции —
    # иначе снятая галочка вернулась бы сама после следующего расчёта.
    broadcast_requested_at = Column(DateTime, nullable=True)
    # Дата «активно с» — для аудита и backfill (подтягивание отгрузок с даты).
    broadcast_active_since = Column(Date, nullable=True)
    updated_at = Column(DateTime, default=now_utc, onupdate=now_utc)

    barcodes = relationship("Barcode", back_populates="product", cascade="all, delete-orphan")
    sync_settings = relationship("SyncSetting", back_populates="product", cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# Таблица "Баркоды" (страница "Мэппинг")
# ---------------------------------------------------------------------------

class Barcode(Base):
    __tablename__ = "barcodes"

    id = Column(Integer, primary_key=True)
    barcode = Column(String(64), unique=True, nullable=False, index=True)
    # Индекс обязателен, и не ради «на всякий случай». По этой колонке баркоды
    # ищут ВСЕ: поиск товара по штрихкоду, подтягивание баркодов к строке,
    # гашение аномалий, выбор ключа отправки. Без него SQLite читает все 154
    # тысячи строк на каждое обращение, а поиск по штрихкоду делает это на
    # каждый из 152 тысяч товаров — страница не отвечала вовсе.
    uid_1c = Column(String(36), ForeignKey("products.uid_1c"), nullable=False, index=True)
    # Площадка (не кабинет) — чисто информационная пометка "откуда впервые
    # увидели баркод"; баркод физический и от конкретного кабинета не зависит.
    source_platform = Column(String(16), nullable=True)
    created_at = Column(DateTime, default=now_utc)

    product = relationship("Product", back_populates="barcodes")


class AlertState(Base):
    """Что мы в последний раз сказали человеку наружу — и когда.

    Нужна ровно для двух вещей, и обе про доверие к уведомлению.

    ПЕРВОЕ: не повторяться. Задание смотрит на систему каждые несколько минут, а
    поломка держится часами. Слать одно и то же каждые пять минут — вернейший
    способ добиться, чтобы уведомления отключили или перестали читать, и тогда
    молчать будет уже всё. Повторяем, только когда картина ИЗМЕНИЛАСЬ, и ещё
    раз — по долгому таймеру, чтобы висящая поломка не забылась.

    ВТОРОЕ: сказать «отбой». Человек, получивший тревогу, обязан узнать, что она
    кончилась, — иначе он либо идёт проверять руками каждый раз, либо перестаёт
    верить и первому сообщению. Отбой шлём ровно один раз, и только если до него
    была тревога.

    Строка одна на систему (`key="main"`): состояние тут общее, а не по каждой
    находке отдельно. Разбивать по находкам значило бы слать пять сообщений там,
    где случилось одно событие.
    """
    __tablename__ = "alert_state"

    id = Column(Integer, primary_key=True)
    key = Column(String(64), unique=True, nullable=False)
    # Отпечаток картины: отсортированный список того, что сейчас не так.
    # Сравнивается как строка — изменилась, значит случилось что-то новое.
    signature = Column(Text, nullable=True)
    # 'alarm' или 'clear'. По нему решаем, нужен ли отбой.
    level = Column(String(16), nullable=True)
    last_sent_at = Column(DateTime, nullable=True)


class AppSetting(Base):
    """Настройка, которую задаёт человек со страницы, а не файл `.env`.

    Заведена под каналы уведомлений, и причин ровно три.

    ПЕРВАЯ, главная: `.env` читается ОДИН РАЗ, на импорте приложения
    (`app/__init__.py`), — значит правка в файле ничего не меняет, пока не
    перезапустишь службу. На боевом Windows это `nssm restart sync_admin_worker`,
    то есть шаг, о котором легко забыть; забыл — и человек уверен, что канал
    настроен, а система молчит. Строка в базе читается в момент обращения, и
    забыть нечего.

    ВТОРАЯ: токен бота лежит зашифрованным (`encrypted_value`), тем же
    механизмом, что и ключи площадок. В `.env` он лежал бы открытым текстом в
    одном файле с `SECRETS_ENCRYPTION_KEY` — то есть ключ и то, что им
    защищено, рядом.

    ТРЕТЬЯ: правку видно. `.env` правят Блокнотом на сервере, и кто именно
    сменил адресата тревог, потом не установить; страница пишет в журнал
    действий.

    Значение хранится РОВНО В ОДНОЙ из двух колонок, и выбирает её описание
    поля в `app/settings_store.py`, а не то, что случайно оказалось записано.
    Разреши мы обе, секрет однажды уехал бы в `value` открытым — молча и
    навсегда.

    Отсутствие строки и пустая строка — РАЗНЫЕ вещи, см. `settings_store.get`.
    """
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True)
    key = Column(String(64), unique=True, nullable=False)
    # Открытым текстом: адреса, порты, имя ящика — то, что человек и так видит
    # на странице и должен видеть, чтобы проверить опечатку.
    value = Column(Text, nullable=True)
    # Шифрованное: токен бота, пароль почты. На страницу возвращается маской.
    encrypted_value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=now_utc, onupdate=now_utc)


class MappingConflict(Base):
    """Регистр «КонфликтыСопоставления» — баркод, не найденный в таблице «Баркоды»."""
    __tablename__ = "mapping_conflicts"

    id = Column(Integer, primary_key=True)
    barcode = Column(String(64), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("platform_accounts.id"), nullable=False)
    attempts = Column(Integer, default=1)
    first_seen = Column(DateTime, default=now_utc)
    last_seen = Column(DateTime, default=now_utc, onupdate=now_utc)

    account = relationship("PlatformAccount")


# ---------------------------------------------------------------------------
# Регистр «НастройкиСинхронизации» (страница "Синхронизируемые товары")
# Теперь на пару Товар+Кабинет, а не Товар+Площадка — у одного товара может
# быть свой порог и свой признак "синхронизировать" в каждом кабинете WB.
# ---------------------------------------------------------------------------

class SyncSetting(Base):
    __tablename__ = "sync_settings"
    __table_args__ = (UniqueConstraint("uid_1c", "account_id", name="uq_product_account"),)

    id = Column(Integer, primary_key=True)
    uid_1c = Column(String(36), ForeignKey("products.uid_1c"), nullable=False)
    account_id = Column(Integer, ForeignKey("platform_accounts.id"), nullable=False)
    enabled = Column(Boolean, default=False, nullable=False)
    has_proposal = Column(Boolean, default=False, nullable=False)
    proposal_source = Column(Enum(ProposalSource), nullable=True)
    proposal_date = Column(DateTime, nullable=True)
    enabled_at = Column(DateTime, nullable=True)
    # Ниже этого значения на площадку уходит 0, а не фактический остаток —
    # страховой буфер, чтобы не продать последние штуки через площадку и не
    # уйти в 0 одновременно на всех каналах. 0 = порог выключен (шлём как есть).
    min_threshold = Column(Integer, default=0, nullable=False)
    # Когда на этот кабинет в последний раз УСПЕШНО ушёл НЕПУСТОЙ остаток.
    #
    # Это ответ на вопрос «есть ли нам что отзывать», и жить он обязан здесь, а не
    # в очереди рассылки. Раньше `transmit.ever_transmitted` искал доказательство
    # отправки в `dispatch_queue`, а суточная чистка (`retention`, 30 суток на
    # терминальные записи) его оттуда удаляет. Для медленного размера — половина
    # каталога одежды и обуви — остаток не меняется месяцами, последняя строка
    # `sent` исчезает, и система забывает, что вообще писала на эту карточку.
    # Дальше оператор снимает галочку: отзыв не отправляется, на площадке остаётся
    # наш остаток, она продолжает продавать, а заказы по снятой паре живой опрос уже
    # пропускает — ни списания у нас, ни документа в 1С. Это оверселл, и ровно та
    # ошибка, ради недопущения которой признак сознательно смещён в сторону
    # ЛИШНЕГО отзыва.
    last_nonzero_sent_at = Column(DateTime, nullable=True)

    product = relationship("Product", back_populates="sync_settings")
    account = relationship("PlatformAccount")


# ---------------------------------------------------------------------------
# Регистр «АномалииСинхронизации»
# ---------------------------------------------------------------------------

class SyncAnomaly(Base):
    __tablename__ = "sync_anomalies"

    id = Column(Integer, primary_key=True)
    uid_1c = Column(String(36), ForeignKey("products.uid_1c"), nullable=False)
    account_id = Column(Integer, ForeignKey("platform_accounts.id"), nullable=False)
    reason = Column(Enum(AnomalyReason), nullable=False)
    order_id = Column(String(128), nullable=True)
    detected_at = Column(DateTime, default=now_utc)
    status = Column(Enum(AnomalyStatus), default=AnomalyStatus.new, nullable=False, index=True)
    # Симулированный заказ — не должен путаться с реальными аномалиями на
    # общей странице «Аномалии» (страница тестирования показывает результат
    # сама, ей отдельная страница не нужна).
    is_test = Column(Boolean, default=False, nullable=False)

    account = relationship("PlatformAccount")


# ---------------------------------------------------------------------------
# Раздел 4 спецификации: очередь рассылки остатков на площадки
# ---------------------------------------------------------------------------

class DispatchStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    error = "error"


class DispatchQueueItem(Base):
    """«ОчередьРассылки» — накапливает изменения остатка, раз в 30-60 секунд
    воркер dispatch.py забирает пачку и отправляет батчем на площадку."""
    __tablename__ = "dispatch_queue"

    id = Column(Integer, primary_key=True)
    uid_1c = Column(String(36), ForeignKey("products.uid_1c"), nullable=False)
    account_id = Column(Integer, ForeignKey("platform_accounts.id"), nullable=False)
    quantity = Column(Integer, nullable=False)  # абсолютное значение на момент постановки в очередь
    # Сколько ушло на площадку НА САМОМ ДЕЛЕ. Не то же самое, что `quantity`:
    # та — исходный остаток на момент постановки в очередь, а итог считается в
    # момент отправки по всей лестнице (`transmit.quantity_for_account`), и с
    # заданным порогом трансляции отличается от неё всегда. Без этой колонки
    # вопрос «какое число мы отправили на площадку» по базе не восстановить —
    # только пересчитать задним числом по текущим настройкам, а они могли уже
    # измениться. NULL — запись ещё не отправляли.
    sent_quantity = Column(Integer, nullable=True)
    # Идентификатор, КОТОРЫМ отправляли (для WB это баркод-sku, для Ozon артикул,
    # для Kit variant_id). Без него по базе не восстановить, под каким ключом
    # число ушло на площадку: у товара бывает несколько баркодов, и выбор делает
    # `dispatch._resolve_push_target` в момент отправки. 19.09 разбор «почему на
    # WB ноль» из-за этого занял час вместо минуты — количество знали, ключ нет.
    sent_sku = Column(String(64), nullable=True)
    # Что площадка держит по этому sku, когда мы спросили ПОСЛЕ отправки, и когда
    # спрашивали. Нужно, потому что успешный ответ на отправку не означает, что
    # число там и осталось: 19.09 на бою выяснилось, что в кабинет пишет ещё одна
    # система и перетирает наши остатки за три минуты. Расхождение этих двух
    # чисел — единственный способ такое увидеть, отправка о нём не знает.
    # NULL — ещё не проверяли (или площадка не умеет отдавать остатки обратно).
    verified_at = Column(DateTime, nullable=True)
    verified_quantity = Column(Integer, nullable=True)

    # Очередь спрашивают двумя способами, и оба были полным чтением таблицы:
    # по паре товар+кабинет (постановка, отзыв, «было ли что отзывать») и по
    # статусу (рассылка забирает pending, отчёт считает error).
    __table_args__ = (
        Index("ix_dispatch_queue_pair", "uid_1c", "account_id"),
        Index("ix_dispatch_queue_status", "status"),
    )
    reason = Column(String(64), nullable=False)  # 'order' / 'cancel' / 'manual_enable' / 'reconciliation'
    status = Column(Enum(DispatchStatus), default=DispatchStatus.pending, nullable=False)
    attempts = Column(Integer, default=0)
    last_error = Column(Text, nullable=True)
    # Не раньше этого момента можно пробовать снова (пауза между попытками после
    # сбоя площадки). NULL — можно прямо сейчас. Статус `error` терминален: он
    # ставится, только когда попытки исчерпаны, см. app/workers/dispatch.py.
    next_attempt_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=now_utc)
    sent_at = Column(DateTime, nullable=True)
    # Раздел про страницу тестирования: запись от симулированного заказа —
    # НИКОГДА не должна реально уйти на площадку. dispatch.py явно исключает
    # такие записи из отправки (см. app/workers/dispatch.py).
    is_test = Column(Boolean, default=False, nullable=False)
    # «Карточки этого товара в кабинете нет» — признак ДАННЫМИ, а не текстом.
    # Следствие у него совсем другое, чем у сбоя отправки: продавать нечего,
    # оверселла не будет, и чинить надо мэппинг, а не связь. Отчёт различал их
    # поиском двух русских подстрок в `last_error` — и промахивался мимо всех
    # текстов, кроме одного WB-шного: у Kit слова другие («площадка не знает
    # такой товар (variant_id …)», «карточка товара в архиве»), у Ozon в
    # `detail` уезжает сообщение площадки по-английски. Все они попадали в
    # КРИТИЧНУЮ находку «рассылка не доехала, площадка продаёт то, чего нет» —
    # и навсегда: запись терминальная, успешной отправки по паре не будет, снять
    # её нечем. Вечно красный отчёт пролистывают не читая, и тогда он бесполезен
    # весь.
    card_missing = Column(Boolean, default=False, nullable=False,
                          server_default="0")

    account = relationship("PlatformAccount")


# ---------------------------------------------------------------------------
# Раздел 4: «ОбработанныеЗаказыПлощадок» — идемпотентность приёма заказов
# ---------------------------------------------------------------------------

class OrderProcessStatus(str, enum.Enum):
    processed = "processed"    # заказ принят («ожидает подтверждения»): ЦС -> Ожидает
    confirmed = "confirmed"    # заказ подтверждён/отгружен: Ожидает -> Склад площадки
    cancelled = "cancelled"    # отменён/возвращён: реверс на ЦС


class ProcessedOrder(Base):
    __tablename__ = "processed_orders"
    __table_args__ = (UniqueConstraint("account_id", "order_id", name="uq_account_order"),)

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("platform_accounts.id"), nullable=False)
    order_id = Column(String(128), nullable=False)
    uid_1c = Column(String(36), ForeignKey("products.uid_1c"), nullable=True, index=True)
    quantity = Column(Integer, nullable=False, default=0)
    status = Column(Enum(OrderProcessStatus), default=OrderProcessStatus.processed, nullable=False)
    processed_at = Column(DateTime, default=now_utc)
    cancelled_at = Column(DateTime, nullable=True)

    account = relationship("PlatformAccount")


# ---------------------------------------------------------------------------
# Раздел 6: канал 1С через FTP — исходящие задания и входящие результаты
# ---------------------------------------------------------------------------

class FtpTaskStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"       # файл выложен на FTP, ждём результата
    done = "done"        # 1С ответила OK: документ создан
    failed = "failed"     # 1С ответила ERROR: документа НЕТ, нужен разбор
    timeout = "timeout"   # результата нет дольше окна ожидания — алерт
    # Разобрано человеком на «Диагностике»: он посмотрел в 1С и сказал, что
    # документа там нет и не будет. Терминальный статус, и главное — он НЕ
    # считается «в пути» (см. `_in_flight_adjustment`), поэтому единица
    # возвращается в наш остаток. Отдельный от `failed` намеренно: `failed` —
    # это отказ 1С, по которому ещё можно что-то сделать, а здесь решение принял
    # человек и задание закрыто окончательно.
    no_document = "no_document"


class FtpTask(Base):
    """Одна строка внутри файла task_*.txt для старой базы 1С."""
    __tablename__ = "ftp_tasks"

    id = Column(Integer, primary_key=True)
    command = Column(String(32), nullable=False)  # CREATE_MOVEMENT / CANCEL_MOVEMENT
    # Индекс не ради страницы, а ради часа сверки: `_in_flight_adjustment`
    # спрашивает задания ПО КАЖДОМУ товару снимка, и без индекса каждый раз
    # читалась вся таблица. Замер на боевом масштабе (152 235 товаров, снимок
    # 1700 позиций): 60 тысяч заданий — 16,3 с на прогон, 180 тысяч — 42,4 с,
    # с индексом — 3,6 с. Срок хранения заданий 180 суток, то есть таблица
    # только растёт, и вместе с ней рос бы каждый часовой прогон.
    barcode = Column(String(64), nullable=True, index=True)
    warehouse_from = Column(String(64), nullable=True)
    warehouse_to = Column(String(64), nullable=True)
    quantity = Column(Integer, nullable=True)
    order_id = Column(String(128), nullable=False, index=True)
    # Кабинет НЕобязателен: у возврата его нет вовсе. Оприходование идёт на ЦС,
    # ИП к документу отношения не имеет, а на приёмке кабинет и неизвестен.
    # Площадка при этом нужна — по ней выбирается склад-источник, — поэтому она
    # своим полем: у заказа берётся из кабинета, у возврата стоит прямо.
    account_id = Column(Integer, ForeignKey("platform_accounts.id"), nullable=True)
    platform = Column(Enum(Platform), nullable=True)
    # Дата документа перемещения в 1С (старт задним числом). NULL = текущая дата.
    movement_date = Column(Date, nullable=True)
    # Задания разбирают по статусу: «в пути» для расчёта остатка, `timeout` для
    # повтора, `pending` для сборки файла. Каждый такой разбор читал таблицу
    # целиком.
    status = Column(Enum(FtpTaskStatus), default=FtpTaskStatus.pending, nullable=False, index=True)
    batch_filename = Column(String(128), nullable=True)  # какой task_*.txt унёс эту строку
    created_at = Column(DateTime, default=now_utc)
    sent_at = Column(DateTime, nullable=True)
    result_status = Column(String(16), nullable=True)   # OK / ERROR
    result_detail = Column(String(255), nullable=True)   # номер документа или текст ошибки
    completed_at = Column(DateTime, nullable=True)
    # Сколько раз задание уже переотправляли после `timeout`. 1С иногда забирает
    # файл и не отвечает по нему НИ ОДНОЙ строкой (19.09: два файла из пятидесяти
    # трёх, 12 строк), и такое задание навсегда остаётся «в пути» — остаток товара
    # вечно занижен, а закрыть его было нечем. Переотправка безопасна ровно
    # постольку, поскольку 1С идемпотентна по номеру заказа: документ с этим
    # номером уже есть — она возвращает OK и второго не создаёт. Счётчик нужен,
    # чтобы не молотить бесконечно: исчерпав `MAX_REPOSTS`, задание остаётся
    # `timeout` и ждёт человека.
    repost_count = Column(Integer, default=0, nullable=False)
    # Симулированный заказ со страницы тестирования — НИКОГДА не должен
    # попасть в реальный файл задания для 1С. ftp_channel.py явно исключает
    # такие строки при сборке task_*.txt.
    is_test = Column(Boolean, default=False, nullable=False)

    account = relationship("PlatformAccount")


# ---------------------------------------------------------------------------
# Раздел 9: сверка (реконсиляция) — по товару в целом, кабинет площадки тут
# ни при чём: сверяем физический остаток ЦС, а не то, что на какой площадке.
# ---------------------------------------------------------------------------

class StockDeltaDocument(Base):
    """Документ 1С, изменение по которому мы уже применили.

    1С сама кладёт `delta_*.txt`, когда меняется остаток ЦС, — чтобы не ждать
    часовой выгрузки. Файл может приехать повторно: переотправка, повторное
    проведение, ручной перезапуск обработки. Второй раз тот же документ
    применять нельзя, поэтому его идентификатор запоминаем здесь.

    Своих движений тут не бывает: документы, созданные по нашим заданиям, 1С
    помечает источником `sync`, и такие строки отбрасываются раньше — иначе мы
    получили бы эхо собственных действий и списали бы единицу дважды.
    """
    __tablename__ = "stock_delta_documents"

    id = Column(Integer, primary_key=True)
    document_id = Column(String(128), unique=True, nullable=False, index=True)
    source = Column(String(32), nullable=True)
    applied_at = Column(DateTime, default=now_utc, nullable=False)
    lines = Column(Integer, default=0, nullable=False)


class ReconciliationClassification(str, enum.Enum):
    normal = "normal"
    auto_plus = "auto_plus"
    auto_minus = "auto_minus"
    needs_review = "needs_review"


class ReconciliationLog(Base):
    __tablename__ = "reconciliation_log"

    id = Column(Integer, primary_key=True)
    # Самая большая таблица системы: 276 тысяч строк на бою и растёт каждый час.
    # Отчёт берёт из неё сутки по `checked_at`, «Диагностика» — историю товара.
    uid_1c = Column(String(36), ForeignKey("products.uid_1c"), nullable=False, index=True)
    checked_at = Column(DateTime, default=now_utc, index=True)
    python_stock = Column(Integer, nullable=False)
    in_flight = Column(Integer, nullable=False, default=0)
    expected_1c = Column(Integer, nullable=False)
    actual_1c = Column(Integer, nullable=False)
    delta = Column(Integer, nullable=False)
    classification = Column(Enum(ReconciliationClassification), nullable=False)
    resolved = Column(Boolean, default=False, nullable=False)


# ---------------------------------------------------------------------------
# Журнал действий — кто и когда включил синхронизацию, разрешил конфликт и т.д.
# ---------------------------------------------------------------------------

class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    actor = Column(String(64), nullable=False)  # логин пользователя или 'system'
    action = Column(String(64), nullable=False)
    details = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now_utc)


# ---------------------------------------------------------------------------
# Снимок каталога кабинета — для страницы «Мэппинг»: обогащение конфликтов
# названием/артикулом с площадки и резолвинг Kit variant_id → реальный баркод
# ---------------------------------------------------------------------------

class PlatformCatalogItem(Base):
    __tablename__ = "platform_catalog_items"
    # Единица строки каталога — БАРКОД, а не external_id. У WB один external_id
    # (nmID карточки) охватывает несколько баркодов (размеров), поэтому ключом
    # уникальности служит (account_id, barcode); external_id остаётся как
    # метаданные (к какой карточке/товару площадки относится баркод).
    __table_args__ = (UniqueConstraint("account_id", "barcode", name="uq_account_barcode"),)

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("platform_accounts.id"), nullable=False)
    external_id = Column(String(128), nullable=False)  # nmID(WB) / product_id(Ozon) / id(Kit)
    barcode = Column(String(64), nullable=True, index=True)
    article = Column(String(128), nullable=True)
    name = Column(String(255), nullable=True)
    fetched_at = Column(DateTime, default=now_utc, onupdate=now_utc)

    account = relationship("PlatformAccount")


# ---------------------------------------------------------------------------
# Живой журнал страницы «Тестирование» — подробный лог каждого действия,
# для отладки при прогоне одного товара перед запуском в бой.
# ---------------------------------------------------------------------------

class TestLogLevel(str, enum.Enum):
    info = "info"
    good = "good"
    warn = "warn"
    error = "error"


class TestLogEntry(Base):
    __tablename__ = "test_log_entries"

    id = Column(Integer, primary_key=True)
    uid_1c = Column(String(36), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("platform_accounts.id"), nullable=False)
    level = Column(Enum(TestLogLevel), default=TestLogLevel.info, nullable=False)
    action = Column(String(64), nullable=False)  # push_stock / simulate_order / simulate_cancel / cleanup
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=now_utc)


# ---------------------------------------------------------------------------
# Диагностика живости фоновых процессов (для внешнего мониторинга/NSSM)
# ---------------------------------------------------------------------------

class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_name = Column(String(64), primary_key=True)
    last_run_at = Column(DateTime, nullable=False)
    last_success = Column(Boolean, default=True, nullable=False)
    last_error = Column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Выгрузка остатков 1С НА ЗАДАННОЕ ЧИСЛО (команда EXPORT_STOCK_ON_DATE).
#
# Это СПРАВКА, а не источник остатка. Обычная выгрузка (stock_*.txt) отражает
# склад «сейчас» и через сверку управляет тем, что уходит на площадки; выгрузка
# на дату отражает склад в прошлом. Применить её как текущий остаток означало бы
# разослать на площадки цифры недельной давности, поэтому она живёт в отдельных
# таблицах, приходит в файлах с другим префиксом (ondate_*.txt) и НИКОГДА не
# трогает Product.stock_on_hand и очередь рассылки.
# ---------------------------------------------------------------------------

class StockDateStatus(str, enum.Enum):
    pending = "pending"   # запрос создан, ещё не ушёл в 1С
    sent = "sent"         # строка ушла в task_*.txt, ждём ondate_*.txt
    done = "done"         # файл получен и разобран
    timeout = "timeout"   # ответа нет дольше окна ожидания — обработка 1С не запускалась?


class DiscrepancySource(str, enum.Enum):
    """Чем вызвана правка расхождения. Нужен, чтобы история отвечала не только
    «стало N», но и «почему»: пересчёт склада и правка числа руками — разные
    основания, и доверие к ним разное."""
    fact = "fact"              # ввели факт на дату, отличный от учёта
    manual = "manual"          # правка самого числа: строка или Excel
    offset = "offset"          # задали порог напрямую, расхождение выведено
    reset = "reset"            # «Сбросить порог» — расхождение снято
    migration = "migration"    # разовый перенос при появлении колонки


class StockDiscrepancyLog(Base):
    """История расхождений по товару: было → стало, чем вызвано, кто и когда.

    Отдельной таблицей, а не записью в общий журнал действий, по двум причинам.
    Нужна цепочка ИМЕННО по товару — «когда и из чего это число стало таким», а
    журнал перемешан со всем остальным и чистится через год. И массовые пути
    (кнопки отбора, импорт Excel) в журнал построчно не пишут вовсе: там одна
    запись на всю пачку, то есть по конкретной строке следа не остаётся.

    Храним и контекст измерения — дату, учёт и факт. Без них «стало 6» через
    месяц не проверить: непонятно, из какого учёта и какого пересчёта оно
    получилось.
    """
    __tablename__ = "stock_discrepancy_log"

    id = Column(Integer, primary_key=True)
    uid_1c = Column(String(36), ForeignKey("products.uid_1c"), nullable=False, index=True)
    old_value = Column(Integer, nullable=True)      # NULL — расхождения ещё не было
    new_value = Column(Integer, nullable=True)      # NULL — расхождение снято
    source = Column(Enum(DiscrepancySource), nullable=False)
    username = Column(String(64), nullable=True)
    # Контекст измерения на момент правки — чтобы число можно было перепроверить.
    base_date = Column(Date, nullable=True)
    base_stock = Column(Integer, nullable=True)
    fact = Column(Integer, nullable=True)
    note = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=now_utc, index=True)


class StockDateSnapshot(Base):
    """Один запрос остатков ЦС на дату и его результат."""
    __tablename__ = "stock_date_snapshots"

    id = Column(Integer, primary_key=True)
    snapshot_date = Column(Date, nullable=False, index=True)
    status = Column(Enum(StockDateStatus), default=StockDateStatus.pending, nullable=False)
    requested_by = Column(String(64), nullable=True)      # логин оператора
    created_at = Column(DateTime, default=now_utc)
    sent_at = Column(DateTime, nullable=True)
    batch_filename = Column(String(128), nullable=True)   # какой task_*.txt унёс запрос
    received_at = Column(DateTime, nullable=True)
    result_filename = Column(String(128), nullable=True)  # какой ondate_*.txt принёс ответ
    rows_count = Column(Integer, default=0, nullable=False)
    note = Column(String(255), nullable=True)

    rows = relationship("StockDateRow", back_populates="snapshot",
                        cascade="all, delete-orphan", passive_deletes=True)


class StockDateRow(Base):
    """Строка выгрузки на дату — ровно то, что прислала 1С, без сопоставления с
    нашим каталогом. Внешнего ключа на products нет намеренно: в выгрузке за
    прошлое число встречаются товары, которых у нас в каталоге уже (или ещё) нет,
    и ссылка молча выкидывала бы их из справки."""
    __tablename__ = "stock_date_rows"

    id = Column(Integer, primary_key=True)
    snapshot_id = Column(Integer, ForeignKey("stock_date_snapshots.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    uid_1c = Column(String(36), nullable=False, index=True)
    article = Column(String(128), nullable=True)
    name = Column(String(255), nullable=True)
    size = Column(String(64), nullable=True)
    color = Column(String(64), nullable=True)
    barcodes = Column(Text, nullable=True)                # через запятую, как в файле
    quantity = Column(Integer, nullable=False, default=0)

    snapshot = relationship("StockDateSnapshot", back_populates="rows")


# ---------------------------------------------------------------------------
# Массовая актуализация остатков («Расчёт» на странице «Товары и остатки»).
#
# Почему это задание в базе, а не работа внутри запроса: по каждому товару надо
# опросить каждый его кабинет по историческим заказам. На полусотне позиций это
# сотни обращений к API площадок и минуты работы — браузер столько не ждёт, а
# перезагрузка страницы посреди прогона оставила бы половину товаров
# необработанными без следа. Задание создаёт веб, выполняет ВОРКЕР (у него и
# ключи площадок, и канал 1С), прогресс виден на странице.
# ---------------------------------------------------------------------------

class RecalcStatus(str, enum.Enum):
    pending = "pending"     # создано, воркер ещё не взял
    running = "running"     # идёт
    done = "done"
    failed = "failed"       # воркер не смог начать (список пуст, сбой на старте)
    cancelled = "cancelled"  # снято оператором


class RecalcJob(Base):
    """Один запуск массовой актуализации."""
    __tablename__ = "recalc_jobs"

    id = Column(Integer, primary_key=True)
    status = Column(Enum(RecalcStatus), default=RecalcStatus.pending, nullable=False, index=True)
    created_by = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=now_utc, nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    total = Column(Integer, default=0, nullable=False)
    processed = Column(Integer, default=0, nullable=False)     # товаров пройдено
    orders_applied = Column(Integer, default=0, nullable=False)
    orders_skipped = Column(Integer, default=0, nullable=False)  # уже проведены раньше
    failed_items = Column(Integer, default=0, nullable=False)
    note = Column(Text, nullable=True)

    items = relationship("RecalcItem", back_populates="job",
                         cascade="all, delete-orphan", passive_deletes=True)


class RecalcItem(Base):
    """Один товар внутри задания. Список фиксируется в момент создания: отбор
    по фильтру мог бы измениться, пока задание стоит в очереди, и оператор
    обработал бы не то, что видел на экране."""
    __tablename__ = "recalc_items"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("recalc_jobs.id", ondelete="CASCADE"),
                    nullable=False, index=True)
    uid_1c = Column(String(36), nullable=False, index=True)
    done = Column(Boolean, default=False, nullable=False, index=True)
    orders_applied = Column(Integer, default=0, nullable=False)
    orders_skipped = Column(Integer, default=0, nullable=False)
    error = Column(Text, nullable=True)

    job = relationship("RecalcJob", back_populates="items")


# ---------------------------------------------------------------------------
# Возвраты с площадок. Вещь физически приезжает на склад, проходит проверку и
# либо возвращается в продажу (перемещение склад площадки → ЦС в 1С), либо
# утилизируется (в 1С не идёт ничего — её там уже нет).
#
# Главное про остаток: **пока вещь в разборе, её нет НИГДЕ** — ни в нашем
# `stock_on_hand`, ни в снимке 1С. Они сходятся, и сверке подстраивать нечего.
# Остаток растёт ровно в момент, когда 1С ответила по заданию, и растёт он
# обычным путём — часовым снимком.
# ---------------------------------------------------------------------------

class ReturnStatus(str, enum.Enum):
    accepted = "accepted"          # принят сканом, ждёт проверки
    cleaning = "cleaning"          # в химчистке
    repack = "repack"              # на переупаковке
    held = "held"                  # отложен: посмотрели, решение не приняли
    # Решено вернуть в продажу, задание в 1С в пути. Выйти отсюда РУКАМИ нельзя:
    # заявить, что остаток вырос, пока 1С этого не сказала, — ровно тот дефект,
    # ради которого весь этот проект и переписывался.
    awaiting_1c = "awaiting_1c"
    back_to_sale = "back_to_sale"  # 1С оприходовала — терминальный
    rejected_1c = "rejected_1c"    # 1С отказала или задание разобрано как «документа нет»
    scrapped = "scrapped"          # утилизирован — терминальный


class ScrapReason(str, enum.Enum):
    """Почему утилизировали. ОБЯЗАТЕЛЬНА, и не ради порядка.

    «Утилизировано 40» — число без смысла. «Из них 12 подмена» — повод для
    претензии площадке, а подмена товара при возврате на маркетплейсах штука
    обычная и дорогая.
    """
    defect = "defect"        # брак
    worn = "worn"            # износ, следы носки
    swapped = "swapped"      # подмена: вернули не тот товар
    illiquid = "illiquid"    # неликвид


# Переходы. Таблица — ЕДИНСТВЕННЫЙ источник правды о том, что куда можно:
# разойдись она с кнопками на странице, страница предлагала бы переход, который
# не состоится, и человек решил бы, что кнопка не нажимается.
RETURN_TRANSITIONS = {
    # Из рабочих статусов — в любой другой рабочий, в отправку и в утиль.
    ReturnStatus.accepted: (ReturnStatus.cleaning, ReturnStatus.repack,
                            ReturnStatus.held, ReturnStatus.awaiting_1c,
                            ReturnStatus.scrapped),
    ReturnStatus.cleaning: (ReturnStatus.repack, ReturnStatus.held,
                            ReturnStatus.awaiting_1c, ReturnStatus.scrapped),
    ReturnStatus.repack: (ReturnStatus.cleaning, ReturnStatus.held,
                          ReturnStatus.awaiting_1c, ReturnStatus.scrapped),
    ReturnStatus.held: (ReturnStatus.cleaning, ReturnStatus.repack,
                        ReturnStatus.awaiting_1c, ReturnStatus.scrapped),
    # Из «ждём 1С» — НИКУДА руками. Оба выхода ставит ответ 1С, и только он.
    ReturnStatus.awaiting_1c: (),
    # Отказ 1С разбирает человек: повторить, отложить или выбросить.
    ReturnStatus.rejected_1c: (ReturnStatus.awaiting_1c, ReturnStatus.held,
                               ReturnStatus.scrapped),
    ReturnStatus.back_to_sale: (),
    ReturnStatus.scrapped: (),
}

# Переходы, которые ставит ТОЛЬКО ответ 1С, минуя таблицу выше.
RETURN_BY_1C = (ReturnStatus.back_to_sale, ReturnStatus.rejected_1c)


class ReturnItem(Base):
    """Одна ФИЗИЧЕСКАЯ вещь. Количество всегда 1, и это не упрощение.

    Баркод опознаёт SKU, а не вещь: три одинаковых свитшота 62 размера дают три
    записи с одним баркодом. Статусы при этом персональные — одна уехала в
    химчистку, вторая в утиль, — значит строка обязана быть одна на вещь, а
    различать их в руках позволяет наклейка с номером `RET-<id>`, которая
    печатается сразу на приёмке. Без наклейки статус через неделю стоял бы не на
    той вещи, и страница уверенно показывала бы неправду.
    """
    __tablename__ = "return_items"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=now_utc, nullable=False, index=True)
    # Что отсканировали, КАК ЕСТЬ. Даже если товар в 1С не нашёлся: вещь
    # физически существует независимо от нашего мэппинга, и отказать в приёмке
    # значит заставить человека отложить её в сторону и забыть.
    barcode = Column(String(64), nullable=False, index=True)
    uid_1c = Column(String(36), nullable=True, index=True)
    # Площадка ОБЯЗАТЕЛЬНА: она определяет склад-источник перемещения в 1С
    # (`PENDING_WAREHOUSE_NAME`). Кабинет (ИП) тут ни при чём — карта складов
    # заведена по площадке. На приёмке площадка известна не по вещи, а по
    # КОРОБКЕ: кладовщик едет в конкретный ПВЗ и привозит возвраты одной
    # площадки, поэтому она выбирается сессией приёмки, а не на каждый скан.
    platform = Column(Enum(Platform), nullable=False, index=True)
    status = Column(Enum(ReturnStatus), default=ReturnStatus.accepted,
                    nullable=False, index=True)
    status_changed_at = Column(DateTime, default=now_utc, nullable=False)
    scrap_reason = Column(Enum(ScrapReason), nullable=True)
    note = Column(String(255), nullable=True)
    ftp_task_id = Column(Integer, ForeignKey("ftp_tasks.id"), nullable=True)
    # Та же граница безопасности, что у очереди рассылки и заданий 1С. Ставится
    # с первого дня, а не когда понадобится: дописывать флаг задним числом по
    # живой таблице — худший момент из возможных.
    is_test = Column(Boolean, default=False, nullable=False)

    task = relationship("FtpTask")
    events = relationship("ReturnItemLog", back_populates="item",
                          cascade="all, delete-orphan", passive_deletes=True)


class ReturnItemLog(Base):
    """История по КОНКРЕТНОЙ вещи.

    Отдельной таблицей, а не записями в общий журнал действий, по той же
    причине, что и история расхождения: нужна цепочка по одной вещи, а массовые
    пути (кнопки по отбору) в журнал построчно не пишут вовсе — следа по строке
    там нет.
    """
    __tablename__ = "return_item_log"

    id = Column(Integer, primary_key=True)
    return_id = Column(Integer, ForeignKey("return_items.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    at = Column(DateTime, default=now_utc, nullable=False)
    from_status = Column(Enum(ReturnStatus), nullable=True)
    to_status = Column(Enum(ReturnStatus), nullable=False)
    note = Column(String(255), nullable=True)

    item = relationship("ReturnItem", back_populates="events")
