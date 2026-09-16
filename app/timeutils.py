"""Единый источник текущего времени для всего приложения.

Заменяет устаревший ``datetime.utcnow()`` (помечен на удаление в будущих
версиях Python). Возвращает **naive** UTC-время — намеренно, без tzinfo:
все колонки ``DateTime`` в моделях naive, а смешивать naive и aware
``datetime`` нельзя — их сравнение бросает ``TypeError``. Так что семантика
ровно та же, что была у ``datetime.utcnow()``, просто без депрекации.
"""
from datetime import datetime, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
