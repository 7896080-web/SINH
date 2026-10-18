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
    # Диапазон безопасности для участия в акциях — маржинальность «от … до …»
    # по ТЕКУЩЕЙ цене площадки (с её скидкой продавца). Ниже «от» — программа
    # возвращает цену по коэффициенту кабинета (`guard.py`); выше «до» — только
    # пометка. NULL — граница не задана.
    guard_min_margin = Column(Numeric(7, 3), nullable=True)
    guard_max_margin = Column(Numeric(7, 3), nullable=True)
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
    # Категория товара на площадке (WB — предмет, Ozon — тип товара, Kit —
    # категория витрины, Lamoda — нижний уровень категории). По ней группируют
    # товары и задают маржинальность на категорию; пусто — площадка не сказала.
    category = Column(String(200), nullable=False, default="")
    category_id = Column(String(64), nullable=False, default="")
    # Комиссия площадки по её ТАРИФУ для этой позиции, % (WB — по предмету из
    # тарифной таблицы, Ozon — по товару из ответа о ценах). FBS — со своего
    # склада, FBO — со склада площадки; какую брать, решает правило площадки.
    # NULL — тариф неизвестен: берётся комиссия из правила.
    tariff_fbs = Column(Numeric(6, 2), nullable=True)
    tariff_fbo = Column(Numeric(6, 2), nullable=True)
    tariff_loaded_at = Column(DateTime, nullable=True)


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


class SyncBarcode(Base):
    """Привязка «баркод -> SKU 1С», сделанная в Sync Admin мимо справочника 1С: файлом
    (`excel_import`), переподвязкой (`excel_repoint`) или догадкой по пулу (`pool_match`).
    Снимок выгрузки «Мэппинг» Sync Admin (`mapping.load_sync_mapping`), заменяется целиком.
    Так репрайсер считает цену по тому же товару 1С, по которому Sync Admin ведёт остатки
    и заказы (`mapping._build`)."""
    __tablename__ = "sync_barcodes"
    id = Column(Integer, primary_key=True)
    barcode = Column(String(64), nullable=False, unique=True, index=True)
    item_id = Column(String(64), nullable=False, index=True)
    article = Column(String(200), nullable=False, default="")
    name = Column(String(500), nullable=False, default="")
    size = Column(String(100), nullable=False, default="")
    color = Column(String(100), nullable=False, default="")
    source = Column(String(32), nullable=False, default="")
    loaded_at = Column(DateTime, nullable=False, default=now_utc)


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
    них одни).

    Цена = БАЗОВАЯ цена × коэффициент, где базовая = себестоимость 1С, $ × курс ЦБ
    (руками не правится — `pricing.base_price`). Коэффициент задаётся на АРТИКУЛ
    по площадке или по кабинету (`ArticleCoef`); `base_coef` здесь — коэффициент
    площадки ПО УМОЛЧАНИЮ для артикулов, у которых своего нет (NULL — не задан:
    у таких артикулов цены нет). Дальше округление вверх до шага с «красивым»
    окончанием; пол маржинальности и лимит шага — по своей комиссии всегда.

    `markup_coef` и `base_platform` — от прежней схемы («себестоимость ×
    коэффициент / (1 − комиссия)» и «цена от другой площадки»), расчётом больше
    не читаются; колонки оставлены, чтобы не перестраивать таблицу."""
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
    # ни ручной ценой. 1 — «не в убыток».
    min_markup_coef = Column(Numeric(7, 3), nullable=False, default=1)
    max_change_percent = Column(Numeric(7, 2), nullable=False, default=20)
    # Надбавка к комиссии, процентных пунктов: «тариф 15,5% + 3» — логистика,
    # эквайринг и прочее, что площадка удерживает сверх процента. Прибавляется
    # и к тарифу, и к комиссии из правила.
    commission_extra = Column(Numeric(6, 2), nullable=False, default=0)
    # Какой тариф площадки брать: fbs (со своего склада) или fbo (со склада площадки).
    tariff_model = Column(String(4), nullable=False, default="fbs")
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
    source = Column(String(16), nullable=False, default="rule")   # rule / manual / platform / base / rollback
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


class ArticleCoef(Base):
    """Наценка, заданная на АРТИКУЛ 1С — сразу для всех его размеров (и цветов).
    `account_id` = 0 — на всю площадку (у WB — для всех трёх ИП), иначе — только
    для этого кабинета, и он главнее площадочного. Ноль, а не NULL: уникальность
    пары с NULL SQLite не проверяет.

    `kind` — что задано: `coef` — коэффициент от базовой (цена = базовая × k),
    `margin` — маржинальность (цена такая, чтобы к получению / себестоимость = m
    при ТЕКУЩЕЙ комиссии и скидке продавца: площадка сменила тариф — цена
    пересчитается, маржинальность останется)."""
    __tablename__ = "article_coefs"
    __table_args__ = (UniqueConstraint("article", "platform", "account_id", name="uq_article_coef"),)
    id = Column(Integer, primary_key=True)
    article = Column(String(200), nullable=False, index=True)
    platform = Column(String(8), nullable=False)
    account_id = Column(Integer, nullable=False, default=0)
    coef = Column(Numeric(7, 3), nullable=False)
    kind = Column(String(8), nullable=False, default="coef")
    updated_at = Column(DateTime, nullable=False, default=now_utc, onupdate=now_utc)
    updated_by = Column(String(64), nullable=False, default="")


class CategoryTarget(Base):
    """Наценка на КАТЕГОРИЮ площадки — по кабинету (`account_id`) или на всю
    площадку (0). Слабее наценки артикула, сильнее умолчания площадки. `kind` —
    как у `ArticleCoef`."""
    __tablename__ = "category_targets"
    __table_args__ = (UniqueConstraint("platform", "account_id", "category", name="uq_category_target"),)
    id = Column(Integer, primary_key=True)
    platform = Column(String(8), nullable=False)
    account_id = Column(Integer, nullable=False, default=0)
    category = Column(String(200), nullable=False)
    kind = Column(String(8), nullable=False, default="margin")
    value = Column(Numeric(7, 3), nullable=False)
    updated_at = Column(DateTime, nullable=False, default=now_utc, onupdate=now_utc)
    updated_by = Column(String(64), nullable=False, default="")


class ReferencePrice(Base):
    """ЭТАЛОННАЯ цена SKU в кабинете WB — последняя цена, которую программа отправила
    на карточку (`reference.py`). От неё считается «площадка снизила цену»: если
    «к получению» по текущей цене площадки ниже эталонной больше чем на 10%,
    эталонная возвращается автоматически. При сдвиге курса больше чем на 2% от
    `usd_rub` эталонная пересчитывается по наценке. `ref_payout` — к получению
    с эталонной цены (с учётом скидки продавца и комиссии на момент записи)."""
    __tablename__ = "reference_prices"
    __table_args__ = (UniqueConstraint("account_id", "item_id", name="uq_reference_price"),)
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    item_id = Column(String(64), nullable=False, index=True)
    ref_price = Column(Integer, nullable=False)
    ref_payout = Column(Numeric(12, 2), nullable=True)
    commission_percent = Column(Numeric(6, 2), nullable=True)
    discount = Column(Numeric(6, 4), nullable=False, default=0)
    usd_rub = Column(Numeric(12, 4), nullable=True)
    source = Column(String(16), nullable=False, default="sent")   # sent / rate / adopt
    set_at = Column(DateTime, nullable=False, default=now_utc)
    # На WB сейчас стоит не эталон, а эталон − столько % — отправлено кнопкой «Отправить сейчас»
    # (`wb_discount.send_now`). Пересчёт эталона по курсу держит этот сдвиг; возврат полного
    # эталона (цена ушла ниже порога) и любая новая запись эталона его снимают.
    held_pct = Column(Numeric(5, 2), nullable=True)


class WbDiscountRule(Base):
    """ПОРОГ скидки от эталона на WB — по категории WB (`kind` = category, ключ — предмет) или по
    артикулу 1С (`kind` = article). Цена после скидки продавца может быть ниже эталона не больше
    чем на `max_disc` %; ниже — правило возврата (`reference.run`) возвращает ПОЛНЫЙ эталон.
    Артикул главнее категории, категория — порога по умолчанию (`ref_drop_percent`). Действует,
    пока его не изменят руками. `account_id` = 0 — на все кабинеты WB, иначе только для этого
    кабинета, и он главнее (ноль, а не NULL: уникальность с NULL SQLite не проверяет). `now_*` —
    последняя отправка «эталон − N%» (`wb_discount.send_now`), для показа на странице."""
    __tablename__ = "wb_discount_rules"
    __table_args__ = (UniqueConstraint("kind", "key", "account_id", name="uq_wb_discount_rule"),)
    id = Column(Integer, primary_key=True)
    kind = Column(String(8), nullable=False)               # category / article
    key = Column(String(200), nullable=False)
    account_id = Column(Integer, nullable=False, default=0)
    max_disc = Column(Numeric(5, 2), nullable=True)
    now_pct = Column(Numeric(5, 2), nullable=True)
    now_at = Column(DateTime, nullable=True)
    now_by = Column(String(64), nullable=False, default="")
    # Порог «для акции» (страница «Акции WB»): действует до `until` (конец акции), потом
    # возвращается `prev_max_disc` (None — своего порога не было). Правка руками делает его
    # постоянным: `until` снимается.
    until = Column(DateTime, nullable=True)
    prev_max_disc = Column(Numeric(5, 2), nullable=True)
    until_promo_id = Column(Integer, nullable=True)
    until_note = Column(String(300), nullable=False, default="")
    updated_at = Column(DateTime, nullable=False, default=now_utc, onupdate=now_utc)
    updated_by = Column(String(64), nullable=False, default="")


class WbPromotion(Base):
    """Акция из календаря акций WB (`wb_promo.refresh`) — по кабинету: ID акции у WB общий, а
    товары для участия у каждого кабинета свои. `type`: regular — акция, auto — автоакция (её
    товары API не отдаёт)."""
    __tablename__ = "wb_promotions"
    __table_args__ = (UniqueConstraint("account_id", "promo_id", name="uq_wb_promotion"),)
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    promo_id = Column(Integer, nullable=False)
    name = Column(String(300), nullable=False, default="")
    type = Column(String(16), nullable=False, default="regular")
    start_at = Column(DateTime, nullable=True)
    end_at = Column(DateTime, nullable=True)
    description = Column(Text, nullable=False, default="")
    advantages = Column(Text, nullable=False, default="")
    participation = Column(Integer, nullable=True)
    in_action_total = Column(Integer, nullable=True)
    exceptions = Column(Integer, nullable=True)
    fetched_at = Column(DateTime, nullable=False, default=now_utc)
    items_fetched_at = Column(DateTime, nullable=True)
    items_note = Column(Text, nullable=False, default="")


class WbPromotionItem(Base):
    """Карточка (nmID) из списка товаров обычной акции: участвует ли, цена и скидка сейчас,
    плановая цена (наибольшая цена со скидкой для участия) и рекомендуемая скидка. `added_at` —
    мы отправили её в акцию и ждём, что WB это покажет."""
    __tablename__ = "wb_promotion_items"
    __table_args__ = (UniqueConstraint("account_id", "promo_id", "nm_id", name="uq_wb_promotion_item"),)
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    promo_id = Column(Integer, nullable=False, index=True)
    nm_id = Column(String(32), nullable=False)
    in_action = Column(Boolean, nullable=False, default=False)
    price = Column(Integer, nullable=True)
    plan_price = Column(Integer, nullable=True)
    discount = Column(Integer, nullable=True)
    plan_discount = Column(Integer, nullable=True)
    fetched_at = Column(DateTime, nullable=False, default=now_utc)
    added_at = Column(DateTime, nullable=True)
    added_by = Column(String(64), nullable=False, default="")
    # Срок участия («Участвовать до»): по его истечении программа ставит полный эталон — цена
    # поднимается, и карточка выходит из акции (`wb_promo.leave_due`). `left_at` — когда вышла.
    leave_at = Column(DateTime, nullable=True)
    leave_by = Column(String(64), nullable=False, default="")
    left_at = Column(DateTime, nullable=True)
    # Акция закончилась — карточке вернули эталон (`wb_promo.end_due`; WB сам цену после акции не
    # возвращает). `end_done_at` — когда обработали, `end_note` — чем кончилось.
    end_done_at = Column(DateTime, nullable=True)
    end_note = Column(String(200), nullable=False, default="")


class ReferenceDiscount(Base):
    """ЭТАЛОННАЯ скидка продавца карточки WB (nmID) — та, которую мы поставили
    (`discount_guard.py`). WB умеет менять скидку сам — автоакцией из календаря акций, — и
    цена для покупателя тогда уходит мимо нас, а правило эталонной цены этого не видит: цена
    до скидки при этом не меняется. Скидка на WB одна на карточку и целая."""
    __tablename__ = "reference_discounts"
    __table_args__ = (UniqueConstraint("account_id", "nm_id", name="uq_reference_discount"),)
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    nm_id = Column(String(32), nullable=False)
    discount = Column(Integer, nullable=False)
    source = Column(String(16), nullable=False, default="adopt")   # adopt / sent
    set_at = Column(DateTime, nullable=False, default=now_utc)


class CardEcon(Base):
    """Экономика строки «карточка площадки + артикул 1С» кабинета, которой нет в отчётах площадки
    через API: доля выплаты от цены продавца `k`, цена покупателя / цена продавца `w`, логистика с
    отказами и штрафы на нетто-единицу `f_logistics`, нетто-продажи за период `net_units`.
    Заполняется импортом Excel на странице «Утверждение цен» (`approval.py`)."""
    __tablename__ = "card_econ"
    __table_args__ = (UniqueConstraint("account_id", "card", "article", name="uq_card_econ"),)
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    card = Column(String(64), nullable=False)
    article = Column(String(200), nullable=False)
    net_units = Column(Integer, nullable=False, default=0)
    k = Column(Numeric(8, 5), nullable=True)
    w = Column(Numeric(8, 5), nullable=True)
    f_logistics = Column(Numeric(12, 2), nullable=True)
    how = Column(String(200), nullable=False, default="")
    updated_at = Column(DateTime, nullable=False, default=now_utc, onupdate=now_utc)



# --- Данные WB для оценки цен и акций (`wb_data.py`) ----------------------------------------------
# У WB они живут недолго (воронка по дням — неделю, заказы — 90 дней), поэтому копим у себя с первого
# дня. Время — UTC, `day` — московский календарный день (так WB считает сутки в отчётах).

class WbDataState(Base):
    """Где остановился сбор набора данных кабинета: `dataset` — orders / sales / funnel / stocks / finance /
    prices, `key` — день (для воронки, остатков, цен) или пусто. `cursor` — продолжение (у заказов —
    `lastChangeDate`, у воронки — номер пачки карточек), `done_at` — последний завершённый проход,
    `next_at` — не раньше этого времени следующий, `error` — последний отказ (пусто — всё в порядке)."""
    __tablename__ = "wb_data_state"
    __table_args__ = (UniqueConstraint("account_id", "dataset", "key", name="uq_wb_data_state"),)
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    dataset = Column(String(16), nullable=False)
    key = Column(String(16), nullable=False, default="")
    cursor = Column(Text, nullable=False, default="")
    done_at = Column(DateTime, nullable=True)
    next_at = Column(DateTime, nullable=True)
    rows = Column(Integer, nullable=False, default=0)
    error = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime, nullable=False, default=now_utc)


class WbOrder(Base):
    """Заказ WB (одна единица товара) — `statistics-api /supplier/orders`. Единственное место, где у каждого
    заказа есть скидка WB (`spp`) и цена для покупателя (`finished_price`); метод WB объявил к отключению."""
    __tablename__ = "wb_orders"
    __table_args__ = (UniqueConstraint("account_id", "srid", name="uq_wb_order"),)
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    srid = Column(String(100), nullable=False)
    day = Column(Date, nullable=False, index=True)
    ordered_at = Column(DateTime, nullable=True)
    changed_at = Column(DateTime, nullable=True)
    nm_id = Column(String(32), nullable=False, default="", index=True)
    barcode = Column(String(64), nullable=False, default="")
    article = Column(String(200), nullable=False, default="")
    size = Column(String(64), nullable=False, default="")
    total_price = Column(Numeric(12, 2), nullable=True)          # цена до скидки продавца
    discount_pct = Column(Integer, nullable=True)                # скидка продавца
    spp = Column(Numeric(6, 2), nullable=True)                   # скидка WB
    price_with_disc = Column(Numeric(12, 2), nullable=True)      # со скидкой продавца
    finished_price = Column(Numeric(12, 2), nullable=True)       # для покупателя (без Кошелька)
    is_cancel = Column(Boolean, nullable=False, default=False)
    cancel_at = Column(DateTime, nullable=True)
    warehouse = Column(String(100), nullable=False, default="")
    region = Column(String(200), nullable=False, default="")
    fetched_at = Column(DateTime, nullable=False, default=now_utc)


class WbSale(Base):
    """Продажа (`sale_id` S…) или возврат (R…) — `statistics-api /supplier/sales`. `for_pay` — к перечислению."""
    __tablename__ = "wb_sales"
    __table_args__ = (UniqueConstraint("account_id", "sale_id", name="uq_wb_sale"),)
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    sale_id = Column(String(64), nullable=False)
    srid = Column(String(100), nullable=False, default="")
    day = Column(Date, nullable=False, index=True)
    sold_at = Column(DateTime, nullable=True)
    changed_at = Column(DateTime, nullable=True)
    nm_id = Column(String(32), nullable=False, default="", index=True)
    barcode = Column(String(64), nullable=False, default="")
    article = Column(String(200), nullable=False, default="")
    size = Column(String(64), nullable=False, default="")
    total_price = Column(Numeric(12, 2), nullable=True)
    discount_pct = Column(Integer, nullable=True)
    spp = Column(Numeric(6, 2), nullable=True)
    price_with_disc = Column(Numeric(12, 2), nullable=True)
    finished_price = Column(Numeric(12, 2), nullable=True)
    for_pay = Column(Numeric(12, 2), nullable=True)
    fetched_at = Column(DateTime, nullable=False, default=now_utc)


class WbFunnelDay(Base):
    """Воронка карточки (nmID) за день — `sales-funnel/products`. Строки только с движением: нет строки за
    собранный день — значит, по карточке ничего не было (собран ли день — `WbDataState`). Выкупы, отмены и
    возвраты WB относит к дню ЗАКАЗА и дописывает неделями — поэтому дни перечитываются ~35 суток."""
    __tablename__ = "wb_funnel_days"
    __table_args__ = (UniqueConstraint("account_id", "day", "nm_id", name="uq_wb_funnel_day"),)
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    day = Column(Date, nullable=False, index=True)
    nm_id = Column(String(32), nullable=False, index=True)
    open_count = Column(Integer, nullable=False, default=0)
    cart_count = Column(Integer, nullable=False, default=0)
    order_count = Column(Integer, nullable=False, default=0)
    order_sum = Column(Integer, nullable=False, default=0)
    buyout_count = Column(Integer, nullable=False, default=0)
    buyout_sum = Column(Integer, nullable=False, default=0)
    cancel_count = Column(Integer, nullable=False, default=0)
    cancel_sum = Column(Integer, nullable=False, default=0)
    avg_price = Column(Integer, nullable=True)
    wishlist = Column(Integer, nullable=False, default=0)
    fetched_at = Column(DateTime, nullable=False, default=now_utc)


class WbStockDay(Base):
    """Остаток размера (nmID + chrtID) на складах WB — снимок раз в сутки (`stocks-report/wb-warehouses`)."""
    __tablename__ = "wb_stock_days"
    __table_args__ = (UniqueConstraint("account_id", "day", "nm_id", "chrt_id", name="uq_wb_stock_day"),)
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    day = Column(Date, nullable=False, index=True)
    nm_id = Column(String(32), nullable=False, index=True)
    chrt_id = Column(String(32), nullable=False, default="")
    quantity = Column(Integer, nullable=False, default=0)
    to_client = Column(Integer, nullable=False, default=0)
    from_client = Column(Integer, nullable=False, default=0)
    fetched_at = Column(DateTime, nullable=False, default=now_utc)


class WbPriceDay(Base):
    """Цена карточки на WB за день — снимок текущих цен кабинета (без запросов к WB: из `platform_items`). Истории
    цен API WB не отдаёт, а без неё не с чем сравнить продажи до и после акции."""
    __tablename__ = "wb_price_days"
    __table_args__ = (UniqueConstraint("account_id", "day", "nm_id", name="uq_wb_price_day"),)
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    day = Column(Date, nullable=False, index=True)
    nm_id = Column(String(32), nullable=False, index=True)
    price = Column(Integer, nullable=True)            # до скидки продавца
    sale_price = Column(Integer, nullable=True)       # со скидкой продавца
    fetched_at = Column(DateTime, nullable=False, default=now_utc)


class WbFinanceRow(Base):
    """Строка детализации к отчёту реализации (`finance-api sales-reports/detailed`): продажа, возврат, логистика,
    хранение, штраф. Отсюда фактические комиссия, эквайринг, логистика и снижение комиссии в акции (`kgvp_v2`).
    `oper` — «обоснование для оплаты» (sellerOperName)."""
    __tablename__ = "wb_finance_rows"
    __table_args__ = (UniqueConstraint("account_id", "rrd_id", name="uq_wb_finance_row"),)
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    rrd_id = Column(String(32), nullable=False)
    report_id = Column(String(32), nullable=False, default="")
    period_from = Column(Date, nullable=True)
    period_to = Column(Date, nullable=True)
    day = Column(Date, nullable=True, index=True)          # дата операции (rrDate)
    nm_id = Column(String(32), nullable=False, default="", index=True)
    barcode = Column(String(64), nullable=False, default="")
    article = Column(String(200), nullable=False, default="")
    size = Column(String(64), nullable=False, default="")
    doc_type = Column(String(64), nullable=False, default="")
    oper = Column(String(100), nullable=False, default="")
    srid = Column(String(100), nullable=False, default="")
    ordered_at = Column(DateTime, nullable=True)
    sold_at = Column(DateTime, nullable=True)
    quantity = Column(Integer, nullable=True)
    retail_price = Column(Numeric(12, 2), nullable=True)
    retail_amount = Column(Numeric(12, 2), nullable=True)
    retail_price_with_disc = Column(Numeric(12, 2), nullable=True)
    sale_pct = Column(Integer, nullable=True)
    commission_pct = Column(Numeric(8, 4), nullable=True)
    kvw_base = Column(Numeric(8, 4), nullable=True)
    kvw = Column(Numeric(8, 4), nullable=True)
    kgvp_v2 = Column(Numeric(8, 4), nullable=True)         # снижение кВВ из-за акции, %
    spp = Column(Numeric(6, 2), nullable=True)
    for_pay = Column(Numeric(12, 2), nullable=True)
    sales_commission = Column(Numeric(12, 2), nullable=True)
    acquiring_fee = Column(Numeric(12, 2), nullable=True)
    acquiring_pct = Column(Numeric(8, 4), nullable=True)
    delivery_count = Column(Integer, nullable=True)
    return_count = Column(Integer, nullable=True)
    logistics = Column(Numeric(12, 2), nullable=True)      # deliveryService
    penalty = Column(Numeric(12, 2), nullable=True)
    additional_payment = Column(Numeric(12, 2), nullable=True)
    storage = Column(Numeric(12, 2), nullable=True)
    deduction = Column(Numeric(12, 2), nullable=True)
    acceptance = Column(Numeric(12, 2), nullable=True)
    rebill_logistics = Column(Numeric(12, 2), nullable=True)
    seller_promo_id = Column(String(32), nullable=False, default="")
    seller_promo_discount = Column(Numeric(8, 4), nullable=True)
    fetched_at = Column(DateTime, nullable=False, default=now_utc)
