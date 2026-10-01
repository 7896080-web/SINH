"""Схема базы программы «Маркировка и поставки».

Любое изменение здесь требует миграции (`alembic/versions`), и миграция
обязана переживать собственный обрыв — правило то же, что у sync_admin.
"""
import enum

from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Integer, JSON, LargeBinary,
    Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from markapp.database import Base
from markapp.timeutils import now_utc


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=now_utc)


class Organization(Base):
    """ИП. Реквизиты для УПД и (с этапа 4) доступ к Честному знаку."""
    __tablename__ = "organizations"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)          # «ИП Яворская Т.Н.» — для экрана
    surname = Column(String(100), nullable=False)
    firstname = Column(String(100), nullable=False)
    patronymic = Column(String(100), nullable=False, default="")
    inn = Column(String(12), nullable=False, unique=True)
    ogrnip = Column(String(15), nullable=False)
    address = Column(String(300), nullable=False)
    signer_role = Column(String(50), nullable=False, default="ИП")
    vat_rate = Column(Integer, nullable=False, default=5)
    # Номер договора с покупателем (в эталонах — «б/н»).
    contract_number = Column(String(50), nullable=False, default="б/н")
    # Отправитель на стикерах коробов («Отправитель: ИП Яворская Т.Н»).
    sticker_sender = Column(String(200), nullable=False, default="")
    # Идентификатор участника ЭДО (часть ИдФайл УПД).
    edo_sender_id = Column(String(100), nullable=False, default="")
    # --- Честный знак (этап 4) ---
    chz_contour = Column(String(20), nullable=False, default="production")
    oms_id = Column(String(64), nullable=True)
    connection_id = Column(String(64), nullable=True)
    nk_api_key_enc = Column(Text, nullable=True)
    cert_thumbprint = Column(String(64), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=now_utc)


class Setting(Base):
    """Настройки программы «ключ — значение» (ИП для Lamoda, нумерация…)."""
    __tablename__ = "settings"
    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False, default="")


class AuditLog(Base):
    """Журнал действий с внешним эффектом и правок, которые важно установить."""
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True)
    at = Column(DateTime, nullable=False, default=now_utc, index=True)
    username = Column(String(64), nullable=False, default="")
    action = Column(String(64), nullable=False)
    object = Column(String(100), nullable=False, default="")
    details = Column(Text, nullable=False, default="")


class CatalogItem(Base):
    """Справочник «Одежда полный» — выгрузка каталога Lamoda Seller."""
    __tablename__ = "catalog_items"
    id = Column(Integer, primary_key=True)
    supplier_sku = Column(String(300), nullable=False, unique=True)
    ean = Column(String(20), nullable=False, default="", index=True)
    price = Column(Numeric(12, 2), nullable=True)
    lamoda_sku = Column(String(64), nullable=False, default="")
    parent_sku = Column(String(300), nullable=False, default="")
    size = Column(String(50), nullable=False, default="")
    color = Column(String(100), nullable=False, default="")
    title = Column(String(500), nullable=False, default="")
    tn_ved = Column(String(20), nullable=False, default="")
    tax_class = Column(String(20), nullable=False, default="")
    first_seen_at = Column(DateTime, nullable=False, default=now_utc)
    # Отсутствующие в новой выгрузке артикулы не удаляются (выгрузка могла быть
    # частичной) — видно, когда артикул встречался последний раз.
    last_seen_at = Column(DateTime, nullable=False, default=now_utc)
    updated_at = Column(DateTime, nullable=False, default=now_utc)


class SupplyStatus(str, enum.Enum):
    """Жизненный цикл поставки (ТЗ, разд. 4). Отмены нет: правка — до перемещения."""
    draft = "draft"            # загружен файл
    checked = "checked"        # SUPPLY_CHECK вернул OK
    moved = "moved"            # SUPPLY_MOVEMENT вернул OK — поставка зафиксирована
    upd_issued = "upd_issued"  # выпущен УПД
    accepted = "accepted"      # приёмка Lamoda разобрана (этап 5)


STATUS_LABELS = {
    SupplyStatus.draft: "черновик",
    SupplyStatus.checked: "проверено в 1С",
    SupplyStatus.moved: "перемещено",
    SupplyStatus.upd_issued: "УПД выпущен",
    SupplyStatus.accepted: "принята",
}

# Что можно править и удалять: только пока в 1С ничего не создано.
EDITABLE_STATUSES = (SupplyStatus.draft, SupplyStatus.checked)


class Supply(Base):
    __tablename__ = "supplies"
    id = Column(Integer, primary_key=True)
    number = Column(String(20), nullable=False, unique=True)
    doc_number = Column(String(20), nullable=False, unique=True)
    supply_date = Column(Date, nullable=True)
    # Плановая дата УПД: по ней выбирается схема для стикеров, которые нужны
    # раньше самого УПД (ТЗ, разд. 8). По умолчанию — дата поставки.
    planned_upd_date = Column(Date, nullable=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    status = Column(String(20), nullable=False, default=SupplyStatus.draft.value)
    scheme_choice = Column(String(20), nullable=False, default="auto")
    scheme_reason = Column(Text, nullable=False, default="")
    source_filename = Column(String(300), nullable=False, default="")
    # Сам входящий файл, из которого собран черновик: к нему возвращаются, когда
    # строка поставки вызывает вопрос («а что было в файле?»).
    source_file = Column(LargeBinary, nullable=True)
    # Заголовки дополнительных колонок входного файла (например «ТНВЭД»):
    # они копируются в каждую строку при развёртке.
    extra_headers = Column(JSON, nullable=False, default=list)
    onec_document = Column(String(50), nullable=False, default="")
    moved_at = Column(DateTime, nullable=True)
    is_test = Column(Boolean, nullable=False, default=False)
    created_by = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=now_utc)

    organization = relationship("Organization")
    rows = relationship("SupplyRow", back_populates="supply", order_by="SupplyRow.position",
                        cascade="all, delete-orphan")


class SupplyRow(Base):
    __tablename__ = "supply_rows"
    id = Column(Integer, primary_key=True)
    supply_id = Column(Integer, ForeignKey("supplies.id", ondelete="CASCADE"), nullable=False, index=True)
    position = Column(Integer, nullable=False)
    supplier_sku = Column(String(300), nullable=False)
    qty = Column(Integer, nullable=False)
    # Значения из справочника «Одежда полный» — они идут в поставку.
    price = Column(Numeric(12, 2), nullable=True)
    ean = Column(String(20), nullable=False, default="")
    # Что было в файле, если было (для предупреждения о расхождении).
    file_price = Column(String(50), nullable=False, default="")
    file_ean = Column(String(20), nullable=False, default="")
    extras = Column(JSON, nullable=False, default=list)
    warnings = Column(Text, nullable=False, default="")
    # Ответ 1С на SUPPLY_CHECK (построчно, из supplycheck_*.txt).
    onec_status = Column(String(20), nullable=False, default="")   # ok | short | not_found | ambiguous
    onec_item_id = Column(String(64), nullable=False, default="")
    onec_article = Column(String(200), nullable=False, default="")
    onec_name = Column(String(500), nullable=False, default="")
    onec_size = Column(String(50), nullable=False, default="")
    onec_color = Column(String(100), nullable=False, default="")
    onec_stock = Column(Integer, nullable=True)

    supply = relationship("Supply", back_populates="rows")


class OnecTaskStatus(str, enum.Enum):
    pending = "pending"    # записано в базу, файл ещё не положен
    sent = "sent"          # файл лежит в C:\sync\tasks
    done = "done"          # 1С ответила OK
    failed = "failed"      # 1С ответила ERROR
    timeout = "timeout"    # ответа нет дольше ONEC_TIMEOUT_MINUTES


class OnecTask(Base):
    """Задание программы в 1С. Только свои команды (ТЗ, 9.2)."""
    __tablename__ = "onec_tasks"
    id = Column(Integer, primary_key=True)
    command = Column(String(32), nullable=False)
    order_id = Column(String(64), nullable=False, index=True)
    line = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default=OnecTaskStatus.pending.value, index=True)
    filename = Column(String(100), nullable=False, default="")
    supply_id = Column(Integer, ForeignKey("supplies.id"), nullable=True, index=True)
    result_status = Column(String(20), nullable=False, default="")
    result_detail = Column(Text, nullable=False, default="")
    is_test = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=now_utc)
    sent_at = Column(DateTime, nullable=True)
    answered_at = Column(DateTime, nullable=True)


class FboUpload(Base):
    """Выгрузка Lamoda «Поставки FBO», загруженная к поставке."""
    __tablename__ = "fbo_uploads"
    id = Column(Integer, primary_key=True)
    supply_id = Column(Integer, ForeignKey("supplies.id"), nullable=False, index=True)
    filename = Column(String(300), nullable=False)
    content = Column(LargeBinary, nullable=False)
    ok = Column(Boolean, nullable=False, default=False)
    report = Column(Text, nullable=False, default="")
    username = Column(String(64), nullable=False, default="")
    uploaded_at = Column(DateTime, nullable=False, default=now_utc)


class UpdDocument(Base):
    """Выпущенный УПД. XML хранится в базе — ровно тот, что ушёл в Диадок."""
    __tablename__ = "upd_documents"
    __table_args__ = (UniqueConstraint("doc_number", name="uq_upd_doc_number"),)
    id = Column(Integer, primary_key=True)
    supply_id = Column(Integer, ForeignKey("supplies.id"), nullable=False, index=True)
    fbo_upload_id = Column(Integer, ForeignKey("fbo_uploads.id"), nullable=False)
    doc_number = Column(String(20), nullable=False)
    doc_date = Column(Date, nullable=False)
    scheme = Column(String(20), nullable=False)
    scheme_manual = Column(Boolean, nullable=False, default=False)
    scheme_reason = Column(Text, nullable=False, default="")
    id_file = Column(String(200), nullable=False)
    xml = Column(LargeBinary, nullable=False)
    positions = Column(Integer, nullable=False)
    total_with_vat = Column(Numeric(14, 2), nullable=False)
    check_report = Column(Text, nullable=False, default="")
    has_errors = Column(Boolean, nullable=False, default=False)
    username = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=now_utc)


class WorkerHeartbeat(Base):
    """Отметка фонового задания: когда отработало и с каким итогом."""
    __tablename__ = "worker_heartbeats"
    name = Column(String(64), primary_key=True)
    last_run_at = Column(DateTime, nullable=True)
    last_success = Column(Boolean, nullable=False, default=True)
    last_error = Column(Text, nullable=False, default="")


class GtinPair(Base):
    """Справочник «размерный артикул ↔ GTIN» (ТЗ, 5.3). Строго один к одному.

    `exported_at` пусто — пару Lamoda ещё не получала (её надо выгрузить в
    `product_gtin`). Пары, пришедшие из файлов `product_gtin`, по определению
    уже у Lamoda.
    """
    __tablename__ = "gtin_pairs"
    id = Column(Integer, primary_key=True)
    supplier_sku = Column(String(300), nullable=False, unique=True)
    gtin = Column(String(14), nullable=False, unique=True)
    source = Column(String(20), nullable=False)          # product_gtin | fbo | manual
    source_name = Column(String(300), nullable=False, default="")
    exported_at = Column(DateTime, nullable=True)
    created_by = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=now_utc)


class NkCard(Base):
    """Кэш карточки Национального каталога по GTIN.

    Лимит `/nk/product` жёсткий (10 запросов за 5 минут), поэтому этикетки и
    проверки читают этот кэш, а не сеть. Сырой ответ хранится целиком: имена
    атрибутов одежды документация показывает только на примере сметаны, и
    разбор настраивается по живому ответу.
    """
    __tablename__ = "nk_cards"
    gtin = Column(String(14), primary_key=True)
    status = Column(String(20), nullable=False, default="pending")  # pending | ok | not_found | error
    good_id = Column(String(32), nullable=False, default="")
    name = Column(String(500), nullable=False, default="")
    card_status = Column(String(64), nullable=False, default="")
    color = Column(String(200), nullable=False, default="")
    size = Column(String(100), nullable=False, default="")
    tn_ved = Column(String(20), nullable=False, default="")
    attrs = Column(JSON, nullable=False, default=list)
    raw = Column(Text, nullable=False, default="")
    error = Column(Text, nullable=False, default="")
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True)
    requested_at = Column(DateTime, nullable=False, default=now_utc)
    fetched_at = Column(DateTime, nullable=True)


class NkRequest(Base):
    """Отметки запросов к Нацкаталогу — для ограничителя. В базе, а не в памяти:
    веб (кнопка «обновить») и воркер — два процесса."""
    __tablename__ = "nk_requests"
    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    at = Column(DateTime, nullable=False, default=now_utc, index=True)
    http_status = Column(Integer, nullable=True)


class OnecBarcode(Base):
    """Справочник баркодов 1С — снимок, тот же, что у sync_admin (barcodes_*.txt):
    строка на баркод, `item_id` — цветоразмерный SKU (характеристика, иначе
    номенклатура). У SKU пул баркодов; баркод обязан принадлежать одному SKU —
    две строки с одним баркодом и разными SKU и есть нарушение, которое страница
    сопоставления показывает. Загрузка заменяет снимок целиком (`mapping.py`)."""
    __tablename__ = "onec_barcodes"
    __table_args__ = (UniqueConstraint("barcode", "item_id", name="uq_onec_barcode_item"),)
    id = Column(Integer, primary_key=True)
    barcode = Column(String(64), nullable=False, index=True)
    item_id = Column(String(64), nullable=False, index=True)
    article = Column(String(200), nullable=False, default="")
    name = Column(String(500), nullable=False, default="")
    size = Column(String(100), nullable=False, default="")
    color = Column(String(100), nullable=False, default="")
