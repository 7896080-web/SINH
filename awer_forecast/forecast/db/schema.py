"""Схема БД AWER Forecast — все таблицы `docs/schema.sql` (PostgreSQL DDL из ТЗ).

Таблицы описаны через SQLAlchemy Core, чтобы одна и та же схема работала и на
PostgreSQL (прод, как в ТЗ), и на SQLite (тесты, локальный запуск). Смысл каждой
колонки — в комментариях `docs/schema.sql`: этот файл повторяет его структуру
(порядок таблиц, разделы, имена ограничений), а не текст комментариев.

Переносимость:
- `JSONB` → `JSONType` (JSONB на PostgreSQL, JSON на SQLite);
- `BIGSERIAL` → `BigIntPK` (BIGSERIAL на PostgreSQL, INTEGER PRIMARY KEY на SQLite —
  иначе SQLite не делает автоинкремент);
- `INT[]` → `IntArray` (массив на PostgreSQL, JSON-список на SQLite).

Отличия от `schema.sql`, которые понадобились в Фазе 1, приезжают отдельной
миграциями 0002–0003 и помечены здесь комментарием «Фаза 1» / «Миграция 0003»: строковый `order_items.lamoda_item_id`
и SKU позиции, принятое количество поставки FBO, служебные поля рекомендаций, новые
таблицы `job_runs` и `channel_sales_daily` (раздел «Расширения Фазы 1» в конце файла).

Любое изменение здесь — только вместе с новой миграцией Alembic (см. CLAUDE.md).
"""
from sqlalchemy import (
    JSON, BigInteger, Boolean, CheckConstraint, Column, Date, DateTime, ForeignKeyConstraint,
    Index, Integer, MetaData, Numeric, PrimaryKeyConstraint, Table, Text, UniqueConstraint,
    false, func, text, true,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

metadata = MetaData()

JSONType = JSON().with_variant(JSONB(), "postgresql")
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")
IntArray = JSON().with_variant(ARRAY(Integer()), "postgresql")



# ==================== 1. СПРАВОЧНИКИ (master data) ====================


t_suppliers = Table(
    'suppliers', metadata,
    Column('supplier_id', Integer, primary_key=True, autoincrement=True),
    Column('name', Text, nullable=False),
    Column('production_days', Integer, nullable=False),
    Column('delivery_days', Integer, nullable=False),
    Column('moq', Integer, nullable=False),
    Column('on_time_delivery_rate_pct', Numeric(5, 2)),
    PrimaryKeyConstraint('supplier_id', name='suppliers_pkey')
)

t_product_models = Table(
    'product_models', metadata,
    Column('product_model_id', Integer, primary_key=True, autoincrement=True),
    Column('article', Text, nullable=False),
    Column('color', Text),
    Column('category', Text, nullable=False),
    Column('subcategory', Text),
    Column('season', Text),
    Column('supplier_id', Integer),
    Column('collection_type', Text, server_default=text("'base'")),
    Column('first_sale_date', Date),
    Column('status', Text, server_default=text("'active'")),
    Column('available_to_reorder', Boolean, server_default=true()),
    Column('discontinued_date', Date),
    Column('vat_rate', Numeric(5, 2)),
    Column('lamoda_sku', Text),
    Column('wb_sku', Text),
    Column('ozon_sku', Text),
    Column('planned_launch_price', Numeric(10, 2)),
    Column('seasonal_group', Text),
    Column('color_group', Text),
    ForeignKeyConstraint(['supplier_id'], ['suppliers.supplier_id'], name='product_models_supplier_id_fkey'),
    PrimaryKeyConstraint('product_model_id', name='product_models_pkey'),
    UniqueConstraint('article', 'color', name='product_models_article_color_key')
)

t_style_tags = Table(
    'style_tags', metadata,
    Column('style_tag_id', Integer, primary_key=True, autoincrement=True),
    Column('product_model_id', Integer),
    Column('tag', Text, nullable=False),
    Column('confidence', Numeric(4, 3)),
    Column('source', Text, server_default=text("'agent'")),
    Column('reviewed', Boolean, server_default=false()),
    Column('created_at', DateTime, server_default=func.now()),
    ForeignKeyConstraint(['product_model_id'], ['product_models.product_model_id'], name='style_tags_product_model_id_fkey'),
    PrimaryKeyConstraint('style_tag_id', name='style_tags_pkey')
)

t_product_sizes = Table(
    'product_sizes', metadata,
    Column('product_size_id', Integer, primary_key=True, autoincrement=True),
    Column('product_model_id', Integer, nullable=False),
    Column('size_label', Text, nullable=False),
    Column('lamoda_sku', Text),
    ForeignKeyConstraint(['product_model_id'], ['product_models.product_model_id'], name='product_sizes_product_model_id_fkey'),
    PrimaryKeyConstraint('product_size_id', name='product_sizes_pkey'),
    UniqueConstraint('product_model_id', 'size_label', name='product_sizes_product_model_id_size_label_key')
)

# Пул баркодов — у одного product_size может быть несколько баркодов одновременно (разные площадки/1С
# на один физический товар), не только исторически при переиздании (valid_from/valid_to). Сопоставление
# (раздел 3/4) — по совпадению ЛЮБОГО одного баркода из пула, не единственного канонического значения.
t_barcode_pool = Table(
    'barcode_pool', metadata,
    Column('barcode_id', Integer, primary_key=True, autoincrement=True),
    Column('product_size_id', Integer, nullable=False),
    Column('barcode', Text, nullable=False),
    Column('seller_sku', Text),
    Column('valid_from', Date, server_default=text('CURRENT_DATE')),
    Column('valid_to', Date),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='barcode_pool_product_size_id_fkey'),
    PrimaryKeyConstraint('barcode_id', name='barcode_pool_pkey'),
    UniqueConstraint('barcode', name='barcode_pool_barcode_key')
)

t_product_analogs = Table(
    'product_analogs', metadata,
    Column('product_model_id_new', Integer, primary_key=True),
    Column('product_model_id_analog', Integer, primary_key=True),
    Column('similarity_weight', Numeric(4, 3), nullable=False),
    ForeignKeyConstraint(['product_model_id_analog'], ['product_models.product_model_id'], name='product_analogs_product_model_id_analog_fkey'),
    ForeignKeyConstraint(['product_model_id_new'], ['product_models.product_model_id'], name='product_analogs_product_model_id_new_fkey'),
    PrimaryKeyConstraint('product_model_id_new', 'product_model_id_analog', name='product_analogs_pkey')
)

# Настройки маржи — версионируемые (история изменений порогов/ставок)
t_margin_settings = Table(
    'margin_settings', metadata,
    Column('setting_id', Integer, primary_key=True, autoincrement=True),
    Column('effective_from', Date, nullable=False),
    Column('lamoda_commission_rate', Numeric(5, 4), nullable=False, server_default=text('0.42')),
    Column('tax_regime', Text, nullable=False, server_default=text("'УСН доходы'")),
    Column('vat_rate', Numeric(5, 4), nullable=False, server_default=text('0.05')),
    Column('usn_rate', Numeric(5, 4), nullable=False, server_default=text('0.06')),
    Column('margin_min_pct', Numeric(5, 2), nullable=False, server_default=text('30')),
    Column('margin_target_pct', Numeric(5, 2), nullable=False, server_default=text('50')),
    Column('margin_max_pct', Numeric(5, 2), nullable=False, server_default=text('80')),
    Column('usd_rate_adjustment', Numeric(6, 2), nullable=False, server_default=text('4')),
    Column('commission_vat_multiplier', Numeric(5, 4), nullable=False, server_default=text('1')),
    Column('liquidation_recovery_rate', Numeric(4, 3), nullable=False, server_default=text('0.5')),
    PrimaryKeyConstraint('setting_id', name='margin_settings_pkey')
)

# Настройки коррекции прогноза (по категории — разное поведение алгоритма/трафика)
t_forecast_correction_settings = Table(
    'forecast_correction_settings', metadata,
    Column('category', Text, primary_key=True),
    Column('recovery_tail_days', Integer, nullable=False, server_default=text('7')),
    Column('recovery_tail_coefficient', Numeric(3, 2), nullable=False, server_default=text('0.5')),
    Column('outlier_sigma_threshold', Numeric(3, 1), nullable=False, server_default=text('3.0')),
    PrimaryKeyConstraint('category', name='forecast_correction_settings_pkey')
)

# Сезонные коэффициенты по группе паттерна (раздел 5.2) — не по категории напрямую,
# разные категории с похожей формой кривой объединяются в одну seasonal_group
t_seasonal_coefficients = Table(
    'seasonal_coefficients', metadata,
    Column('seasonal_group', Text, primary_key=True),
    Column('month', Integer, primary_key=True),
    Column('coefficient', Numeric(6, 3), nullable=False),
    Column('computed_at', DateTime, server_default=func.now()),
    CheckConstraint('month >= 1 AND month <= 12', name='seasonal_coefficients_month_check'),
    PrimaryKeyConstraint('seasonal_group', 'month', name='seasonal_coefficients_pkey')
)

t_promo_calendar = Table(
    'promo_calendar', metadata,
    Column('promo_id', Integer, primary_key=True, autoincrement=True),
    Column('name', Text),
    Column('date_from', Date, nullable=False),
    Column('date_to', Date, nullable=False),
    PrimaryKeyConstraint('promo_id', name='promo_calendar_pkey')
)


# ==================== 2. ВХОДНЫЕ ПОТОКИ (факты, временные ряды) ====================


t_stock_1c = Table(
    'stock_1c', metadata,
    Column('product_size_id', Integer, primary_key=True),
    Column('snapshot_date', Date, primary_key=True),
    Column('qty', Integer, nullable=False),
    # Миграция 0003: момент выгрузки остатка из 1С (UTC). Отгрузку на FBO в 1С списывают при
    # создании поставки в Lamoda — сравнение с этим моментом показывает, сидит ли товар
    # поставки ещё в этом срезе остатка ЦС.
    Column('taken_at', DateTime),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='stock_1c_product_size_id_fkey'),
    PrimaryKeyConstraint('product_size_id', 'snapshot_date', name='stock_1c_pkey')
)

t_stock_fbo = Table(
    'stock_fbo', metadata,
    Column('product_size_id', Integer, primary_key=True),
    Column('warehouse_code', Text, primary_key=True),
    Column('snapshot_date', Date, primary_key=True),
    Column('qty', Integer, nullable=False),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='stock_fbo_product_size_id_fkey'),
    PrimaryKeyConstraint('product_size_id', 'warehouse_code', 'snapshot_date', name='stock_fbo_pkey'),
    Index('idx_stock_fbo_date', 'snapshot_date')
)

# Факт создания поставки на FBO (не рекомендация, а реально отправленная поставка) —
# источник компонента "в пути на FBO" в прогноз_остатка(T), раздел 5.4
t_fbo_shipments = Table(
    'fbo_shipments', metadata,
    Column('shipment_id', Text, primary_key=True),
    Column('product_size_id', Integer, primary_key=True),
    Column('qty', Integer, nullable=False),
    Column('created_date', Date, nullable=False),
    Column('expected_arrival_date', Date),
    Column('actual_arrival_date', Date),
    Column('validation_status', Text),
    Column('status', Text, server_default=text("'in_transit'")),
    # Фаза 1: принятое Lamoda количество (`quantityAccepted`) — «в пути» = план − принято,
    # и склад поставки (BYKOVO / SOFINO).
    Column('qty_accepted', Integer),
    Column('warehouse_code', Text),
    # Миграция 0003: точный момент создания поставки в Lamoda (`createdAt`, UTC) — см. stock_1c.taken_at.
    Column('created_at', DateTime),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='fbo_shipments_product_size_id_fkey'),
    PrimaryKeyConstraint('shipment_id', 'product_size_id', name='fbo_shipments_pkey')
)

# Идемпотентная обработка вебхуков Lamoda (раздел 4/7) — дедупликация по sequenceNumber,
# чтобы повторная отправка одного события (retry-логика Lamoda при 5хх) не применялась дважды
t_webhook_events_log = Table(
    'webhook_events_log', metadata,
    Column('event_id', BigIntPK, primary_key=True, autoincrement=True),
    Column('notification_type', Text, nullable=False),
    Column('tracking_id', Text),
    Column('sequence_number', BigInteger),
    Column('payload', JSONType, nullable=False),
    Column('confirmed_via_get', Boolean, server_default=false()),
    Column('gap_resend_requested', Boolean, server_default=false()),
    Column('processed', Boolean, server_default=false()),
    Column('received_at', DateTime, server_default=func.now()),
    PrimaryKeyConstraint('event_id', name='webhook_events_log_pkey')
)

# Сверка расчётного состояния (state-machine) с фактическим снимком stock_fbo —
# фиксирует расхождения, чтобы не терять их молча (раздел 5.4, открытый вопрос №2 из аудита)
t_stock_reconciliation_log = Table(
    'stock_reconciliation_log', metadata,
    Column('reconciliation_id', BigIntPK, primary_key=True, autoincrement=True),
    Column('product_size_id', Integer, nullable=False),
    Column('check_date', Date, nullable=False),
    Column('expected_qty_computed', Numeric(10, 2)),
    Column('actual_qty_from_snapshot', Numeric(10, 2)),
    Column('discrepancy', Numeric(10, 2)),
    Column('resolved', Boolean, server_default=false()),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='stock_reconciliation_log_product_size_id_fkey'),
    PrimaryKeyConstraint('reconciliation_id', name='stock_reconciliation_log_pkey')
)

t_illiquid_flags = Table(
    'illiquid_flags', metadata,
    Column('product_size_id', Integer, primary_key=True),
    Column('snapshot_date', Date, primary_key=True),
    Column('is_illiquid', Boolean, nullable=False),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='illiquid_flags_product_size_id_fkey'),
    PrimaryKeyConstraint('product_size_id', 'snapshot_date', name='illiquid_flags_pkey')
)

# Сырые строки заказов из /v2/orders — источник продаж и резервный источник возвратов
t_order_items = Table(
    'order_items', metadata,
    Column('order_item_id', BigIntPK, primary_key=True, autoincrement=True),
    Column('order_id', Text, nullable=False),
    Column('tracking_id', Text),
    # Фаза 1: в schema.sql BIGINT, но в API v2 `items[].id` — строка (OpenAPI: OrderDetailsItem.id: string).
    Column('lamoda_item_id', Text),
    Column('product_size_id', Integer),
    Column('status', Text, nullable=False),
    Column('price_actual', Numeric(10, 2)),
    Column('price_base', Numeric(10, 2)),
    Column('total_discount', Numeric(10, 2)),
    Column('event_date', Date, nullable=False),
    Column('ingested_at', DateTime, server_default=func.now()),
    # Фаза 1: SKU позиции из ответа (`items[].sku` / `items[].externalSku`) — позицию, которую
    # не удалось сопоставить сразу, можно сопоставить позже, после пополнения справочника.
    Column('lamoda_sku', Text),
    Column('external_sku', Text),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='order_items_product_size_id_fkey'),
    PrimaryKeyConstraint('order_item_id', name='order_items_pkey'),
    UniqueConstraint('order_id', 'lamoda_item_id', 'status', name='order_items_order_id_lamoda_item_id_status_key'),
    Index('idx_order_items_size_date', 'product_size_id', 'event_date')
)

# Агрегированные дневные продажи (по net-спросу, Delivered − возвраты)
t_sales_daily = Table(
    'sales_daily', metadata,
    Column('product_size_id', Integer, primary_key=True),
    Column('sale_date', Date, primary_key=True),
    Column('qty_delivered', Integer, nullable=False, server_default=text('0')),
    Column('qty_returned', Integer, nullable=False, server_default=text('0')),
    Column('revenue', Numeric(12, 2)),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='sales_daily_product_size_id_fkey'),
    PrimaryKeyConstraint('product_size_id', 'sale_date', name='sales_daily_pkey'),
    Index('idx_sales_daily_date', 'sale_date')
)

# ⚠️ НЕ АВТОМАТИЗИРОВАНО: источник (отчёт «Воронка продаж») доступен только Excel-выгрузкой из ЛК,
# API нет (раздел 4). Таблица не заполняется регулярным пайплайном — опциональна для ручного разбора.
t_funnel_model = Table(
    'funnel_model', metadata,
    Column('product_model_id', Integer, primary_key=True),
    Column('period_start', Date, primary_key=True),
    Column('period_end', Date, primary_key=True),
    Column('views', Integer),
    Column('cart_conversion_pct', Numeric(6, 3)),
    Column('cart_adds', Integer),
    Column('order_share_pct', Numeric(6, 3)),
    Column('orders_qty', Integer),
    Column('purchased_qty', Integer),
    Column('purchase_rate_pct', Numeric(6, 3)),
    ForeignKeyConstraint(['product_model_id'], ['product_models.product_model_id'], name='funnel_model_product_model_id_fkey'),
    PrimaryKeyConstraint('product_model_id', 'period_start', 'period_end', name='funnel_model_pkey')
)

# ⚠️ НЕ АВТОМАТИЗИРОВАНО (та же причина, что funnel_model выше). Возвратность вместо этого
# берётся напрямую из /v2/orders (раздел 4) — см. demand_timeseries / order_items.
t_funnel_size = Table(
    'funnel_size', metadata,
    Column('product_size_id', Integer, primary_key=True),
    Column('period_start', Date, primary_key=True),
    Column('period_end', Date, primary_key=True),
    Column('orders_qty', Integer),
    Column('purchased_qty', Integer),
    Column('purchase_rate_pct', Numeric(6, 3)),
    Column('returns_qty', Integer),
    Column('return_rate_pct', Numeric(6, 3)),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='funnel_size_product_size_id_fkey'),
    PrimaryKeyConstraint('product_size_id', 'period_start', 'period_end', name='funnel_size_pkey')
)

# Собственный расчёт (раздел 5.6) — агрегация истории заказов из /v2/orders, а не отчёт Lamoda (без API)
t_size_profile = Table(
    'size_profile', metadata,
    Column('category', Text, primary_key=True),
    Column('gender', Text, primary_key=True),
    Column('size_label', Text, primary_key=True),
    Column('share_of_sales_pct', Numeric(6, 3), nullable=False),
    Column('report_date', Date, primary_key=True),
    PrimaryKeyConstraint('category', 'gender', 'size_label', 'report_date', name='size_profile_pkey')
)

# Историческая выгрузка продаж с других каналов (раздел 4) — разовая/редкая загрузка, не ежедневный поток.
# WB: GET /api/v5/supplier/reportDetailByPeriod (Statistics API, лимит 1 запрос/мин — закладывать паузы).
# Ozon: AnalyticsAPI/FinanceAPI, отчёты о продажах (Client-Id+Api-Key, не Bearer-токен).
# Ключ сопоставления — баркод цветоразмерного SKU через весь barcode_pool (совпадение любого одного
# баркода из пула достаточно, раздел 3), не артикул целиком и не SKU площадки.
t_external_channel_sales_history = Table(
    'external_channel_sales_history', metadata,
    Column('channel', Text, primary_key=True),
    Column('barcode', Text, primary_key=True),
    Column('month', Date, primary_key=True),
    Column('qty_sold', Numeric(10, 2)),
    Column('revenue', Numeric(14, 2)),
    Column('loaded_at', DateTime, server_default=func.now()),
    PrimaryKeyConstraint('channel', 'barcode', 'month', name='external_channel_sales_history_pkey')
)

# Сезонный индекс из долгой истории (раздел 5.2) — форма сезонной кривой по seasonal_group,
# приоритетнее короткой истории Lamoda, пока своя не накоплена (1-2 полных цикла)
t_seasonal_index_reference = Table(
    'seasonal_index_reference', metadata,
    Column('seasonal_group', Text, primary_key=True),
    Column('month_number', Integer, primary_key=True),
    Column('index_value', Numeric(6, 4), nullable=False),
    Column('source_channels', Text),
    Column('computed_at', DateTime, server_default=func.now()),
    PrimaryKeyConstraint('seasonal_group', 'month_number', name='seasonal_index_reference_pkey')
)

# Коэффициент пересчёта продаж канала в Lamoda-эквивалент (раздел 5.6) — калибруется по товарам,
# продающимся на обоих каналах одновременно; используется для новинок на Lamoda с историей
# на другом канале (третий, приоритетный метод прогноза новинок)
t_channel_ratio_calibration = Table(
    'channel_ratio_calibration', metadata,
    Column('channel', Text, primary_key=True),
    Column('category', Text, primary_key=True),
    Column('ratio', Numeric(6, 4), nullable=False),
    Column('sample_size', Integer),
    Column('computed_at', DateTime, server_default=func.now()),
    PrimaryKeyConstraint('channel', 'category', name='channel_ratio_calibration_pkey')
)

# Недооценённый потенциал (раздел 5.6) — товар уже на Lamoda, но продаётся заметно хуже,
# чем предсказывает канал-коэффициент по истории на WB/Ozon/рознице — сигнал проверить
# листинг (карточка/цена/категория/видимость), не факт отсутствия спроса
t_underperformance_flags = Table(
    'underperformance_flags', metadata,
    Column('product_model_id', Integer, primary_key=True),
    Column('period_start', Date, primary_key=True),
    Column('period_end', Date, primary_key=True),
    Column('expected_sales_qty', Numeric(10, 2)),
    Column('actual_sales_qty', Numeric(10, 2)),
    Column('shortfall_pct', Numeric(6, 2)),
    Column('in_stock_full_period', Boolean),
    Column('attributes_incomplete', Boolean),
    Column('popular_sizes_out_of_stock', Boolean),
    Column('price_gap_vs_category_pct', Numeric(6, 2)),
    Column('price_gap_vs_source_channel_pct', Numeric(6, 2)),
    Column('card_status_issue', Boolean),
    Column('competitors_in_promo_we_are_not', Boolean),
    Column('flagged', Boolean, server_default=false()),
    Column('reviewed_at', DateTime),
    Column('review_outcome', Text),
    Column('created_at', DateTime, server_default=func.now()),
    ForeignKeyConstraint(['product_model_id'], ['product_models.product_model_id'], name='underperformance_flags_product_model_id_fkey'),
    PrimaryKeyConstraint('product_model_id', 'period_start', 'period_end', name='underperformance_flags_pkey')
)

# Норматив распределения по цвету (раздел 5.6) — для нового цвета существующего артикула:
# прогноз_по_артикулу × доля_цвета, точнее подбора аналогов среди чужих моделей
t_color_profile = Table(
    'color_profile', metadata,
    Column('category', Text, primary_key=True),
    Column('color_group', Text, primary_key=True),
    Column('share_of_sales_pct', Numeric(6, 3), nullable=False),
    Column('report_date', Date, primary_key=True),
    PrimaryKeyConstraint('category', 'color_group', 'report_date', name='color_profile_pkey')
)

# Кросс-эластичность между парой связанных SKU (раздел 5.3) — формализует каннибализацию
# той же регрессией, что и собственная эластичность (price_elasticity ниже)
t_cross_elasticity = Table(
    'cross_elasticity', metadata,
    Column('product_size_id_a', Integer, primary_key=True),
    Column('product_size_id_b', Integer, primary_key=True),
    Column('cross_elasticity_coefficient', Numeric(6, 4), nullable=False),
    Column('sample_size', Integer),
    Column('computed_at', DateTime, server_default=func.now()),
    ForeignKeyConstraint(['product_size_id_a'], ['product_sizes.product_size_id'], name='cross_elasticity_product_size_id_a_fkey'),
    ForeignKeyConstraint(['product_size_id_b'], ['product_sizes.product_size_id'], name='cross_elasticity_product_size_id_b_fkey'),
    PrimaryKeyConstraint('product_size_id_a', 'product_size_id_b', name='cross_elasticity_pkey')
)

# Промо-каннибализация между РАЗНЫМИ артикулами (раздел 5.3) — второй, отдельный механизм:
# акция на артикул B уводит покупателей от артикула A по обычной цене. Двухвыборочный t-test,
# не регрессия (в отличие от cross_elasticity выше, которая про доступность внутри артикула)
t_promo_cannibalization_pairs = Table(
    'promo_cannibalization_pairs', metadata,
    Column('product_model_id_a', Integer, primary_key=True),
    Column('product_model_id_b', Integer, primary_key=True),
    Column('mean_sales_a_no_promo_b', Numeric(10, 2)),
    Column('mean_sales_a_promo_b', Numeric(10, 2)),
    Column('demand_shift_d', Numeric(10, 2)),
    Column('p_value', Numeric(6, 5)),
    Column('significant', Boolean),
    Column('computed_at', DateTime, server_default=func.now()),
    ForeignKeyConstraint(['product_model_id_a'], ['product_models.product_model_id'], name='promo_cannibalization_pairs_product_model_id_a_fkey'),
    ForeignKeyConstraint(['product_model_id_b'], ['product_models.product_model_id'], name='promo_cannibalization_pairs_product_model_id_b_fkey'),
    PrimaryKeyConstraint('product_model_id_a', 'product_model_id_b', name='promo_cannibalization_pairs_pkey')
)

# Сигнал каннибализации между цветами/размерами одного артикула (раздел 5.3) — конкретные события,
# demand_shift_qty теперь считается через cross_elasticity, а не отдельной эвристикой
t_cannibalization_events = Table(
    'cannibalization_events', metadata,
    Column('event_id', BigIntPK, primary_key=True, autoincrement=True),
    Column('product_size_id_out_of_stock', Integer, nullable=False),
    Column('product_size_id_gained_demand', Integer, nullable=False),
    Column('event_date', Date, nullable=False),
    Column('demand_shift_qty', Numeric(10, 2)),
    Column('detected_at', DateTime, server_default=func.now()),
    ForeignKeyConstraint(['product_size_id_gained_demand'], ['product_sizes.product_size_id'], name='cannibalization_events_product_size_id_gained_demand_fkey'),
    ForeignKeyConstraint(['product_size_id_out_of_stock'], ['product_sizes.product_size_id'], name='cannibalization_events_product_size_id_out_of_stock_fkey'),
    PrimaryKeyConstraint('event_id', name='cannibalization_events_pkey')
)

# Источник: GET /v2/promotions (раздел 4) — API v2, найден повторным изучением документации после того,
# как считался недоступным по устаревшей v1-документации. Точное соответствие полей не подтверждено —
# уточнить на этапе реализации (может не покрывать разбивку "Общие данные" 1:1, раньше бравшуюся из Excel)
t_promo_report_daily = Table(
    'promo_report_daily', metadata,
    Column('report_date', Date, primary_key=True),
    Column('orders_sum_promo', Numeric(12, 2)),
    Column('orders_sum_all', Numeric(12, 2)),
    Column('orders_share_promo_pct', Numeric(6, 2)),
    Column('redemption_sum_promo', Numeric(12, 2)),
    Column('redemption_sum_all', Numeric(12, 2)),
    Column('redemption_share_promo_pct', Numeric(6, 2)),
    PrimaryKeyConstraint('report_date', name='promo_report_daily_pkey')
)

# Источник: GET /v2/promotions/{promotionId}/products (раздел 4) — по одному товару на акцию,
# естественно ложится в структуру этой таблицы. avg_price_seller — проверить, отдаёт ли API
# эту разбивку явно или только итоговую цену покупателя (раздел 5.7, открытый вопрос №1)
t_promo_report_sku = Table(
    'promo_report_sku', metadata,
    Column('product_size_id', Integer, primary_key=True),
    Column('period_start', Date, primary_key=True),
    Column('period_end', Date, primary_key=True),
    Column('orders_sum_promo', Numeric(12, 2)),
    Column('orders_qty_promo', Integer),
    Column('redemption_sum_promo', Numeric(12, 2)),
    Column('redemption_qty_promo', Integer),
    Column('redemption_rate_pct', Numeric(6, 3)),
    Column('avg_price_seller_and_lamoda', Numeric(10, 2)),
    Column('avg_discount_seller_and_lamoda_pct', Numeric(6, 3)),
    Column('avg_price_seller', Numeric(10, 2)),
    Column('avg_discount_seller_pct', Numeric(6, 3)),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='promo_report_sku_product_size_id_fkey'),
    PrimaryKeyConstraint('product_size_id', 'period_start', 'period_end', name='promo_report_sku_pkey')
)

t_wordstat_data = Table(
    'wordstat_data', metadata,
    Column('keyword', Text, primary_key=True),
    Column('stat_date', Date, primary_key=True),
    Column('region', Text, primary_key=True),
    Column('frequency', Integer),
    PrimaryKeyConstraint('keyword', 'stat_date', 'region', name='wordstat_data_pkey')
)

# Google Trends (раздел 5.3) — независимый источник поискового интереса, не замена Вордстату, а второй замер
t_google_trends_data = Table(
    'google_trends_data', metadata,
    Column('keyword', Text, primary_key=True),
    Column('trend_date', Date, primary_key=True),
    Column('region', Text, primary_key=True),
    Column('popularity_index', Numeric(5, 2)),
    PrimaryKeyConstraint('keyword', 'trend_date', 'region', name='google_trends_data_pkey')
)

# Клики Яндекс.Директ на карточки своего сайта (раздел 5.3) — сигнал интереса точнее Вордстата,
# т.к. это клик по конкретной модели, а не поиск по фасону/категории в целом
# Сопоставление объявления и товара (раздел 4) — заполняется при создании кампании/объявления,
# не парсится из статистики Директа (там нет посадочной страницы как поля отчёта по умолчанию)
t_yandex_direct_ad_mapping = Table(
    'yandex_direct_ad_mapping', metadata,
    Column('ad_id', Text, primary_key=True),
    Column('campaign_id', Text),
    Column('product_model_id', Integer, nullable=False),
    Column('created_at', DateTime, server_default=func.now()),
    ForeignKeyConstraint(['product_model_id'], ['product_models.product_model_id'], name='yandex_direct_ad_mapping_product_model_id_fkey'),
    PrimaryKeyConstraint('ad_id', name='yandex_direct_ad_mapping_pkey')
)

t_yandex_direct_clicks = Table(
    'yandex_direct_clicks', metadata,
    Column('ad_id', Text, primary_key=True),
    Column('click_date', Date, primary_key=True),
    Column('clicks', Integer),
    Column('impressions', Integer),
    Column('ctr_pct', Numeric(6, 3)),
    Column('spend', Numeric(10, 2)),
    ForeignKeyConstraint(['ad_id'], ['yandex_direct_ad_mapping.ad_id'], name='yandex_direct_clicks_ad_id_fkey'),
    PrimaryKeyConstraint('ad_id', 'click_date', name='yandex_direct_clicks_pkey')
)

# Аудитория кликнувших по рекламе (раздел 5.3) — из отчёта "Аудитория" Яндекс.Метрики,
# сравнивается с эталонным профилем покупателей для калибровки веса коэф_direct
t_metrika_audience_segments = Table(
    'metrika_audience_segments', metadata,
    Column('product_model_id', Integer, primary_key=True),
    Column('segment_date', Date, primary_key=True),
    Column('segment_type', Text, primary_key=True),
    Column('segment_value', Text, primary_key=True),
    Column('share_of_clicks_pct', Numeric(6, 3), nullable=False),
    ForeignKeyConstraint(['product_model_id'], ['product_models.product_model_id'], name='metrika_audience_segments_product_model_id_fkey'),
    PrimaryKeyConstraint('product_model_id', 'segment_date', 'segment_type', 'segment_value', name='metrika_audience_segments_pkey')
)

# Эталонный профиль реальных покупателей (раздел 5.3) — из CRM AWER, заказы с собственного сайта
t_buyer_profile_reference = Table(
    'buyer_profile_reference', metadata,
    Column('segment_type', Text, primary_key=True),
    Column('segment_value', Text, primary_key=True),
    Column('share_of_buyers_pct', Numeric(6, 3), nullable=False),
    Column('computed_at', DateTime, server_default=func.now()),
    PrimaryKeyConstraint('segment_type', 'segment_value', name='buyer_profile_reference_pkey')
)

# Качество аудитории кликнувших относительно реальных покупателей (раздел 5.3) —
# множитель для коэф_direct: 1 = аудитория идентична покупателям, 0 = совпадений нет
t_audience_quality_score = Table(
    'audience_quality_score', metadata,
    Column('product_model_id', Integer, primary_key=True),
    Column('score_date', Date, primary_key=True),
    Column('quality_score', Numeric(4, 3), nullable=False),
    Column('computed_at', DateTime, server_default=func.now()),
    ForeignKeyConstraint(['product_model_id'], ['product_models.product_model_id'], name='audience_quality_score_product_model_id_fkey'),
    PrimaryKeyConstraint('product_model_id', 'score_date', name='audience_quality_score_pkey')
)

t_weather_data = Table(
    'weather_data', metadata,
    Column('region', Text, primary_key=True),
    Column('weather_date', Date, primary_key=True),
    Column('avg_temp_c', Numeric(4, 1)),
    PrimaryKeyConstraint('region', 'weather_date', name='weather_data_pkey')
)

# Конкурентная аналитика по категориям (раздел 5.6) — внешний рыночный тренд,
# ценен особенно там, где своей истории в категории мало или нет вообще
t_category_market_trends = Table(
    'category_market_trends', metadata,
    Column('category', Text, primary_key=True),
    Column('trend_date', Date, primary_key=True),
    Column('market_growth_index', Numeric(6, 3)),
    Column('competitor_count', Integer),
    Column('avg_category_price', Numeric(10, 2)),
    Column('source', Text),
    PrimaryKeyConstraint('category', 'trend_date', name='category_market_trends_pkey')
)

t_usd_rate = Table(
    'usd_rate', metadata,
    Column('rate_date', Date, primary_key=True),
    Column('cbr_rate', Numeric(8, 4), nullable=False),
    Column('adjusted_rate', Numeric(8, 4), nullable=False),
    PrimaryKeyConstraint('rate_date', name='usd_rate_pkey')
)


# ==================== 3. РАСЧЁТНЫЕ СУЩНОСТИ ====================


t_demand_timeseries = Table(
    'demand_timeseries', metadata,
    Column('product_size_id', Integer, primary_key=True),
    Column('demand_date', Date, primary_key=True),
    Column('net_demand', Numeric(10, 2), nullable=False),
    Column('base_demand', Numeric(10, 2)),
    Column('promo_uplift', Numeric(10, 2)),
    Column('is_stockout_corrected', Boolean, server_default=false()),
    Column('is_return_censored', Boolean, server_default=false()),
    Column('is_recovery_tail_corrected', Boolean, server_default=false()),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='demand_timeseries_product_size_id_fkey'),
    PrimaryKeyConstraint('product_size_id', 'demand_date', name='demand_timeseries_pkey')
)

t_abc_xyz_classification = Table(
    'abc_xyz_classification', metadata,
    Column('product_model_id', Integer, primary_key=True),
    Column('period_start', Date, primary_key=True),
    Column('combined_score', Numeric(10, 4)),
    Column('abc_class', Text, nullable=False),
    Column('coefficient_variation', Numeric(6, 3)),
    Column('xyz_class', Text, nullable=False),
    Column('combined_segment', Text, nullable=False),
    Column('service_level_z', Numeric(4, 3), nullable=False),
    Column('review_frequency', Text),
    Column('review_interval_days', Integer),
    ForeignKeyConstraint(['product_model_id'], ['product_models.product_model_id'], name='abc_xyz_classification_product_model_id_fkey'),
    PrimaryKeyConstraint('product_model_id', 'period_start', name='abc_xyz_classification_pkey')
)

t_lead_time_stats = Table(
    'lead_time_stats', metadata,
    Column('scope_type', Text, primary_key=True),
    Column('scope_id', Text, primary_key=True),
    Column('lead_type', Text, primary_key=True),
    Column('median_days', Numeric(6, 2), nullable=False),
    Column('std_days', Numeric(6, 2)),
    Column('computed_at', DateTime, server_default=func.now()),
    PrimaryKeyConstraint('scope_type', 'scope_id', 'lead_type', name='lead_time_stats_pkey')
)

t_demand_forecast = Table(
    'demand_forecast', metadata,
    Column('product_size_id', Integer, primary_key=True),
    Column('channel', Text, primary_key=True, server_default=text("'lamoda'")),
    Column('period_start', Date, primary_key=True),
    Column('period_end', Date, primary_key=True),
    Column('forecast_qty', Numeric(10, 2), nullable=False),
    Column('lower_bound', Numeric(10, 2)),
    Column('upper_bound', Numeric(10, 2)),
    Column('model_type', Text),
    Column('adi_days', Numeric(6, 2)),
    Column('cv_squared', Numeric(6, 3)),
    Column('demand_pattern', Text),
    Column('computed_at', DateTime, server_default=func.now()),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='demand_forecast_product_size_id_fkey'),
    PrimaryKeyConstraint('product_size_id', 'channel', 'period_start', 'period_end', name='demand_forecast_pkey'),
    Index('idx_demand_forecast_period', 'period_start', 'period_end')
)

# Агрегированный спрос на ЦС по всем каналам сразу (раздел 5) — источник для точки заказа/закупки
# у поставщика (раздел 5.5/6), т.к. WB/Ozon/Яндекс Маркет (FBS) тянут остаток напрямую с ЦС,
# не через отдельный склад площадки, как Lamoda (FBO)
t_central_stock_aggregate_demand = Table(
    'central_stock_aggregate_demand', metadata,
    Column('product_size_id', Integer, primary_key=True),
    Column('period_start', Date, primary_key=True),
    Column('period_end', Date, primary_key=True),
    Column('total_forecast_qty', Numeric(10, 2), nullable=False),
    Column('channel_breakdown', JSONType),
    Column('computed_at', DateTime, server_default=func.now()),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='central_stock_aggregate_demand_product_size_id_fkey'),
    PrimaryKeyConstraint('product_size_id', 'period_start', 'period_end', name='central_stock_aggregate_demand_pkey')
)

t_reorder_points = Table(
    'reorder_points', metadata,
    Column('product_size_id', Integer, primary_key=True),
    Column('computed_at', DateTime, primary_key=True, server_default=func.now()),
    Column('lead_time_used_days', Numeric(6, 2)),
    Column('sigma_used', Numeric(10, 3)),
    Column('formula_variant', Text, server_default=text("'basic'")),
    Column('safety_stock', Numeric(10, 2)),
    Column('reorder_point_qty', Numeric(10, 2), nullable=False),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='reorder_points_product_size_id_fkey'),
    PrimaryKeyConstraint('product_size_id', 'computed_at', name='reorder_points_pkey')
)

t_new_item_forecast = Table(
    'new_item_forecast', metadata,
    Column('product_model_id', Integer, primary_key=True),
    Column('computed_at', DateTime, primary_key=True, server_default=func.now()),
    Column('analog_based_forecast_qty', Numeric(10, 2)),
    Column('category_correction_median', Numeric(6, 4)),
    Column('category_correction_std', Numeric(6, 4)),
    Column('wordstat_coef', Numeric(6, 4)),
    Column('final_forecast_qty', Numeric(10, 2)),
    Column('deepar_forecast_qty', Numeric(10, 2)),
    Column('coverage_period_days', Integer),
    Column('underage_cost_cu', Numeric(10, 2)),
    Column('overage_cost_co', Numeric(10, 2)),
    Column('critical_fractile', Numeric(5, 4)),
    Column('z_newsvendor', Numeric(5, 3)),
    Column('recommended_first_shipment_qty', Numeric(10, 2)),
    Column('moq_adjusted_qty', Numeric(10, 2)),
    Column('risk_flag', Boolean, server_default=false()),
    ForeignKeyConstraint(['product_model_id'], ['product_models.product_model_id'], name='new_item_forecast_product_model_id_fkey'),
    PrimaryKeyConstraint('product_model_id', 'computed_at', name='new_item_forecast_pkey')
)

# Явный шаг раскладки агрегатного прогноза новинки по размерам через size_profile —
# без него purchase_recommendations (уровень размера) неоткуда получить количество
t_new_item_size_allocation = Table(
    'new_item_size_allocation', metadata,
    Column('product_model_id', Integer, primary_key=True),
    Column('computed_at', DateTime, primary_key=True),
    Column('product_size_id', Integer, primary_key=True),
    Column('allocated_qty', Numeric(10, 2), nullable=False),
    ForeignKeyConstraint(['product_model_id', 'computed_at'], ['new_item_forecast.product_model_id', 'new_item_forecast.computed_at'], name='new_item_size_allocation_product_model_id_computed_at_fkey'),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='new_item_size_allocation_product_size_id_fkey'),
    PrimaryKeyConstraint('product_model_id', 'computed_at', 'product_size_id', name='new_item_size_allocation_pkey')
)

# Ежемесячная сверка с официальным отчётом комиссионера Lamoda (раздел 5.7) — не автоматизировано (ЭДО-документ,
# не API), заполняется вручную раз в месяц для контроля накопленного расхождения margin_calc
t_lamoda_official_reconciliation = Table(
    'lamoda_official_reconciliation', metadata,
    Column('period_month', Date, primary_key=True),
    Column('official_price_realized_incl_vat', Numeric(14, 2)),
    Column('our_computed_sum', Numeric(14, 2)),
    Column('discrepancy_pct', Numeric(6, 2)),
    Column('reconciled_at', DateTime, server_default=func.now()),
    PrimaryKeyConstraint('period_month', name='lamoda_official_reconciliation_pkey')
)

t_margin_calc = Table(
    'margin_calc', metadata,
    Column('product_size_id', Integer, primary_key=True),
    Column('calc_date', Date, primary_key=True),
    Column('channel', Text, primary_key=True, server_default=text("'lamoda'")),
    Column('price_realized', Numeric(10, 2), nullable=False),
    Column('cost_unit', Numeric(10, 2), nullable=False),
    Column('commission_amount', Numeric(10, 2)),
    Column('logistics_fbo_amount', Numeric(10, 2)),
    Column('vat_amount', Numeric(10, 2)),
    Column('usn_tax_amount', Numeric(10, 2)),
    Column('margin_abs', Numeric(10, 2)),
    Column('margin_pct', Numeric(6, 2)),
    Column('deviation_reason', Text),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='margin_calc_product_size_id_fkey'),
    PrimaryKeyConstraint('product_size_id', 'calc_date', 'channel', name='margin_calc_pkey'),
    Index('idx_margin_calc_date', 'calc_date')
)

# Комиссия/логистика по каналу и категории (раздел 5.7-доп) — WB: Tariffs API (комиссии, тарифы на короба/
# паллеты/возврат/приёмку); Ozon: FinanceAPI/тарифы (точный метод уточнить при реализации, раздел 9)
t_channel_commission_rates = Table(
    'channel_commission_rates', metadata,
    Column('channel', Text, primary_key=True),
    Column('category', Text, primary_key=True),
    Column('commission_pct', Numeric(6, 3)),
    Column('logistics_fee', Numeric(10, 2)),
    Column('effective_from', Date, primary_key=True),
    Column('effective_to', Date),
    PrimaryKeyConstraint('channel', 'category', 'effective_from', name='channel_commission_rates_pkey')
)

t_anomaly_triage_log = Table(
    'anomaly_triage_log', metadata,
    Column('triage_id', BigIntPK, primary_key=True, autoincrement=True),
    Column('product_size_id', Integer),
    Column('period_start', Date),
    Column('period_end', Date),
    Column('signals', JSONType),
    Column('probable_cause', Text),
    Column('recommended_action', Text),
    Column('confidence', Numeric(4, 3)),
    Column('manager_decision', Text),
    Column('decided_at', DateTime),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='anomaly_triage_log_product_size_id_fkey'),
    PrimaryKeyConstraint('triage_id', name='anomaly_triage_log_pkey')
)

# Точность прогноза (раздел 5.8) — по каждому SKU и периоду, для агрегации по сегменту ABC×XYZ
t_forecast_accuracy_log = Table(
    'forecast_accuracy_log', metadata,
    Column('product_size_id', Integer, primary_key=True),
    Column('period_start', Date, primary_key=True),
    Column('period_end', Date, primary_key=True),
    Column('forecast_qty', Numeric(10, 2)),
    Column('actual_qty', Numeric(10, 2)),
    Column('mape', Numeric(6, 3)),
    Column('wape', Numeric(6, 3)),
    Column('mae', Numeric(10, 2)),
    Column('rmse', Numeric(10, 2)),
    Column('avg_deficit', Numeric(10, 2)),
    Column('avg_surplus', Numeric(10, 2)),
    Column('deviation_status', Text, server_default=text("'normal'")),
    Column('consecutive_warning_count', Integer, server_default=text('0')),
    Column('computed_at', DateTime, server_default=func.now()),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='forecast_accuracy_log_product_size_id_fkey'),
    PrimaryKeyConstraint('product_size_id', 'period_start', 'period_end', name='forecast_accuracy_log_pkey')
)

# Пороги эскалации по MAPE (раздел 5.8) — по категории, т.к. у категорий разная естественная волатильность
t_forecast_deviation_settings = Table(
    'forecast_deviation_settings', metadata,
    Column('category', Text, primary_key=True),
    Column('mape_warning_threshold', Numeric(5, 2), nullable=False, server_default=text('20')),
    Column('mape_critical_threshold', Numeric(5, 2), nullable=False, server_default=text('40')),
    Column('consecutive_periods_trigger', Integer, nullable=False, server_default=text('3')),
    PrimaryKeyConstraint('category', name='forecast_deviation_settings_pkey')
)

# Лог смены метода прогноза при систематическом отклонении (раздел 5.8) или по бэктесту (5.2)
t_model_recalibration_log = Table(
    'model_recalibration_log', metadata,
    Column('recalibration_id', BigIntPK, primary_key=True, autoincrement=True),
    Column('product_size_id', Integer, nullable=False),
    Column('triggered_at', DateTime, nullable=False, server_default=func.now()),
    Column('reason', Text),
    Column('old_model_type', Text),
    Column('new_model_type', Text),
    Column('triggered_by', Text, server_default=text("'auto'")),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='model_recalibration_log_product_size_id_fkey'),
    PrimaryKeyConstraint('recalibration_id', name='model_recalibration_log_pkey')
)

# Результаты бэктест-турнира методов прогноза (раздел 5.2) — по каждому кандидату на holdout-окне
t_model_backtest_results = Table(
    'model_backtest_results', metadata,
    Column('backtest_id', BigIntPK, primary_key=True, autoincrement=True),
    Column('product_size_id', Integer, nullable=False),
    Column('backtest_date', Date, nullable=False),
    Column('model_type', Text, nullable=False),
    Column('wape_holdout', Numeric(6, 3), nullable=False),
    Column('selected', Boolean, server_default=false()),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='model_backtest_results_product_size_id_fkey'),
    PrimaryKeyConstraint('backtest_id', name='model_backtest_results_pkey')
)

# Степень достоверности рекомендации (раздел 5.8) — композитный индикатор в процентах для страницы
# поставок (раздел 6), каждый штраф хранится отдельно для прозрачности, не просто итоговое число
t_recommendation_confidence = Table(
    'recommendation_confidence', metadata,
    Column('product_size_id', Integer, primary_key=True),
    Column('computed_at', DateTime, primary_key=True, server_default=func.now()),
    Column('wape_penalty', Numeric(5, 2)),
    Column('demand_pattern_penalty', Numeric(5, 2)),
    Column('short_history_penalty', Numeric(5, 2)),
    Column('model_disagreement_penalty', Numeric(5, 2)),
    Column('recent_deviation_penalty', Numeric(5, 2)),
    Column('stockout_share_penalty', Numeric(5, 2)),
    Column('confidence_pct', Numeric(5, 2), nullable=False),
    Column('explanation_text', Text),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='recommendation_confidence_product_size_id_fkey'),
    PrimaryKeyConstraint('product_size_id', 'computed_at', name='recommendation_confidence_pkey')
)

# Fill rate (Type 2 service level, раздел 5.8) — доля фактически закрытого спроса,
# метрика мониторинга, отдельно от z (cycle service level, используется для расчёта буфера)
t_fill_rate_log = Table(
    'fill_rate_log', metadata,
    Column('product_size_id', Integer, primary_key=True),
    Column('period_start', Date, primary_key=True),
    Column('period_end', Date, primary_key=True),
    Column('demand_qty_total', Numeric(10, 2)),
    Column('demand_qty_fulfilled', Numeric(10, 2)),
    Column('fill_rate_pct', Numeric(6, 3)),
    Column('computed_at', DateTime, server_default=func.now()),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='fill_rate_log_product_size_id_fkey'),
    PrimaryKeyConstraint('product_size_id', 'period_start', 'period_end', name='fill_rate_log_pkey')
)

# Надёжность поставщика (раздел 5.9) — факт vs заявленный lead time по каждой поставке
t_supplier_deliveries = Table(
    'supplier_deliveries', metadata,
    Column('delivery_id', BigIntPK, primary_key=True, autoincrement=True),
    Column('supplier_id', Integer, nullable=False),
    Column('ordered_date', Date, nullable=False),
    Column('expected_ready_date', Date, nullable=False),
    Column('actual_ready_date', Date),
    Column('on_time', Boolean),
    ForeignKeyConstraint(['supplier_id'], ['suppliers.supplier_id'], name='supplier_deliveries_supplier_id_fkey'),
    PrimaryKeyConstraint('delivery_id', name='supplier_deliveries_pkey')
)

# Ценовая эластичность (раздел 5.10) — по модели, с fallback на категорию при нехватке истории
t_price_elasticity = Table(
    'price_elasticity', metadata,
    Column('scope_type', Text, primary_key=True),
    Column('scope_id', Text, primary_key=True),
    Column('method', Text, server_default=text("'log_log_regression'")),
    Column('elasticity_coefficient', Numeric(6, 4), nullable=False),
    Column('r_squared', Numeric(4, 3)),
    Column('sample_size', Integer),
    Column('computed_at', DateTime, server_default=func.now()),
    PrimaryKeyConstraint('scope_type', 'scope_id', name='price_elasticity_pkey')
)

# Сценарии "что если цена изменится" (раздел 5.10) — просчитанные и применённые
t_price_scenario_log = Table(
    'price_scenario_log', metadata,
    Column('scenario_id', BigIntPK, primary_key=True, autoincrement=True),
    Column('product_size_id', Integer, nullable=False),
    Column('price_change_pct', Numeric(6, 2), nullable=False),
    Column('predicted_demand_change_pct', Numeric(6, 2)),
    Column('predicted_margin_impact', Numeric(10, 2)),
    Column('applied', Boolean, server_default=false()),
    Column('created_at', DateTime, server_default=func.now()),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='price_scenario_log_product_size_id_fkey'),
    PrimaryKeyConstraint('scenario_id', name='price_scenario_log_pkey')
)

# A/B-тест тестового повышения цены (раздел 5.10) — difference-in-differences с контрольной группой похожих SKU,
# т.к. на маркетплейсе нельзя случайно разделить покупателей на группы
t_price_ab_test = Table(
    'price_ab_test', metadata,
    Column('test_id', BigIntPK, primary_key=True, autoincrement=True),
    Column('treatment_product_size_id', Integer, nullable=False),
    Column('control_product_size_ids', IntArray, nullable=False),
    Column('scenario_id', BigInteger),
    Column('test_start', Date, nullable=False),
    Column('test_end', Date, nullable=False),
    Column('demand_change_pct_treatment', Numeric(6, 2)),
    Column('demand_change_pct_control', Numeric(6, 2)),
    Column('diff_in_diff_effect', Numeric(6, 2)),
    Column('decision', Text),
    Column('created_at', DateTime, server_default=func.now()),
    ForeignKeyConstraint(['scenario_id'], ['price_scenario_log.scenario_id'], name='price_ab_test_scenario_id_fkey'),
    ForeignKeyConstraint(['treatment_product_size_id'], ['product_sizes.product_size_id'], name='price_ab_test_treatment_product_size_id_fkey'),
    PrimaryKeyConstraint('test_id', name='price_ab_test_pkey')
)

# Оптимизация цен по всей линейке (раздел 5.11) — один запуск решает задачу сразу для группы связанных SKU
t_price_optimization_run = Table(
    'price_optimization_run', metadata,
    Column('run_id', BigIntPK, primary_key=True, autoincrement=True),
    Column('run_date', Date, nullable=False),
    Column('scope', Text),
    Column('objective_value_before', Numeric(14, 2)),
    Column('objective_value_after', Numeric(14, 2)),
    Column('status', Text, server_default=text("'completed'")),
    Column('created_at', DateTime, server_default=func.now()),
    PrimaryKeyConstraint('run_id', name='price_optimization_run_pkey')
)

# Результат оптимизации по каждому SKU, входящему в запуск (раздел 5.11)
t_price_optimization_result = Table(
    'price_optimization_result', metadata,
    Column('run_id', BigInteger, primary_key=True),
    Column('product_size_id', Integer, primary_key=True),
    Column('current_price', Numeric(10, 2)),
    Column('recommended_price', Numeric(10, 2)),
    Column('price_change_pct', Numeric(6, 2)),
    Column('predicted_margin_impact', Numeric(10, 2)),
    Column('predicted_demand_impact_pct', Numeric(6, 2)),
    Column('stock_check_passed', Boolean, server_default=true()),
    Column('included_in_recommendations', Boolean, server_default=false()),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='price_optimization_result_product_size_id_fkey'),
    ForeignKeyConstraint(['run_id'], ['price_optimization_run.run_id'], name='price_optimization_result_run_id_fkey'),
    PrimaryKeyConstraint('run_id', 'product_size_id', name='price_optimization_result_pkey')
)

# Ручные корректировки прогноза (раздел 6) — расчётное значение никогда не перезаписывается,
# только добавляется override поверх; история видна целиком
t_forecast_overrides = Table(
    'forecast_overrides', metadata,
    Column('override_id', BigIntPK, primary_key=True, autoincrement=True),
    Column('product_size_id', Integer, nullable=False),
    Column('period_start', Date, nullable=False),
    Column('period_end', Date, nullable=False),
    Column('original_forecast_qty', Numeric(10, 2)),
    Column('override_qty', Numeric(10, 2), nullable=False),
    Column('reason', Text, nullable=False),
    Column('overridden_by', Text, nullable=False),
    Column('created_at', DateTime, server_default=func.now()),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='forecast_overrides_product_size_id_fkey'),
    PrimaryKeyConstraint('override_id', name='forecast_overrides_pkey')
)


# ==================== 4. ВЫХОДНЫЕ РЕКОМЕНДАЦИИ ====================


t_shipment_recommendations = Table(
    'shipment_recommendations', metadata,
    Column('recommendation_id', BigIntPK, primary_key=True, autoincrement=True),
    Column('product_size_id', Integer, nullable=False),
    Column('stock_1c_qty_snapshot', Numeric(10, 2)),
    Column('stock_fbo_qty_snapshot', Numeric(10, 2)),
    Column('qty_to_ship', Numeric(10, 2), nullable=False),
    Column('confidence_pct', Numeric(5, 2)),
    Column('confidence_explanation', Text),
    Column('confirmed_qty', Numeric(10, 2)),
    Column('related_fbo_shipment_id', Text),
    Column('correction_pct_vs_original', Numeric(6, 2)),
    Column('penalty_risk', Boolean, server_default=false()),
    Column('target_ship_date', Date),
    Column('status', Text, server_default=text("'pending'")),
    Column('created_at', DateTime, server_default=func.now()),
    # Фаза 1: номер пересчёта (job_runs.run_id) — страница показывает последний; кто и когда
    # подтвердил; номер сборки поставки (job_runs.run_id задания fbo_assembly). Статус
    # 'superseded' — строка заменена более свежим пересчётом.
    Column('batch_id', BigInteger),
    Column('confirmed_by', Text),
    Column('confirmed_at', DateTime),
    Column('assembly_id', BigInteger),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='shipment_recommendations_product_size_id_fkey'),
    PrimaryKeyConstraint('recommendation_id', name='shipment_recommendations_pkey')
)

t_purchase_recommendations = Table(
    'purchase_recommendations', metadata,
    Column('recommendation_id', BigIntPK, primary_key=True, autoincrement=True),
    Column('product_size_id', Integer, nullable=False),
    Column('supplier_id', Integer),
    Column('qty_to_purchase', Numeric(10, 2), nullable=False),
    Column('ready_by_date', Date),
    Column('status', Text, server_default=text("'pending'")),
    Column('created_at', DateTime, server_default=func.now()),
    # Фаза 1: снимок входов расчёта (как у shipment_recommendations — чтобы выгрузка не «уезжала»),
    # пояснение, номер пересчёта и отметка «заказано». Статусы сверх pending/ordered/received:
    # 'no_reorder' — пополнение нужно, но товар недоступен к заказу (предупреждение раздела 5.5),
    # 'superseded' — строка заменена более свежим пересчётом.
    Column('batch_id', BigInteger),
    Column('stock_1c_qty_snapshot', Numeric(10, 2)),
    Column('stock_fbo_qty_snapshot', Numeric(10, 2)),
    Column('in_transit_qty_snapshot', Numeric(10, 2)),
    Column('on_order_qty_snapshot', Numeric(10, 2)),
    Column('daily_demand', Numeric(10, 3)),
    Column('reorder_point_qty', Numeric(10, 2)),
    Column('explanation', Text),
    Column('ordered_qty', Numeric(10, 2)),
    Column('ordered_by', Text),
    Column('ordered_at', DateTime),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='purchase_recommendations_product_size_id_fkey'),
    ForeignKeyConstraint(['supplier_id'], ['suppliers.supplier_id'], name='purchase_recommendations_supplier_id_fkey'),
    PrimaryKeyConstraint('recommendation_id', name='purchase_recommendations_pkey')
)

t_pricing_recommendations = Table(
    'pricing_recommendations', metadata,
    Column('recommendation_id', BigIntPK, primary_key=True, autoincrement=True),
    Column('product_size_id', Integer, nullable=False),
    Column('recommendation_type', Text, nullable=False),
    Column('rationale', Text),
    Column('margin_pct_at_calc', Numeric(6, 2)),
    Column('source', Text, server_default=text("'rule'")),
    Column('optimization_run_id', BigInteger),
    Column('status', Text, server_default=text("'pending'")),
    Column('suppressed_by_triage_id', BigInteger),
    Column('approved_by', Text),
    Column('approved_at', DateTime),
    Column('created_at', DateTime, server_default=func.now()),
    ForeignKeyConstraint(['optimization_run_id'], ['price_optimization_run.run_id'], name='pricing_recommendations_optimization_run_id_fkey'),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='pricing_recommendations_product_size_id_fkey'),
    ForeignKeyConstraint(['suppressed_by_triage_id'], ['anomaly_triage_log.triage_id'], name='pricing_recommendations_suppressed_by_triage_id_fkey'),
    PrimaryKeyConstraint('recommendation_id', name='pricing_recommendations_pkey')
)

# Лог фактического применения цены через API Lamoda (раздел 6) — по каждой рекомендации, применённой менеджером
t_price_change_log = Table(
    'price_change_log', metadata,
    Column('price_change_id', BigIntPK, primary_key=True, autoincrement=True),
    Column('recommendation_id', BigInteger, nullable=False),
    Column('product_size_id', Integer, nullable=False),
    Column('old_price', Numeric(10, 2)),
    Column('new_price', Numeric(10, 2), nullable=False),
    Column('api_method', Text, server_default=text("'POST /v2/nomenclature-price'")),
    Column('api_response_status', Text),
    Column('fraud_validation_result', Text),
    Column('api_error_message', Text),
    Column('submitted_at', DateTime, server_default=func.now()),
    ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='price_change_log_product_size_id_fkey'),
    ForeignKeyConstraint(['recommendation_id'], ['pricing_recommendations.recommendation_id'], name='price_change_log_recommendation_id_fkey'),
    PrimaryKeyConstraint('price_change_id', name='price_change_log_pkey')
)


# ==================== Расширения Фазы 1 (миграция 0002) ====================
# Индексы из конца schema.sql объявлены прямо в своих таблицах выше.

# Журнал заданий: опрос Lamoda, чтение 1С, пересчёт, сборка поставки. Курсор опроса
# заказов (`updatedAtFrom` следующего запуска) хранится здесь же, в `cursor`.
t_job_runs = Table(
    'job_runs', metadata,
    Column('run_id', BigIntPK, primary_key=True, autoincrement=True),
    Column('job', Text, nullable=False),
    Column('started_at', DateTime, nullable=False, server_default=func.now()),
    Column('finished_at', DateTime),
    Column('status', Text, nullable=False, server_default=text("'running'")),  # running / ok / error
    Column('message', Text),
    Column('details', JSONType),
    Column('cursor', Text),
    Column('started_by', Text),
    PrimaryKeyConstraint('run_id', name='job_runs_pkey'),
    Index('idx_job_runs_job_started', 'job', 'started_at')
)

# Дневные продажи каналов, кроме Lamoda (розница из 1С; в Фазе 3 — WB/Ozon/сайт). В schema.sql
# дневной таблицы с каналом нет: sales_daily — только Lamoda, external_channel_sales_history —
# помесячная история. Ключ — баркод (как у external_channel_sales_history): сопоставление
# с product_size идёт при расчёте через весь barcode_pool, поэтому пополнение справочника
# задним числом подхватывает уже загруженные продажи.
t_channel_sales_daily = Table(
    'channel_sales_daily', metadata,
    Column('channel', Text, nullable=False),
    Column('barcode', Text, nullable=False),
    Column('sale_date', Date, nullable=False),
    Column('qty', Numeric(10, 2), nullable=False),     # минус — возврат
    Column('revenue', Numeric(12, 2)),
    Column('source', Text),                            # имя файла выгрузки
    Column('loaded_at', DateTime, server_default=func.now()),
    PrimaryKeyConstraint('channel', 'barcode', 'sale_date', name='channel_sales_daily_pkey')
)

