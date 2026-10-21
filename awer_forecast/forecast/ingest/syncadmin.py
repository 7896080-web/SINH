"""Спрос WB/Ozon/сайта из журнала Sync Admin → channel_sales_daily (источник `sync_admin`)."""
from __future__ import annotations

from datetime import date

from sqlalchemy import and_, delete
from sqlalchemy.engine import Connection

from forecast.db.schema import t_channel_sales_daily
from forecast.integrations.syncadmin import CHANNELS, MarketplaceDemand

SOURCE = "sync_admin"


def store_marketplace_demand(conn: Connection, demand: MarketplaceDemand, since_day: date) -> int:
    """Окно с `since_day` заменяется целиком: отменённый с прошлого запуска заказ исчезает,
    повторный запуск ничего не задваивает."""
    conn.execute(delete(t_channel_sales_daily).where(and_(
        t_channel_sales_daily.c.channel.in_(CHANNELS), t_channel_sales_daily.c.sale_date >= since_day)))
    rows = [{"channel": channel, "barcode": barcode, "sale_date": day, "qty": qty, "revenue": None,
             "source": SOURCE} for (channel, barcode, day), qty in demand.totals.items()]
    if rows:
        conn.execute(t_channel_sales_daily.insert(), rows)
    return len(rows)
