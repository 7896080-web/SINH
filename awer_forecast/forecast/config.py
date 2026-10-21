"""Настройки из переменных окружения (и файла `.env` рядом с проектом).

Секреты (ключи Lamoda, пароль входа, строка подключения к БД) живут только в
`.env` / окружении — не в коде, не в репозитории и не в логах. Параметры расчёта
тоже здесь, а не константами в коде: ТЗ требует, чтобы пороги были настраиваемыми.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from forecast.integrations.lamoda.statuses import SHIPMENT_ON_CS_STATUSES

PROJECT_DIR = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """Минимальный разбор `.env`: КЛЮЧ=значение, `#` — комментарий. Уже заданные
    переменные окружения не перезаписываются (окружение главнее файла)."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip().replace(",", ".")
    return float(value) if value else default


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class PlanningSettings:
    """Параметры расчёта точки заказа (раздел 5.5 ТЗ). Значения по умолчанию —
    стартовые допущения Фазы 1, их видно на главной странице."""
    service_level_z: float = 1.65          # 95% — единый уровень сервиса до ABC×XYZ (Фаза 2)
    review_interval_days: int = 7          # интервал пересмотра — входит в окно L
    fbo_lead_days_default: int = 7         # срок «создание поставки → приёмка FBO», пока нет своей статистики
    supplier_lead_days_default: int = 60   # если у модели не указан поставщик
    history_days: int = 180                # сколько дней истории брать в прогноз
    min_history_days: int = 14             # меньше наблюдений — «мало истории», прогноз = среднее
    forecast_horizon_days: int = 28        # период, на который пишется demand_forecast
    outlier_sigma: float = 3.0             # фильтр выбросов при расчёте σ (outlier_sigma_threshold)
    smoothing_alpha: float | None = None   # None — подбирается по истории
    cs_reserve_qty: int = 0                # сколько штук каждого размера не отгружать с ЦС на FBO
    purchase_on_order_grace_days: int = 14 # заказанное у поставщика считаем «в пути» до срока готовности + N дней
    # статусы поставки Lamoda, в которых её товар ещё не списан с ЦС в 1С (см. statuses.py)
    fbo_statuses_on_cs: frozenset[str] = SHIPMENT_ON_CS_STATUSES


@dataclass(frozen=True)
class Settings:
    database_url: str
    session_secret: str
    login: str
    password_hash: str
    lamoda_base_url: str
    lamoda_client_id: str
    lamoda_client_secret: str
    lamoda_seller_id: str
    lamoda_country: str
    orders_backfill_days: int
    orders_overlap_hours: int
    onec_exchange_dirs: tuple[Path, ...]
    onec_sales_dir: Path | None
    planning: PlanningSettings = field(default_factory=PlanningSettings)

    @property
    def lamoda_configured(self) -> bool:
        return bool(self.lamoda_client_id and self.lamoda_client_secret and self.lamoda_seller_id)


def get_settings() -> Settings:
    load_dotenv(PROJECT_DIR / ".env")
    alpha = _str("PLAN_SMOOTHING_ALPHA")
    on_cs = _str("PLAN_FBO_STATUSES_ON_CS")
    planning = PlanningSettings(
        service_level_z=_float("PLAN_SERVICE_LEVEL_Z", 1.65),
        review_interval_days=_int("PLAN_REVIEW_INTERVAL_DAYS", 7),
        fbo_lead_days_default=_int("PLAN_FBO_LEAD_DAYS", 7),
        supplier_lead_days_default=_int("PLAN_SUPPLIER_LEAD_DAYS", 60),
        history_days=_int("PLAN_HISTORY_DAYS", 180),
        min_history_days=_int("PLAN_MIN_HISTORY_DAYS", 14),
        forecast_horizon_days=_int("PLAN_FORECAST_HORIZON_DAYS", 28),
        outlier_sigma=_float("PLAN_OUTLIER_SIGMA", 3.0),
        smoothing_alpha=float(alpha.replace(",", ".")) if alpha else None,
        cs_reserve_qty=_int("PLAN_CS_RESERVE_QTY", 0),
        purchase_on_order_grace_days=_int("PLAN_PURCHASE_GRACE_DAYS", 14),
        fbo_statuses_on_cs=(frozenset(s.strip().upper() for s in on_cs.split(",") if s.strip())
                            if on_cs else SHIPMENT_ON_CS_STATUSES),
    )
    exchange_dirs = tuple(
        Path(p.strip()) for p in _str("ONEC_EXCHANGE_DIRS", r"C:\sync\results;C:\sync\archive").split(";")
        if p.strip()
    )
    sales_dir = _str("ONEC_SALES_DIR")
    return Settings(
        database_url=_str("FORECAST_DATABASE_URL", f"sqlite:///{PROJECT_DIR / 'forecast.db'}"),
        session_secret=_str("FORECAST_SESSION_SECRET"),
        login=_str("FORECAST_LOGIN", "admin"),
        password_hash=_str("FORECAST_PASSWORD_HASH"),
        lamoda_base_url=_str("LAMODA_BASE_URL", "https://public-api-seller.lamoda.ru/api").rstrip("/"),
        lamoda_client_id=_str("LAMODA_CLIENT_ID"),
        lamoda_client_secret=_str("LAMODA_CLIENT_SECRET"),
        lamoda_seller_id=_str("LAMODA_SELLER_ID"),
        lamoda_country=_str("LAMODA_COUNTRY", "RU"),
        orders_backfill_days=_int("LAMODA_ORDERS_BACKFILL_DAYS", 180),
        orders_overlap_hours=_int("LAMODA_ORDERS_OVERLAP_HOURS", 6),
        onec_exchange_dirs=exchange_dirs,
        onec_sales_dir=Path(sales_dir) if sales_dir else None,
        planning=planning,
    )
