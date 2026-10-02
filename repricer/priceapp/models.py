"""Схема базы программы «Репрайсер».

Любое изменение здесь требует миграции (`alembic/versions`), и миграция обязана
переживать собственный обрыв — правило то же, что у sync_admin и «Маркировки».

Экономика (так её задал заказчик):
    себестоимость, ₽  = себестоимость 1С, $  ×  курс ЦБ
    к получению, ₽    = цена на площадке  ×  (1 − комиссия площадки, %)
    наценка, ₽        = к получению − себестоимость, ₽
    коэффициент       = к получению / себестоимость, ₽   (2 = +100%, 2,5 = +150%, 3 = +200%)
"""
import enum

from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Integer, Numeric, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from priceapp.database import Base
from priceapp.timeutils import now_utc


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=now_utc)


class Setting(Base):
    """Настройки «ключ — значение» (режим курса, ручной курс, отметки обмена с 1С)."""
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


class WorkerHeartbeat(Base):
    """Отметка фонового задания: когда отработало и с каким итогом."""
    __tablename__ = "worker_heartbeats"
    name = Column(String(64), primary_key=True)
    last_run_at = Column(DateTime, nullable=True)
    last_success = Column(Boolean, nullable=False, default=True)
    last_error = Column(Text, nullable=False, default="")


# --- Кабинеты площадок и их ключи (страница «API-ключи») ---------------------------

class Account(Base):
    """Кабинет площадки. У WB их может быть несколько (разные ИП) — со своими
    ключами и каталогом. Правила цены и комиссия — у ПЛОЩАДКИ (`PlatformRule`),
    общие для всех её кабинетов."""
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    platform = Column(String(8), nullable=False)          # wb / ozon / kit / lamoda
    name = Column(String(128), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    last_check_at = Column(DateTime, nullable=True)
    last_check_ok = Column(Boolean, nullable=True)
    last_check_message = Column(Text, nullable=False, default="")
    catalog_loaded_at = Column(DateTime, nullable=True)
    catalog_note = Column(Text, nullable=False, default="")
    # Текущие цены с площадки (`accounts.load_prices`): когда и с какой оговоркой.
    prices_loaded_at = Column(DateTime, nullable=True)
    prices_note = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=now_utc)

    credentials = relationship("ApiCredential", back_populates="account",
                               cascade="all, delete-orphan")


class ApiCredential(Base):
    """Ключ кабинета — ЗАШИФРОВАННЫМ (`crypto.py`)."""
    __tablename__ = "api_credentials"
    __table_args__ = (UniqueConstraint("account_id", "field_name", name="uq_cred_account_field"),)
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    field_name = Column(String(32), nullable=False)
    encrypted_value = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime, nullable=False, default=now_utc, onupdate=now_utc)

    account = relationship("Account", back_populates="credentials")


class PlatformItem(Base):
    """Строка каталога кабинета: баркод — единица (у WB одна карточка nmID
    охватывает несколько размеров). `external_id` — размер-цвет на площадке
    (WB `nmID:chrtID`, Ozon product_id, Kit variant_id)."""
    __tablename__ = "platform_items"
    __table_args__ = (UniqueConstraint("account_id", "barcode", name="uq_item_account_barcode"),)
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False,
                        index=True)
    external_id = Column(String(128), nullable=False, default="")
    barcode = Column(String(64), nullable=False, index=True)
    article = Column(String(200), nullable=False, default="")
    name = Column(String(500), nullable=False, default="")
    size = Column(String(64), nullable=False, default="")
    fetched_at = Column(DateTime, nullable=False, default=now_utc)
    # Цена, которая СЕЙЧАС стоит на площадке (`accounts.load_prices`). NULL — не
    # загружали или площадка о ней не сказала. `current_price` — то же поле, что мы
    # отправляем (у WB цена до скидки), `current_sale_price` — что платит
    # покупатель (у WB с учётом скидки продавца): наценку по текущей считаем от неё.
    current_price = Column(Integer, nullable=True)
    current_sale_price = Column(Integer, nullable=True)
    price_loaded_at = Column(DateTime, nullable=True)
    # Что площадка говорит о цене: OK / PROCESSING / ERROR / QUARANTINE (сейчас
    # сообщает только Lamoda). NULL — площадка статуса не даёт.
    price_status = Column(String(16), nullable=True)
    # Минимальная цена площадки для этой позиции, ₽ (Lamoda: по категории и
    # бренду). Только ПРЕДУПРЕЖДЕНИЕ: сопоставление категорий — по названиям.
    min_price = Column(Integer, nullable=True)


# --- 1С: справочник баркодов, себестоимость, задания ------------------------------

class OnecBarcode(Base):
    """Справочник баркодов 1С — снимок (тот же формат, что у sync_admin):
    строка на баркод, `item_id` — размер-цвет SKU 1С. Заменяется целиком."""
    __tablename__ = "onec_barcodes"
    __table_args__ = (UniqueConstraint("barcode", "item_id", name="uq_onec_barcode_item"),)
    id = Column(Integer, primary_key=True)
    barcode = Column(String(64), nullable=False, index=True)
    item_id = Column(String(64), nullable=False, index=True)
    article = Column(String(200), nullable=False, default="")
    name = Column(String(500), nullable=False, default="")
    size = Column(String(100), nullable=False, default="")
    color = Column(String(100), nullable=False, default="")


class OnecCost(Base):
    """Себестоимость SKU 1С в ДОЛЛАРАХ («Цена СС» поступлений, регистр
    «Себестоимость номенклатуры»). Снимок по команде EXPORT_COST_PRICES:
    SKU, которого нет в новом файле, прежнюю себестоимость сохраняет."""
    __tablename__ = "onec_costs"
    item_id = Column(String(64), primary_key=True)
    cost_usd = Column(Numeric(12, 2), nullable=False)
    loaded_at = Column(DateTime, nullable=False, default=now_utc)


class OnecTaskStatus(str, enum.Enum):
    pending = "pending"    # записано в базу, файл ещё не положен
    sent = "sent"          # файл лежит в C:\sync\tasks
    done = "done"          # 1С ответила OK
    failed = "failed"      # 1С ответила ERROR
    timeout = "timeout"    # ответа нет дольше ONEC_TIMEOUT_MINUTES


class OnecTask(Base):
    """Задание программы в 1С. Только свои команды (`onec.COMMANDS`)."""
    __tablename__ = "onec_tasks"
    id = Column(Integer, primary_key=True)
    command = Column(String(32), nullable=False)
    order_id = Column(String(64), nullable=False, index=True)
    line = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default=OnecTaskStatus.pending.value, index=True)
    filename = Column(String(100), nullable=False, default="")
    result_status = Column(String(20), nullable=False, default="")
    result_detail = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=now_utc)
    sent_at = Column(DateTime, nullable=True)
    answered_at = Column(DateTime, nullable=True)


# --- Курс доллара -------------------------------------------------------------------

class ExchangeRate(Base):
    """Курс USD/RUB на дату. Источник — ЦБ РФ (`rates.py`) или ввод руками."""
    __tablename__ = "exchange_rates"
    __table_args__ = (UniqueConstraint("rate_date", "source", name="uq_rate_date_source"),)
    id = Column(Integer, primary_key=True)
    rate_date = Column(Date, nullable=False, index=True)
    usd_rub = Column(Numeric(12, 4), nullable=False)
    source = Column(String(16), nullable=False)            # cbr / manual
    fetched_at = Column(DateTime, nullable=False, default=now_utc)


# --- Сопоставление ------------------------------------------------------------------

class ManualLink(Base):
    """Баркод площадки -> SKU 1С, подтверждённый человеком на странице
    «Сопоставление» (по правилам артикулов). Основной путь — совпадение баркода
    со справочником 1С; это — дополнение для баркодов, которых в 1С нет."""
    __tablename__ = "manual_links"
    __table_args__ = (UniqueConstraint("account_id", "barcode", name="uq_link_account_barcode"),)
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    barcode = Column(String(64), nullable=False)
    item_id = Column(String(64), nullable=False)
    source = Column(String(32), nullable=False, default="article_match")
    created_by = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=now_utc)


class ArticleMatchRule(Base):
    """Правило сопоставления артикулов кабинета (`article_matching.py`)."""
    __tablename__ = "article_match_rules"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False,
                        unique=True)
    kinds = Column(String(128), nullable=False, default="exact,size")
    strip_prefix = Column(String(64), nullable=True)
    strip_suffix = Column(String(64), nullable=True)
    updated_at = Column(DateTime, nullable=False, default=now_utc, onupdate=now_utc)


# --- Цены ---------------------------------------------------------------------------

class PlatformRule(Base):
    """Правило цены ПЛОЩАДКИ — одно на все её кабинеты (у WB три ИП, условия у
    них одни). Наценка — КОЭФФИЦИЕНТОМ к себестоимости (2 = +100%, 2,5 = +150%,
    3 = +200%). Цена = себестоимость ₽ × коэффициент / (1 − комиссия%), вверх до
    шага с «красивым» окончанием.

    Или — от ДРУГОЙ площадки: при заданной `base_platform` цена = расчётная цена
    базовой площадки × `base_coef` (Ozon = WB × 1,1). Цепочек нет: база сама
    базы не имеет. Пол наценки проверяется по СВОЕЙ комиссии всегда."""
    __tablename__ = "platform_rules"
    id = Column(Integer, primary_key=True)
    platform = Column(String(8), nullable=False, unique=True)
    # Комиссия площадки, % от цены. NULL — не задана: расчёт по площадке не идёт
    # (ноль — тоже значение, но его ставят явно).
    commission_percent = Column(Numeric(6, 2), nullable=True)
    # 1 — наценки нет: правило не настроено, расчёт по нему не идёт.
    markup_coef = Column(Numeric(7, 3), nullable=False, default=1)
    round_step = Column(Integer, nullable=False, default=1)
    round_minus = Column(Integer, nullable=False, default=0)
    # Пол: к получению не меньше себестоимости × этот коэффициент — ни расчётом,
    # ни ручной ценой, ни ценой от базовой площадки. 1 — «не в убыток».
    min_markup_coef = Column(Numeric(7, 3), nullable=False, default=1)
    max_change_percent = Column(Numeric(7, 2), nullable=False, default=20)
    base_platform = Column(String(8), nullable=True)
    base_coef = Column(Numeric(7, 3), nullable=True)
    updated_at = Column(DateTime, nullable=False, default=now_utc, onupdate=now_utc)


class ProductPrice(Base):
    """Цена SKU 1С в кабинете: ручная (обходит правило, но не пол) и последняя
    принятая площадкой."""
    __tablename__ = "product_prices"
    __table_args__ = (UniqueConstraint("item_id", "account_id", name="uq_price_item_account"),)
    id = Column(Integer, primary_key=True)
    item_id = Column(String(64), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    manual_price = Column(Integer, nullable=True)
    last_sent_price = Column(Integer, nullable=True)
    last_sent_at = Column(DateTime, nullable=True)


class PriceChangeStatus(str, enum.Enum):
    proposed = "proposed"    # рассчитано, ждёт решения
    blocked = "blocked"      # нарушает ограничитель (пол / шаг) — см. block_reason
    approved = "approved"    # подтверждено, ждёт отправки
    sent = "sent"            # площадка приняла
    error = "error"          # площадка отказала за все попытки
    rejected = "rejected"    # отклонено или вытеснено новым расчётом


class PriceChange(Base):
    """Предложение изменить цену SKU в кабинете — со всей экономикой на момент расчёта."""
    __tablename__ = "price_changes"
    id = Column(Integer, primary_key=True)
    item_id = Column(String(64), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    barcode = Column(String(64), nullable=False, default="")     # чем адресуем позицию
    cost_usd = Column(Numeric(12, 2), nullable=True)
    usd_rub = Column(Numeric(12, 4), nullable=True)
    cost_rub = Column(Numeric(12, 2), nullable=True)
    commission_percent = Column(Numeric(6, 2), nullable=True)
    old_price = Column(Integer, nullable=True)
    new_price = Column(Integer, nullable=False)
    markup_rub = Column(Numeric(12, 2), nullable=True)
    markup_coef = Column(Numeric(8, 2), nullable=True)     # к получению / себестоимость
    source = Column(String(16), nullable=False, default="rule")   # rule / manual / base
    status = Column(String(16), nullable=False, default=PriceChangeStatus.proposed.value, index=True)
    block_reason = Column(String(16), nullable=True)
    note = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, default=now_utc)
    decided_by = Column(String(64), nullable=True)
    decided_at = Column(DateTime, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    # Тренировочная запись НИКОГДА не уходит на площадку (`dispatch.py`).
    is_test = Column(Boolean, nullable=False, default=False)

    account = relationship("Account")


class SavedFilter(Base):
    """Сохранённый отбор — ссылка на страницу с параметрами. Одна на установку:
    отборы общие для всех операторов."""
    __tablename__ = "saved_filters"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    url = Column(String(1000), nullable=False)
    created_by = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=now_utc)
