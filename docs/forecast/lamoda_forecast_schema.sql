-- ============================================================
-- Структура БД: система прогнозирования спроса — Lamoda FBO, WB/Ozon (FBS), собственный сайт
-- Иерархия: артикул → модель (артикул+цвет, = Lamoda SKU) → размер (= Lamoda SKU+размер)
-- Все входные потоки и расчёты ведутся на уровне product_size,
-- кроме подбора аналогов — на уровне product_model. Таблицы funnel_model/funnel_size —
-- без автоматического источника (Excel без API), опциональны, не заполняются пайплайном.
-- promo_report_daily/sku — автоматизированы через GET /v2/promotions (раздел 4).
-- ⚠️ demand_forecast/margin_calc — многоканальные (поле channel: lamoda/wb/ozon/own_site, раздел 5/5.7-доп).
-- reorder_points/shipment_recommendations остаются Lamoda-only (только у Lamoda есть склад FBO, раздел 5.5) —
-- закупка у поставщика (purchase_recommendations) использует central_stock_aggregate_demand (сумма по всем
-- каналам), не demand_forecast одного канала напрямую.
-- ⚠️ Lamoda API отдаёт денежные суммы как целые числа в минорных единицах (копейки для RUB) —
-- все NUMERIC(x,2)-поля этой схемы, заполняемые из API, требуют деления на 100 при приёме (раздел 7).
-- ============================================================


-- ================= 1. СПРАВОЧНИКИ (master data) =================

CREATE TABLE suppliers (
    supplier_id         SERIAL PRIMARY KEY,
    name                 TEXT NOT NULL,
    production_days      INT NOT NULL,          -- срок производства
    delivery_days        INT NOT NULL,          -- срок доставки от поставщика
    moq                   INT NOT NULL,          -- минимальная партия
    on_time_delivery_rate_pct  NUMERIC(5,2)      -- кэш агрегата из supplier_deliveries, раздел 5.9
);

CREATE TABLE product_models (
    product_model_id     SERIAL PRIMARY KEY,
    article               TEXT NOT NULL,         -- собственный артикул
    color                 TEXT,
    category              TEXT NOT NULL,
    subcategory            TEXT,
    season                TEXT,                  -- лето/зима/демисезон
    supplier_id           INT REFERENCES suppliers(supplier_id),
    collection_type        TEXT DEFAULT 'base',   -- base / capsule (капсульная не идёт в регулярный цикл)
    first_sale_date        DATE,
    status                 TEXT DEFAULT 'active', -- new / active / fading / discontinued
    available_to_reorder   BOOLEAN DEFAULT TRUE,   -- ведётся вручную
    discontinued_date       DATE,
    vat_rate                NUMERIC(5,2),          -- ставка НДС товара
    lamoda_sku               TEXT,                 -- Lamoda SKU модели (напр. MP002XM0WFWS)
    wb_sku                    TEXT,                 -- WB nmID — из начальной загрузки карты сопоставления (раздел 3)
    ozon_sku                    TEXT,                -- Ozon offer_id/SKU — то же
    planned_launch_price      NUMERIC(10,2),        -- плановая цена запуска — источник Cu для newsvendor (5.6),
                                                     -- пока нет фактических продаж и, соответственно, margin_calc
    seasonal_group             TEXT,                -- группа по паттерну сезонности (раздел 5.2), не по категории —
                                                      -- назначается вручную или кластеризацией помесячных профилей
    color_group                  TEXT,               -- напр. 'базовые'/'яркие'/'принт' — для color_profile (раздел 5.6)
    UNIQUE (article, color)
);

CREATE TABLE style_tags (
    style_tag_id          SERIAL PRIMARY KEY,
    product_model_id       INT REFERENCES product_models(product_model_id),
    tag                    TEXT NOT NULL,          -- оверсайз / приталенный / принт / ...
    confidence              NUMERIC(4,3),           -- уверенность vision-агента (раздел 8.1)
    source                  TEXT DEFAULT 'agent',   -- agent / manual / lamoda_attribute (из GET /v2/nomenclatures/{sku}/
                                                      -- attributes, раздел 8.1 — приоритетнее agent, если есть у категории)
    reviewed                 BOOLEAN DEFAULT FALSE,  -- прошёл ли ручную проверку (при низком confidence)
    created_at                TIMESTAMP DEFAULT now()
);

CREATE TABLE product_sizes (
    product_size_id        SERIAL PRIMARY KEY,
    product_model_id         INT NOT NULL REFERENCES product_models(product_model_id),
    size_label                TEXT NOT NULL,        -- 48, 50, M, L, ...
    lamoda_sku                 TEXT,                 -- Lamoda SKU размера (напр. MP002XM0WFWSINXL)
    UNIQUE (product_model_id, size_label)
);

-- Пул баркодов — у одного product_size может быть несколько баркодов одновременно (разные площадки/1С
-- на один физический товар), не только исторически при переиздании (valid_from/valid_to). Сопоставление
-- (раздел 3/4) — по совпадению ЛЮБОГО одного баркода из пула, не единственного канонического значения.
CREATE TABLE barcode_pool (
    barcode_id              SERIAL PRIMARY KEY,
    product_size_id           INT NOT NULL REFERENCES product_sizes(product_size_id),
    barcode                   TEXT NOT NULL UNIQUE,
    seller_sku                 TEXT,
    valid_from                  DATE DEFAULT CURRENT_DATE,
    valid_to                     DATE                 -- null = текущий активный
);

CREATE TABLE product_analogs (
    product_model_id_new       INT NOT NULL REFERENCES product_models(product_model_id),
    product_model_id_analog     INT NOT NULL REFERENCES product_models(product_model_id),
    similarity_weight            NUMERIC(4,3) NOT NULL,  -- 0..1
    PRIMARY KEY (product_model_id_new, product_model_id_analog)
);

-- Настройки маржи — версионируемые (история изменений порогов/ставок)
CREATE TABLE margin_settings (
    setting_id                SERIAL PRIMARY KEY,
    effective_from               DATE NOT NULL,
    lamoda_commission_rate        NUMERIC(5,4) NOT NULL DEFAULT 0.42,
    tax_regime                     TEXT NOT NULL DEFAULT 'УСН доходы',
    vat_rate                        NUMERIC(5,4) NOT NULL DEFAULT 0.05,
    usn_rate                         NUMERIC(5,4) NOT NULL DEFAULT 0.06,
    margin_min_pct                    NUMERIC(5,2) NOT NULL DEFAULT 30,
    margin_target_pct                  NUMERIC(5,2) NOT NULL DEFAULT 50,
    margin_max_pct                      NUMERIC(5,2) NOT NULL DEFAULT 80,
    usd_rate_adjustment                  NUMERIC(6,2) NOT NULL DEFAULT 4, -- +/- к курсу ЦБ
    commission_vat_multiplier              NUMERIC(5,4) NOT NULL DEFAULT 1, -- заглушка: множитель компенсации
                                                                              -- скидки на комиссию по ставке НДС
                                                                              -- (открытый вопрос №2, уточнить у Lamoda)
    liquidation_recovery_rate                NUMERIC(4,3) NOT NULL DEFAULT 0.5 -- доля себестоимости, возвращаемая
                                                                                 -- уценкой неликвида (newsvendor Co, 5.6)
);

-- Настройки коррекции прогноза (по категории — разное поведение алгоритма/трафика)
CREATE TABLE forecast_correction_settings (
    category                    TEXT PRIMARY KEY,
    recovery_tail_days            INT NOT NULL DEFAULT 7,     -- 3-14 дней, раздел 5.1
    recovery_tail_coefficient       NUMERIC(3,2) NOT NULL DEFAULT 0.5,  -- 0.3-0.7
    outlier_sigma_threshold           NUMERIC(3,1) NOT NULL DEFAULT 3.0  -- фильтр выбросов при расчёте σ, раздел 5.5
);

-- Сезонные коэффициенты по группе паттерна (раздел 5.2) — не по категории напрямую,
-- разные категории с похожей формой кривой объединяются в одну seasonal_group
CREATE TABLE seasonal_coefficients (
    seasonal_group                TEXT NOT NULL,
    month                            INT NOT NULL CHECK (month BETWEEN 1 AND 12),
    coefficient                       NUMERIC(6,3) NOT NULL,  -- мультипликативный, отклонение от средних продаж
    computed_at                         TIMESTAMP DEFAULT now(),
    PRIMARY KEY (seasonal_group, month)
);

CREATE TABLE promo_calendar (
    promo_id                   SERIAL PRIMARY KEY,
    name                         TEXT,                -- напр. "чёрная пятница"
    date_from                     DATE NOT NULL,
    date_to                        DATE NOT NULL
);


-- ================= 2. ВХОДНЫЕ ПОТОКИ (факты, временные ряды) =================

CREATE TABLE stock_1c (
    product_size_id        INT NOT NULL REFERENCES product_sizes(product_size_id),
    snapshot_date            DATE NOT NULL,
    qty                        INT NOT NULL,
    PRIMARY KEY (product_size_id, snapshot_date)
);

CREATE TABLE stock_fbo (
    product_size_id        INT NOT NULL REFERENCES product_sizes(product_size_id),
    warehouse_code           TEXT NOT NULL,
    snapshot_date             DATE NOT NULL,
    qty                        INT NOT NULL,          -- 0 = флаг stockout
    PRIMARY KEY (product_size_id, warehouse_code, snapshot_date)
);

-- Факт создания поставки на FBO (не рекомендация, а реально отправленная поставка) —
-- источник компонента "в пути на FBO" в прогноз_остатка(T), раздел 5.4
CREATE TABLE fbo_shipments (
    shipment_id               TEXT NOT NULL,        -- ID поставки из Lamoda (POST /v2/fbo/shipments)
    product_size_id             INT NOT NULL REFERENCES product_sizes(product_size_id),
    qty                           INT NOT NULL,
    created_date                   DATE NOT NULL,
    expected_arrival_date            DATE,
    actual_arrival_date                DATE,             -- заполняется по появлению в stock_fbo
    validation_status                    TEXT,           -- из вебхука fulfilmentShipmentValidationRest (раздел 4)
    status                               TEXT DEFAULT 'in_transit',  -- in_transit / received / discrepancy
    PRIMARY KEY (shipment_id, product_size_id)  -- одна поставка обычно содержит несколько SKU
);

-- Идемпотентная обработка вебхуков Lamoda (раздел 4/7) — дедупликация по sequenceNumber,
-- чтобы повторная отправка одного события (retry-логика Lamoda при 5хх) не применялась дважды
CREATE TABLE webhook_events_log (
    event_id                  BIGSERIAL PRIMARY KEY,
    notification_type            TEXT NOT NULL,     -- statusChanged / moderationApproved / ... (раздел 4)
    tracking_id                     TEXT,            -- группировочный ID заказа в теле вебхука (не order_id для API)
    sequence_number                   BIGINT,        -- для проверки устаревших/повторных событий
    payload                              JSONB NOT NULL,
    confirmed_via_get                     BOOLEAN DEFAULT FALSE, -- подтверждено GET /v2/orders/{orderId}
                                                                    -- перед применением (раздел 4 — вебхук не
                                                                    -- гарантирует доставку/порядок сам по себе)
    gap_resend_requested                    BOOLEAN DEFAULT FALSE, -- запрошена переотправка недоставленных
                                                                     -- нотификаций (только для заказов, раздел 4)
    processed                               BOOLEAN DEFAULT FALSE,
    received_at                              TIMESTAMP DEFAULT now()
);

-- Сверка расчётного состояния (state-machine) с фактическим снимком stock_fbo —
-- фиксирует расхождения, чтобы не терять их молча (раздел 5.4, открытый вопрос №2 из аудита)
CREATE TABLE stock_reconciliation_log (
    reconciliation_id           BIGSERIAL PRIMARY KEY,
    product_size_id                INT NOT NULL REFERENCES product_sizes(product_size_id),
    check_date                       DATE NOT NULL,
    expected_qty_computed               NUMERIC(10,2),   -- по state-machine
    actual_qty_from_snapshot              NUMERIC(10,2), -- из stock_fbo
    discrepancy                             NUMERIC(10,2),
    resolved                                  BOOLEAN DEFAULT FALSE
);

CREATE TABLE illiquid_flags (
    product_size_id        INT NOT NULL REFERENCES product_sizes(product_size_id),
    snapshot_date            DATE NOT NULL,
    is_illiquid               BOOLEAN NOT NULL,
    PRIMARY KEY (product_size_id, snapshot_date)
);

-- Сырые строки заказов из /v2/orders — источник продаж и резервный источник возвратов
CREATE TABLE order_items (
    order_item_id            BIGSERIAL PRIMARY KEY,
    order_id                    TEXT NOT NULL,        -- data.id из API/вебхука (напр. "CZ117391950") — не trackingId
    tracking_id                   TEXT,                -- отдельное поле вебхука, для группировки событий по заказу
    lamoda_item_id                  BIGINT,            -- items[].id — для идемпотентной обработки (раздел 4)
    product_size_id                   INT REFERENCES product_sizes(product_size_id),
    status                              TEXT NOT NULL,  -- Delivered / Not delivered / Canceled / Returned / ...
    price_actual                         NUMERIC(10,2),
    price_base                            NUMERIC(10,2),
    total_discount                          NUMERIC(10,2), -- items[].totalDiscount из вебхука — скидка на уровне
                                                             -- позиции без похода в /v2/promotions отдельно
    event_date                              DATE NOT NULL,
    ingested_at                              TIMESTAMP DEFAULT now(),
    UNIQUE (order_id, lamoda_item_id, status)  -- идемпотентность: "id заказа + статус" (раздел 4)
);

-- Агрегированные дневные продажи (по net-спросу, Delivered − возвраты)
CREATE TABLE sales_daily (
    product_size_id           INT NOT NULL REFERENCES product_sizes(product_size_id),
    sale_date                    DATE NOT NULL,
    qty_delivered                  INT NOT NULL DEFAULT 0,
    qty_returned                    INT NOT NULL DEFAULT 0,
    revenue                          NUMERIC(12,2),
    PRIMARY KEY (product_size_id, sale_date)
);

-- ⚠️ НЕ АВТОМАТИЗИРОВАНО: источник (отчёт «Воронка продаж») доступен только Excel-выгрузкой из ЛК,
-- API нет (раздел 4). Таблица не заполняется регулярным пайплайном — опциональна для ручного разбора.
CREATE TABLE funnel_model (
    product_model_id            INT NOT NULL REFERENCES product_models(product_model_id),
    period_start                   DATE NOT NULL,
    period_end                      DATE NOT NULL,
    views                             INT,
    cart_conversion_pct                NUMERIC(6,3),
    cart_adds                            INT,
    order_share_pct                       NUMERIC(6,3),
    orders_qty                             INT,
    purchased_qty                           INT,
    purchase_rate_pct                        NUMERIC(6,3),
    PRIMARY KEY (product_model_id, period_start, period_end)
);

-- ⚠️ НЕ АВТОМАТИЗИРОВАНО (та же причина, что funnel_model выше). Возвратность вместо этого
-- берётся напрямую из /v2/orders (раздел 4) — см. demand_timeseries / order_items.
CREATE TABLE funnel_size (
    product_size_id             INT NOT NULL REFERENCES product_sizes(product_size_id),
    period_start                   DATE NOT NULL,
    period_end                      DATE NOT NULL,
    orders_qty                        INT,
    purchased_qty                       INT,
    purchase_rate_pct                     NUMERIC(6,3),
    returns_qty                             INT,
    return_rate_pct                           NUMERIC(6,3),
    PRIMARY KEY (product_size_id, period_start, period_end)
);

-- Собственный расчёт (раздел 5.6) — агрегация истории заказов из /v2/orders, а не отчёт Lamoda (без API)
CREATE TABLE size_profile (
    category                      TEXT NOT NULL,
    gender                          TEXT,                    -- муж/жен — размерный профиль различается
    size_label                        TEXT NOT NULL,
    share_of_sales_pct                  NUMERIC(6,3) NOT NULL,
    report_date                           DATE NOT NULL,
    PRIMARY KEY (category, gender, size_label, report_date)
);

-- Историческая выгрузка продаж с других каналов (раздел 4) — разовая/редкая загрузка, не ежедневный поток.
-- WB: GET /api/v5/supplier/reportDetailByPeriod (Statistics API, лимит 1 запрос/мин — закладывать паузы).
-- Ozon: AnalyticsAPI/FinanceAPI, отчёты о продажах (Client-Id+Api-Key, не Bearer-токен).
-- Ключ сопоставления — баркод цветоразмерного SKU через весь barcode_pool (совпадение любого одного
-- баркода из пула достаточно, раздел 3), не артикул целиком и не SKU площадки.
CREATE TABLE external_channel_sales_history (
    channel                        TEXT NOT NULL,          -- wildberries / ozon / retail_1 / retail_2
    barcode                           TEXT NOT NULL,        -- join по barcode_pool.barcode (любой из пула для
                                                              -- данного product_size), не по одному полю-канону
    month                                DATE NOT NULL,     -- первое число месяца — история помесячная
    qty_sold                              NUMERIC(10,2),
    revenue                                 NUMERIC(14,2),
    loaded_at                                 TIMESTAMP DEFAULT now(),
    PRIMARY KEY (channel, barcode, month)
);

-- Сезонный индекс из долгой истории (раздел 5.2) — форма сезонной кривой по seasonal_group,
-- приоритетнее короткой истории Lamoda, пока своя не накоплена (1-2 полных цикла)
CREATE TABLE seasonal_index_reference (
    seasonal_group                 TEXT NOT NULL,
    month_number                      INT NOT NULL,          -- 1-12
    index_value                          NUMERIC(6,4) NOT NULL, -- нормализовано: среднее за год = 1.0
    source_channels                        TEXT,              -- какие каналы вошли в расчёт
    computed_at                              TIMESTAMP DEFAULT now(),
    PRIMARY KEY (seasonal_group, month_number)
);

-- Коэффициент пересчёта продаж канала в Lamoda-эквивалент (раздел 5.6) — калибруется по товарам,
-- продающимся на обоих каналах одновременно; используется для новинок на Lamoda с историей
-- на другом канале (третий, приоритетный метод прогноза новинок)
CREATE TABLE channel_ratio_calibration (
    channel                        TEXT NOT NULL,
    category                          TEXT NOT NULL,          -- коэффициент может отличаться по категориям
    ratio                                NUMERIC(6,4) NOT NULL, -- среднее(Lamoda_продажи / продажи_канала)
    sample_size                           INT,                 -- кол-во товаров, по которым калибровался
    computed_at                             TIMESTAMP DEFAULT now(),
    PRIMARY KEY (channel, category)
);

-- Недооценённый потенциал (раздел 5.6) — товар уже на Lamoda, но продаётся заметно хуже,
-- чем предсказывает канал-коэффициент по истории на WB/Ozon/рознице — сигнал проверить
-- листинг (карточка/цена/категория/видимость), не факт отсутствия спроса
CREATE TABLE underperformance_flags (
    product_model_id               INT NOT NULL REFERENCES product_models(product_model_id),
    period_start                      DATE NOT NULL,
    period_end                          DATE NOT NULL,
    expected_sales_qty                    NUMERIC(10,2),  -- продажи_канала × channel_ratio_calibration.ratio
    actual_sales_qty                        NUMERIC(10,2),
    shortfall_pct                             NUMERIC(6,2), -- факт / ожидание × 100
    in_stock_full_period                        BOOLEAN,    -- TRUE = дефицита не было, разница не в наличии
    -- автоматические диагностические проверки (раздел 5.6) — не причина сами по себе, а подсказка менеджеру
    attributes_incomplete                         BOOLEAN,   -- есть незаполненные атрибуты карточки
    popular_sizes_out_of_stock                      BOOLEAN, -- ходовые размеры (по каналу-источнику) не в наличии,
                                                                -- хотя in_stock_full_period может быть TRUE
    price_gap_vs_category_pct                         NUMERIC(6,2), -- % выше среднего по категории
    price_gap_vs_source_channel_pct                     NUMERIC(6,2), -- % выше цены на канале-источнике
    card_status_issue                                     BOOLEAN, -- модерация/маркировка блокирует продажу
    competitors_in_promo_we_are_not                         BOOLEAN,
    flagged                                       BOOLEAN DEFAULT FALSE,
    reviewed_at                                     TIMESTAMP,
    review_outcome                                    TEXT, -- listing_issue_fixed / objectively_lower_demand / null
    created_at                                          TIMESTAMP DEFAULT now(),
    PRIMARY KEY (product_model_id, period_start, period_end)
);

-- Норматив распределения по цвету (раздел 5.6) — для нового цвета существующего артикула:
-- прогноз_по_артикулу × доля_цвета, точнее подбора аналогов среди чужих моделей
CREATE TABLE color_profile (
    category                      TEXT NOT NULL,
    color_group                     TEXT NOT NULL,           -- напр. 'базовые', 'яркие', 'принт'
    share_of_sales_pct                NUMERIC(6,3) NOT NULL,
    report_date                         DATE NOT NULL,
    PRIMARY KEY (category, color_group, report_date)
);

-- Кросс-эластичность между парой связанных SKU (раздел 5.3) — формализует каннибализацию
-- той же регрессией, что и собственная эластичность (price_elasticity ниже)
CREATE TABLE cross_elasticity (
    product_size_id_a                 INT NOT NULL REFERENCES product_sizes(product_size_id), -- чья доступность
    product_size_id_b                   INT NOT NULL REFERENCES product_sizes(product_size_id), -- чей спрос затронут
    cross_elasticity_coefficient          NUMERIC(6,4) NOT NULL,  -- ∂ln(спрос_B) / ∂ln(доступность_A)
    sample_size                             INT,                  -- кол-во исторических эпизодов stockout A
    computed_at                               TIMESTAMP DEFAULT now(),
    PRIMARY KEY (product_size_id_a, product_size_id_b)
);

-- Промо-каннибализация между РАЗНЫМИ артикулами (раздел 5.3) — второй, отдельный механизм:
-- акция на артикул B уводит покупателей от артикула A по обычной цене. Двухвыборочный t-test,
-- не регрессия (в отличие от cross_elasticity выше, которая про доступность внутри артикула)
CREATE TABLE promo_cannibalization_pairs (
    product_model_id_a                INT NOT NULL REFERENCES product_models(product_model_id), -- теряет продажи
    product_model_id_b                  INT NOT NULL REFERENCES product_models(product_model_id), -- в акции
    mean_sales_a_no_promo_b               NUMERIC(10,2),   -- Snp
    mean_sales_a_promo_b                    NUMERIC(10,2), -- Sp
    demand_shift_d                            NUMERIC(10,2), -- Snp - Sp
    p_value                                     NUMERIC(6,5),
    significant                                   BOOLEAN,  -- p < 0.05 и D > 0
    computed_at                                     TIMESTAMP DEFAULT now(),
    PRIMARY KEY (product_model_id_a, product_model_id_b)
);

-- Сигнал каннибализации между цветами/размерами одного артикула (раздел 5.3) — конкретные события,
-- demand_shift_qty теперь считается через cross_elasticity, а не отдельной эвристикой
CREATE TABLE cannibalization_events (
    event_id                       BIGSERIAL PRIMARY KEY,
    product_size_id_out_of_stock       INT NOT NULL REFERENCES product_sizes(product_size_id),
    product_size_id_gained_demand        INT NOT NULL REFERENCES product_sizes(product_size_id),
    event_date                             DATE NOT NULL,
    demand_shift_qty                         NUMERIC(10,2),   -- из cross_elasticity, раздел 5.3
    detected_at                                TIMESTAMP DEFAULT now()
);

-- Источник: GET /v2/promotions (раздел 4) — API v2, найден повторным изучением документации после того,
-- как считался недоступным по устаревшей v1-документации. Точное соответствие полей не подтверждено —
-- уточнить на этапе реализации (может не покрывать разбивку "Общие данные" 1:1, раньше бравшуюся из Excel)
CREATE TABLE promo_report_daily (
    report_date                    DATE PRIMARY KEY,
    orders_sum_promo                  NUMERIC(12,2),
    orders_sum_all                      NUMERIC(12,2),
    orders_share_promo_pct                NUMERIC(6,2),
    redemption_sum_promo                    NUMERIC(12,2),
    redemption_sum_all                        NUMERIC(12,2),
    redemption_share_promo_pct                  NUMERIC(6,2)
);

-- Источник: GET /v2/promotions/{promotionId}/products (раздел 4) — по одному товару на акцию,
-- естественно ложится в структуру этой таблицы. avg_price_seller — проверить, отдаёт ли API
-- эту разбивку явно или только итоговую цену покупателя (раздел 5.7, открытый вопрос №1)
CREATE TABLE promo_report_sku (
    product_size_id                  INT NOT NULL REFERENCES product_sizes(product_size_id),
    period_start                        DATE NOT NULL,
    period_end                            DATE NOT NULL,
    orders_sum_promo                        NUMERIC(12,2),
    orders_qty_promo                          INT,
    redemption_sum_promo                        NUMERIC(12,2),
    redemption_qty_promo                          INT,
    redemption_rate_pct                             NUMERIC(6,3),
    avg_price_seller_and_lamoda                       NUMERIC(10,2),
    avg_discount_seller_and_lamoda_pct                  NUMERIC(6,3),
    avg_price_seller                                      NUMERIC(10,2),  -- цена, реально влияющая на маржу
    avg_discount_seller_pct                                 NUMERIC(6,3),
    PRIMARY KEY (product_size_id, period_start, period_end)
);

CREATE TABLE wordstat_data (
    keyword                          TEXT NOT NULL,
    stat_date                          DATE NOT NULL,
    region                               TEXT,
    frequency                             INT,
    PRIMARY KEY (keyword, stat_date, region)
);

-- Google Trends (раздел 5.3) — независимый источник поискового интереса, не замена Вордстату, а второй замер
CREATE TABLE google_trends_data (
    keyword                          TEXT NOT NULL,
    trend_date                         DATE NOT NULL,
    region                               TEXT,
    popularity_index                      NUMERIC(5,2),  -- 0-100, шкала Google Trends
    PRIMARY KEY (keyword, trend_date, region)
);

-- Клики Яндекс.Директ на карточки своего сайта (раздел 5.3) — сигнал интереса точнее Вордстата,
-- т.к. это клик по конкретной модели, а не поиск по фасону/категории в целом
-- Сопоставление объявления и товара (раздел 4) — заполняется при создании кампании/объявления,
-- не парсится из статистики Директа (там нет посадочной страницы как поля отчёта по умолчанию)
CREATE TABLE yandex_direct_ad_mapping (
    ad_id                          TEXT PRIMARY KEY,
    campaign_id                      TEXT,
    product_model_id                   INT NOT NULL REFERENCES product_models(product_model_id),
    created_at                           TIMESTAMP DEFAULT now()
);

CREATE TABLE yandex_direct_clicks (
    ad_id                             TEXT NOT NULL REFERENCES yandex_direct_ad_mapping(ad_id),
    click_date                          DATE NOT NULL,
    clicks                                    INT,
    impressions                                 INT,
    ctr_pct                                       NUMERIC(6,3),
    spend                                           NUMERIC(10,2),
    PRIMARY KEY (ad_id, click_date)
);

-- Аудитория кликнувших по рекламе (раздел 5.3) — из отчёта "Аудитория" Яндекс.Метрики,
-- сравнивается с эталонным профилем покупателей для калибровки веса коэф_direct
CREATE TABLE metrika_audience_segments (
    product_model_id                 INT NOT NULL REFERENCES product_models(product_model_id),
    segment_date                        DATE NOT NULL,
    segment_type                          TEXT NOT NULL,   -- gender / age / device / geo
    segment_value                           TEXT NOT NULL, -- напр. 'женский', '25-34', 'mobile', 'Москва'
    share_of_clicks_pct                       NUMERIC(6,3) NOT NULL,
    PRIMARY KEY (product_model_id, segment_date, segment_type, segment_value)
);

-- Эталонный профиль реальных покупателей (раздел 5.3) — из CRM AWER, заказы с собственного сайта
CREATE TABLE buyer_profile_reference (
    segment_type                     TEXT NOT NULL,        -- gender / age / device / geo
    segment_value                      TEXT NOT NULL,
    share_of_buyers_pct                  NUMERIC(6,3) NOT NULL,
    computed_at                            TIMESTAMP DEFAULT now(),
    PRIMARY KEY (segment_type, segment_value)
);

-- Качество аудитории кликнувших относительно реальных покупателей (раздел 5.3) —
-- множитель для коэф_direct: 1 = аудитория идентична покупателям, 0 = совпадений нет
CREATE TABLE audience_quality_score (
    product_model_id                 INT NOT NULL REFERENCES product_models(product_model_id),
    score_date                          DATE NOT NULL,
    quality_score                         NUMERIC(4,3) NOT NULL,  -- среднее TVD по 4 измерениям отдельно (раздел 5.3),
                                                                    -- не сумма по всем сегментам разом
    computed_at                             TIMESTAMP DEFAULT now(),
    PRIMARY KEY (product_model_id, score_date)
);

CREATE TABLE weather_data (
    region                            TEXT NOT NULL,
    weather_date                        DATE NOT NULL,
    avg_temp_c                            NUMERIC(4,1),
    PRIMARY KEY (region, weather_date)
);

-- Конкурентная аналитика по категориям (раздел 5.6) — внешний рыночный тренд,
-- ценен особенно там, где своей истории в категории мало или нет вообще
CREATE TABLE category_market_trends (
    category                       TEXT NOT NULL,
    trend_date                        DATE NOT NULL,
    market_growth_index                 NUMERIC(6,3),   -- индекс роста категории по рынку в целом
    competitor_count                      INT,
    avg_category_price                      NUMERIC(10,2),
    source                                    TEXT,       -- напр. 'mpstats' / 'manual'
    PRIMARY KEY (category, trend_date)
);

CREATE TABLE usd_rate (
    rate_date                         DATE PRIMARY KEY,
    cbr_rate                            NUMERIC(8,4) NOT NULL,
    adjusted_rate                         NUMERIC(8,4) NOT NULL   -- cbr_rate + usd_rate_adjustment
);


-- ================= 3. РАСЧЁТНЫЕ СУЩНОСТИ =================

CREATE TABLE demand_timeseries (
    product_size_id                 INT NOT NULL REFERENCES product_sizes(product_size_id),
    demand_date                        DATE NOT NULL,
    net_demand                           NUMERIC(10,2) NOT NULL,   -- Delivered - возвраты_с_лагом
    base_demand                            NUMERIC(10,2),           -- контрфактический прогноз без промо (раздел 5.3)
    promo_uplift                             NUMERIC(10,2),         -- net_demand - base_demand за период акции
    is_stockout_corrected                  BOOLEAN DEFAULT FALSE,   -- исключён/скорректирован из-за stockout
    is_return_censored                       BOOLEAN DEFAULT FALSE, -- в пределах lead_возврат от текущей даты —
                                                                     -- исключается из обучающей выборки
    is_recovery_tail_corrected                 BOOLEAN DEFAULT FALSE, -- хвост восстановления после OOS,
                                                                        -- net_demand скорректирован делением
                                                                        -- на recovery_tail_coefficient
    PRIMARY KEY (product_size_id, demand_date)
);

CREATE TABLE abc_xyz_classification (
    product_model_id                  INT NOT NULL REFERENCES product_models(product_model_id),
    period_start                         DATE NOT NULL,
    combined_score                         NUMERIC(10,4),           -- скор по выручке + количеству (ABC)
    abc_class                                TEXT NOT NULL,          -- A / B / C / fading
    coefficient_variation                      NUMERIC(6,3),         -- CV спроса, % (XYZ)
    xyz_class                                    TEXT NOT NULL,       -- X / Y / Z
    combined_segment                               TEXT NOT NULL,     -- AX / AY / AZ / BX / ... / CZ
    service_level_z                                  NUMERIC(4,3) NOT NULL,
    review_frequency                                   TEXT,          -- ежедневно / еженедельно / раз в 2 недели / по триггеру
    review_interval_days                                 INT,          -- числовое значение той же частоты, для формулы L (5.5)
    PRIMARY KEY (product_model_id, period_start)
);

CREATE TABLE lead_time_stats (
    scope_type                         TEXT NOT NULL,               -- 'model' / 'category'
    scope_id                             TEXT NOT NULL,              -- product_model_id либо название категории
    lead_type                              TEXT NOT NULL,            -- fbo_delivery / return / supplier
    median_days                              NUMERIC(6,2) NOT NULL,
    std_days                                   NUMERIC(6,2),
    computed_at                                  TIMESTAMP DEFAULT now(),
    PRIMARY KEY (scope_type, scope_id, lead_type)
);

CREATE TABLE demand_forecast (
    product_size_id                    INT NOT NULL REFERENCES product_sizes(product_size_id),
    channel                               TEXT NOT NULL DEFAULT 'lamoda', -- lamoda/wb/ozon/own_site
                                                                            -- (раздел 5 — многоканальный прогноз;
                                                                            -- own_site = awer-russia.ru, Яндекс KIT)
    period_start                          DATE NOT NULL,
    period_end                              DATE NOT NULL,
    forecast_qty                              NUMERIC(10,2) NOT NULL,
    lower_bound                                 NUMERIC(10,2),
    upper_bound                                   NUMERIC(10,2),
    model_type                                      TEXT,           -- smoothing / tsb / croston_sba / analog_based / gradient_boosting / deepar
    adi_days                                          NUMERIC(6,2), -- Average inter-Demand Interval
    cv_squared                                          NUMERIC(6,3), -- Squared Coefficient of Variation
    demand_pattern                                        TEXT,      -- smooth / irregular / intermittent / lumpy
                                                                       -- (SBC-классификация, раздел 5.2)
    computed_at                                       TIMESTAMP DEFAULT now(),
    PRIMARY KEY (product_size_id, channel, period_start, period_end)
);

-- Агрегированный спрос на ЦС по всем каналам сразу (раздел 5) — источник для точки заказа/закупки
-- у поставщика (раздел 5.5/6), т.к. WB/Ozon/Яндекс Маркет (FBS) тянут остаток напрямую с ЦС,
-- не через отдельный склад площадки, как Lamoda (FBO)
CREATE TABLE central_stock_aggregate_demand (
    product_size_id                    INT NOT NULL REFERENCES product_sizes(product_size_id),
    period_start                          DATE NOT NULL,
    period_end                              DATE NOT NULL,
    total_forecast_qty                        NUMERIC(10,2) NOT NULL, -- Σ demand_forecast.forecast_qty по всем
                                                                        -- channel для этого product_size/периода
    channel_breakdown                           JSONB,                -- {"lamoda": X, "wb": Y, ...} для прозрачности
    computed_at                                   TIMESTAMP DEFAULT now(),
    PRIMARY KEY (product_size_id, period_start, period_end)
);

CREATE TABLE reorder_points (
    product_size_id                    INT NOT NULL REFERENCES product_sizes(product_size_id),
    computed_at                           TIMESTAMP NOT NULL DEFAULT now(),
    lead_time_used_days                     NUMERIC(6,2),      -- L: lead_поставка(_FBO/+поставщик) + интервал_пересмотра
    sigma_used                                NUMERIC(10,3),   -- σ ошибки прогноза (или сумм по окну для прерывистых), после фильтра 3σ
    formula_variant                             TEXT DEFAULT 'basic', -- basic / lead_time_variance / intermittent_window
    safety_stock                              NUMERIC(10,2),
    reorder_point_qty                           NUMERIC(10,2) NOT NULL,  -- считается от forecast_overrides.override_qty,
                                                                          -- если активный override есть на период (раздел 6),
                                                                          -- иначе от demand_forecast.forecast_qty
    PRIMARY KEY (product_size_id, computed_at)
);

CREATE TABLE new_item_forecast (
    product_model_id                     INT NOT NULL REFERENCES product_models(product_model_id),
    computed_at                             TIMESTAMP NOT NULL DEFAULT now(),
    analog_based_forecast_qty                 NUMERIC(10,2),
    category_correction_median                  NUMERIC(6,4),        -- историческая эффективность новинок категории
    category_correction_std                       NUMERIC(6,4),      -- σ_поправки_категории (раздел 5.6, буфер newsvendor)
    wordstat_coef                                 NUMERIC(6,4),      -- коэф_поискового_интереса^вес (раздел 5.6) —
                                                                        -- Вордстат+Google Trends вместе (раздел 5.3),
                                                                        -- имя поля осталось историческим
    final_forecast_qty                              NUMERIC(10,2),   -- агрегат по всей модели
    deepar_forecast_qty                               NUMERIC(10,2), -- сверочный прогноз DeepAR (раздел 5.2/5.6),
                                                                        -- альтернатива аналог-based методу выше
    coverage_period_days                              INT,
    underage_cost_cu                                    NUMERIC(10,2), -- маржа_ед, newsvendor (раздел 5.6)
    overage_cost_co                                       NUMERIC(10,2), -- себестоимость × (1 - liquidation_recovery_rate)
    critical_fractile                                       NUMERIC(5,4), -- Cu / (Cu + Co)
    z_newsvendor                                              NUMERIC(5,3), -- Φ⁻¹(critical_fractile)
    recommended_first_shipment_qty                              NUMERIC(10,2), -- агрегат, до раскладки по размерам
    moq_adjusted_qty                                              NUMERIC(10,2),
    risk_flag                                                       BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (product_model_id, computed_at)
);

-- Явный шаг раскладки агрегатного прогноза новинки по размерам через size_profile —
-- без него purchase_recommendations (уровень размера) неоткуда получить количество
CREATE TABLE new_item_size_allocation (
    product_model_id                     INT NOT NULL,
    computed_at                             TIMESTAMP NOT NULL,
    product_size_id                           INT NOT NULL REFERENCES product_sizes(product_size_id),
    allocated_qty                               NUMERIC(10,2) NOT NULL, -- moq_adjusted_qty × доля из size_profile
    FOREIGN KEY (product_model_id, computed_at) REFERENCES new_item_forecast(product_model_id, computed_at),
    PRIMARY KEY (product_model_id, computed_at, product_size_id)
);

-- Ежемесячная сверка с официальным отчётом комиссионера Lamoda (раздел 5.7) — не автоматизировано (ЭДО-документ,
-- не API), заполняется вручную раз в месяц для контроля накопленного расхождения margin_calc
CREATE TABLE lamoda_official_reconciliation (
    period_month                  DATE NOT NULL,        -- первое число месяца отчёта
    official_price_realized_incl_vat NUMERIC(14,2),      -- «Цена реализации, руб. вкл. НДС» из отчёта комиссионера
    our_computed_sum                  NUMERIC(14,2),      -- Σ margin_calc.price_realized за тот же период
    discrepancy_pct                     NUMERIC(6,2),
    reconciled_at                         TIMESTAMP DEFAULT now(),
    PRIMARY KEY (period_month)
);

CREATE TABLE margin_calc (
    product_size_id                        INT NOT NULL REFERENCES product_sizes(product_size_id),
    calc_date                                 DATE NOT NULL,
    channel                                     TEXT NOT NULL DEFAULT 'lamoda', -- lamoda / wb / ozon (раздел 5.7-доп)
    price_realized                              NUMERIC(10,2) NOT NULL, -- Lamoda: «цена за счёт продавца», из
                                                                          -- promo_report_sku.avg_price_seller
                                                                          -- (источник GET /v2/promotions/.../products,
                                                                          -- раздел 4) — если акция активна; иначе
                                                                          -- price_actual из order_items.
                                                                          -- WB/Ozon: price_actual из своих потоков
                                                                          -- (раздел 4), нет аналога «цены продавца»
    cost_unit                                     NUMERIC(10,2) NOT NULL, -- из 1С, канало-независимо (раздел 5.7)
    commission_amount                               NUMERIC(10,2),        -- по channel_commission_rates ниже
    logistics_fbo_amount                              NUMERIC(10,2),
    vat_amount                                          NUMERIC(10,2),
    usn_tax_amount                                        NUMERIC(10,2),
    margin_abs                                              NUMERIC(10,2),
    margin_pct                                                NUMERIC(6,2),
    deviation_reason                                            TEXT,     -- курс / спрос / комиссия
    PRIMARY KEY (product_size_id, calc_date, channel)
);

-- Комиссия/логистика по каналу и категории (раздел 5.7-доп) — WB: Tariffs API (комиссии, тарифы на короба/
-- паллеты/возврат/приёмку); Ozon: FinanceAPI/тарифы (точный метод уточнить при реализации, раздел 9)
CREATE TABLE channel_commission_rates (
    channel                        TEXT NOT NULL,          -- wb / ozon
    category                          TEXT NOT NULL,
    commission_pct                       NUMERIC(6,3),
    logistics_fee                          NUMERIC(10,2),  -- фиксированная составляющая, если есть у канала
    effective_from                           DATE NOT NULL,
    effective_to                               DATE,        -- null = текущая ставка
    PRIMARY KEY (channel, category, effective_from)
);

CREATE TABLE anomaly_triage_log (
    triage_id                             BIGSERIAL PRIMARY KEY,
    product_size_id                          INT REFERENCES product_sizes(product_size_id),
    period_start                                DATE,
    period_end                                    DATE,
    signals                                          JSONB,          -- набор сработавших сигналов
    probable_cause                                      TEXT,         -- вывод агента (раздел 8.2)
    recommended_action                                    TEXT,
    confidence                                              NUMERIC(4,3),
    manager_decision                                          TEXT,   -- confirmed / rejected / null
    decided_at                                                  TIMESTAMP
);

-- Точность прогноза (раздел 5.8) — по каждому SKU и периоду, для агрегации по сегменту ABC×XYZ
CREATE TABLE forecast_accuracy_log (
    product_size_id                       INT NOT NULL REFERENCES product_sizes(product_size_id),
    period_start                             DATE NOT NULL,
    period_end                                 DATE NOT NULL,
    forecast_qty                                 NUMERIC(10,2),
    actual_qty                                     NUMERIC(10,2),
    mape                                             NUMERIC(6,3),
    wape                                               NUMERIC(6,3),
    mae                                                  NUMERIC(10,2),  -- абсолютная ошибка в штуках
    rmse                                                   NUMERIC(10,2), -- штраф за крупные единичные промахи
    avg_deficit                                              NUMERIC(10,2), -- max(факт-прогноз, 0) — недопрогноз
    avg_surplus                                                NUMERIC(10,2), -- max(прогноз-факт, 0) — перепрогноз
    deviation_status                                     TEXT DEFAULT 'normal', -- normal / warning / critical
    consecutive_warning_count                              INT DEFAULT 0,        -- подряд периодов в статусе warning
    computed_at                                              TIMESTAMP DEFAULT now(),
    PRIMARY KEY (product_size_id, period_start, period_end)
);

-- Пороги эскалации по MAPE (раздел 5.8) — по категории, т.к. у категорий разная естественная волатильность
CREATE TABLE forecast_deviation_settings (
    category                    TEXT PRIMARY KEY,
    mape_warning_threshold        NUMERIC(5,2) NOT NULL DEFAULT 20,  -- %
    mape_critical_threshold         NUMERIC(5,2) NOT NULL DEFAULT 40, -- %
    consecutive_periods_trigger       INT NOT NULL DEFAULT 3
);

-- Лог смены метода прогноза при систематическом отклонении (раздел 5.8) или по бэктесту (5.2)
CREATE TABLE model_recalibration_log (
    recalibration_id             BIGSERIAL PRIMARY KEY,
    product_size_id                 INT NOT NULL REFERENCES product_sizes(product_size_id),
    triggered_at                      TIMESTAMP NOT NULL DEFAULT now(),
    reason                               TEXT,   -- напр. "систематическое отклонение 3 периода подряд" / "бэктест"
    old_model_type                        TEXT,
    new_model_type                          TEXT,
    triggered_by                              TEXT DEFAULT 'auto' -- auto / manual / backtest
);

-- Результаты бэктест-турнира методов прогноза (раздел 5.2) — по каждому кандидату на holdout-окне
CREATE TABLE model_backtest_results (
    backtest_id                  BIGSERIAL PRIMARY KEY,
    product_size_id                 INT NOT NULL REFERENCES product_sizes(product_size_id),
    backtest_date                     DATE NOT NULL,
    model_type                          TEXT NOT NULL,   -- smoothing / tsb / croston_sba / seasonal_naive / gradient_boosting / deepar
    wape_holdout                          NUMERIC(6,3) NOT NULL,
    selected                                BOOLEAN DEFAULT FALSE  -- true у метода-победителя турнира
);

-- Степень достоверности рекомендации (раздел 5.8) — композитный индикатор в процентах для страницы
-- поставок (раздел 6), каждый штраф хранится отдельно для прозрачности, не просто итоговое число
CREATE TABLE recommendation_confidence (
    product_size_id                       INT NOT NULL REFERENCES product_sizes(product_size_id),
    computed_at                              TIMESTAMP NOT NULL DEFAULT now(),
    wape_penalty                               NUMERIC(5,2),   -- min(WAPE_%, 40)
    demand_pattern_penalty                       NUMERIC(5,2), -- Smooth=0/Irregular=5/Intermittent=10/Lumpy=20
    short_history_penalty                          NUMERIC(5,2), -- новинка=25/короче сезонного цикла=10/иначе 0
    model_disagreement_penalty                       NUMERIC(5,2), -- |победитель-DeepAR|/среднее × 30, макс 20
    recent_deviation_penalty                           NUMERIC(5,2), -- warning=10/critical=20/recalibration=10
    stockout_share_penalty                               NUMERIC(5,2), -- доля is_stockout_corrected × 15
    confidence_pct                                         NUMERIC(5,2) NOT NULL, -- 100 - Σ штрафов, floor 0
    explanation_text                                          TEXT,     -- человекочитаемый список сработавших
                                                                           -- штрафов через запятую (раздел 5.8/6),
                                                                           -- "Все факторы в норме" если пусто
    PRIMARY KEY (product_size_id, computed_at)
);

-- Fill rate (Type 2 service level, раздел 5.8) — доля фактически закрытого спроса,
-- метрика мониторинга, отдельно от z (cycle service level, используется для расчёта буфера)
CREATE TABLE fill_rate_log (
    product_size_id                       INT NOT NULL REFERENCES product_sizes(product_size_id),
    period_start                             DATE NOT NULL,
    period_end                                 DATE NOT NULL,
    demand_qty_total                             NUMERIC(10,2),
    demand_qty_fulfilled                           NUMERIC(10,2),
    fill_rate_pct                                    NUMERIC(6,3),
    computed_at                                        TIMESTAMP DEFAULT now(),
    PRIMARY KEY (product_size_id, period_start, period_end)
);

-- Надёжность поставщика (раздел 5.9) — факт vs заявленный lead time по каждой поставке
CREATE TABLE supplier_deliveries (
    delivery_id                           BIGSERIAL PRIMARY KEY,
    supplier_id                              INT NOT NULL REFERENCES suppliers(supplier_id),
    ordered_date                               DATE NOT NULL,
    expected_ready_date                          DATE NOT NULL,   -- по номинальному lead_time из справочника
    actual_ready_date                              DATE,
    on_time                                          BOOLEAN       -- actual_ready_date <= expected_ready_date
);

-- Ценовая эластичность (раздел 5.10) — по модели, с fallback на категорию при нехватке истории
CREATE TABLE price_elasticity (
    scope_type                    TEXT NOT NULL,             -- 'model' / 'category'
    scope_id                        TEXT NOT NULL,            -- product_model_id либо название категории
    method                            TEXT DEFAULT 'log_log_regression', -- log_log_regression / gradient_boosting_monotone
    elasticity_coefficient            NUMERIC(6,4) NOT NULL,   -- b из ln(спрос) = a + b×ln(цена) (для log-log метода)
    r_squared                           NUMERIC(4,3),          -- качество оценки
    sample_size                           INT,                 -- кол-во точек с разной ценой, использованных
    computed_at                             TIMESTAMP DEFAULT now(),
    PRIMARY KEY (scope_type, scope_id)
);

-- Сценарии "что если цена изменится" (раздел 5.10) — просчитанные и применённые
CREATE TABLE price_scenario_log (
    scenario_id                   BIGSERIAL PRIMARY KEY,
    product_size_id                  INT NOT NULL REFERENCES product_sizes(product_size_id),
    price_change_pct                   NUMERIC(6,2) NOT NULL,   -- гипотетическое %Δцены
    predicted_demand_change_pct          NUMERIC(6,2),
    predicted_margin_impact                NUMERIC(10,2),
    applied                                  BOOLEAN DEFAULT FALSE, -- решение реально применено, не только просчитано
    created_at                                 TIMESTAMP DEFAULT now()
);

-- A/B-тест тестового повышения цены (раздел 5.10) — difference-in-differences с контрольной группой похожих SKU,
-- т.к. на маркетплейсе нельзя случайно разделить покупателей на группы
CREATE TABLE price_ab_test (
    test_id                        BIGSERIAL PRIMARY KEY,
    treatment_product_size_id         INT NOT NULL REFERENCES product_sizes(product_size_id),
    control_product_size_ids            INT[] NOT NULL,      -- похожие SKU (product_analogs / тот же сегмент), цена не менялась
    scenario_id                           BIGINT REFERENCES price_scenario_log(scenario_id), -- прогноз до теста
    test_start                              DATE NOT NULL,
    test_end                                  DATE NOT NULL,
    demand_change_pct_treatment                 NUMERIC(6,2),  -- Δспрос тестовой группы
    demand_change_pct_control                     NUMERIC(6,2),-- Δспрос контрольной группы за тот же период
    diff_in_diff_effect                             NUMERIC(6,2), -- разница — эффект, очищенный от общего шума периода
    decision                                          TEXT,     -- rollback / keep / extend
    created_at                                          TIMESTAMP DEFAULT now()
);

-- Оптимизация цен по всей линейке (раздел 5.11) — один запуск решает задачу сразу для группы связанных SKU
CREATE TABLE price_optimization_run (
    run_id                        BIGSERIAL PRIMARY KEY,
    run_date                        DATE NOT NULL,
    scope                             TEXT,               -- напр. название категории или 'all'
    objective_value_before              NUMERIC(14,2),     -- суммарная маржа линейки до оптимизации
    objective_value_after                 NUMERIC(14,2),   -- суммарная маржа по найденному решению
    status                                  TEXT DEFAULT 'completed', -- completed / infeasible / error
    created_at                                TIMESTAMP DEFAULT now()
);

-- Результат оптимизации по каждому SKU, входящему в запуск (раздел 5.11)
CREATE TABLE price_optimization_result (
    run_id                        BIGINT NOT NULL REFERENCES price_optimization_run(run_id),
    product_size_id                 INT NOT NULL REFERENCES product_sizes(product_size_id),
    current_price                     NUMERIC(10,2),
    recommended_price                   NUMERIC(10,2),
    price_change_pct                      NUMERIC(6,2),
    predicted_margin_impact                 NUMERIC(10,2),
    predicted_demand_impact_pct               NUMERIC(6,2),
    stock_check_passed                          BOOLEAN DEFAULT TRUE, -- FALSE = остаток_FBO+в_пути < прогноз на
                                                                        -- период акции при снижении цены (5.11)
    included_in_recommendations                 BOOLEAN DEFAULT FALSE, -- передан в pricing_recommendations
    PRIMARY KEY (run_id, product_size_id)
);

-- Ручные корректировки прогноза (раздел 6) — расчётное значение никогда не перезаписывается,
-- только добавляется override поверх; история видна целиком
CREATE TABLE forecast_overrides (
    override_id                   BIGSERIAL PRIMARY KEY,
    product_size_id                  INT NOT NULL REFERENCES product_sizes(product_size_id),
    period_start                       DATE NOT NULL,
    period_end                           DATE NOT NULL,
    original_forecast_qty                  NUMERIC(10,2),   -- расчётное значение на момент override
    override_qty                             NUMERIC(10,2) NOT NULL,
    reason                                      TEXT NOT NULL,
    overridden_by                                 TEXT NOT NULL,
    created_at                                      TIMESTAMP DEFAULT now()
);


-- ================= 4. ВЫХОДНЫЕ РЕКОМЕНДАЦИИ =================

CREATE TABLE shipment_recommendations (
    recommendation_id                     BIGSERIAL PRIMARY KEY,
    product_size_id                          INT NOT NULL REFERENCES product_sizes(product_size_id),
    stock_1c_qty_snapshot                       NUMERIC(10,2),  -- остаток на ЦС на момент формирования (раздел 6) —
                                                                  -- снимок, не живой join, чтобы Excel-выгрузка не
                                                                  -- "уезжала" при повторном обращении к stock_1c
    stock_fbo_qty_snapshot                        NUMERIC(10,2), -- остаток на FBO на тот же момент, сумма по
                                                                    -- всем warehouse_code (из stock_fbo)
    qty_to_ship                                 NUMERIC(10,2) NOT NULL,  -- рекомендация системы
    confidence_pct                                NUMERIC(5,2),          -- снимок из recommendation_confidence
                                                                            -- на момент формирования (раздел 5.8/6)
    confidence_explanation                          TEXT,                 -- снимок explanation_text, тот же момент
    confirmed_qty                                 NUMERIC(10,2),         -- "подтверждено к поставке" (раздел 6) —
                                                                            -- пустое по умолчанию, заполнение переводит
                                                                            -- строку в status='confirmed' и включает её
                                                                            -- в выгрузку "Собрать поставку"
    related_fbo_shipment_id                       TEXT,          -- если это корректировка уже созданной поставки
    correction_pct_vs_original                     NUMERIC(6,2), -- % отличия от исходного плана, если корректировка
    penalty_risk                                     BOOLEAN DEFAULT FALSE, -- TRUE если |correction_pct| > 15%
                                                                              -- и близко к плановой дате (раздел 6)
    target_ship_date                              DATE,
    status                                          TEXT DEFAULT 'pending', -- pending/confirmed/sent_to_edo/shipped
                                                                              -- confirmed = confirmed_qty заполнено
    created_at                                        TIMESTAMP DEFAULT now()
);

CREATE TABLE purchase_recommendations (
    recommendation_id                     BIGSERIAL PRIMARY KEY,
    product_size_id                          INT NOT NULL REFERENCES product_sizes(product_size_id),
    -- qty_to_purchase считается от central_stock_aggregate_demand (раздел 5), не только от спроса Lamoda —
    -- ЦС питает WB/Ozon/Яндекс Маркет (FBS) напрямую, плюс FBO-поставки Lamoda и собственный сайт/розницу
    supplier_id                                INT REFERENCES suppliers(supplier_id),
    qty_to_purchase                              NUMERIC(10,2) NOT NULL,
    ready_by_date                                  DATE,
    status                                           TEXT DEFAULT 'pending', -- pending/ordered/received
    created_at                                         TIMESTAMP DEFAULT now()
);

CREATE TABLE pricing_recommendations (
    recommendation_id                     BIGSERIAL PRIMARY KEY,
    product_size_id                          INT NOT NULL REFERENCES product_sizes(product_size_id),
    recommendation_type                        TEXT NOT NULL,   -- raise/lower/test_raise/no_change
    rationale                                    TEXT,
    margin_pct_at_calc                             NUMERIC(6,2),
    source                                           TEXT DEFAULT 'rule', -- rule (5.7) / optimization (5.11)
    optimization_run_id                                BIGINT REFERENCES price_optimization_run(run_id),
    status                                           TEXT DEFAULT 'pending', -- pending/approved/applied/api_failed/
                                                                               -- quarantined/suppressed/superseded
    suppressed_by_triage_id                            BIGINT REFERENCES anomaly_triage_log(triage_id),
    approved_by                                          TEXT,     -- менеджер, подтвердивший применение
    approved_at                                            TIMESTAMP,
    created_at                                       TIMESTAMP DEFAULT now()
);

-- Лог фактического применения цены через API Lamoda (раздел 6) — по каждой рекомендации, применённой менеджером
CREATE TABLE price_change_log (
    price_change_id                BIGSERIAL PRIMARY KEY,
    recommendation_id                 BIGINT NOT NULL REFERENCES pricing_recommendations(recommendation_id),
    product_size_id                     INT NOT NULL REFERENCES product_sizes(product_size_id),
    old_price                             NUMERIC(10,2),
    new_price                               NUMERIC(10,2) NOT NULL,
    api_method                                TEXT DEFAULT 'POST /v2/nomenclature-price', -- v1-эквивалент
                                                                                              -- v1.nomenclature.update-price
                                                                                              -- отключается 01.01.27
    api_response_status                         TEXT,           -- success / error / quarantined / validation_failed /
                                                                   -- fraud_rejected (раздел 6 — validation_failed =
                                                                   -- медиана за 30 дней, fraud_rejected = отдельная
                                                                   -- фрод-проверка, обе внешние проверки Lamoda)
    fraud_validation_result                       TEXT,           -- сырое значение fraudValidationResult из ответа
    api_error_message                             TEXT,
    submitted_at                                    TIMESTAMP DEFAULT now()
);


-- ================= Индексы для частых запросов =================

CREATE INDEX idx_sales_daily_date ON sales_daily (sale_date);
CREATE INDEX idx_stock_fbo_date ON stock_fbo (snapshot_date);
CREATE INDEX idx_demand_forecast_period ON demand_forecast (period_start, period_end);
CREATE INDEX idx_margin_calc_date ON margin_calc (calc_date);
CREATE INDEX idx_order_items_size_date ON order_items (product_size_id, event_date);
