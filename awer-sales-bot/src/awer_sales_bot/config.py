"""Настройки: config.yaml (несекретное) + переменные окружения / .env (секреты)."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .planning.engine import Params


class Columns(BaseModel):
    """Строки ОРП (ТЧ «Товары» и «Возвращенные товары»). Сравнение без учёта регистра."""

    date: str = "Дата"
    store: str = "Склад"  # склад строки («ГОРОД_склад») → магазин через onec.store_map
    seller: str = "Продавец"
    doc: str = "Документ"
    item: str = "Номенклатура"
    category: str = "Номенклатурная группа"
    qty: str = "Количество"
    amount: str = "Сумма"  # со скидкой
    discount: str = "Скидка"


class DocColumns(BaseModel):
    """Шапки ОРП: чеков и выручка по кассе за день (средний чек и вещей в чеке — по магазину)."""

    date: str = "Дата"
    store: str = "Магазин"
    kassa: str = "Касса"
    doc: str = "Документ"
    checks: str = "Чеков"
    return_checks: str = "Чеков возврата"
    revenue: str = "Выручка"


class OneC(BaseModel):
    inbox: Path = Path("data/inbox")
    archive: Path = Path("data/archive")
    columns: Columns = Columns()
    doc_columns: DocColumns = DocColumns()
    # склад / магазин / касса из 1С → короткое имя магазина (без учёта регистра; частичное совпадение)
    store_map: dict[str, str] = {"ГОРОД": "Город", "МСЛ": "МСЛ"}
    exclude_sellers: list[str] = ["", "Wildberries", "Ozon", "Lamoda"]
    # «Букалова Юлия МСЛ» -> продавец «Букалова Юлия», магазин «МСЛ» (как в ведомостях ЗП)
    seller_store_suffixes: dict[str, str] = {" МСЛ": "МСЛ"}
    # если в выгрузке нет колонки «Магазин» — какой магазин подставить
    default_store: str = "Город"


class Schedule(BaseModel):
    daily_send_time: str = "09:30"  # HH:MM, отправка за вчерашний день
    retry_minutes: int = 30  # повтор, если день ещё не закрыт (нет выгрузки)
    deadline_time: str = "13:00"  # после — предупреждение владельцу, отправка без пропавшего магазина


class MaxCfg(BaseModel):
    base_url: str = "https://platform-api.max.ru"
    auth: str = "header"  # header -> Authorization: <token>; query -> ?access_token=<token>
    owners: list[int] = Field(default_factory=list)  # user_id владельца/управляющих — получают сводку
    poll_timeout: int = 30
    bot_link: str = ""  # https://max.ru/<имя_бота> — для ссылок-приглашений


class Recs(BaseModel):
    lookback_days: int = 90  # окно для «что докупают вместе»
    llm: bool = False  # переформулировать советы через Claude API (нужен ANTHROPIC_API_KEY)
    llm_model: str = "claude-sonnet-5-5"


class Settings(BaseModel):
    db_path: Path = Path("data/awer.db")
    timezone: str = "Europe/Moscow"
    stores: list[str] = ["Город", "МСЛ"]  # день закрыт, когда есть выгрузка по всем
    norm_shifts: int = 15  # смен в полном месяце (для пересчёта на полный месяц)
    params: Params = Params()
    onec: OneC = OneC()
    schedule: Schedule = Schedule()
    max: MaxCfg = MaxCfg()
    recommendations: Recs = Recs()

    # секреты — только из окружения
    @property
    def max_token(self) -> str:
        tok = os.environ.get("MAX_BOT_TOKEN", "")
        if not tok:
            raise RuntimeError("MAX_BOT_TOKEN не задан (см. .env.example)")
        return tok

    @property
    def anthropic_key(self) -> str | None:
        return os.environ.get("ANTHROPIC_API_KEY") or None


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load(path: str | Path = "config.yaml") -> Settings:
    path = Path(path)
    _load_dotenv(path.parent / ".env")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return Settings.model_validate(data or {})
