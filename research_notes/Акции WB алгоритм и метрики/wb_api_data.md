# Данные WB API для измерения эффекта цен и акций по карточке (nmID): каталог методов (состояние на 17.10.2026)

> Методика. Основной источник — официальные OpenAPI-спецификации WB, скачанные 17.10.2026 напрямую:
> `https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml` (Аналитика и данные), `12-reports.yaml` (Отчёты, в т.ч. statistics-api),
> `13-finances.yaml` (Документы и бухгалтерия), `08-promotion.yaml` (Маркетинг и продвижение), `02-items.yaml` (Работа с товарами), `01-general.yaml` (Общее).
> Снимок `github.com/ZloyDeDD/wb-api-skill/swagger/*.yaml` скачан там же и сверен diff-ом: отличия только косметические («аккаунт продавца» → «профиль продавца», пустые строки), по сути методов снимок совпадает с официальной спецификацией.
> Даты изменений — из официального Telegram-канала команды WB API `t.me/wb_api_notifications` (посты подписаны «команда WB API»; дата — дата поста). Страницы `dev.wildberries.ru/release-notes`, `/news` отдают HTTP 498 (антибот), поэтому сами тексты журнала изменений прочитать не удалось.
> Таблица лимитов в каждом методе дана по типам токенов: «Персональный | Сервисный | Базовый с секретом | Базовый». Ниже лимит «П/С» = персональный и сервисный (у «базового с секретом» в тех же таблицах те же значения), «Б» = базовый.

## 1. «Воронка продаж» (sales funnel) и аналитика продавца CSV

### Takeaway
Старые `/api/v2/nm-report/detail` и `/detail/history` (v2) отключены 9 декабря 2025; сейчас воронка — три метода v3 на `seller-analytics-api.wildberries.ru` (`/api/analytics/v3/sales-funnel/products`, `.../products/history`, `.../grouped/history`). Метрики: переходы в карточку, корзины, заказы (шт/₽), выкупы, отмены+возвраты, средняя цена, «Отложенные», конверсии, WB Клуб; **показов (impressions) в воронке нет**. JSON-история по дням — только за последнюю неделю; история до 365 дней — только CSV-отчётом `DETAIL_HISTORY_REPORT`, доступным лишь с подпиской «Джем». Данные обновляются раз в 2 часа (с 24.09.2026), выкупы/отмены/возвраты записываются в день ЗАКАЗА.

### Cited Findings
**Статус/история версий**
- 30.10.2025 WB объявил новую версию «Воронки продаж v3» и что «текущая версия v2 будет отключена 9 декабря» (повтор 05.12.2025) — [t.me/wb_api_notifications/297](https://t.me/wb_api_notifications/297), [/316](https://t.me/wb_api_notifications/316).
- Ранее, 21.04.2025: метод `/nm-report/grouped` («Статистика групп карточек товаров за период») отключён 29.04.2025 — [t.me/wb_api_notifications/207](https://t.me/wb_api_notifications/207).
- В текущей спецификации путей `/api/v2/nm-report/detail*` нет; есть только `/api/analytics/v3/sales-funnel/*` и CSV `/api/v2/nm-report/downloads*` — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml) (скачан 17.10.2026).

**`POST https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products` — «Статистика карточек товаров за период»** — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml)
- Отчёт сравнивает `selectedPeriod` с `pastPeriod`; «Можно получить отчёт максимум за последние 365 дней»; если начало `pastPeriod` раньше года назад, оно принудительно сдвигается на «текущая дата — 365 дней».
- Фильтры: `nmIds` (до 1000), `brandNames`, `subjectIds`, `tagIds`, `skipDeletedNm`; сортировка `orderBy.field` (openCard, addToCart, orderCount, orderSum, buyoutCount, buyoutSum, cancelCount, cancelSum, avgPrice, stockMpQty, stockWbQty, shareOrderPercent, addToWishlist, timeToReady…); пагинация `limit` (по умолчанию 50, максимум 1000) + `offset`. Предел 1000 карточек в запросе введён с 08.12.2025 — [t.me/wb_api_notifications/312](https://t.me/wb_api_notifications/312).
- Поля ответа `statistic.selected` / `statistic.past`: `openCount` (переходы в карточку), `cartCount`, `orderCount`, `orderSum`, `buyoutCount`, `buyoutSum`, `cancelCount`, `cancelSum` (отменили и вернули), `avgPrice`, `avgOrdersCountPerDay`, `shareOrderPercent` (доля в выручке), `addToWishlist`, `timeToReady {days,hours,mins}` (среднее время доставки), `localizationPercent` (сейчас может быть только 100 — см. release-notes?id=570), `wbClub {orderCount, orderSum, buyoutSum, buyoutCount, cancelSum, cancelCount, avgPrice, buyoutPercent, avgOrderCountPerDay}`, `conversions {addToCartPercent, cartToOrderPercent, buyoutPercent}`; блок `comparison` с `*Dynamic` (в т.ч. `avgPriceDynamic` — «Учитываются скидки для акций»).
- Карточка `product`: `nmId, title, vendorCode, brandName, subjectId, subjectName, tags, productRating, feedbackRating, stocks {wb, mp, balanceSum}` (остатки на текущий день).
- Лимит: П/С — 3 запроса/мин (интервал 20 с, всплеск 3); Б — 2 запроса/час.

**`POST .../api/analytics/v3/sales-funnel/products/history` — «Статистика карточек товаров по дням»** — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml)
- «Можно получить данные максимум за последнюю неделю»; `nmIds` обязателен, 1–20 артикулов; `aggregationLevel` = `day` (по умолчанию) или `week`.
- Поля `history[]`: `date, openCount, cartCount, orderCount, orderSum, buyoutCount, buyoutSum, buyoutPercent, addToCartConversion, cartToOrderConversion, addToWishlistCount`; плюс `currency`.
- Прямая оговорка в описании: «Чтобы получать отчёты за период до года, используйте методы Аналитика продавца CSV — тип `DETAIL_HISTORY_REPORT`. Отчёты этого типа доступны только с подпиской Джем».
- Лимит тот же: П/С 3/мин, Б 2/час.
- `POST .../sales-funnel/grouped/history` — то же по группам (предмет/бренд/ярлык), тоже «максимум за последнюю неделю», `day`/`week`; для года — CSV `GROUPED_HISTORY_REPORT` (только с «Джем»).

**Общие оговорки по свежести данных (во всех трёх методах)** — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml)
- «Данные отчёта обновляются 1 раз в 2 часа»; «В течение часа после события появляется большая часть данных о заказах, о переходах в карточку товара, о добавлениях товаров в корзину. Малая часть этих данных может появляться в течение нескольких дней».
- «Выкупы, отмены и возвраты отображаются в отчёте за тот день, когда товар был заказан» (пример: заказ 1 января, возврат 10 января → возврат попадёт в отчёт за 1 января); «Окончательные итоги продаж вы можете отслеживать с помощью детализаций к отчётам реализации».
- Обновление раз в 2 часа введено с 24.09.2026 для products, products/history, grouped/history, DETAIL_HISTORY_REPORT, GROUPED_HISTORY_REPORT; «Чтобы получить текущие данные без задержки обновления, используйте метод `POST /api/analytics/v1/order-feed`» — [t.me/wb_api_notifications/473](https://t.me/wb_api_notifications/473).

**CSV: `POST/GET https://seller-analytics-api.wildberries.ru/api/v2/nm-report/downloads`, `/downloads/retry`, `/downloads/file/{downloadId}`** — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml)
- Типы отчётов: `DETAIL_HISTORY_REPORT` (воронка по nmID), `GROUPED_HISTORY_REPORT` (по предметам/брендам/ярлыкам), `SEARCH_QUERIES_PREMIUM_REPORT_GROUP`, `SEARCH_QUERIES_PREMIUM_REPORT_PRODUCT`, `SEARCH_QUERIES_PREMIUM_REPORT_TEXT`, `STOCK_HISTORY_REPORT_CSV`, `STOCK_HISTORY_DAILY_CSV`. Отчёты по остаткам «можно создать без подписки Джем».
- `DETAIL_HISTORY_REPORT` params: `nmIDs` (до 1000, пусто = все), `subjectIds`, `brandNames`, `tagIds`, `startDate`, `endDate`, `timezone` (IANA, по умолчанию Europe/Moscow), `aggregationLevel` = day/week/month, `skipDeletedNm`; ID отчёта — UUID, генерирует продавец.
- Колонки CSV воронки: `nmID, dt, openCardCount, addToCartCount, ordersCount, ordersSumRub, buyoutsCount, buyoutsSumRub, cancelCount, cancelSumRub, addToCartConversion, cartToOrderConversion, buyoutPercent, addToWishlist, currency`.
- Готовый файл: «Можно получить отчёт, который сгенерирован за последние 48 часов», ZIP с CSV.
- Лимит создания отчёта: П/С 3/мин; Б — 1 запрос/час.

**402 «Требуется платёж»** — во всех аналитических методах; по схеме ответа ошибка «возвращается только сервисам из Каталога решений для бизнеса» (пример: `wb solution for business has insufficient funds on its balance`) — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml).

### Inferences
- Для истории воронки по дням без «Джем» единственный путь — ежедневно (или чаще) снимать `products/history` за последние 7 дней по пачкам ≤20 nmID и копить у себя; при 3 запросах/мин это ~60 nmID/мин, т.е. 1000 карточек ≈ 17 минут на один проход. Альтернатива — `products` с `selectedPeriod` = один день (до 1000 nmID на страницу) на каждый день за 365 дней — это тоже даёт дневную гранулярность, но ценой одного запроса на день×страницу (вывод из параметров, WB так прямо не рекомендует).
- Поскольку выкупы/отмены/возвраты привязаны к дате заказа, `buyoutCount`/`cancelCount` за последние дни будут дорастать неделями (срок доставки + возврат); для оценки эффекта акции на выкуп нужно перезабирать хвост окна (минимум 7 дней доступно в JSON) и/или сверять с финансовой детализацией.
- Воронка не даёт показов (impressions) — CTR «показ → карточка» для органики через API не посчитать; ближайшие заменители — «видимость» и средняя позиция из отчёта по поисковым запросам (раздел 2) и `views` из рекламной статистики (раздел 5).
- Почасовой гранулярности у воронки нет (только day/week/month); почасовые заказы можно собрать только из Ленты заказов (раздел 3) по `createdAt`.

### Gaps
- Не удалось прочитать полный текст журнала изменений на dev.wildberries.ru (HTTP 498), поэтому соответствие старых полей v2 (`openCardCount`, `addToCartCount`, …) новым полям v3 подтверждено только по CSV-описанию и по текущей схеме, без официальной таблицы маппинга.
- Неизвестно, даёт ли v3 `products` корректные данные при запросе однодневных периодов далеко в прошлом (до 365 дней) без «Джем» — в спецификации ограничения нет, но и подтверждения на практике не найдено.

## 2. Поисковые запросы, позиции и видимость карточки

### Takeaway
Раздел «Поисковые запросы по вашим товарам» (`/api/v2/search-report/*` на `seller-analytics-api.wildberries.ru`) даёт по nmID: тексты запросов, их частоту, среднюю и медианную позицию, «видимость» (%), переходы/корзины/заказы из поиска, конверсии, мин/макс цену, признак рекламы; по дням — позицию и заказы по каждому запросу (окно ≤7 дней, глубина ≤365 дней). WB прямо пишет, что эти методы доступны только с подпиской «Джем»; лимит 30 запросов на товар (100 — для тарифов «Продвинутый»/«Премиальный»). Обновление — раз в 2 часа с 12.10.2026.

### Cited Findings
- Методы: `POST /api/v2/search-report/report` (основная страница), `/table/groups`, `/table/details`, `/product/search-texts`, `/product/orders` — все на `https://seller-analytics-api.wildberries.ru` — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml).
- Требование подписки: «Данные методы доступны только с подпиской Джем» (пост об изменениях в методах Поисковых запросов, 25.06.2025) — [t.me/wb_api_notifications/234](https://t.me/wb_api_notifications/234); то же в новости WB от 06.02.2025 (id 7373) про CSV по текстам запросов: «Эти методы доступны только с подпиской «Джем»» — пример ответа API новостей в [01-general.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/01-general.yaml).
- Лимит всех search-report методов: П/С 3 запроса/мин (интервал 20 с, всплеск 3); Б — 1 запрос/час — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml).
- Периоды: `currentPeriod.start/end` — «Не ранее 365 суток от сегодня»; `pastPeriod` — не длиннее текущего; фильтр `positionCluster` = all / firstHundred (1–100) / secondHundred (101–200) / below (201+); `includeSubstitutedSKUs` и `includeSearchTexts` не могут быть оба `false` — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml).
- `/report` (основная страница): `positionInfo.average/median` и `chartItems[] {dt, average, median}` (позиция по датам); `visibilityInfo.visibility` — «процент вероятности, что пользователь увидит карточку товара. Зависит от средней позиции», `byDay[] {dt, visibility, open}`, `byWeek`, `byMonth`; по группам — `avgPosition, openCard, addToCart, openToCart, orders, cartToOrder, visibility`; по товарам — `nmId, isAdvertised` («Находится ли товар в продвижении в Поисковой выдаче»), `isSubstitutedSKU`; `commonInfo.advertisedProducts` — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml).
- `/product/search-texts`: `nmIds` до 50; `limit` запросов на товар — максимум 30 для «стандартного» тарифа, 100 для «Продвинутого»/«Премиального» тарифов Джема; `topOrderBy` (openCard/addToCart/openToCart/orders/cartToOrder). Поля по каждому тексту: `text, nmId, frequency {current, dynamics}`, `weekFrequency`, `medianPosition` («Учитываются только те позиции, из которых пользователи добавляли товар в корзину или переходили в его карточку»), `avgPosition`, `openCard`, `addToCart`, `openToCart`, `orders`, `cartToOrder` (у части — `percentile` относительно карточек других продавцов), `visibility`, `price {minPrice, maxPrice}` (цена со скидкой продавца, без WB Клуба), `rating`, `feedbackRating` — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml).
- `/product/orders`: по одному `nmId` и `searchTexts` (до 30; до 100 для Продвинутого/Премиального), период «Максимум 7 суток», глубина «максимум за последние 365 дней»; ответ — по дням: `total[] {dt, avgPosition, orders}` и по каждому тексту `frequency` + `dateItems[] {dt, avgPosition, orders}` — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml).
- CSV `SEARCH_QUERIES_PREMIUM_REPORT_GROUP/PRODUCT/TEXT` — те же метрики (AveragePosition, OpenCard, AddToCart, OpenToCart, Orders, CartToOrder, Visibility и *Past) — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml).
- С 12.10.2026 из CSV `SEARCH_QUERIES_PREMIUM_REPORT_TEXT` удалены пять полей `*Percentile` (OpenCardPercentile, AddToCartPercentile и др.) — [t.me/wb_api_notifications/476](https://t.me/wb_api_notifications/476); в JSON-схеме `/product/search-texts` поля `percentile` на 17.10.2026 ещё описаны — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml).
- С 12.10.2026 данные поисковых запросов обновляются раз в 2 часа — [t.me/wb_api_notifications/483](https://t.me/wb_api_notifications/483).
- Рекламная позиция по кластерам — отдельно в API продвижения: `POST /adv/v1/normquery/stats` даёт `avgPos` по дням для пары кампания+nmID (см. раздел 5) — [08-promotion.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/08-promotion.yaml).

### Inferences
- «Видимость» и средняя позиция — единственная доступная через API органическая метрика «до клика»; для оценки эффекта акции (ранжирование/буст в поиске) это главный индикатор, но он требует «Джем».
- Дневную позицию по конкретным запросам можно накапливать только окнами по 7 дней через `/product/orders` (по одному nmID за запрос, 3 запроса/мин) — для большого каталога это дорого; разумно ограничиться топ-запросами ключевых карточек.

### Gaps
- Не подтверждено, что «стандартный» тариф в описании `limit` — это базовый тариф Джема, а не работа без подписки; сочетание текста спецификации («для стандартного тарифа» со ссылкой на `monetization/tariffs`) и постов WB («только с подпиской Джем») говорит в пользу первого, но официальной таблицы тарифов не найдено.
- Неизвестна методика расчёта «видимости» сверх фразы «зависит от средней позиции».

## 3. Заказы, продажи и финансы: statistics-api, Лента заказов, детализация реализации

### Takeaway
Оперативные заказы/продажи по единицам: `GET statistics-api.wildberries.ru/api/v1/supplier/orders` и `/sales` (обновление раз в 30 мин, хранение 90 дней, поля `totalPrice, discountPercent, spp, finishedPrice, priceWithDisc, forPay, isCancel`) — работают, но WB объявил их будущее отключение и предлагает замену — `POST /api/analytics/v1/order-feed` (реальное время, 31 день, статусы и причины отмен, но **без spp/finishedPrice**, только `sellerPrice`). Финальные деньги — новая детализация реализации на `finance-api.wildberries.ru` (`POST /api/finance/v1/sales-reports/detailed`, данные с 29.01.2024, daily/weekly, комиссия/кВВ, скидка за акцию, логистика, эквайринг, СПП). Старый `GET /api/v5/supplier/reportDetailByPeriod` — отключение назначено на 15 июля (2026) и из спецификации он уже убран.

### Cited Findings
**`GET https://statistics-api.wildberries.ru/api/v1/supplier/orders` — «Заказы»** — [12-reports.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/12-reports.yaml)
- «Данные обновляются раз в 30 минут»; «1 строка = 1 заказ = 1 сборочное задание = 1 единица товара»; идентификатор — `srid`; «Информация о заказе хранится 90 дней с момента оформления»; «Данные этого отчёта являются предварительными и служат для оперативного мониторинга»; могут отсутствовать заказы с неподтверждённой оплатой (отложенные платежи, рассрочка) — для всех заказов WB отсылает к Ленте заказов.
- Параметры: `dateFrom` (RFC3339), `flag` (0 — по `lastChangeDate`, лимит ~80 000 строк на ответ, далее листать по `lastChangeDate` последней строки; 1 — все заказы за дату `date`).
- Поля: `date, lastChangeDate, warehouseName, warehouseType, countryName, oblastOkrugName, regionName, supplierArticle, nmId, barcode, category, subject, brand, techSize, incomeID, isSupply, isRealization, totalPrice` (цена без скидок), `discountPercent` (скидка продавца, %), `spp` (Скидка WB, %), `finishedPrice` (цена с учётом всех скидок, кроме суммы по WB Кошельку), `priceWithDisc` (цена со скидкой продавца, в т.ч. со скидкой WB Клуба), `isCancel`, `cancelDate`, `sticker`, `gNumber`, `srid`.
- Лимит: П/С 1 запрос/мин (всплеск 10); Б — 1 запрос/3 ч.

**`GET https://statistics-api.wildberries.ru/api/v1/supplier/sales` — «Продажи»** — [12-reports.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/12-reports.yaml)
- Продажи и возвраты; обновление раз в 30 минут; хранение 90 дней; предварительные данные.
- Оговорки: «Значения полей `priceWithDisc` и `forPay` рассчитываются по упрощённой логике и могут отличаться от `retail_price_withdisc_rub` и `ppvz_for_pay`» в детализации; «Поля `finishedPrice`, `priceWithDisc`, `forPay` могут временно иметь значение `0`: данные заполняются асинхронно, актуализируются в течение 24 часов»; «Для точных финансовых расчётов… используйте детализации к отчётам реализации».
- Поля (сверх общих с заказами): `paymentSaleAmount` (скидка за оплату WB Кошельком, ₽), `forPay` (к перечислению продавцу), `finishedPrice`, `priceWithDisc`, `spp`, `saleID` (`S…` — продажа, `R…` — возврат).
- Лимит: П/С 1/мин (всплеск 1); Б — 1 запрос/2 ч.

**`POST https://seller-analytics-api.wildberries.ru/api/analytics/v1/order-feed` — «Лента заказов»** — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml)
- «Данные отчёта обновляются в режиме реального времени»; «Можно получить отчёт максимум за последние 31 день»; заказы отдаются по времени текущего статуса, от нового к старому; пагинация `limit` (до 10 000) + `offset` в рамках одного `snapshotTime`.
- Поля: `nmId, chrtId, srid, createdAt, updatedAt, status` (created / buyout / cancel / return / returnDefective), `cancelType` (app — отказ до получения, receipt — при получении, expire — истёк срок, other — техническая), `warehouseName`, `warehouseRegion`, `isMp`, `destinationCity`, `destinationDistrict`, `sellerPrice` («Цена продавца со скидкой продавца (без учёта скидки WB Клуба и оптовой скидки для B2B-продаж)»), `isB2b`.
- Лимит: П/С 1 запрос/мин; Б — 1 запрос/3 ч.
- Анонс 06.08.2026: Лента заказов — замена `GET /api/v1/supplier/orders` и `/sales`; «Методы Заказы и Продажи продолжают работать, но… в будущем они будут отключены. О дате отключения сообщим заранее»; преимущества: реальное время, заказы с отложенной оплатой, статусы и причины отмен, тип продажи B2B/B2C; «Метод доступен по токену любого типа для категории Аналитика» — [t.me/wb_api_notifications/454](https://t.me/wb_api_notifications/454).
- 05.10.2026: `GET /api/v1/analytics/region-sale` отключается 3 ноября 2026, вместо него — Лента заказов — [t.me/wb_api_notifications/481](https://t.me/wb_api_notifications/481); в спецификации метод помечен `deprecated` — [12-reports.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/12-reports.yaml).

**Детализация реализации: `POST https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed` (+ `/sales-reports/list`, `/sales-reports/detailed/{reportId}`)** — [13-finances.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/13-finances.yaml)
- «Данные доступны с 29 января 2024 года»; параметры `dateFrom`, `dateTo`, `limit` (до 100 000), `rrdId` (пагинация, начинать с 0), `period` = `weekly` (по умолчанию) | `daily`, `fields[]` (выбор возвращаемых полей); ответ 204 при отсутствии данных.
- Лимит: П/С 1 запрос/мин; Б — 2 запроса/24 ч. Методы `/list` и `/detailed/{reportId}` — только персональный и сервисный токены (`x-token-types`).
- Поля, важные для цены/акции: `retailPrice`, `retailPriceWithDisc` (с учётом согласованной скидки), `retailAmount`, `salePercent`, `productDiscountForReport`, `spp` («Платформенные скидки, %»), `commissionPercent` (кВВ, %), `kvwBase`, `kvw`, `supRatingUp` (снижение кВВ из-за рейтинга), `isKgvpV2` («Размер снижения кВВ из-за акции, %»), `ppvzSalesCommission`, `forPay`, `vw`, `vwNds`, `acquiringFee`, `acquiringPercent`, `paymentProcessing`, `acquiringBank`, `deliveryService` (логистика), `rebillLogisticCost`, `paidStorage`, `paidAcceptance`, `penalty`, `deduction`, `additionalPayment`, `sellerPromo` (промокод, %), `sellerPromoId`, `sellerPromoDiscount` (собственная акция продавца), `loyaltyId`, `loyaltyDiscount`, `uuidPromocode`, `salePricePromocodeDiscountPrc`, `articleSubstitution`, `salePriceAffiliatedDiscountPrc`, `salePriceWholesaleDiscountPrc`, `wibesDiscountPercent`, `installmentCofinancingAmount`, `cashbackAmount`, `cashbackDiscount`, `cashbackCommissionChange`, `deliveryMethod`, `warehouseLogisticsCoeff`, `orderDt`, `saleDt`, `rrDate`, `docTypeName`, `sellerOperName`, `bonusTypeName`, `isB2b`, `srid`, `orderUid`, `nmId`, `sku`, `title`.
- Отдельные отчёты по эквайрингу: `POST /api/finance/v1/acquiring/list`, `/acquiring/detailed`, `/acquiring/detailed/{reportId}` — [13-finances.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/13-finances.yaml).
- Новые методы и отключение старого: «Добавили новую версию метода получения детализаций… `POST /api/finance/v1/sales-reports/detailed`» (пост от 07.07.2026) — [t.me/wb_api_notifications/442](https://t.me/wb_api_notifications/442); по данным Авошоп (15.04.2026): поля переведены в camelCase, денежные поля стали строками, добавлено `title`, убраны `suppliercontract_code` и `ppvz_supplier_id`, а «текущую версию `GET /api/v5/supplier/reportDetailByPeriod` мы отключим 15 июля» — [avoshop.ru](https://avoshop.ru/company/news/2026/wildberries_novye_api_metody_otchyetov_realizatsii/). Расхождение дат публикации (апрель у Авошоп vs июль в канале) — возможно, повторный пост в канале; дата отключения в обоих — 15 июля.
- В текущей спецификации [12-reports.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/12-reports.yaml) метода `reportDetailByPeriod` нет (есть только orders и sales на statistics-api).
- Хронология полей детализации (до перехода): собственные акции продавца с доп. скидкой — 30.12.2025 ([/333](https://t.me/wb_api_notifications/333)); скидки лояльности продавца — 13.01.2026 ([/335](https://t.me/wb_api_notifications/335)); скидки за промокоды — 20.01.2026 ([/340](https://t.me/wb_api_notifications/340)); стоимость участия в программе лояльности — 22.09.2025 ([/276](https://t.me/wb_api_notifications/276)); с 14.07.2025 семь полей переданы в процентах, а не в долях ([/235](https://t.me/wb_api_notifications/235)); КПП B2B, номер/дата УПД — 28.09.2026 ([/474](https://t.me/wb_api_notifications/474)).

### Inferences
- После отключения `/supplier/orders` и `/supplier/sales` построчный `spp` (Скидка WB, «СПП») и `finishedPrice` на уровне заказа останутся только в финансовой детализации (по факту продажи/возврата, daily/weekly), а Лента заказов даёт лишь цену продавца со скидкой продавца. Если программа хочет мерить «цену для покупателя» по дням заказа, данные `spp`/`finishedPrice` нужно копить уже сейчас, пока orders/sales работают.
- Для измерения акции: `isKgvpV2` (снижение кВВ из-за акции) и `sellerPromoId/sellerPromoDiscount` в детализации — единственные финансовые поля, явно связывающие строку продажи с акцией.
- Заказы в Ленте (31 день) + орд/продажи (90 дней) + детализация (с 29.01.2024) — три горизонта; глубокую историю по заказам/ценам до 90 дней назад можно выгрузить один раз из orders/sales, дальше — только из детализации.

### Gaps
- Не проверено, отвечает ли `GET /api/v5/supplier/reportDetailByPeriod` после 15.07.2026 (спецификация его не содержит; живого запроса не делали).
- Дата отключения `/supplier/orders` и `/supplier/sales` не объявлена (на 17.10.2026).
- Таблица маппинга старых snake_case полей детализации на новые camelCase находится в журнале изменений dev.wildberries.ru, недоступном для чтения (HTTP 498).

## 4. История цен, загрузки цен и календарь акций

### Takeaway
WB API не отдаёт историю изменения цен как таковую: `GET /api/v2/list/goods/filter` (и POST-вариант, и `/list/goods/size/nm`) возвращает только ТЕКУЩИЕ цену, скидку продавца, цену со скидкой и скидку WB Клуба; история есть лишь по собственным загрузкам — `GET /api/v2/history/tasks` и `/api/v2/history/goods/task` по известному `uploadID` (метода «список всех загрузок» нет). СПП в ценовом API отсутствует. Календарь акций (`dp-calendar-api.wildberries.ru`) даёт список акций с датами, детали (`ranging` с `boost`, `participationPercentage`, счётчики товаров) и по каждой акции — текущую и плановую цену/скидку товара; история участия не хранится.

### Cited Findings
**Цены и скидки (`https://discounts-prices-api.wildberries.ru`)** — [02-items.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/02-items.yaml)
- Методы: `POST /api/v2/upload/task` (установить цены и скидки), `/upload/task/size`, `/upload/task/club-discount`, `POST /api/discounts-prices/v1/upload/task/b2b/wholesale`; `GET /api/v2/history/tasks`, `GET /api/v2/history/goods/task` (обработанные загрузки), `GET /api/v2/buffer/tasks`, `/buffer/goods/task` (необработанные), `GET/POST /api/v2/list/goods/filter`, `GET /api/v2/list/goods/size/nm`, `GET /api/v2/quarantine/goods`.
- `GET /api/v2/list/goods/filter`: `limit` до 1000 + `offset`, `filterNmID`; поля `nmID, vendorCode, sizes[] {sizeID (=chrtID), price, discountedPrice, clubDiscountedPrice, techSizeName}, currencyIsoCode4217, discount, clubDiscount, editableSizePrice, wholesaleDiscountThreshold[]`.
- `GET /api/v2/history/tasks?uploadID=…`: `status` (3 — обработана без ошибок; 4 — отменена; 5 — с ошибками…), `uploadDate`, `activationDate`, `overAllGoodsNumber`, `successGoodsNumber`.
- `GET /api/v2/history/goods/task?uploadID=…&limit&offset`: по товару `nmID, vendorCode, sizeID, techSizeName, price, currencyIsoCode4217, discount, clubDiscount, status` (2 — обновилась, 3 — ошибка), `errorText` (напр. «New price is several times lower than the current price. Item has been moved to Price Quarantine»).
- `GET /api/v2/quarantine/goods`: `newPrice, oldPrice, newDiscount, oldDiscount, priceDiff`; для поразмерных цен карантин не применяется.
- Лимит на ВСЕ методы категории «Цены и скидки»: П/С 10 запросов/6 с (интервал 600 мс, всплеск 5); Б — 4 запроса/час.

**Календарь акций (`https://dp-calendar-api.wildberries.ru`)** — [08-promotion.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/08-promotion.yaml)
- `GET /api/v1/calendar/promotions` (`startDateTime`, `endDateTime`, `allPromo` true/false, `limit` до 1000, `offset`): `id, name, startDateTime, endDateTime, type` (`regular` — акция, `auto` — автоакция).
- `GET /api/v1/calendar/promotions/details?promotionIDs=…`: `description, advantages[]` (напр. «Плашка», «Баннер», «Топ выдачи товаров»), `startDateTime, endDateTime, inPromoActionLeftovers, inPromoActionTotal, notInPromoActionLeftovers, notInPromoActionTotal, participationPercentage` («Уже участвующие в акции товары, %. Рассчитывается по товарам в акции и с остатком»), `type`, `exceptionProductsCount` (исключённые из автоакции), `ranging[] {condition (productsInPromotion | calculateProducts | allProducts), participationRate (% товаров для перехода на следующий уровень), boost («Текущий уровень поднятия в поиске, %»)}`.
- `GET /api/v1/calendar/promotions/nomenclatures?promotionID&inAction&limit&offset`: `id` (nmID), `inAction, price` (текущая розничная), `currencyCode, planPrice` («цена во время акции»), `discount, planDiscount` («Рекомендуемая скидка для участия»); «Данный метод неприменим для автоакций».
- `POST /api/v1/calendar/promotions/upload` — добавить товар в акцию.
- Лимит категории «Календарь акций»: П/С 10 запросов/6 с (всплеск 5); Б — 1 запрос/час.

### Inferences
- Чтобы иметь ряд «цена на дату» для расчёта эластичности, программа должна сама снимать `list/goods/filter` (ежедневно или после каждой своей загрузки) и сохранять `uploadID` каждой загрузки; цену, установленную в ЛК или другой системой, можно увидеть только как разницу снимков.
- Фактическую цену покупателя (после СПП) по дням надёжнее брать из `finishedPrice`/`priceWithDisc`/`spp` в заказах statistics-api и из `retailPriceWithDisc`/`spp` детализации, а не из ценового API.
- Факт участия nmID в акции на прошлую дату восстановить через API нельзя (nomenclatures показывает текущее состояние) — нужно снимать `inAction` по всем активным акциям в период их проведения. Для автоакций — только косвенно (детализация: `isKgvpV2`, изменение цены).

### Gaps
- Сколько времени WB хранит данные `history/tasks` по старым `uploadID` — в спецификации не указано.
- Нет официального описания, как `boost` из `ranging` соотносится с позицией в выдаче (кроме ссылки на справку A-385).

## 5. Рекламная статистика: отделение рекламных заказов от органики, расходы по nmID

### Takeaway
`GET https://advert-api.wildberries.ru/adv/v3/fullstats` (v3 с сентября 2025) отдаёт по кампании → по дням → по платформе → по nmID: показы, клики, CTR, CPC, CR, корзины, заказы, заказанные штуки, сумму заказов, отмены и затраты; период ≤31 день, ≤50 кампаний в запросе, 3 запроса/мин. Для поисковых кластеров — `POST /adv/v1/normquery/stats` (по дням: показы, клики, корзины, заказы, средняя позиция, расход). Фактические списания — `GET /adv/v1/upd` (по кампании, не по nmID).

### Cited Findings
- `GET /adv/v3/fullstats?ids=…&beginDate=…&endDate=…`: «Метод формирует статистику для кампаний независимо от типа. Максимальный период в запросе — 31 день. Для кампаний в статусах 7, 9 и 11»; `ids` — «максимум 50 значений». Лимит П/С 3 запроса/мин (интервал 20 с, всплеск 1); Б — 1 запрос/час — [08-promotion.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/08-promotion.yaml).
- Поля верхнего уровня и в `days[]` → `apps[]` (appType 1 — сайт, 32 — Android, 64 — iOS) → `nms[]`: `views, clicks, ctr, cpc, cr, atbs` (добавления в корзину), `orders, shks` (заказанные штуки), `sum` (затраты), `sum_price` (заказов на сумму), `canceled`; в `nms[]` — `nmId, name`; `boosterStats[] {date, nm, avg_position}` для кампаний с единой ставкой; `currency` — [08-promotion.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/08-promotion.yaml).
- Новая версия статистики кампаний реализована 10.09.2025, «Текущая версия будет доступна до 30 сентября»; статистика продвижения перенесена из раздела «Аналитика и данные» в «Маркетинг и продвижение» — [t.me/wb_api_notifications/273](https://t.me/wb_api_notifications/273). С 27.11.2025 изменилось допустимое число ID кампаний в запросе — [/304](https://t.me/wb_api_notifications/304).
- С 07.07.2026 ответы fullstats и других методов продвижения содержат `currency` (API работает в валюте профиля продавца) — [t.me/wb_api_notifications/441](https://t.me/wb_api_notifications/441).
- `POST /adv/v1/normquery/stats` («Статистика по поисковым кластерам с детализацией по дням», для CPM и CPC кампаний): тело `from, to, items[] {advertId, nmId}` (до 100); ответ `dailyStats[] {date, stat {normQuery, views (null для CPC), clicks, atbs, orders, ctr, cpc, cpm, avgPos, shks, spend}}`. Лимит П/С 10 запросов/мин (всплеск 20); Б — 2/час — [08-promotion.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/08-promotion.yaml). Есть и агрегированный `POST /adv/v0/normquery/stats`.
- `GET /adv/v1/upd?from&to` (интервал 1–31 день): `updNum, updTime, updSum, advertId, campName, advertType, paymentType` (Баланс/Бонусы/Счёт/Кэшбэк), `advertStatus`. Лимит П/С 1 запрос/с (всплеск 5); Б — 1/час — [08-promotion.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/08-promotion.yaml).
- Список кампаний — `GET /adv/v1/promotion/count`, детали — `GET /api/advert/v2/adverts`; `GET /adv/v1/budget` помечен deprecated и отключается 16.11.2026, замена — `POST /api/advert/v2/budget` — [08-promotion.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/08-promotion.yaml), [t.me/wb_api_notifications/467](https://t.me/wb_api_notifications/467).
- Пост от 25.12.2025 дословно: «Переходим на работу с кампаниями с ручной и единой ставкой. Методы для работы с кампаниями с единой ставкой будут отключены 2 февраля» — [t.me/wb_api_notifications/329](https://t.me/wb_api_notifications/329); в текущей спецификации тип `8` «кампания с единой ставкой» помечен «устаревший тип» — [08-promotion.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/08-promotion.yaml). (Формулировка поста внутренне неоднозначна — речь, по-видимому, о старом типе 8; полный текст журнала изменений не прочитан.)
- Медиакампании (ВБ.Медиа) — отдельный хост `advert-media-api.wildberries.ru`, `POST /adv/v1/stats` — [08-promotion.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/08-promotion.yaml).

### Inferences
- Органические заказы по nmID ≈ `orderCount` воронки − сумма `orders` по nmID из fullstats за тот же день. Это оценка: окно атрибуции рекламного заказа WB в спецификации не описано, и одна и та же покупка может засчитываться иначе.
- Расход по nmID в день = `sum` в `days[].apps[].nms[]` fullstats; `upd` нужен только для сверки с фактическими списаниями по кампании.
- При ≤31 дне на запрос и 3 запросах/мин историю рекламы нужно накапливать регулярно (ежедневный сбор за вчера + перезабор хвоста).

### Gaps
- Глубина хранения рекламной статистики (за сколько дней назад можно запросить fullstats) в спецификации не указана (кроме песочницы — «за последние 30 дней»).
- Окно и правила атрибуции рекламных `orders`/`shks` не описаны.

## 6. Остатки: оборачиваемость, дни покрытия, упущенные продажи

### Takeaway
Текущие остатки: `POST /api/analytics/v1/stocks-report/wb-warehouses` (склады WB, обновление раз в 30 минут, до 250 000 строк, 3 запроса/мин) и `/stocks-report/seller-warehouses` (склады продавца). Старый `GET /api/v1/supplier/stocks` отключён 23.06.2026. С 15.08.2026 данные по складам WB обезличены: склад = «Склад WB», `warehouseId = -999999`. История остатков (`/api/v2/stocks-report/*`, глубина ≤3 месяцев, обновление раз в 2 часа) уже считает оборачиваемость (`saleRate`, `avgStockTurnover`), время отсутствия товара, упущенные заказы/выкупы и текущую цену; CSV по остаткам доступны без «Джем».

### Cited Findings
- `POST https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/wb-warehouses`: «Метод возвращает текущие остатки товаров на складах WB. Данные обновляются 1 раз в 30 минут. 1 строка ответа — данные об 1 размере товара на 1 складе WB»; фильтры `nmIds` (до 1000), `chrtIds`, `limit` (до 250 000), `offset`; поля `nmId, chrtId, warehouseId, warehouseName, regionName, quantity` (доступно для добавления в корзину), `inWayToClient, inWayFromClient`; токены — персональный, сервисный, базовый с секретом; лимит 3 запроса/мин (интервал 20 с) — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml).
- В описании полей: `warehouseId` «может быть только `-999999`», `warehouseName`/`regionName` «может быть только `Склад WB`» со ссылкой на release-notes?id=570 — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml). Анонс: «C 15 августа вносим временные изменения в работу WB API. Часть информации по складам WB будет недоступна» (17.08.2026) — [t.me/wb_api_notifications/459](https://t.me/wb_api_notifications/459). Та же оговорка — в Ленте заказов (`warehouseName`, `warehouseRegion`) и в воронке (`localizationPercent` может быть только 100).
- 23.03.2026 WB добавил `stocks-report/wb-warehouses` и объявил: «Текущий метод `GET /api/v1/supplier/stocks` будет отключён 23 июня» — [t.me/wb_api_notifications/372](https://t.me/wb_api_notifications/372); в текущей спецификации `supplier/stocks` нет — [12-reports.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/12-reports.yaml).
- `POST .../stocks-report/seller-warehouses` (с 24.08.2026, вместо `POST /api/v3/stocks/{warehouseId}` для чтения): обновление раз в 30 минут; поля `nmId, chrtId, warehouseId, warehouseName, regionName, quantity`; только персональный и сервисный токен категории «Аналитика» — [t.me/wb_api_notifications/460](https://t.me/wb_api_notifications/460), [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml).
- История остатков `POST /api/v2/stocks-report/products/products` (также `/products/groups`, `/products/sizes`, `/offices`): `currentPeriod` «Не ранее 3 месяцев от текущей даты»; `stockType` (все / wb / mp); `availabilityFilters` (deficient, actual, balanced, nonActual, nonLiquid, invalidData); `limit` до 1000. Метрики: `ordersCount, ordersSum, avgOrders, avgOrdersByMonth[], buyoutCount, buyoutSum, buyoutPercent, stockCount, stockSum, saleRate` (оборачиваемость текущих остатков, days/hours; −1 — бесконечность, −2 — ноль, −3 — не рассчитано), `avgStockTurnover`, `toClientCount, fromClientCount, officeMissingTime` (время отсутствия на складе), `lostOrdersCount, lostOrdersSum, lostBuyoutsCount, lostBuyoutsSum` (упущенные), `currentPrice {minPrice, maxPrice}`, `availability`. Лимит П/С 3/мин; Б 2/час — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml).
- С 17.09.2026 история остатков (группы/товары/размеры/склады и CSV `STOCK_HISTORY_REPORT_CSV`, `STOCK_HISTORY_DAILY_CSV`) обновляется раз в 2 часа; для текущих данных — `wb-warehouses`/`seller-warehouses` — [t.me/wb_api_notifications/468](https://t.me/wb_api_notifications/468). Отчёты по остаткам в CSV «можно создать без подписки Джем» — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml); с 04.12.2025 в CSV по остаткам добавлен столбец ID размера — [/303](https://t.me/wb_api_notifications/303).
- Есть также отчёт-задание «Остатки на складах»: `GET /api/v1/warehouse_remains` → `/tasks/{task_id}/status` → `/download`; с 11.03.2026 такие отчёты при отсутствии данных отвечают 204 вместо пустого массива — [12-reports.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/12-reports.yaml), [t.me/wb_api_notifications/353](https://t.me/wb_api_notifications/353).
- Отдельный контекст: в середине сентября 2025 WB закрыл публичный доступ (в коде страниц сайта) к остаткам на своих складах — сторонним сервисам видно не более 100 единиц; свои данные продавец по-прежнему получает в ЛК или по API — [sberbusiness.live, 01.10.2025](https://sberbusiness.live/news/wildberries-zakril-dostup-k-dannim-ob-ostatkah-tovarov-na-svoih-skladah) (со ссылкой на «Коммерсантъ»).

### Inferences
- Дни покрытия = остаток (`wb-warehouses.quantity` + `seller-warehouses.quantity`) / средние дневные заказы (из воронки или `avgOrders` истории остатков); WB уже отдаёт готовую `saleRate` — её стоит хранить как контрольную.
- `lostOrdersCount`/`officeMissingTime` важны для чистки эффекта акции: падение заказов в акцию из-за отсутствия остатка не должно читаться как низкая эластичность.
- После обезличивания складов WB (15.08.2026) региональный анализ (локализация, склад отгрузки) через API фактически недоступен; это «временные изменения», и их снятие нужно отслеживать.
- Истории остатков глубже 3 месяцев API не даёт — ежедневный снимок нужно хранить у себя.

### Gaps
- Полный текст release-notes?id=570 (какие именно поля и методы затронуты обезличиванием и до какого срока) прочитать не удалось (HTTP 498).

## 7. Официальные заявления о свежести и глубине данных; токены и лимиты

### Takeaway
Свежесть: воронка, поисковые запросы и история остатков — раз в 2 часа (с сентября–октября 2026); заказы/продажи statistics-api и текущие остатки — раз в 30 минут; Лента заказов — в реальном времени. Глубина: воронка и поисковые отчёты — 365 дней (JSON-история воронки — только 7 дней, год — CSV с «Джем»); заказы/продажи — 90 дней; Лента заказов — 31 день; история остатков — 3 месяца; детализация реализации — с 29.01.2024; реклама — 31 день на запрос (общая глубина не указана). Токены с 20.10.2025 — четыре типа; с 28.04.2026 базовый токен получает минимальные лимиты, полные лимиты — у персонального и сервисного.

### Cited Findings
**Свежесть и глубина (сводно, по спецификации на 17.10.2026)**
- Воронка v3 `products`: обновление раз в 2 часа, глубина 365 дней; `products/history` и `grouped/history`: не более последней недели; CSV `DETAIL_HISTORY_REPORT`/`GROUPED_HISTORY_REPORT`: до года, только с «Джем»; готовый CSV-файл живёт 48 часов — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml); переход на «раз в 2 часа» — с 24.09.2026 — [t.me/wb_api_notifications/473](https://t.me/wb_api_notifications/473).
- Поисковые запросы: глубина 365 дней, `/product/orders` — окно ≤7 дней; обновление раз в 2 часа с 12.10.2026 — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml), [t.me/wb_api_notifications/483](https://t.me/wb_api_notifications/483).
- Заказы/продажи statistics-api: раз в 30 минут, хранение 90 дней, предварительные данные; `finishedPrice`/`priceWithDisc`/`forPay` актуализируются в течение 24 часов — [12-reports.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/12-reports.yaml).
- Лента заказов: реальное время, 31 день — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml).
- Остатки: текущие — раз в 30 минут; история — раз в 2 часа (с 17.09.2026), глубина 3 месяца — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml), [t.me/wb_api_notifications/468](https://t.me/wb_api_notifications/468).
- Детализация реализации: данные с 29.01.2024, отчёты daily/weekly — [13-finances.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/13-finances.yaml).
- Реклама fullstats: ≤31 день на запрос — [08-promotion.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/08-promotion.yaml).

**Типы токенов и лимиты**
- С 20.10.2025 доступны четыре типа токенов: Персональный (для собственных/купленных on-premise программ продавца), Сервисный (для облачных сервисов из официального Каталога решений, работает только с этим сервисом), Базовый («для тестирования на реальных данных и остальных случаев»), Тестовый (песочница) — [t.me/wb_api_notifications/289](https://t.me/wb_api_notifications/289).
- С 30.03.2026 лимиты зависят от типа токена: для Персональных и Сервисных не меняются, для Базовых и Тестовых — ниже; WB рекомендует переводить интеграции на Персональные/Сервисные и «не рекомендуем передавать Персональные токены сторонним сервисам» — [t.me/wb_api_notifications/369](https://t.me/wb_api_notifications/369). 23.04.2026 — обновление лимитов категории Продвижение, 28.04.2026 — Статистика, Аналитика, Контент, Цены и скидки, Маркетплейс; «После 28 апреля Базовый токен можно будет использовать только с минимальным лимитом» — [t.me/wb_api_notifications/392](https://t.me/wb_api_notifications/392).
- Сторонние сервисы вне Каталога решений могут работать с базовыми токенами без сниженных лимитов, идентифицировав трафик (заявка на business-solutions@rwb.ru) — [t.me/wb_api_notifications/373](https://t.me/wb_api_notifications/373).
- В спецификации у части методов явно ограничен тип токена (`x-token-types`): `stocks-report/seller-warehouses`, `item-rating`, финансовые `/sales-reports/list`, `/sales-reports/detailed/{reportId}`, `/acquiring/*`, оптовые B2B-скидки, рекомендации, дневные лимиты рекламы — только персональный и сервисный; `stocks-report/wb-warehouses` — персональный, сервисный и «базовый с секретом» — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml), [13-finances.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/13-finances.yaml), [02-items.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/02-items.yaml), [08-promotion.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/08-promotion.yaml).
- Категории токена, в которых живут методы (поле `x-category` спецификации): воронка, поисковые запросы, остатки, Лента заказов — `contentanalytics` («Аналитика»); orders/sales — `statistics` («Статистика»); детализация — `finance` («Финансы»); цены и календарь акций — `discountsandprices` («Цены и скидки»); реклама — `advert` («Продвижение») — [11-analytics.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml), [12-reports.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/12-reports.yaml), [13-finances.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/13-finances.yaml), [02-items.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/02-items.yaml), [08-promotion.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/08-promotion.yaml).
- Наличие подписки «Джем» можно проверить API: `GET https://common-api.wildberries.ru/api/common/v1/subscriptions` («Информацию о подписке Джем можно получить с токеном любой категории»; пустой 200 — если подписку никогда не подключали) — [01-general.yaml](https://dev.wildberries.ru/api/swagger/yaml/ru/01-general.yaml).
- Сводная таблица лимитов (П/С → Б): воронка v3 3/мин → 2/час; CSV-создание 3/мин → 1/час; поисковые запросы 3/мин → 1/час; Лента заказов 1/мин → 1/3 ч; orders 1/мин (всплеск 10) → 1/3 ч; sales 1/мин → 1/2 ч; детализация 1/мин → 2/24 ч; цены 10/6 с → 4/час; календарь акций 10/6 с → 1/час; fullstats 3/мин → 1/час; normquery/stats 10/мин → 2/час; остатки текущие 3/мин — [спецификации, см. выше](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml).

### Inferences
- Для программы-репрайсера/аналитики самого продавца правильный тип — Персональный токен с категориями Аналитика, Статистика, Финансы, Цены и скидки, Продвижение; с Базовым токеном (2 запроса/час на воронку, 1/час на календарь акций) регулярный сбор по каталогу практически невозможен.
- Минимальная задержка «акция → видимый эффект» в API: заказы — минуты (Лента заказов), воронка/позиции — до 2 часов, выкупы — дни/недели (привязка к дате заказа), финансовый итог — следующий daily/weekly отчёт реализации.
- Из-за коротких окон (7 дней JSON-воронки, 31 день Ленты/рекламы, 3 месяца остатков) историю нужно копить в своей базе с первого дня; ретроспективно можно получить только воронку за 365 дней (CSV с «Джем» или посуточные запросы `products`), заказы за 90 дней и финансы с 29.01.2024.

### Gaps
- Официальная статья о новых лимитах по типам токенов (ссылка «в статье» из поста 369) и полная таблица «Базовый с секретом» vs «Базовый» не прочитаны — dev.wildberries.ru блокирует автоматическое чтение (HTTP 498).
- Стоимость/условия платного доступа для сервисов из Каталога решений (ответ 402) не исследованы.
