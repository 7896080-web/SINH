"""Схема базы программы «Финансы площадок».

Деньги — целыми копейками (`*_kop`, BigInteger): SQLite считает Numeric через
float, а сверка отчёта с выплатой площадки должна сходиться до копейки.
"""
import enum

from sqlalchemy import (BigInteger, Boolean, Column, Date, DateTime, Enum, ForeignKey, Integer,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import relationship

from finapp.database import Base
from finapp.timeutils import now_utc

# Площадки, у которых бывают кабинеты. Отчёты WB и KIT забираются по API, Lamoda
# и Ozon — файлом (у Lamoda финансовых отчётов в API нет вовсе).
PLATFORMS = {"wb": "Wildberries", "kit": "Яндекс KIT", "lamoda": "Lamoda", "ozon": "Ozon"}


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=now_utc)


class Setting(Base):
    """Настройки «ключ — значение» (`settings.py`)."""
    __tablename__ = "settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False, default="")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=now_utc, index=True)
    actor = Column(String(64), nullable=False, default="")
    action = Column(String(64), nullable=False)
    details = Column(Text, nullable=True)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    name = Column(String(64), primary_key=True)
    last_run_at = Column(DateTime, nullable=True)
    last_success = Column(Boolean, default=True, nullable=False)
    last_error = Column(Text, nullable=False, default="")


class PlatformAccount(Base):
    """Кабинет площадки. У WB может быть несколько кабинетов (разных ИП/ООО)."""
    __tablename__ = "platform_accounts"

    id = Column(Integer, primary_key=True)
    platform = Column(String(16), nullable=False)       # ключ PLATFORMS
    name = Column(String(128), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    # Тот же кабинет в sync_admin (его `platform_accounts.id`) — для сверки
    # отчёта с заказами, которые принял sync_admin. Пусто — сверки заказов нет.
    sync_account_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=now_utc)


class ApiCredential(Base):
    """Ключ кабинета, зашифрованный ключом программы (`crypto.py`)."""
    __tablename__ = "api_credentials"
    __table_args__ = (UniqueConstraint("account_id", "field_name", name="uq_credential_field"),)

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("platform_accounts.id"), nullable=False, index=True)
    field_name = Column(String(64), nullable=False)
    encrypted_value = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime, default=now_utc, onupdate=now_utc)


class Product(Base):
    """Товар 1С (размер-цвет) — из справочника баркодов обработки (BARCODE_DICT).

    Строки не удаляются, даже если товар пропал из справочника: на них ссылаются
    себестоимость и прошлые отчёты."""
    __tablename__ = "products"

    uid_1c = Column(String(64), primary_key=True)
    article = Column(String(200), nullable=True, index=True)
    name = Column(String(500), nullable=True)
    size = Column(String(100), nullable=True)
    color = Column(String(100), nullable=True)
    seen_at = Column(DateTime, default=now_utc)


class Barcode(Base):
    """Баркод → товар 1С. Снимок справочника, заменяется целиком. Один баркод у
    двух товаров бывает (ошибка в 1С) — такую строку отчёта не сопоставляем."""
    __tablename__ = "barcodes"

    barcode = Column(String(64), primary_key=True)
    uid_1c = Column(String(64), primary_key=True, index=True)


class LamodaArticle(Base):
    """Артикул Lamoda (модель-размер) → EAN — из партнёрского отчёта Lamoda
    («Lamoda Sales Report - AWER ….xlsx», лист «Артикулы», `lamoda.import_catalog`).

    В еженедельном отчёте EAN нет — только короткий артикул модели и размер
    партнёра, — а в «Мэппинге» Sync Admin артикулов Lamoda нет вовсе. Партнёрский
    отчёт называет в одной строке короткий артикул, размер партнёра, полный артикул
    и EAN: по нему строка недельного отчёта находит товар 1С. Строки не удаляются:
    на снятый с продажи артикул ещё ссылаются прошлые недели."""
    __tablename__ = "lamoda_articles"

    lamoda_sku = Column(String(64), primary_key=True)                 # полный: MP002XM0WFXNINL
    short_sku = Column(String(32), nullable=False, index=True)        # модель: MP002XM0WFXN
    partner_size = Column(String(32), nullable=False, default="")     # как в недельном: «XXL», «2.5XL»
    lamoda_size = Column(String(32), nullable=True)
    ean = Column(String(64), nullable=True)
    partner_article = Column(String(255), nullable=True)
    report_date = Column(Date, nullable=True)                         # «Дата отчета» файла
    updated_at = Column(DateTime, default=now_utc, onupdate=now_utc)


class CategoryTariff(Base):
    """Текущая комиссия площадки по категории (`finance/tariffs.py`). У WB —
    `GET common-api…/api/v1/tariffs/commission` по предметам, тем же запросом, что
    в «Репрайсере»: FBS — `kgvpMarketplace` (модель «Маркетплейс»), FBO —
    `paidStorageKgvp` («Склад WB»). Проценты — в сотых долях (25,5% → 2550): без
    float. Прежний тариф FBS хранится, чтобы показать, что он сменился."""
    __tablename__ = "category_tariffs"

    platform = Column(String(16), primary_key=True)
    category_id = Column(String(64), primary_key=True)
    category_name = Column(String(200), nullable=False, default="", index=True)
    parent_name = Column(String(200), nullable=True)
    fbs_bp = Column(Integer, nullable=True)
    fbo_bp = Column(Integer, nullable=True)
    prev_fbs_bp = Column(Integer, nullable=True)
    changed_at = Column(DateTime, nullable=True)      # когда FBS сменился в последний раз
    loaded_at = Column(DateTime, default=now_utc)


class WbCard(Base):
    """Артикул WB (nmID) → предмет: из детализации отчётов WB («Предмет»,
    `subjectName`). По предмету карточка находит свой тариф в `CategoryTariff`."""
    __tablename__ = "wb_cards"

    nm_id = Column(String(32), primary_key=True)
    subject_name = Column(String(200), nullable=False)
    seen_at = Column(DateTime, default=now_utc)


class OnecTaskStatus(str, enum.Enum):
    pending = "pending"     # ждёт файла заданий
    sent = "sent"           # строка в task_fin_*.txt, ждём result_fin_*.txt
    done = "done"
    failed = "failed"
    timeout = "timeout"     # ответа нет дольше срока; поздний ответ ещё примем


class OnecTask(Base):
    """Команда обработке 1С: FIN_PING, BARCODE_DICT, EXPORT_COST_PRICES.
    Отчёты о продажах (SALES_REPORT) ведут свой статус на самом отчёте."""
    __tablename__ = "onec_tasks"

    id = Column(Integer, primary_key=True)
    command = Column(String(32), nullable=False, index=True)
    key = Column(String(64), nullable=False, index=True)
    line = Column(String(255), nullable=False)
    status = Column(String(16), default=OnecTaskStatus.pending.value, nullable=False, index=True)
    requested_by = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=now_utc)
    filename = Column(String(128), nullable=True)
    sent_at = Column(DateTime, nullable=True)
    answered_at = Column(DateTime, nullable=True)
    result_status = Column(String(16), nullable=True)
    result_detail = Column(Text, nullable=True)


class KitVariant(Base):
    """Баркод варианта товара KIT. Кэш: KIT отдаёт баркод запросом на вариант и
    на частые запросы быстро отвечает 429."""
    __tablename__ = "kit_variants"

    account_id = Column(Integer, ForeignKey("platform_accounts.id"), primary_key=True)
    variant_id = Column(String(64), primary_key=True)
    barcode = Column(String(64), nullable=True)
    fetched_at = Column(DateTime, default=now_utc)


class OneCUploadStatus(str, enum.Enum):
    none = "none"         # в 1С не отправлялся
    pending = "pending"   # оператор нажал «Отправить», ждёт ближайшего файла заданий
    sent = "sent"         # строки ушли в task_fin_*.txt, ждём ответа
    done = "done"         # 1С ответила OK: документы созданы
    failed = "failed"     # 1С ответила ERROR: документов нет
    timeout = "timeout"   # ответа нет дольше окна ожидания


class SalesReport(Base):
    """Один загруженный отчёт площадки — шапка."""
    __tablename__ = "sales_reports"

    id = Column(Integer, primary_key=True)
    platform = Column(String(16), nullable=False, index=True)       # wb / ozon / kit / lamoda
    account_id = Column(Integer, ForeignKey("platform_accounts.id"), nullable=True)
    report_number = Column(String(64), nullable=True)               # номер отчёта площадки, если есть
    period_from = Column(Date, nullable=False)
    period_to = Column(Date, nullable=False)
    source_format = Column(String(32), nullable=False)              # каким разборщиком прочитан
    # Откуда отчёт: «file» — загружен оператором, «api» — официальный отчёт,
    # забранный у площадки по API (WB finance-api, заказы KIT).
    source = Column(String(16), default="file", nullable=False)
    # Вид отчёта у площадки, если она их различает (WB: «основной» / «по выкупам»).
    report_kind = Column(String(32), nullable=True)
    file_name = Column(String(255), nullable=True)
    file_sha256 = Column(String(64), nullable=True, index=True)
    uploaded_by = Column(String(64), nullable=True)
    uploaded_at = Column(DateTime, default=now_utc)
    lines_count = Column(Integer, default=0, nullable=False)
    # Строки продаж/возвратов, баркод которых не нашёлся в справочнике 1С. Пока
    # их больше нуля, в 1С отчёт не отправляется: документ без части товаров —
    # это неверная выручка в учёте, а не «почти готово».
    unmatched_lines = Column(Integer, default=0, nullable=False)
    # «Итого к оплате» самой площадки (WB отдаёт его в списке отчётов) и
    # фактическое поступление на расчётный счёт — вводит оператор для сверки.
    declared_payout_kop = Column(BigInteger, nullable=True)
    bank_payout_kop = Column(BigInteger, nullable=True)
    note = Column(Text, nullable=True)

    # Ключ документа в 1С. Стабилен для одного и того же отчёта (площадка +
    # номер или площадка + кабинет + период), поэтому повторная загрузка
    # исправленного отчёта ЗАМЕНЯЕТ документ в 1С, а не создаёт второй.
    onec_key = Column(String(64), unique=True, nullable=False)
    onec_status = Column(Enum(OneCUploadStatus), default=OneCUploadStatus.none, nullable=False)
    onec_requested_by = Column(String(64), nullable=True)
    onec_requested_at = Column(DateTime, nullable=True)
    onec_sent_at = Column(DateTime, nullable=True)
    onec_batch_filename = Column(String(128), nullable=True)
    onec_completed_at = Column(DateTime, nullable=True)
    onec_detail = Column(String(255), nullable=True)     # номера документов или текст ошибки
    onec_lines_sent = Column(Integer, default=0, nullable=False)
    # Версия разбора детализации: у WB 2 — с ценой продавца, кВВ и видами
    # начислений (`formats.WB_DETAIL_VERSION`). Отчёт старой версии дополняется
    # файлом детализации из кабинета, если итоги сошлись (`importer.enrich_report`).
    parser_version = Column(Integer, default=0, nullable=False, server_default="0")

    account = relationship("PlatformAccount")
    lines = relationship("SalesReportLine", back_populates="report",
                         cascade="all, delete-orphan", passive_deletes=True)


class SalesReportLine(Base):
    """Строка отчёта в единой форме. Знаки:
    - revenue/payout: продажа «+», возврат «−»;
    - commission: удержание площадки с продажи (включая эквайринг), возврат
      возвращает его «−»;
    - logistics/storage/penalty/acceptance/deduction: расход «+», сторно «−»;
    - compensation: доход «+» (доплаты, компенсации ущерба).
    Итог строки к перечислению = payout − расходы + compensation."""
    __tablename__ = "sales_report_lines"

    id = Column(Integer, primary_key=True)
    report_id = Column(Integer, ForeignKey("sales_reports.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    row_no = Column(Integer, nullable=True)                  # строка исходного файла — для разбора
    operation = Column(String(16), nullable=False, index=True)
    source_operation = Column(String(128), nullable=True)    # как назвала площадка
    operation_date = Column(Date, nullable=False, index=True)
    order_date = Column(Date, nullable=True)
    order_id = Column(String(128), nullable=True, index=True)
    # Индекс — для защиты от повторов (`finance/repeats.py`): продажа ищется по номеру заказа.
    srid = Column(String(128), nullable=True, index=True)
    barcode = Column(String(64), nullable=True, index=True)
    article = Column(String(128), nullable=True)
    platform_sku = Column(String(64), nullable=True)
    name = Column(String(255), nullable=True)
    size = Column(String(64), nullable=True)
    # Без внешнего ключа намеренно: в отчёте бывают товары, которых в
    # справочнике 1С нет, и ссылка не дала бы их сохранить.
    uid_1c = Column(String(64), nullable=True, index=True)
    quantity = Column(Integer, default=0, nullable=False)
    revenue_kop = Column(BigInteger, default=0, nullable=False)
    commission_kop = Column(BigInteger, default=0, nullable=False)
    payout_kop = Column(BigInteger, default=0, nullable=False)
    logistics_kop = Column(BigInteger, default=0, nullable=False)
    storage_kop = Column(BigInteger, default=0, nullable=False)
    penalty_kop = Column(BigInteger, default=0, nullable=False)
    acceptance_kop = Column(BigInteger, default=0, nullable=False)
    deduction_kop = Column(BigInteger, default=0, nullable=False)
    compensation_kop = Column(BigInteger, default=0, nullable=False)
    # Детализация WB — то, что объясняет деньги строки (19.10.2026, отчёт №876850339
    # сверен построчно). У продажи WB выручка `revenue_kop` — по ЦЕНЕ ПРОДАВЦА
    # («Цена розничная с учётом согласованной скидки» × кол-во): от неё WB считает
    # выплату, К перечислению = цена × (1 − кВВ − эквайринг). Пусто — площадка
    # этого не сообщила (другие площадки, старые файлы WB).
    # «Вайлдберриз реализовал» — сколько заплатил покупатель (цена − СПП, скидка WB
    # за свой счёт); это «Продажа» свода WB и БАЗА НАЛОГА. Пусто — равна выручке.
    realized_kop = Column(BigInteger, nullable=True)
    list_price_kop = Column(BigInteger, nullable=True)      # «Цена розничная» × кол-во — до скидки продавца
    acquiring_kop = Column(BigInteger, nullable=True)       # эквайринг (входит в комиссию)
    fee_pct_bp = Column(Integer, nullable=True)             # «Размер кВВ, %» × 100
    # Вид начисления площадки: у WB «Виды доставок, штрафов и корректировок ВВ»
    # (К клиенту при продаже / при отмене, WB Продвижение, вид штрафа…).
    charge_kind = Column(String(128), nullable=True)
    warehouse = Column(String(128), nullable=True)          # склад продажи («Склад WB» — FBO)

    report = relationship("SalesReport", back_populates="lines")

    @property
    def total_kop(self) -> int:
        return (self.payout_kop - self.logistics_kop - self.storage_kop - self.penalty_kop
                - self.acceptance_kop - self.deduction_kop + self.compensation_kop)


class ProductCost(Base):
    """Себестоимость товара (за штуку), действующая с даты. Продажа берёт цену,
    действовавшую на дату операции, — переоценка не переписывает прошлые месяцы.

    Валюта — своя у каждой записи: себестоимость из 1С (регистр «Себестоимость
    номенклатуры», команда EXPORT_COST_PRICES) ведётся в ДОЛЛАРАХ, из Excel
    оператор вносит рубли. В рубли доллары переводятся курсом ЦБ на дату
    операции (`CurrencyRate`)."""
    __tablename__ = "product_costs"
    __table_args__ = (UniqueConstraint("uid_1c", "valid_from", name="uq_cost_uid_date"),)

    id = Column(Integer, primary_key=True)
    uid_1c = Column(String(64), ForeignKey("products.uid_1c"), nullable=False, index=True)
    valid_from = Column(Date, nullable=False)
    cost_minor = Column(BigInteger, nullable=False)      # копейки или центы — по валюте
    currency = Column(String(3), default="RUB", nullable=False)
    source = Column(String(16), default="excel", nullable=False)   # excel / 1c
    updated_by = Column(String(64), nullable=True)
    updated_at = Column(DateTime, default=now_utc, onupdate=now_utc)


class CurrencyRate(Base):
    """Курс доллара ЦБ РФ на дату — им себестоимость 1С ($) становится рублями.
    Целым числом (курс × 10 000): SQLite считает Numeric через float."""
    __tablename__ = "currency_rates"

    rate_date = Column(Date, primary_key=True)
    usd_rub_e4 = Column(Integer, nullable=False)
    source = Column(String(16), default="cbr", nullable=False)
    fetched_at = Column(DateTime, default=now_utc, onupdate=now_utc)


class ReportFetch(Base):
    """Заявка на загрузку официальных отчётов площадки по API (`wb_api`, `kit_api`).

    Отдельной очередью, а не прямо в обработчике страницы: WB отдаёт отчёты
    по одному запросу в минуту, загрузка за несколько недель идёт десятки минут,
    и держать на ней веб-запрос нельзя. Заявки разбирает фоновое задание
    (`finance/fetch.py`)."""
    __tablename__ = "report_fetches"

    id = Column(Integer, primary_key=True)
    kind = Column(String(32), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("platform_accounts.id"), nullable=True)
    date_from = Column(Date, nullable=True)
    date_to = Column(Date, nullable=True)
    status = Column(String(16), default="pending", nullable=False, index=True)  # pending → running → done | failed
    requested_by = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=now_utc)
    started_at = Column(DateTime, nullable=True)
    # Обновляется после каждого шага: заявка «running», у которой отметка давно
    # не двигалась, — оборванный перезапуском службы проход, его берут заново.
    progress_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    found = Column(Integer, default=0, nullable=False)
    imported = Column(Integer, default=0, nullable=False)
    message = Column(Text, nullable=True)
    # Шкала прогресса (`/progress/jobs`): сделано шагов из запланированных и что
    # делается сейчас. Шаг у WB — детализация одного отчёта (минута на запрос).
    progress_done = Column(Integer, default=0, nullable=False, server_default="0")
    progress_total = Column(Integer, nullable=True)
    progress_note = Column(String(200), nullable=True)

    account = relationship("PlatformAccount")


class PlatformStatement(Base):
    """Отчёт по своду самой площадки — списку финансовых отчётов WB (выгрузка из
    кабинета «Финансовые отчёты» или `sales-reports/list` в API): номер, период,
    вид и итоги шапки, главное — «Итого к оплате». Эталон для сверки: каждый ли
    отчёт загружен и сходится ли с ним итог строк (`finance/statements.py`)."""
    __tablename__ = "platform_statements"
    __table_args__ = (UniqueConstraint("platform", "report_number", name="uq_statement_number"),)

    id = Column(Integer, primary_key=True)
    platform = Column(String(16), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("platform_accounts.id"), nullable=True)
    report_number = Column(String(64), nullable=False)
    report_kind = Column(String(32), nullable=True)            # основной / по выкупам
    legal_entity = Column(String(255), nullable=True)
    period_from = Column(Date, nullable=False, index=True)
    period_to = Column(Date, nullable=False)
    formed_at = Column(Date, nullable=True)
    # Итоги шапки — как их называет WB. Пусто — площадка этого не сообщила
    # (список в API отдаёт не все колонки файла).
    sale_kop = Column(BigInteger, nullable=True)               # «Продажа»
    for_pay_kop = Column(BigInteger, nullable=True)            # «К перечислению за товар»
    delivery_kop = Column(BigInteger, nullable=True)           # «Стоимость доставки»
    storage_kop = Column(BigInteger, nullable=True)            # «Стоимость хранения»
    acceptance_kop = Column(BigInteger, nullable=True)         # «Стоимость обработки товара»
    penalty_kop = Column(BigInteger, nullable=True)            # «Общая сумма штрафов»
    # «Прочие удержания/выплаты» + корректировка ВВ + лояльность + срок перечисления
    # — то, что у нас ложится в «удержания».
    deduction_kop = Column(BigInteger, nullable=True)
    total_kop = Column(BigInteger, nullable=False)             # «Итого к оплате»
    source = Column(String(16), default="file", nullable=False)  # file / api
    loaded_at = Column(DateTime, default=now_utc, onupdate=now_utc)

    account = relationship("PlatformAccount")


class MailMessage(Base):
    """Письмо с отчётом, уже разобранное с почты (`mail.py`). Ящик программа
    только читает и ничего в нём не помечает — что разобрано, помнит здесь."""
    __tablename__ = "mail_messages"

    id = Column(Integer, primary_key=True)
    message_id = Column(String(255), unique=True, nullable=False)
    received_at = Column(DateTime, nullable=True)
    sender = Column(String(255), nullable=True)
    subject = Column(String(500), nullable=True)
    attachments = Column(Text, nullable=True)            # имена вложений через «; »
    status = Column(String(16), nullable=False)          # imported / skipped / failed
    note = Column(Text, nullable=True)
    processed_at = Column(DateTime, default=now_utc)
