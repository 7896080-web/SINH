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
    price: str = "Цена"
    card: str = "Дисконтная карта"
    manual_pct: str = "Ручная скидка %"
    receipt: str = "Чек"  # есть только при выгрузке из чеков
    time: str = "Время"


class StockColumns(BaseModel):
    """Остатки на складах магазинов на дату (stock_*.csv) и поступления за день (arrivals_*.csv)."""

    date: str = "Дата"
    store: str = "Склад"
    item: str = "Номенклатура"
    category: str = "Номенклатурная группа"
    size: str = "Характеристика"
    stock: str = "Остаток"
    arrived: str = "Поступило"
    doc: str = "Документ"


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
    stock_columns: StockColumns = StockColumns()
    # склад / магазин / касса из 1С → короткое имя магазина (без учёта регистра; частичное совпадение)
    store_map: dict[str, str] = {"ГОРОД": "Город", "МСЛ": "МСЛ"}
    exclude_sellers: list[str] = ["Wildberries", "Ozon", "Lamoda"]
    # строки без продавца не теряются: идут в общий выторг под этим именем и видны владельцу
    no_seller_name: str = "(без продавца)"
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


class TrainingBot(BaseModel):
    """Второй бот — «Обучение» (уроки, квизы, программа новичка, тесты, опросы, база знаний).
    Токен — MAX_TRAINING_BOT_TOKEN в .env; без него всё обучение идёт в бот «План»."""

    bot_link: str = ""  # https://max.ru/<имя бота обучения> — для кнопки «Подключить обучение»


class Recs(BaseModel):
    lookback_days: int = 90  # окно для «что докупают вместе»
    # запасной список «дополнений», пока в 1С нет чеков/покупок по картам; в советах всё равно только то,
    # что есть на остатке магазина по выгрузке 1С
    add_ons: list[str] = ["Ремень", "Футболка", "Рубашка", "Носки", "Галстук", "Свитшот", "Джемпер"]
    # что уже идёт в комплекте — такие связки не советуем (категории по началу названия, как в 1С)
    included: dict[str, list[str]] = {"Джинсы": ["Ремень"]}
    llm: bool = False  # переформулировать советы через Claude API (нужен ANTHROPIC_API_KEY)
    llm_model: str = "claude-sonnet-5-5"


class Motivation(BaseModel):
    """Мотивация без денег: ставка зависит только от плана по выручке (docs/SPEC.md)."""

    records: bool = True  # личные рекорды и серии в утреннем сообщении
    record_min_shifts: int = 10  # «рекорд за всё время» — когда в истории бота уже столько смен продавца
    streak_min: int = 3  # серия смен выше нормы показывается с этой длины
    weekly_goal: bool = True  # цель недели по самому слабому показателю
    goal_lookback_days: int = 28  # окно для выбора цели недели
    store_goal: bool = True  # «магазины вместе: X из плана» — только в сводке владельца, продавцам не показываем
    coach_sheet: bool = True  # по понедельникам старшим и владельцу — листок для разговоров 1:1


class Training(BaseModel):
    lessons_path: Path = Path("content/lessons.yaml")  # уроки, квизы, программа новичка
    weekly_lesson: bool = True  # урок недели по теме цели недели
    quizzes: bool = True  # 2 вопроса с кнопками после урока (нужны входящие события MAX)
    onboarding_shifts: int = 10  # новичок — первые N смен: вместо урока недели уроки программы новичка


class Staffing(BaseModel):
    """Минимум продавцов в каждом магазине (решение владельца 18.10.2026)."""

    weekday_min: int = 2  # понедельник–пятница
    weekend_min: int = 3  # суббота и воскресенье
    lookahead_days: int = 7  # за сколько дней вперёд предупреждать владельца о недоборе


class Settings(BaseModel):
    db_path: Path = Path("data/awer.db")
    timezone: str = "Europe/Moscow"
    stores: list[str] = ["Город", "МСЛ"]  # день закрыт, когда есть выгрузка по всем
    norm_shifts: int = 18  # смен в полном месяце (решение владельца 18.10.2026); график — админка «График смен»
    params: Params = Params()
    onec: OneC = OneC()
    schedule: Schedule = Schedule()
    max: MaxCfg = MaxCfg()
    max_training: TrainingBot = TrainingBot()
    recommendations: Recs = Recs()
    staffing: Staffing = Staffing()
    motivation: Motivation = Motivation()
    training: Training = Training()

    # секреты — только из окружения
    @property
    def max_token(self) -> str:
        tok = os.environ.get("MAX_BOT_TOKEN", "")
        if not tok:
            raise RuntimeError("MAX_BOT_TOKEN не задан (см. .env.example)")
        return tok

    @property
    def training_token(self) -> str | None:
        """Токен бота «Обучение»; None — второго бота нет."""
        return os.environ.get("MAX_TRAINING_BOT_TOKEN") or None

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
