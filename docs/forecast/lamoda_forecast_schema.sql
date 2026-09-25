-- ============================================================
-- Структура БД: система прогнозирования спроса — Lamoda FBO, WB/Ozon (FBS), собственный сайт, розница
-- Версия после аудита (AUDIT.md в этой же папке — перечень исправлений с номерами A-xx).
--
-- СУБД: только PostgreSQL 16+ (JSONB, INT[], BIGSERIAL, частичные индексы, UNIQUE NULLS NOT DISTINCT — с PG 15). SQLite не поддерживается —
-- тесты расчётного контура гоняются на PostgreSQL (A-40). Отдельная база, не схема рядом с sync-admin (A-41).
--
-- Иерархия: артикул → модель (артикул+цвет, = Lamoda parent SKU) → размер (= Lamoda SKU размера).
-- Уровни:
--   * факты (остатки, продажи, спрос)             — product_size
--   * прогноз                                        — product_size, но при малой истории считается на уровне
--                                                      модели и раскладывается по size_profile (forecast_level, A-30)
--   * цена (рекомендации, применение, эластичность) — product_model: цена на Lamoda/WB задаётся на модель
--                                                      (parent SKU / nmID), не на размер (A-24)
--   * аналоги, ABC×XYZ                               — product_model
--
-- Каналы (единый словарь во всех таблицах, A-20): lamoda / wb / ozon / own_site / retail.
--   own_site = awer-russia.ru на Яндекс KIT (не Яндекс Маркет, A-21).
--   Служебное значение 'aggregate' — только в таблицах, описывающих суммарный спрос на ЦС.
--
-- reorder_points/shipment_recommendations: scope 'fbo_lamoda' — только Lamoda (единственный канал со складом
-- площадки); scope 'central_stock' — точка заказа ЦС от суммарного спроса всех каналов (A-12).
--
-- ⚠️ Lamoda API отдаёт денежные суммы целыми числами в минорных единицах (копейки) — делить на 100 при приёме.
-- Касается ТОЛЬКО Lamoda: WB и Ozon отдают рубли (A-22). Все NUMERIC-суммы в этой схеме — рубли.
-- ============================================================


-- ================= 1. СПРАВОЧНИКИ (master data) =================

CREATE TABLE suppliers (
    supplier_id                 SERIAL PRIMARY KEY,
    name                        TEXT NOT NULL,
    production_days             INT NOT NULL,          -- срок производства
    delivery_days               INT NOT NULL,          -- срок доставки от поставщика
    moq                         INT NOT NULL,          -- минимальная партия
    on_time_delivery_rate_pct   NUMERIC(5,2)           -- кэш агрегата из purchase_orders, раздел 5.9
);

CREATE TABLE product_models (
    product_model_id        SERIAL PRIMARY KEY,
    article                 TEXT NOT NULL,              -- собственный артикул
    color                   TEXT NOT NULL DEFAULT '',   -- '' = без цвета; NOT NULL, иначе UNIQUE ниже
                                                        -- пропускает дубли (NULL ≠ NULL), A-03
    gender                  TEXT NOT NULL DEFAULT 'unisex'
                            CHECK (gender IN ('male', 'female', 'unisex', 'kids')), -- для size_profile, A-26
    category                TEXT NOT NULL,
    subcategory             TEXT,
    season                  TEXT,                       -- лето/зима/демисезон
    supplier_id             INT REFERENCES suppliers(supplier_id),
    collection_type         TEXT NOT NULL DEFAULT 'base'
                            CHECK (collection_type IN ('base', 'capsule')), -- капсульная не идёт в регулярный цикл
    first_sale_date         DATE,
    status                  TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('new', 'active', 'fading', 'discontinued')),
    available_to_reorder    BOOLEAN NOT NULL DEFAULT TRUE,   -- ведётся вручную
    discontinued_date       DATE,
    vat_rate                NUMERIC(5,4),               -- NULL = брать margin_settings.vat_rate; заполнено —
                                                        -- приоритетнее настроек (A-35)
    lamoda_sku              TEXT,                       -- Lamoda parent SKU модели (напр. MP002XM0WFWS) — он же
                                                        -- lamoda_parent_sku в POST /v2/nomenclature-price (A-24)
    -- wb_sku / ozon_sku убраны (A-23): у Ozon offer_id — на размер, не на модель; внешние идентификаторы
    -- всех площадок — в marketplace_listings (уровень размера, с кабинетом).
    planned_launch_price    NUMERIC(10,2),              -- плановая цена запуска — источник Cu для newsvendor (5.6)
    seasonal_group          TEXT,                       -- группа по паттерну сезонности (раздел 5.2)
    color_group             TEXT,                       -- 'базовые'/'яркие'/'принт' — для color_profile (5.6)
    UNIQUE (article, color)
);

CREATE TABLE style_tags (
    style_tag_id        SERIAL PRIMARY KEY,
    product_model_id    INT NOT NULL REFERENCES product_models(product_model_id),
    tag                 TEXT NOT NULL,                  -- оверсайз / приталенный / принт / ...
    confidence          NUMERIC(4,3),                   -- уверенность vision-агента (раздел 8.1)
    source              TEXT NOT NULL DEFAULT 'agent'
                        CHECK (source IN ('agent', 'manual', 'lamoda_attribute')),
    reviewed            BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE product_sizes (
    product_size_id     SERIAL PRIMARY KEY,
    product_model_id    INT NOT NULL REFERENCES product_models(product_model_id),
    size_label          TEXT NOT NULL,                  -- 48, 50, M, L, ...
    uid_1c              VARCHAR(36) UNIQUE,             -- УИД номенклатуры+характеристики в 1С = products.uid_1c
                                                        -- в sync-admin; связь с остатками и себестоимостью ЦС (A-04)
    lamoda_sku          TEXT,                           -- Lamoda SKU размера (напр. MP002XM0WFWSINXL)
    UNIQUE (product_model_id, size_label)
);

-- Пул баркодов — у одного product_size может быть несколько баркодов одновременно (разные площадки/1С
-- на один физический товар), не только исторически (valid_from/valid_to). Сопоставление — по совпадению
-- ЛЮБОГО баркода из пула. Первичный источник — таблица barcodes из sync-admin (через uid_1c), A-05.
CREATE TABLE barcode_pool (
    barcode_id          SERIAL PRIMARY KEY,
    product_size_id     INT NOT NULL REFERENCES product_sizes(product_size_id),
    barcode             TEXT NOT NULL UNIQUE,
    source              TEXT NOT NULL DEFAULT '1c'
                        CHECK (source IN ('1c', 'lamoda', 'wb', 'ozon', 'own_site', 'excel_import', 'manual')),
    seller_sku          TEXT,
    valid_from          DATE NOT NULL DEFAULT CURRENT_DATE,
    valid_to            DATE                            -- null = текущий активный
);

-- Внешние идентификаторы товара на площадках (A-23) — на уровне размера и кабинета: у WB nmID общий на
-- модель (повторяется по размерам), у Ozon offer_id/sku — свой на размер, у Lamoda — SKU размера.
-- account_id — id кабинета из sync-admin (platform_accounts.id), NULL для Lamoda (один кабинет) и розницы.
CREATE TABLE marketplace_listings (
    listing_id          SERIAL PRIMARY KEY,
    channel             TEXT NOT NULL CHECK (channel IN ('lamoda', 'wb', 'ozon', 'own_site')),
    account_id          INT,
    product_size_id     INT NOT NULL REFERENCES product_sizes(product_size_id),
    external_id         TEXT NOT NULL,                  -- nmID / offer_id / Lamoda SKU / KIT variant id
    external_parent_id  TEXT,                           -- nmID карточки WB / Lamoda parent SKU
    UNIQUE NULLS NOT DISTINCT (channel, account_id, external_id, product_size_id) -- account_id NULL у Lamoda
);

CREATE TABLE product_analogs (
    product_model_id_new     INT NOT NULL REFERENCES product_models(product_model_id),
    product_model_id_analog  INT NOT NULL REFERENCES product_models(product_model_id),
    similarity_weight        NUMERIC(4,3) NOT NULL CHECK (similarity_weight BETWEEN 0 AND 1),
    PRIMARY KEY (product_model_id_new, product_model_id_analog)
);

-- Настройки маржи — версионируемые (история изменений порогов/ставок)
CREATE TABLE margin_settings (
    setting_id                  SERIAL PRIMARY KEY,
    effective_from              DATE NOT NULL UNIQUE,
    lamoda_commission_rate      NUMERIC(5,4) NOT NULL DEFAULT 0.42, -- ставка по умолчанию; если в
                                                                   -- channel_commission_rates есть ставка категории
                                                                   -- для lamoda — берётся она (A-36)
    tax_regime                  TEXT NOT NULL DEFAULT 'УСН доходы',
    vat_rate                    NUMERIC(5,4) NOT NULL DEFAULT 0.05,
    usn_rate                    NUMERIC(5,4) NOT NULL DEFAULT 0.06,
    margin_min_pct              NUMERIC(5,2) NOT NULL DEFAULT 30,
    margin_target_pct           NUMERIC(5,2) NOT NULL DEFAULT 50,
    margin_max_pct              NUMERIC(5,2) NOT NULL DEFAULT 80,
    margin_floor_band_pp        NUMERIC(5,2) NOT NULL DEFAULT 5,   -- «нижняя граница» маржи = [мин; мин + band]
                                                                   -- в процентных пунктах, раздел 5.7 (A-33)
    usd_rate_adjustment         NUMERIC(6,2) NOT NULL DEFAULT 4,   -- +/- к курсу ЦБ
    commission_vat_multiplier   NUMERIC(5,4) NOT NULL DEFAULT 1,   -- заглушка, открытый вопрос №2
    liquidation_recovery_rate   NUMERIC(4,3) NOT NULL DEFAULT 0.5, -- newsvendor Co, 5.6
    max_step_pct                NUMERIC(5,2),                      -- guardrail на шаг цены за раз (5.7/5.11/6);
                                                                   -- в исходной схеме отсутствовал (A-34).
                                                                   -- NULL = не задан → оптимизация (5.11) не запускается
    return_logistics_per_unit   NUMERIC(10,2) NOT NULL DEFAULT 0   -- стоимость обратной логистики на возврат (A-37)
);

-- Глобальные настройки прогноза (A-28, A-29, A-30, A-31) — версионируемые, как margin_settings
CREATE TABLE forecast_settings (
    setting_id                      SERIAL PRIMARY KEY,
    effective_from                  DATE NOT NULL UNIQUE,
    classification_bucket_days      INT NOT NULL DEFAULT 7,     -- ADI/CV² (SBC) и XYZ считаются по недельным
                                                                -- корзинам, не по дням (A-28)
    xyz_x_max_cv_pct                NUMERIC(6,2) NOT NULL DEFAULT 25,  -- X: CV < 25% (на недельном ряду)
    xyz_y_max_cv_pct                NUMERIC(6,2) NOT NULL DEFAULT 50,  -- Y: 25–50%, Z: > 50%; калибруются по
                                                                       -- фактическому распределению после запуска
    return_censoring_percentile     NUMERIC(4,3) NOT NULL DEFAULT 0.9, -- право-цензурирование: N = этот перцентиль
                                                                       -- lead_return_initiation, не медиана (A-29)
    size_level_min_sales            INT NOT NULL DEFAULT 30,    -- продаж размера за 180 дней, ниже — прогноз на
                                                                -- уровне модели + раскладка по size_profile (A-30)
    cannibalization_fdr_q           NUMERIC(4,3) NOT NULL DEFAULT 0.05 -- уровень FDR (Бенджамини–Хохберг), A-32
);

-- Настройки коррекции прогноза (по категории)
CREATE TABLE forecast_correction_settings (
    category                    TEXT PRIMARY KEY,
    recovery_tail_days          INT NOT NULL DEFAULT 7 CHECK (recovery_tail_days BETWEEN 3 AND 14),
    recovery_tail_coefficient   NUMERIC(3,2) NOT NULL DEFAULT 0.5 CHECK (recovery_tail_coefficient BETWEEN 0.3 AND 0.7),
    outlier_sigma_threshold     NUMERIC(3,1) NOT NULL DEFAULT 3.0  -- фильтр выбросов (5.2/5.5); по категории,
                                                                   -- поэтому убран из «Настроек маржи» раздела 3 (A-35)
);

-- Сезонный индекс по группе паттерна (раздел 5.2). Объединяет две таблицы исходной схемы
-- (seasonal_coefficients + seasonal_index_reference описывали одно и то же, A-38).
-- source: long_history — из многолетней истории продаж в 1С (WB/Ozon/розница, channel_sales source='1c');
--         own — из собственной истории канала после 1-2 циклов (для Lamoda и сайта — новых каналов).
CREATE TABLE seasonal_index_reference (
    seasonal_group      TEXT NOT NULL,
    month_number        INT NOT NULL CHECK (month_number BETWEEN 1 AND 12),
    source              TEXT NOT NULL DEFAULT 'long_history' CHECK (source IN ('long_history', 'own')),
    index_value         NUMERIC(6,4) NOT NULL,          -- нормализовано: среднее за год = 1.0
    source_channels     TEXT,                           -- какие каналы вошли в расчёт
    computed_at         TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (seasonal_group, month_number, source)
);

CREATE TABLE promo_calendar (
    promo_id            SERIAL PRIMARY KEY,
    name                TEXT,                           -- напр. "чёрная пятница"
    date_from           DATE NOT NULL,
    date_to             DATE NOT NULL,
    CHECK (date_to >= date_from)
);


-- ================= 2. ВХОДНЫЕ ПОТОКИ (факты, временные ряды) =================

-- Остаток ЦС: ежедневный снимок products.stock_on_hand из sync-admin (выгрузка 1С stock_*.txt),
-- история — из выгрузок на дату (ondate_*.txt, EXPORT_STOCK_ON_DATE), A-04
CREATE TABLE stock_1c (
    product_size_id     INT NOT NULL REFERENCES product_sizes(product_size_id),
    snapshot_date       DATE NOT NULL,
    qty                 INT NOT NULL,                   -- 0 = stockout ЦС → для FBS-каналов и сайта (5.1)
    PRIMARY KEY (product_size_id, snapshot_date)
);

-- Себестоимость из 1С (A-01): новая команда обмена EXPORT_COST → файл cost_*.txt, отдельный от stock_*.txt
-- (формат остатков не меняем — его разбор в sync-admin фиксирован по числу полей).
-- Хранится в валюте учёта 1С (USD); переоценка в рубли — на нашей стороне по usd_rate на дату расчёта (5.7).
-- Подтверждено: логистика от поставщика, пошлина и КИЗ уже входят в себестоимость 1С — отдельной таблицы
-- доп. составляющих нет (cost_extra_components из первой версии аудита убрана, A-50).
CREATE TABLE cost_1c (
    product_size_id     INT NOT NULL REFERENCES product_sizes(product_size_id),
    snapshot_date       DATE NOT NULL,
    cost_unit           NUMERIC(12,4) NOT NULL,         -- полная себестоимость единицы в валюте currency
    currency            TEXT NOT NULL DEFAULT 'USD' CHECK (currency IN ('USD', 'RUB')),
    cost_kind           TEXT NOT NULL DEFAULT 'average'
                        CHECK (cost_kind IN ('average', 'last_purchase')), -- открытый вопрос №7
    PRIMARY KEY (product_size_id, snapshot_date)
);

CREATE TABLE stock_fbo (
    product_size_id     INT NOT NULL REFERENCES product_sizes(product_size_id),
    warehouse_code      TEXT NOT NULL,
    snapshot_date       DATE NOT NULL,
    qty                 INT NOT NULL,                   -- 0 = флаг stockout (запрос с withZeroQuantity)
    PRIMARY KEY (product_size_id, warehouse_code, snapshot_date)
);

-- Факт созданной поставки на FBO — источник «в пути на FBO» (5.4/5.5)
CREATE TABLE fbo_shipments (
    shipment_id             TEXT NOT NULL,              -- ID поставки Lamoda
    product_size_id         INT NOT NULL REFERENCES product_sizes(product_size_id),
    qty                     INT NOT NULL,
    planned_qty             INT,                        -- исходный план — база для правила штрафа 15% (раздел 6)
    created_date            DATE NOT NULL,
    planned_ship_date       DATE,
    expected_arrival_date   DATE,
    actual_arrival_date     DATE,                       -- по появлению в stock_fbo
    validation_status       TEXT,                       -- из вебхука fulfilmentShipmentValidationRest
    status                  TEXT NOT NULL DEFAULT 'in_transit'
                            CHECK (status IN ('draft', 'in_transit', 'received', 'discrepancy', 'cancelled')),
    is_test                 BOOLEAN NOT NULL DEFAULT FALSE, -- A-42: внешний эффект → граница is_test, как в sync-admin
    PRIMARY KEY (shipment_id, product_size_id)
);

-- Вебхуки Lamoda (раздел 4/7). Идемпотентность — по dedupe_key (A-06), единое правило:
--   заказы:   'order:'  || orderId || ':' || itemId || ':' || status
--   цены:     'price:'  || sku || ':' || тип события || ':' || id модерации
--   поставки: 'ship:'   || shipmentId || ':' || sequenceNumber
-- Вебхук только ускоряет реакцию: событие подтверждается GET-запросом, регулярный опрос остаётся источником истины.
CREATE TABLE webhook_events_log (
    event_id                BIGSERIAL PRIMARY KEY,
    notification_type       TEXT NOT NULL,              -- statusChanged / itemStatusChanged / moderationApproved / ...
    dedupe_key              TEXT NOT NULL UNIQUE,
    order_id                TEXT,                       -- data.id (напр. "CZ117391950") — для GET /v2/orders/{orderId}
    tracking_id             TEXT,                       -- trackingId — только для группировки и resend, не для GET
    sequence_number         BIGINT,
    payload                 JSONB NOT NULL,
    confirmed_via_get       BOOLEAN NOT NULL DEFAULT FALSE,
    gap_resend_requested    BOOLEAN NOT NULL DEFAULT FALSE,
    processed               BOOLEAN NOT NULL DEFAULT FALSE,
    received_at             TIMESTAMP NOT NULL DEFAULT now()
);

-- Сверка расчётного состояния FBO с фактическим снимком stock_fbo (5.4)
CREATE TABLE stock_reconciliation_log (
    reconciliation_id       BIGSERIAL PRIMARY KEY,
    product_size_id         INT NOT NULL REFERENCES product_sizes(product_size_id),
    check_date              DATE NOT NULL,
    expected_qty_computed   NUMERIC(10,2),
    actual_qty_from_snapshot NUMERIC(10,2),
    discrepancy             NUMERIC(10,2),
    resolved                BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE illiquid_flags (
    product_size_id     INT NOT NULL REFERENCES product_sizes(product_size_id),
    snapshot_date       DATE NOT NULL,
    is_illiquid         BOOLEAN NOT NULL,
    PRIMARY KEY (product_size_id, snapshot_date)
);

-- Строки заказов Lamoda из GET /v2/orders — продажи и возвраты Lamoda.
-- Другие каналы — channel_sales ниже (A-10).
CREATE TABLE order_items (
    order_item_id       BIGSERIAL PRIMARY KEY,
    order_id            TEXT NOT NULL,                  -- data.id (напр. "CZ117391950") — не trackingId
    tracking_id         TEXT,
    lamoda_item_id      BIGINT NOT NULL,                -- items[].id; NOT NULL — иначе UNIQUE ниже не защищает
                                                        -- от дублей (NULL ≠ NULL), A-06
    product_size_id     INT REFERENCES product_sizes(product_size_id), -- NULL = баркод не сопоставлен (разбор вручную)
    status              TEXT NOT NULL,                  -- Delivered / Not delivered / Canceled / Returned / ... —
                                                        -- неизвестный статус сохраняется и логируется, не роняет приём
    price_actual        NUMERIC(10,2),                  -- рубли (из копеек при приёме)
    price_base          NUMERIC(10,2),
    total_discount      NUMERIC(10,2),
    event_date          DATE NOT NULL,                  -- дата перехода в этот статус
    ingested_at         TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE (order_id, lamoda_item_id, status)
);

-- Продажи WB / Ozon / собственного сайта / розницы (раздел 4) — и многолетняя история, и текущий поток
-- (в исходной схеме таблицы не было вовсе, A-10). Одна строка = одна позиция продажи или возврата.
-- Источники (A-51):
--   '1c'       — выгрузка продаж из 1С (EXPORT_SALES → sales_*.txt): вся история WB/Ozon/розницы за годы
--                + текущий поток розницы; ключ — uid_1c, строка документа 1С
--   'wb_api'   — отчёт о реализации WB (reportDetailByPeriod, понедельно — достаточно, подтверждено)
--   'ozon_api' — FinanceAPI Ozon
--   'kit_api'  — заказы собственного сайта (KIT API, клиент проверен в работе)
-- Какой источник считается для канала на какую дату — sales_source_config ниже (без двойного счёта).
CREATE TABLE channel_sales (
    channel_sale_id     BIGSERIAL PRIMARY KEY,
    channel             TEXT NOT NULL CHECK (channel IN ('wb', 'ozon', 'own_site', 'retail')),
    source              TEXT NOT NULL CHECK (source IN ('1c', 'wb_api', 'ozon_api', 'kit_api')),
    account_id          INT,                            -- кабинет sync-admin (у WB их может быть несколько)
    store_code          TEXT NOT NULL DEFAULT '',       -- для retail: код магазина; иначе ''
    source_row_id       TEXT NOT NULL,                  -- rrd_id (WB) / operation_id (Ozon) / документ+строка 1С —
                                                        -- идемпотентность повторной загрузки
    uid_1c              VARCHAR(36),                    -- из выгрузки 1С; без FK — в истории есть снятые товары
    barcode             TEXT,                           -- из API площадок; сопоставление через barcode_pool
    product_size_id     INT REFERENCES product_sizes(product_size_id), -- через uid_1c или barcode_pool
    sale_date           DATE NOT NULL,
    qty                 INT NOT NULL,                   -- > 0 продажа, < 0 возврат
    price_realized      NUMERIC(10,2),                  -- цена реализации, рубли
    commission_amount   NUMERIC(10,2),                  -- если канал отдаёт построчно
    logistics_amount    NUMERIC(10,2),
    ingested_at         TIMESTAMP NOT NULL DEFAULT now(),
    CHECK (uid_1c IS NOT NULL OR barcode IS NOT NULL),
    -- NULLS NOT DISTINCT: account_id пуст у строк 1С и розницы — без этого повторная загрузка той же строки
    -- проходит как новая (NULL ≠ NULL), тот же класс ошибки, что A-06
    UNIQUE NULLS NOT DISTINCT (source, channel, account_id, store_code, source_row_id)
);

-- Какой источник продаж канала учитывается в спросе (A-51): до cutover_date — source_before, с неё — source_after.
-- Исключает двойной счёт, когда одна и та же продажа WB есть и в 1С, и в отчёте WB.
-- Стартовая настройка: wb/ozon — '1c' (история и текущий поток), при включении API (фаза 3) задаётся
-- cutover_date и source_after = 'wb_api'/'ozon_api'; retail — всегда '1c'; own_site — 'kit_api'.
CREATE TABLE sales_source_config (
    channel             TEXT PRIMARY KEY CHECK (channel IN ('wb', 'ozon', 'own_site', 'retail')),
    source_before       TEXT NOT NULL CHECK (source_before IN ('1c', 'wb_api', 'ozon_api', 'kit_api')),
    cutover_date        DATE,                           -- NULL = source_after не используется
    source_after        TEXT CHECK (source_after IN ('1c', 'wb_api', 'ozon_api', 'kit_api')),
    CHECK ((cutover_date IS NULL) = (source_after IS NULL))
);

-- Агрегированные дневные продажи по каналам (из order_items и channel_sales)
CREATE TABLE sales_daily (
    product_size_id     INT NOT NULL REFERENCES product_sizes(product_size_id),
    channel             TEXT NOT NULL CHECK (channel IN ('lamoda', 'wb', 'ozon', 'own_site', 'retail')),
    sale_date           DATE NOT NULL,
    qty_delivered       INT NOT NULL DEFAULT 0,
    qty_returned        INT NOT NULL DEFAULT 0,
    revenue             NUMERIC(12,2),
    PRIMARY KEY (product_size_id, channel, sale_date)   -- channel добавлен (A-10)
);

-- ⚠️ НЕ АВТОМАТИЗИРОВАНО: «Воронка продаж» Lamoda — только Excel из ЛК, API нет. Опционально, ручной разбор.
CREATE TABLE funnel_model (
    product_model_id    INT NOT NULL REFERENCES product_models(product_model_id),
    period_start        DATE NOT NULL,
    period_end          DATE NOT NULL,
    views               INT,
    cart_conversion_pct NUMERIC(6,3),
    cart_adds           INT,
    order_share_pct     NUMERIC(6,3),
    orders_qty          INT,
    purchased_qty       INT,
    purchase_rate_pct   NUMERIC(6,3),
    PRIMARY KEY (product_model_id, period_start, period_end)
);

-- ⚠️ НЕ АВТОМАТИЗИРОВАНО (та же причина). Возвратность берётся из order_items.
CREATE TABLE funnel_size (
    product_size_id     INT NOT NULL REFERENCES product_sizes(product_size_id),
    period_start        DATE NOT NULL,
    period_end          DATE NOT NULL,
    orders_qty          INT,
    purchased_qty       INT,
    purchase_rate_pct   NUMERIC(6,3),
    returns_qty         INT,
    return_rate_pct     NUMERIC(6,3),
    PRIMARY KEY (product_size_id, period_start, period_end)
);

-- Размерный профиль — собственный расчёт из истории заказов
CREATE TABLE size_profile (
    category            TEXT NOT NULL,
    gender              TEXT NOT NULL DEFAULT 'unisex', -- NOT NULL: колонка в PK, NULL в PK запрещён (A-02)
    size_label          TEXT NOT NULL,
    share_of_sales_pct  NUMERIC(6,3) NOT NULL,
    report_date         DATE NOT NULL,
    PRIMARY KEY (category, gender, size_label, report_date)
);

-- external_channel_sales_history (помесячная разовая выгрузка истории из API WB/Ozon) убрана (A-51):
-- многолетняя история продаж есть в 1С и загружается в channel_sales (source = '1c') по дням;
-- помесячные ряды для сезонного индекса — агрегат запросом.

-- Коэффициент пересчёта продаж канала-источника с долгой историей в новый канал (5.6). Новых каналов два —
-- Lamoda и собственный сайт (A-52); источники с историей в 1С — wb / ozon / retail.
CREATE TABLE channel_ratio_calibration (
    target_channel      TEXT NOT NULL DEFAULT 'lamoda' CHECK (target_channel IN ('lamoda', 'own_site')),
    channel             TEXT NOT NULL CHECK (channel IN ('wb', 'ozon', 'retail')), -- канал-источник
    category            TEXT NOT NULL,
    ratio               NUMERIC(8,4) NOT NULL,          -- среднее(продажи_нового_канала / продажи_источника)
    sample_size         INT,
    computed_at         TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (target_channel, channel, category)
);

-- Недооценённый потенциал (5.6) — ожидание по истории канала-источника из channel_sales
CREATE TABLE underperformance_flags (
    product_model_id                INT NOT NULL REFERENCES product_models(product_model_id),
    period_start                    DATE NOT NULL,
    period_end                      DATE NOT NULL,
    source_channel                  TEXT NOT NULL CHECK (source_channel IN ('wb', 'ozon', 'retail')),
    expected_sales_qty              NUMERIC(10,2),
    actual_sales_qty                NUMERIC(10,2),
    shortfall_pct                   NUMERIC(8,2),       -- факт / ожидание × 100
    in_stock_full_period            BOOLEAN,
    attributes_incomplete           BOOLEAN,
    popular_sizes_out_of_stock      BOOLEAN,
    price_gap_vs_category_pct       NUMERIC(8,2),       -- NULL, если нет подписки на category_market_trends
    price_gap_vs_source_channel_pct NUMERIC(8,2),
    card_status_issue               BOOLEAN,
    lamoda_promo_in_category_not_joined BOOLEAN,        -- было competitors_in_promo_we_are_not: API /v2/promotions
                                                        -- показывает акции Lamoda, доступные НАМ, а не участие
                                                        -- конкурентов — проверяем «в категории идёт акция, мы не в ней» (A-27)
    flagged                         BOOLEAN NOT NULL DEFAULT FALSE,
    reviewed_at                     TIMESTAMP,
    review_outcome                  TEXT CHECK (review_outcome IN ('listing_issue_fixed', 'objectively_lower_demand')),
    created_at                      TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (product_model_id, period_start, period_end, source_channel)
);

-- Норматив распределения по цвету (5.6)
CREATE TABLE color_profile (
    category            TEXT NOT NULL,
    color_group         TEXT NOT NULL,
    share_of_sales_pct  NUMERIC(6,3) NOT NULL,
    report_date         DATE NOT NULL,
    PRIMARY KEY (category, color_group, report_date)
);

-- Перетекание спроса при отсутствии соседнего цвета/размера (5.3). Доступность A — бинарная (0/1), поэтому
-- ln(доступность) не определён; модель полулогарифмическая (A-25):
--   ln(спрос_B(t)) = a + β · stockout_A(t) + контроль(сезон, акции)
--   эффект_перетекания = e^β − 1  (доля прироста спроса B, пока A нет в наличии)
CREATE TABLE cross_elasticity (
    product_size_id_a       INT NOT NULL REFERENCES product_sizes(product_size_id), -- чья доступность
    product_size_id_b       INT NOT NULL REFERENCES product_sizes(product_size_id), -- чей спрос затронут
    beta_coefficient        NUMERIC(8,4) NOT NULL,      -- β из полулогарифмической регрессии
    uplift_share            NUMERIC(8,4),               -- e^β − 1
    sample_size             INT,                        -- число дней/эпизодов stockout A
    computed_at             TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (product_size_id_a, product_size_id_b)
);

-- Промо-каннибализация между РАЗНЫМИ артикулами (5.3) — t-тест с поправкой на множественные сравнения
CREATE TABLE promo_cannibalization_pairs (
    product_model_id_a      INT NOT NULL REFERENCES product_models(product_model_id), -- теряет продажи
    product_model_id_b      INT NOT NULL REFERENCES product_models(product_model_id), -- в акции
    mean_sales_a_no_promo_b NUMERIC(10,2),              -- Snp
    mean_sales_a_promo_b    NUMERIC(10,2),              -- Sp
    demand_shift_d          NUMERIC(10,2),              -- Snp - Sp
    p_value                 NUMERIC(8,7),
    p_value_adjusted        NUMERIC(8,7),               -- Бенджамини–Хохберг по всем парам категории (A-32)
    significant             BOOLEAN,                    -- p_value_adjusted < cannibalization_fdr_q и D > 0
    computed_at             TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (product_model_id_a, product_model_id_b)
);

-- События перетекания спроса (5.3) — количество из cross_elasticity.uplift_share
CREATE TABLE cannibalization_events (
    event_id                        BIGSERIAL PRIMARY KEY,
    product_size_id_out_of_stock    INT NOT NULL REFERENCES product_sizes(product_size_id),
    product_size_id_gained_demand   INT NOT NULL REFERENCES product_sizes(product_size_id),
    event_date                      DATE NOT NULL,
    demand_shift_qty                NUMERIC(10,2),
    detected_at                     TIMESTAMP NOT NULL DEFAULT now()
);

-- Источник: GET /v2/promotions (раздел 4). Точное соответствие полей не подтверждено (открытый вопрос №1)
CREATE TABLE promo_report_daily (
    report_date                 DATE PRIMARY KEY,
    orders_sum_promo            NUMERIC(12,2),
    orders_sum_all              NUMERIC(12,2),
    orders_share_promo_pct      NUMERIC(6,2),
    redemption_sum_promo        NUMERIC(12,2),
    redemption_sum_all          NUMERIC(12,2),
    redemption_share_promo_pct  NUMERIC(6,2)
);

-- Источник: GET /v2/promotions/{promotionId}/products
CREATE TABLE promo_report_sku (
    product_size_id                     INT NOT NULL REFERENCES product_sizes(product_size_id),
    promotion_id                        TEXT NOT NULL DEFAULT '',
    period_start                        DATE NOT NULL,
    period_end                          DATE NOT NULL,
    orders_sum_promo                    NUMERIC(12,2),
    orders_qty_promo                    INT,
    redemption_sum_promo                NUMERIC(12,2),
    redemption_qty_promo                INT,
    redemption_rate_pct                 NUMERIC(6,3),
    avg_price_seller_and_lamoda         NUMERIC(10,2),
    avg_discount_seller_and_lamoda_pct  NUMERIC(6,3),
    avg_price_seller                    NUMERIC(10,2),  -- цена, реально влияющая на маржу (если API отдаёт)
    avg_discount_seller_pct             NUMERIC(6,3),
    PRIMARY KEY (product_size_id, promotion_id, period_start, period_end)
);

CREATE TABLE wordstat_data (
    keyword             TEXT NOT NULL,
    stat_date           DATE NOT NULL,
    region              TEXT NOT NULL DEFAULT '',       -- '' = вся Россия; NOT NULL — колонка в PK (A-02)
    frequency           INT,
    PRIMARY KEY (keyword, stat_date, region)
);

CREATE TABLE google_trends_data (
    keyword             TEXT NOT NULL,
    trend_date          DATE NOT NULL,
    region              TEXT NOT NULL DEFAULT '',       -- '' = вся страна (geo='RU'); NOT NULL — в PK (A-02)
    popularity_index    NUMERIC(5,2),                   -- 0-100
    PRIMARY KEY (keyword, trend_date, region)
);

-- Какой товар рекламируется объявлением Директа (5.3) — заполняется при создании объявления
CREATE TABLE yandex_direct_ad_mapping (
    ad_id               TEXT PRIMARY KEY,
    campaign_id         TEXT,
    product_model_id    INT NOT NULL REFERENCES product_models(product_model_id),
    created_at          TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE yandex_direct_clicks (
    ad_id               TEXT NOT NULL REFERENCES yandex_direct_ad_mapping(ad_id),
    click_date          DATE NOT NULL,
    clicks              INT,
    impressions         INT,
    ctr_pct             NUMERIC(6,3),
    spend               NUMERIC(10,2),
    PRIMARY KEY (ad_id, click_date)
);

CREATE TABLE metrika_audience_segments (
    product_model_id    INT NOT NULL REFERENCES product_models(product_model_id),
    segment_date        DATE NOT NULL,
    segment_type        TEXT NOT NULL CHECK (segment_type IN ('gender', 'age', 'device', 'geo')),
    segment_value       TEXT NOT NULL,
    share_of_clicks_pct NUMERIC(6,3) NOT NULL,
    PRIMARY KEY (product_model_id, segment_date, segment_type, segment_value)
);

-- Эталонный профиль покупателей (5.3) — из CRM AWER
CREATE TABLE buyer_profile_reference (
    segment_type        TEXT NOT NULL CHECK (segment_type IN ('gender', 'age', 'device', 'geo')),
    segment_value       TEXT NOT NULL,
    share_of_buyers_pct NUMERIC(6,3) NOT NULL,
    computed_at         TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (segment_type, segment_value)
);

CREATE TABLE audience_quality_score (
    product_model_id    INT NOT NULL REFERENCES product_models(product_model_id),
    score_date          DATE NOT NULL,
    quality_score       NUMERIC(4,3) NOT NULL CHECK (quality_score BETWEEN 0 AND 1),
    computed_at         TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (product_model_id, score_date)
);

-- Погода: Open-Meteo (архив + прогноз, без ключа)
CREATE TABLE weather_data (
    region              TEXT NOT NULL,
    weather_date        DATE NOT NULL,
    avg_temp_c          NUMERIC(4,1),
    is_forecast         BOOLEAN NOT NULL DEFAULT FALSE, -- прогноз перезаписывается фактом из архива
    PRIMARY KEY (region, weather_date)
);

-- Конкурентная аналитика по категориям (5.6) — только при платной подписке MPSTATS/EggHeads
CREATE TABLE category_market_trends (
    category            TEXT NOT NULL,
    trend_date          DATE NOT NULL,
    market_growth_index NUMERIC(6,3),
    competitor_count    INT,
    avg_category_price  NUMERIC(10,2),
    source              TEXT,                           -- 'mpstats' / 'eggheads' / 'manual'
    PRIMARY KEY (category, trend_date)
);

CREATE TABLE usd_rate (
    rate_date           DATE PRIMARY KEY,
    cbr_rate            NUMERIC(8,4) NOT NULL,
    adjusted_rate       NUMERIC(8,4) NOT NULL           -- cbr_rate + margin_settings.usd_rate_adjustment
);


-- ================= 3. РАСЧЁТНЫЕ СУЩНОСТИ =================

-- Спрос по каналам (5.1). Для lamoda stockout — по stock_fbo; для wb/ozon/own_site/retail — по stock_1c
-- (они делят один физический остаток ЦС, A-11).
CREATE TABLE demand_timeseries (
    product_size_id             INT NOT NULL REFERENCES product_sizes(product_size_id),
    channel                     TEXT NOT NULL CHECK (channel IN ('lamoda', 'wb', 'ozon', 'own_site', 'retail')),
    demand_date                 DATE NOT NULL,
    net_demand                  NUMERIC(10,2) NOT NULL, -- Delivered − возвраты_с_лагом
    base_demand                 NUMERIC(10,2),          -- контрфактический спрос без промо (5.3)
    promo_uplift                NUMERIC(10,2),
    is_stockout_corrected       BOOLEAN NOT NULL DEFAULT FALSE,
    is_return_censored          BOOLEAN NOT NULL DEFAULT FALSE,
    is_recovery_tail_corrected  BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (product_size_id, channel, demand_date) -- channel добавлен (A-10)
);

CREATE TABLE abc_xyz_classification (
    product_model_id        INT NOT NULL REFERENCES product_models(product_model_id),
    period_start            DATE NOT NULL,
    combined_score          NUMERIC(10,4),
    abc_class               TEXT NOT NULL CHECK (abc_class IN ('A', 'B', 'C', 'fading')),
    coefficient_variation   NUMERIC(10,3),              -- CV, %, на недельном ряду; было NUMERIC(6,3) —
                                                        -- переполнение при CV > 999% (A-07)
    xyz_class               TEXT CHECK (xyz_class IN ('X', 'Y', 'Z')), -- NULL для fading
    combined_segment        TEXT NOT NULL,              -- AX / AY / ... / CZ / fading
    service_level_z         NUMERIC(4,3),               -- NULL для CZ и fading (автопополнение не считается)
    review_frequency        TEXT,
    review_interval_days    INT,                        -- для L в 5.5
    PRIMARY KEY (product_model_id, period_start)
);

-- Сроки (5.4). Возврат разделён на два разных срока (A-29):
--   return_initiation — от доставки покупателю до статуса Returned (окно право-цензурирования, 5.1)
--   return_restock    — от статуса Returned до появления на остатке FBO («в пути от клиента», 5.5)
CREATE TABLE lead_time_stats (
    scope_type          TEXT NOT NULL CHECK (scope_type IN ('model', 'category', 'supplier')),
    scope_id            TEXT NOT NULL,
    lead_type           TEXT NOT NULL CHECK (lead_type IN ('fbo_delivery', 'return_initiation', 'return_restock', 'supplier')),
    median_days         NUMERIC(6,2) NOT NULL,
    p90_days            NUMERIC(6,2),                   -- для цензурирования (5.1) и скошенных сроков (5.5)
    p95_days            NUMERIC(6,2),
    std_days            NUMERIC(6,2),
    computed_at         TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_type, scope_id, lead_type)
);

CREATE TABLE demand_forecast (
    product_size_id     INT NOT NULL REFERENCES product_sizes(product_size_id),
    channel             TEXT NOT NULL DEFAULT 'lamoda'
                        CHECK (channel IN ('lamoda', 'wb', 'ozon', 'own_site', 'retail')), -- retail добавлен (A-13)
    period_start        DATE NOT NULL,
    period_end          DATE NOT NULL,
    forecast_qty        NUMERIC(10,2) NOT NULL,
    lower_bound         NUMERIC(10,2),
    upper_bound         NUMERIC(10,2),
    forecast_level      TEXT NOT NULL DEFAULT 'size'
                        CHECK (forecast_level IN ('size', 'model_topdown')), -- model_topdown = прогноз модели ×
                                                                             -- доля размера из size_profile (A-30)
    model_type          TEXT CHECK (model_type IN ('smoothing', 'tsb', 'croston_sba', 'seasonal_naive',
                                                   'analog_based', 'channel_ratio', 'gradient_boosting', 'deepar')),
    adi_days            NUMERIC(8,2),
    cv_squared          NUMERIC(10,3),                  -- было (6,3), A-07
    demand_pattern      TEXT CHECK (demand_pattern IN ('smooth', 'irregular', 'intermittent', 'lumpy')),
    computed_at         TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (product_size_id, channel, period_start, period_end)
);

-- Суммарный спрос на ЦС по всем каналам (5.5/6) — источник для закупки у поставщика.
CREATE TABLE central_stock_aggregate_demand (
    product_size_id     INT NOT NULL REFERENCES product_sizes(product_size_id),
    period_start        DATE NOT NULL,
    period_end          DATE NOT NULL,
    total_forecast_qty  NUMERIC(10,2) NOT NULL,         -- Σ demand_forecast.forecast_qty по всем каналам
    fbs_forecast_qty    NUMERIC(10,2) NOT NULL,         -- wb+ozon+own_site+retail — тянут напрямую с ЦС
    lamoda_forecast_qty NUMERIC(10,2) NOT NULL,         -- обслуживается через FBO; в закупке уменьшается
                                                        -- на остаток FBO + в пути на FBO (A-12)
    channel_breakdown   JSONB,                          -- {"lamoda": X, "wb": Y, ...}
    computed_at         TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (product_size_id, period_start, period_end)
);

-- Точка заказа (5.5): scope различает FBO Lamoda и ЦС (A-12)
CREATE TABLE reorder_points (
    product_size_id     INT NOT NULL REFERENCES product_sizes(product_size_id),
    scope               TEXT NOT NULL CHECK (scope IN ('fbo_lamoda', 'central_stock')),
    computed_at         TIMESTAMP NOT NULL DEFAULT now(),
    lead_time_used_days NUMERIC(6,2),                   -- L = срок + интервал пересмотра
    sigma_used          NUMERIC(10,3),
    formula_variant     TEXT NOT NULL DEFAULT 'basic'
                        CHECK (formula_variant IN ('basic', 'lead_time_variance', 'intermittent_window')),
    safety_stock        NUMERIC(10,2),
    reorder_point_qty   NUMERIC(10,2) NOT NULL,         -- от forecast_overrides.override_qty, если активен
    order_up_to_qty     NUMERIC(10,2),                  -- целевой уровень S = точка заказа + спрос за интервал
                                                        -- пересмотра — до него пополняем (A-14)
    PRIMARY KEY (product_size_id, scope, computed_at)
);

CREATE TABLE new_item_forecast (
    product_model_id                INT NOT NULL REFERENCES product_models(product_model_id),
    computed_at                     TIMESTAMP NOT NULL DEFAULT now(),
    method                          TEXT NOT NULL CHECK (method IN ('channel_ratio', 'new_color', 'analog_based', 'deepar')),
    analog_based_forecast_qty       NUMERIC(10,2),
    category_correction_median      NUMERIC(6,4),
    category_correction_std         NUMERIC(6,4),
    search_interest_coef            NUMERIC(6,4),       -- было wordstat_coef — Вордстат + Google Trends (5.3)
    final_forecast_qty              NUMERIC(10,2),      -- агрегат по модели, на период охвата
    deepar_forecast_qty             NUMERIC(10,2),
    coverage_period_days            INT,
    underage_cost_cu                NUMERIC(10,2),
    overage_cost_co                 NUMERIC(10,2),
    critical_fractile               NUMERIC(5,4),
    z_newsvendor                    NUMERIC(5,3),
    recommended_first_shipment_qty  NUMERIC(10,2),
    moq_adjusted_qty                NUMERIC(10,2),
    risk_flag                       BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (product_model_id, computed_at)
);

-- Раскладка новинки по размерам через size_profile. Два РАЗНЫХ числа (A-15):
--   forecast_qty_allocated — прогноз (final_forecast_qty × доля) — идёт в forecast_accuracy_log (5.8)
--   allocated_qty          — объём первой поставки (moq_adjusted_qty × доля) — идёт в закупку
CREATE TABLE new_item_size_allocation (
    product_model_id        INT NOT NULL,
    computed_at             TIMESTAMP NOT NULL,
    product_size_id         INT NOT NULL REFERENCES product_sizes(product_size_id),
    forecast_qty_allocated  NUMERIC(10,2) NOT NULL,
    allocated_qty           NUMERIC(10,2) NOT NULL,
    FOREIGN KEY (product_model_id, computed_at) REFERENCES new_item_forecast(product_model_id, computed_at),
    PRIMARY KEY (product_model_id, computed_at, product_size_id)
);

-- Ежемесячная сверка с отчётом комиссионера Lamoda (ЭДО, ручной ввод)
CREATE TABLE lamoda_official_reconciliation (
    period_month                        DATE PRIMARY KEY CHECK (EXTRACT(DAY FROM period_month) = 1),
    official_price_realized_incl_vat    NUMERIC(14,2),
    our_computed_sum                    NUMERIC(14,2),
    discrepancy_pct                     NUMERIC(8,2),
    reconciled_at                       TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE margin_calc (
    product_size_id         INT NOT NULL REFERENCES product_sizes(product_size_id),
    calc_date               DATE NOT NULL,
    channel                 TEXT NOT NULL DEFAULT 'lamoda' CHECK (channel IN ('lamoda', 'wb', 'ozon')),
    price_realized          NUMERIC(10,2) NOT NULL,     -- Lamoda: цена за счёт продавца (promo_report_sku) либо
                                                        -- price_actual из order_items; WB/Ozon — из channel_sales
    cost_unit               NUMERIC(10,2) NOT NULL,     -- рубли: cost_1c × usd_rate.adjusted_rate на calc_date
                                                        -- (доп. расходы уже в себестоимости 1С), A-01/A-50
    usd_rate_used           NUMERIC(8,4),               -- курс, по которому пересчитана себестоимость
    commission_amount       NUMERIC(10,2),
    logistics_fbo_amount    NUMERIC(10,2),
    returns_cost_amount     NUMERIC(10,2),              -- (1/доля_выкупа − 1) × return_logistics_per_unit (A-37)
    vat_amount              NUMERIC(10,2),
    usn_tax_amount          NUMERIC(10,2),
    margin_abs              NUMERIC(10,2),
    margin_pct              NUMERIC(8,2),
    deviation_reason        TEXT CHECK (deviation_reason IN ('rate', 'demand', 'commission')), -- курс/спрос/комиссия
    PRIMARY KEY (product_size_id, calc_date, channel)
);

-- Комиссия/логистика по каналу и категории. Lamoda добавлена (A-36): у Lamoda ставка тоже зависит от категории,
-- margin_settings.lamoda_commission_rate — только значение по умолчанию.
CREATE TABLE channel_commission_rates (
    channel             TEXT NOT NULL CHECK (channel IN ('lamoda', 'wb', 'ozon')),
    category            TEXT NOT NULL,
    commission_pct      NUMERIC(6,3),
    logistics_fee       NUMERIC(10,2),
    effective_from      DATE NOT NULL,
    effective_to        DATE,                           -- null = текущая ставка
    PRIMARY KEY (channel, category, effective_from)
);

CREATE TABLE anomaly_triage_log (
    triage_id           BIGSERIAL PRIMARY KEY,
    product_size_id     INT REFERENCES product_sizes(product_size_id),
    period_start        DATE,
    period_end          DATE,
    signals             JSONB,
    probable_cause      TEXT,
    recommended_action  TEXT,
    confidence          NUMERIC(4,3),
    manager_decision    TEXT CHECK (manager_decision IN ('confirmed', 'rejected')),
    decided_at          TIMESTAMP
);

-- Точность прогноза (5.8). Статус определяется по WAPE, не MAPE (A-08): при прерывистом спросе факт часто 0,
-- MAPE не определён или уходит в тысячи процентов.
CREATE TABLE forecast_accuracy_log (
    product_size_id     INT NOT NULL REFERENCES product_sizes(product_size_id),
    channel             TEXT NOT NULL CHECK (channel IN ('lamoda', 'wb', 'ozon', 'own_site', 'retail')),
    period_start        DATE NOT NULL,
    period_end          DATE NOT NULL,
    forecast_qty        NUMERIC(10,2),
    actual_qty          NUMERIC(10,2),
    mape                NUMERIC(10,3),                  -- справочно; NULL, если в периоде есть дни с фактом 0
    wape                NUMERIC(10,3),                  -- было (6,3) — переполнение (A-07)
    mae                 NUMERIC(10,2),
    rmse                NUMERIC(10,2),
    avg_deficit         NUMERIC(10,2),
    avg_surplus         NUMERIC(10,2),
    deviation_status    TEXT NOT NULL DEFAULT 'normal' CHECK (deviation_status IN ('normal', 'warning', 'critical')),
    consecutive_warning_count INT NOT NULL DEFAULT 0,
    computed_at         TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (product_size_id, channel, period_start, period_end)
);

-- Пороги эскалации (5.8) — по WAPE (A-08)
CREATE TABLE forecast_deviation_settings (
    category                    TEXT PRIMARY KEY,
    wape_warning_threshold      NUMERIC(6,2) NOT NULL DEFAULT 30,  -- %
    wape_critical_threshold     NUMERIC(6,2) NOT NULL DEFAULT 60,  -- %
    consecutive_periods_trigger INT NOT NULL DEFAULT 3
);

CREATE TABLE model_recalibration_log (
    recalibration_id    BIGSERIAL PRIMARY KEY,
    product_size_id     INT NOT NULL REFERENCES product_sizes(product_size_id),
    channel             TEXT NOT NULL CHECK (channel IN ('lamoda', 'wb', 'ozon', 'own_site', 'retail')),
    triggered_at        TIMESTAMP NOT NULL DEFAULT now(),
    reason              TEXT,
    old_model_type      TEXT,
    new_model_type      TEXT,
    triggered_by        TEXT NOT NULL DEFAULT 'auto' CHECK (triggered_by IN ('auto', 'manual', 'backtest'))
);

CREATE TABLE model_backtest_results (
    backtest_id         BIGSERIAL PRIMARY KEY,
    product_size_id     INT NOT NULL REFERENCES product_sizes(product_size_id),
    channel             TEXT NOT NULL CHECK (channel IN ('lamoda', 'wb', 'ozon', 'own_site', 'retail')),
    backtest_date       DATE NOT NULL,
    model_type          TEXT NOT NULL,
    wape_holdout        NUMERIC(10,3) NOT NULL,
    selected            BOOLEAN NOT NULL DEFAULT FALSE
);

-- Степень достоверности рекомендации (5.8). scope: fbo_lamoda — для страницы поставки, central_stock — для закупки
CREATE TABLE recommendation_confidence (
    product_size_id             INT NOT NULL REFERENCES product_sizes(product_size_id),
    scope                       TEXT NOT NULL CHECK (scope IN ('fbo_lamoda', 'central_stock')),
    computed_at                 TIMESTAMP NOT NULL DEFAULT now(),
    wape_penalty                NUMERIC(5,2),
    demand_pattern_penalty      NUMERIC(5,2),
    short_history_penalty       NUMERIC(5,2),
    model_disagreement_penalty  NUMERIC(5,2),           -- 0, пока DeepAR не внедрён (фаза 4)
    recent_deviation_penalty    NUMERIC(5,2),
    stockout_share_penalty      NUMERIC(5,2),
    confidence_pct              NUMERIC(5,2) NOT NULL CHECK (confidence_pct BETWEEN 0 AND 100),
    explanation_text            TEXT,
    PRIMARY KEY (product_size_id, scope, computed_at)
);

CREATE TABLE fill_rate_log (
    product_size_id         INT NOT NULL REFERENCES product_sizes(product_size_id),
    channel                 TEXT NOT NULL CHECK (channel IN ('lamoda', 'wb', 'ozon', 'own_site', 'retail')),
    period_start            DATE NOT NULL,
    period_end              DATE NOT NULL,
    demand_qty_total        NUMERIC(10,2),
    demand_qty_fulfilled    NUMERIC(10,2),
    fill_rate_pct           NUMERIC(6,3),
    computed_at             TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (product_size_id, channel, period_start, period_end)
);

-- Заказы поставщику (5.5/5.9). Заменяют supplier_deliveries (A-16): там не было ни товара, ни количества —
-- нельзя было посчитать «в пути от поставщика» для закупки и надёжность по товарам.
CREATE TABLE purchase_orders (
    purchase_order_id   BIGSERIAL PRIMARY KEY,
    supplier_id         INT NOT NULL REFERENCES suppliers(supplier_id),
    ordered_date        DATE NOT NULL,
    expected_ready_date DATE NOT NULL,                  -- по номинальному сроку из справочника
    actual_ready_date   DATE,
    received_date       DATE,                           -- приход на ЦС (по 1С)
    on_time             BOOLEAN GENERATED ALWAYS AS (actual_ready_date <= expected_ready_date) STORED,
    status              TEXT NOT NULL DEFAULT 'ordered' CHECK (status IN ('ordered', 'ready', 'received', 'cancelled'))
);

CREATE TABLE purchase_order_lines (
    purchase_order_id   BIGINT NOT NULL REFERENCES purchase_orders(purchase_order_id),
    product_size_id     INT NOT NULL REFERENCES product_sizes(product_size_id),
    qty_ordered         INT NOT NULL,
    qty_received        INT,
    recommendation_id   BIGINT,                         -- из purchase_recommendations, если заказ по рекомендации
    PRIMARY KEY (purchase_order_id, product_size_id)
);

-- Ценовая эластичность (5.10) — по модели, fallback на категорию
CREATE TABLE price_elasticity (
    scope_type              TEXT NOT NULL CHECK (scope_type IN ('model', 'category')),
    scope_id                TEXT NOT NULL,
    channel                 TEXT NOT NULL DEFAULT 'lamoda' CHECK (channel IN ('lamoda', 'wb', 'ozon')),
    method                  TEXT NOT NULL DEFAULT 'log_log_regression'
                            CHECK (method IN ('log_log_regression', 'gradient_boosting_monotone')),
    elasticity_coefficient  NUMERIC(8,4) NOT NULL,
    r_squared               NUMERIC(4,3),
    sample_size             INT,
    computed_at             TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_type, scope_id, channel)
);

-- Все ценовые таблицы ниже — на уровне product_model (A-24): цена на Lamoda задаётся на parent SKU,
-- на WB — на карточку (nmID); рекомендация «на размер» не может быть применена.
CREATE TABLE price_scenario_log (
    scenario_id                 BIGSERIAL PRIMARY KEY,
    product_model_id            INT NOT NULL REFERENCES product_models(product_model_id),
    channel                     TEXT NOT NULL DEFAULT 'lamoda' CHECK (channel IN ('lamoda', 'wb', 'ozon')),
    price_change_pct            NUMERIC(6,2) NOT NULL,
    predicted_demand_change_pct NUMERIC(8,2),
    predicted_margin_impact     NUMERIC(12,2),
    applied                     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at                  TIMESTAMP NOT NULL DEFAULT now()
);

-- A/B-тест повышения цены (5.10) — difference-in-differences
CREATE TABLE price_ab_test (
    test_id                     BIGSERIAL PRIMARY KEY,
    treatment_product_model_id  INT NOT NULL REFERENCES product_models(product_model_id),
    control_product_model_ids   INT[] NOT NULL,
    scenario_id                 BIGINT REFERENCES price_scenario_log(scenario_id),
    test_start                  DATE NOT NULL,
    test_end                    DATE NOT NULL,
    demand_change_pct_treatment NUMERIC(8,2),
    demand_change_pct_control   NUMERIC(8,2),
    diff_in_diff_effect         NUMERIC(8,2),
    decision                    TEXT CHECK (decision IN ('rollback', 'keep', 'extend')),
    created_at                  TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE price_optimization_run (
    run_id                  BIGSERIAL PRIMARY KEY,
    run_date                DATE NOT NULL,
    scope                   TEXT,
    objective_value_before  NUMERIC(14,2),
    objective_value_after   NUMERIC(14,2),
    status                  TEXT NOT NULL DEFAULT 'completed' CHECK (status IN ('completed', 'infeasible', 'error')),
    created_at              TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE price_optimization_result (
    run_id                      BIGINT NOT NULL REFERENCES price_optimization_run(run_id),
    product_model_id            INT NOT NULL REFERENCES product_models(product_model_id),
    current_price               NUMERIC(10,2),
    recommended_price           NUMERIC(10,2),
    price_change_pct            NUMERIC(6,2),
    predicted_margin_impact     NUMERIC(12,2),
    predicted_demand_impact_pct NUMERIC(8,2),
    stock_check_passed          BOOLEAN NOT NULL DEFAULT TRUE,
    included_in_recommendations BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (run_id, product_model_id)
);

-- Ручные корректировки прогноза (раздел 6) — расчётное значение не перезаписывается
CREATE TABLE forecast_overrides (
    override_id             BIGSERIAL PRIMARY KEY,
    product_size_id         INT NOT NULL REFERENCES product_sizes(product_size_id),
    channel                 TEXT NOT NULL DEFAULT 'lamoda' CHECK (channel IN ('lamoda', 'wb', 'ozon', 'own_site', 'retail')),
    period_start            DATE NOT NULL,
    period_end              DATE NOT NULL,
    original_forecast_qty   NUMERIC(10,2),
    override_qty            NUMERIC(10,2) NOT NULL,
    reason                  TEXT NOT NULL,
    overridden_by           TEXT NOT NULL,
    created_at              TIMESTAMP NOT NULL DEFAULT now()
);


-- ================= 4. ВЫХОДНЫЕ РЕКОМЕНДАЦИИ =================

-- Поставка с ЦС на FBO Lamoda (раздел 6). Формула (A-14):
--   fbo_need_qty    = max(0, order_up_to_qty(fbo_lamoda) − прогноз_остатка_FBO(T))
--   cs_protected_qty = прогноз FBS-спроса (wb+ozon+own_site+retail) на срок пополнения ЦС + страховой запас ЦС
--   qty_to_ship     = min(fbo_need_qty, max(0, stock_1c − cs_protected_qty))
CREATE TABLE shipment_recommendations (
    recommendation_id       BIGSERIAL PRIMARY KEY,
    product_size_id         INT NOT NULL REFERENCES product_sizes(product_size_id),
    stock_1c_qty_snapshot   NUMERIC(10,2),
    stock_fbo_qty_snapshot  NUMERIC(10,2),
    fbo_need_qty            NUMERIC(10,2),
    cs_protected_qty        NUMERIC(10,2),              -- сколько остаётся на ЦС под WB/Ozon/сайт/розницу
    qty_to_ship             NUMERIC(10,2) NOT NULL,
    confidence_pct          NUMERIC(5,2),
    confidence_explanation  TEXT,
    confirmed_qty           NUMERIC(10,2),              -- «подтверждено к поставке», пусто по умолчанию
    related_fbo_shipment_id TEXT,
    correction_pct_vs_original NUMERIC(8,2),
    penalty_risk            BOOLEAN NOT NULL DEFAULT FALSE,
    target_ship_date        DATE,
    status                  TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'confirmed', 'sent_to_edo', 'shipped', 'superseded')),
    is_test                 BOOLEAN NOT NULL DEFAULT FALSE, -- A-42
    created_at              TIMESTAMP NOT NULL DEFAULT now(),
    CHECK ((confirmed_qty IS NULL) = (status = 'pending') OR status = 'superseded')
);

-- Закупка у поставщика (раздел 6). Формула (A-12):
--   чистая_потребность = order_up_to_qty(central_stock) по fbs_forecast_qty
--                        + max(0, lamoda_forecast_qty − остаток_FBO − в_пути_на_FBO)
--                        − stock_1c − в_пути_от_поставщика (purchase_order_lines без received)
--   qty_to_purchase    = округление вверх до MOQ, если чистая_потребность > 0 и available_to_reorder
CREATE TABLE purchase_recommendations (
    recommendation_id       BIGSERIAL PRIMARY KEY,
    product_size_id         INT NOT NULL REFERENCES product_sizes(product_size_id),
    supplier_id             INT REFERENCES suppliers(supplier_id),
    net_requirement_qty     NUMERIC(10,2) NOT NULL,
    qty_to_purchase         NUMERIC(10,2) NOT NULL,     -- после округления до MOQ
    moq_forced              BOOLEAN NOT NULL DEFAULT FALSE, -- MOQ заметно выше потребности — сигнал риска
    ready_by_date           DATE,
    confidence_pct          NUMERIC(5,2),
    confidence_explanation  TEXT,
    status                  TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'ordered', 'received', 'rejected', 'superseded')),
    created_at              TIMESTAMP NOT NULL DEFAULT now()
);
-- Для SKU с available_to_reorder = false строка не создаётся — вместо неё предупреждение «идёт на исчерпание» (5.5)

-- Ценовые рекомендации (5.7/5.11) — на уровне модели (A-24). Единый словарь статусов (A-39).
CREATE TABLE pricing_recommendations (
    recommendation_id       BIGSERIAL PRIMARY KEY,
    product_model_id        INT NOT NULL REFERENCES product_models(product_model_id),
    channel                 TEXT NOT NULL DEFAULT 'lamoda' CHECK (channel IN ('lamoda', 'wb', 'ozon')),
    recommendation_type     TEXT NOT NULL CHECK (recommendation_type IN ('raise', 'lower', 'test_raise', 'no_change')),
    recommended_price       NUMERIC(10,2),
    rationale               TEXT,
    margin_pct_at_calc      NUMERIC(8,2),
    source                  TEXT NOT NULL DEFAULT 'rule' CHECK (source IN ('rule', 'optimization')),
    optimization_run_id     BIGINT REFERENCES price_optimization_run(run_id),
    status                  TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'approved', 'applied', 'api_failed', 'validation_failed',
                                              'fraud_rejected', 'quarantined', 'suppressed', 'superseded')),
    suppressed_by_triage_id BIGINT REFERENCES anomaly_triage_log(triage_id),
    approved_by             TEXT,
    approved_at             TIMESTAMP,
    is_test                 BOOLEAN NOT NULL DEFAULT FALSE, -- A-42: тестовая рекомендация не уходит в API
    created_at              TIMESTAMP NOT NULL DEFAULT now()
);

-- Лог применения цены через API (раздел 6). api_response_status — тот же словарь, что статусы рекомендации (A-39)
CREATE TABLE price_change_log (
    price_change_id         BIGSERIAL PRIMARY KEY,
    recommendation_id       BIGINT NOT NULL REFERENCES pricing_recommendations(recommendation_id),
    product_model_id        INT NOT NULL REFERENCES product_models(product_model_id),
    old_price               NUMERIC(10,2),
    new_price               NUMERIC(10,2) NOT NULL,
    api_method              TEXT NOT NULL DEFAULT 'POST /v2/nomenclature-price',
    api_response_status     TEXT CHECK (api_response_status IN ('applied', 'api_failed', 'validation_failed',
                                                                'fraud_rejected', 'quarantined')),
    fraud_validation_result TEXT,
    api_error_message       TEXT,
    submitted_at            TIMESTAMP NOT NULL DEFAULT now()
);


-- ================= Индексы для частых запросов =================

CREATE INDEX idx_sales_daily_date ON sales_daily (sale_date);
CREATE INDEX idx_stock_fbo_date ON stock_fbo (snapshot_date);
CREATE INDEX idx_demand_forecast_period ON demand_forecast (period_start, period_end);
CREATE INDEX idx_margin_calc_date ON margin_calc (calc_date);
CREATE INDEX idx_order_items_size_date ON order_items (product_size_id, event_date);
CREATE INDEX idx_channel_sales_size_date ON channel_sales (product_size_id, sale_date);
CREATE INDEX idx_channel_sales_barcode ON channel_sales (barcode);
CREATE INDEX idx_barcode_pool_size ON barcode_pool (product_size_id);
CREATE INDEX idx_webhook_unprocessed ON webhook_events_log (received_at) WHERE NOT processed;
CREATE INDEX idx_pricing_pending ON pricing_recommendations (created_at) WHERE status = 'pending';
