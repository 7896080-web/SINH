# Lamoda Seller Partner API v2 — справочные материалы

`openapi-v2.yaml` — официальная OpenAPI-спецификация «Lamoda Seller REST API» (74 метода),
скачана 21.10.2026 со страницы Seller Academy
<https://academy.lamoda.ru/articles/api/spec/10_v2_spec/> (файл, который страница подгружает
в Swagger UI). Держим копию, чтобы следующие фазы (цены, акции, вебхуки) писались по точным
именам полей, а не по пересказу. При расхождении с живым API главнее живой API и канал
анонсов `@apilamoda`; обновлённую спецификацию — скачать заново тем же путём.

Что из неё взято в Фазе 1 (и где в коде):

| Метод | Что берём | Код |
|---|---|---|
| `POST /v2/auth-token` | тело `grant_type`/`client_id`/`client_secret` (snake_case), ответ `access_token`, `expires_in` | `forecast/integrations/lamoda/client.py` |
| `GET /v2/nomenclatures` | `country` обязателен, `limit` ≤ 25; `sku`, `parentSku`, `externalSku`, `barcode` | `forecast/catalog/lamoda_sync.py` |
| `GET /v2/fbo/stocks` | `withZeroQuantity`, `limit` ≤ 1000; `sku`, `externalSku`, `warehouseCode`, `quantity` | `forecast/ingest/lamoda.py` |
| `GET /v2/fbo/shipments`, `…/{id}/items` | статус поставки, `plannedDate`, `createdAt`; `sku`, `ean`, `quantityPlanned`, `quantityAccepted` | `forecast/ingest/lamoda.py` |
| `GET /v2/orders` | `updatedAtFrom`/`updatedAtTo` (RFC 3339 UTC), `sort`, `limit` ≤ 1000; список БЕЗ позиций | `forecast/ingest/lamoda.py` |
| `GET /v2/orders/{orderId}` | путь — внутренний `id` (не `orderId`); `items[]`: `id` (строка), `sku`, `externalSku`, `status`, `paidPrice`/`basePrice`/`totalDiscount` (`{amount, currency}`, копейки) | `forecast/ingest/lamoda.py` |

Во всех GET-методах `sellerId` — обязательный query-параметр. Проверено без ключей
(21.10.2026): все эти пути на `public-api-seller.lamoda.ru/api` отвечают 401 (несуществующий —
404), `auth-token` с неверными ключами — `{"error":{"code":"VALIDATION_FAILED","message":"invalid_client"}}`.
С настоящими ключами клиент ещё не запускался.
