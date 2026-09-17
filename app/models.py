import enum
import uuid
from datetime import datetime
from app.timeutils import now_utc

from sqlalchemy import (
    Column, String, Boolean, Integer, DateTime, Date, Enum, ForeignKey,
    UniqueConstraint, Text,
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
    uid_1c = Column(String(36), ForeignKey("products.uid_1c"), nullable=False)
    # Площадка (не кабинет) — чисто информационная пометка "откуда впервые
    # увидели баркод"; баркод физический и от конкретного кабинета не зависит.
    source_platform = Column(String(16), nullable=True)
    created_at = Column(DateTime, default=now_utc)

    product = relationship("Product", back_populates="barcodes")


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
    status = Column(Enum(AnomalyStatus), default=AnomalyStatus.new, nullable=False)
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
    uid_1c = Column(String(36), ForeignKey("products.uid_1c"), nullable=True)
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


class FtpTask(Base):
    """Одна строка внутри файла task_*.txt для старой базы 1С."""
    __tablename__ = "ftp_tasks"

    id = Column(Integer, primary_key=True)
    command = Column(String(32), nullable=False)  # CREATE_MOVEMENT / CANCEL_MOVEMENT
    barcode = Column(String(64), nullable=True)
    warehouse_from = Column(String(64), nullable=True)
    warehouse_to = Column(String(64), nullable=True)
    quantity = Column(Integer, nullable=True)
    order_id = Column(String(128), nullable=False)
    account_id = Column(Integer, ForeignKey("platform_accounts.id"), nullable=False)
    # Дата документа перемещения в 1С (старт задним числом). NULL = текущая дата.
    movement_date = Column(Date, nullable=True)
    status = Column(Enum(FtpTaskStatus), default=FtpTaskStatus.pending, nullable=False)
    batch_filename = Column(String(128), nullable=True)  # какой task_*.txt унёс эту строку
    created_at = Column(DateTime, default=now_utc)
    sent_at = Column(DateTime, nullable=True)
    result_status = Column(String(16), nullable=True)   # OK / ERROR
    result_detail = Column(String(255), nullable=True)   # номер документа или текст ошибки
    completed_at = Column(DateTime, nullable=True)
    # Симулированный заказ со страницы тестирования — НИКОГДА не должен
    # попасть в реальный файл задания для 1С. ftp_channel.py явно исключает
    # такие строки при сборке task_*.txt.
    is_test = Column(Boolean, default=False, nullable=False)

    account = relationship("PlatformAccount")


# ---------------------------------------------------------------------------
# Раздел 9: сверка (реконсиляция) — по товару в целом, кабинет площадки тут
# ни при чём: сверяем физический остаток ЦС, а не то, что на какой площадке.
# ---------------------------------------------------------------------------

class ReconciliationClassification(str, enum.Enum):
    normal = "normal"
    auto_plus = "auto_plus"
    auto_minus = "auto_minus"
    needs_review = "needs_review"


class ReconciliationLog(Base):
    __tablename__ = "reconciliation_log"

    id = Column(Integer, primary_key=True)
    uid_1c = Column(String(36), ForeignKey("products.uid_1c"), nullable=False)
    checked_at = Column(DateTime, default=now_utc)
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
