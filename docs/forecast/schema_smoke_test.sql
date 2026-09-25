-- Проверка ограничений схемы (AUDIT.md). Запуск на пустой базе после lamoda_forecast_schema.sql:
--   psql -f lamoda_forecast_schema.sql && psql -f schema_smoke_test.sql
-- Ошибка ожидаема ровно на одной вставке после каждой пометки «ожидаем ОШИБКУ» (для дублей — на второй).
\set ON_ERROR_STOP 0
insert into suppliers(name,production_days,delivery_days,moq) values('s',30,20,100);
insert into product_models(article,category) values('A','Куртки');
insert into product_sizes(product_model_id,size_label,uid_1c) values(1,'M','uid-1');
-- ожидаем OK: NULL-случаи теперь через дефолты
insert into size_profile(category,size_label,share_of_sales_pct,report_date) values('x','M',1,now());
insert into wordstat_data(keyword,stat_date,frequency) values('k',now(),1);
-- ожидаем ОШИБКУ: дубль модели без цвета
insert into product_models(article,category) values('A','Куртки');
-- ожидаем ОШИБКУ: order_items без item id
insert into order_items(order_id,product_size_id,status,event_date) values('CZ1',1,'Delivered',now());
-- ожидаем ОШИБКУ на второй: дубль позиции
insert into order_items(order_id,lamoda_item_id,product_size_id,status,event_date) values('CZ1',7,1,'Delivered',now());
insert into order_items(order_id,lamoda_item_id,product_size_id,status,event_date) values('CZ1',7,1,'Delivered',now());
-- ожидаем OK: большие WAPE/MAPE
insert into forecast_accuracy_log(product_size_id,channel,period_start,period_end,mape,wape) values(1,'lamoda',now(),now(),1500,2500);
-- ожидаем ОШИБКУ: неизвестный канал
insert into sales_daily(product_size_id,channel,sale_date) values(1,'wildberries',now());
-- ожидаем ОШИБКУ: дубль вебхука
insert into webhook_events_log(notification_type,dedupe_key,payload) values('statusChanged','order:CZ1:7:Delivered','{}');
insert into webhook_events_log(notification_type,dedupe_key,payload) values('statusChanged','order:CZ1:7:Delivered','{}');
-- ожидаем ОШИБКУ: подтверждено без количества
insert into shipment_recommendations(product_size_id,qty_to_ship,status) values(1,5,'confirmed');
insert into shipment_recommendations(product_size_id,qty_to_ship,status,confirmed_qty) values(1,5,'confirmed',4);
-- on_time вычисляется сам
insert into purchase_orders(supplier_id,ordered_date,expected_ready_date,actual_ready_date) values(1,'2026-09-01','2026-10-01','2026-10-05');
select 'on_time=' || on_time from purchase_orders;
-- Продажи из 1С и API (A-51)
insert into sales_source_config(channel,source_before) values('retail','1c');
insert into sales_source_config(channel,source_before,cutover_date,source_after) values('wb','1c','2027-03-01','wb_api');
-- ожидаем ОШИБКУ: дата переключения без источника после неё
insert into sales_source_config(channel,source_before,cutover_date) values('ozon','1c','2027-03-01');
insert into channel_sales(channel,source,source_row_id,uid_1c,product_size_id,sale_date,qty) values('retail','1c','РТ-001:1','uid-1',1,'2020-03-01',2);
-- та же строка из 1С повторно — ожидаем ОШИБКУ (идемпотентность загрузки)
insert into channel_sales(channel,source,source_row_id,uid_1c,product_size_id,sale_date,qty) values('retail','1c','РТ-001:1','uid-1',1,'2020-03-01',2);
-- ожидаем ОШИБКУ: ни uid_1c, ни баркода
insert into channel_sales(channel,source,source_row_id,sale_date,qty) values('wb','wb_api','rrd-1',now(),1);
-- ожидаем ОШИБКУ: тот же внешний id Lamoda без кабинета повторно
insert into marketplace_listings(channel,product_size_id,external_id) values('lamoda',1,'MP002XM0WFWSINXL');
insert into marketplace_listings(channel,product_size_id,external_id) values('lamoda',1,'MP002XM0WFWSINXL');
