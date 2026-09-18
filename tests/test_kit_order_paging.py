"""Конец ленты заказов Kit определяет `total_count`, а не длина страницы.

Короткая страница не обязана значить «данные кончились»: на этом WB терял заказы
неделями, и у Kit стоял тот же стоп `len(orders) < 100`. 18.09 на живом кабинете
он оказался честным (100/100/88, четвёртая страница пустая, `total_count` = 288
сошёлся), но это совпадение, а не гарантия — а цена ошибки здесь та же, что у WB:
расчёт отчитается «проблем нет» и поставит товару «актуализирован» по продажам,
которых не видел.
"""
from datetime import date

from app.workers.platform_clients.kit import KitClient, MAX_ORDER_PAGES


def _order(oid, day="2026-09-10", variant="v1", status="WAIT_FOR_CONFIRMATION"):
    return {
        "id": oid,
        "status": status,
        "created_at": f"{day}T10:00:00+03:00",
        "delivery_chunks": [{"id": "0", "items": [
            {"id": f"i{oid}", "product_variant_id": variant, "quantity": 1},
        ]}],
    }


class _Kit(KitClient):
    """Клиент с подставной лентой: страницы задаются списком, сеть не трогаем."""

    def __init__(self, pages, total_count=None):
        super().__init__(token="t", variant_map_loader=lambda: {"v1": "111"})
        self.pages = pages
        self.total_count = total_count
        self.asked = []

    def _get(self, path, params=None):
        page = (params or {}).get("page", 1)
        self.asked.append(page)
        orders = self.pages[page - 1] if page <= len(self.pages) else []
        body = {"orders": orders}
        if self.total_count is not None:
            body["total_count"] = self.total_count
        return body


def test_a_short_page_in_the_middle_does_not_end_the_feed():
    """Ровно тот случай, которого боимся: короткая страница, а за ней данные."""
    c = _Kit(pages=[[_order("1")] * 100, [_order("2")] * 3, [_order("3")] * 40])

    orders = c.get_orders_since(date(2026, 1, 1))

    assert len(orders) == 143
    assert c.asked == [1, 2, 3, 4]      # спросили и за короткой страницей


def test_total_count_closes_the_feed_without_an_extra_request():
    c = _Kit(pages=[[_order("1")] * 100, [_order("2")] * 88], total_count=188)

    orders = c.get_orders_since(date(2026, 1, 1))

    assert len(orders) == 188
    assert c.asked == [1, 2]            # третью страницу не спрашивали вовсе


def test_an_empty_page_ends_the_feed_when_there_is_no_total_count():
    c = _Kit(pages=[[_order("1")] * 100, [_order("2")] * 88])

    orders = c.get_orders_since(date(2026, 1, 1))

    assert len(orders) == 188
    assert c.asked == [1, 2, 3]


def test_the_page_guard_marks_the_picture_incomplete():
    """Уткнулись в свой предел — расчёт обязан узнать, что не всё видел."""
    c = _Kit(pages=[[_order(str(i))] * 100 for i in range(MAX_ORDER_PAGES + 5)])

    c.get_orders_since(date(2026, 1, 1))

    assert c.last_truncated is True
    assert len(c.asked) == MAX_ORDER_PAGES


def test_a_clean_walk_leaves_the_picture_complete():
    c = _Kit(pages=[[_order("1")] * 10], total_count=10)

    c.get_orders_since(date(2026, 1, 1))

    assert c.last_truncated is False


def test_the_flag_is_reset_between_calls():
    c = _Kit(pages=[[_order("1")] * 10], total_count=10)
    c.last_truncated = True

    c.get_orders_since(date(2026, 1, 1))

    assert c.last_truncated is False


def test_orders_older_than_the_base_date_are_filtered_out_not_stopped_on():
    """Лента идёт от свежих к старым, но опираться на порядок мы не вправе:
    старый заказ отсеиваем по дате и идём дальше, а не обрываем обход."""
    c = _Kit(pages=[
        [_order("old", day="2026-01-05")] * 100,
        [_order("new", day="2026-09-10")] * 5,
    ])

    orders = c.get_orders_since(date(2026, 8, 7))

    assert len(orders) == 5


# --------------------------------------------------- та же лента у приёма заказов

def test_awaiting_confirmation_also_reads_past_a_short_page():
    c = _Kit(pages=[
        [_order("1")] * 100,
        [_order("2")] * 3,
        [_order("3", status="COMPLETED")] * 10 + [_order("4")] * 7,
    ])

    orders = c.get_orders_awaiting_confirmation()

    assert len(orders) == 110          # COMPLETED не наш статус, остальные наши
    assert c.asked == [1, 2, 3, 4]


def test_awaiting_confirmation_marks_a_truncated_walk():
    c = _Kit(pages=[[_order(str(i))] * 100 for i in range(MAX_ORDER_PAGES + 5)])

    c.get_orders_awaiting_confirmation()

    assert c.last_truncated is True
