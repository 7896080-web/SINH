"""Текущая комиссия WB по категории — FBS и FBO, по API, как в «Репрайсере».

`GET https://common-api.wildberries.ru/api/v1/tariffs/commission?locale=ru` —
раздел «Тарифы», по спецификации подходит токен ЛЮБОЙ категории (тот же токен
«Финансы», которым забираются отчёты), лимит — запрос в минуту. Ответ — таблица
по предметам: `subjectID`, `subjectName`, `parentName` и проценты по моделям
продаж. FBS — `kgvpMarketplace` (модель «Маркетплейс»), FBO — `paidStorageKgvp`
(«Склад WB»); те же поля читает «Репрайсер» (`priceapp/platforms.py`,
`parse_wb_tariffs`). Вживую из этой программы не проверялось.

Тариф — свойство ПРЕДМЕТА, а не товара. Предмет карточки берётся из
детализации отчётов WB: там у каждой строки есть и артикул WB (`nmId` / «Код
номенклатуры»), и «Предмет» (`subjectName`) — `remember_subjects` запоминает
пару при каждой загрузке отчёта (`WbCard`). Отдельного токена «Контент» для этого
не нужно; карточка, по которой не было ни одной строки в отчётах, предмета не
знает — но в юнит-экономике её и нет.

Тариф и «удержано WB» в юнит-экономике — РАЗНЫЕ числа, и это не ошибка: в
удержанное входят ещё эквайринг, услуги ПВЗ, программа лояльности и продажи со
склада WB (у них тариф FBO). Тариф показывается рядом, чтобы было видно, сколько
WB берёт по своей таблице СЕЙЧАС и что он его поменял.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import requests
from sqlalchemy.orm import Session

from finapp import accounts, settings
from finapp.http_retry import with_retry
from finapp.models import CategoryTariff, PlatformAccount, WbCard
from finapp.timeutils import now_utc

logger = logging.getLogger("mpfinance.tariffs")

WB_COMMON = "https://common-api.wildberries.ru"
WB_TARIFFS_PATH = "/api/v1/tariffs/commission"
# Раз в сутки: тарифы WB меняет не чаще, а суточное задание идёт каждый час.
REFRESH_EVERY = timedelta(hours=20)
# «Тариф сменился» показывается столько после смены.
CHANGE_VISIBLE = timedelta(days=30)
_CHUNK = 500


class TariffError(RuntimeError):
    pass


def _bp(value) -> int | None:
    """Процент → сотые доли процента; вне 0–100 — не тариф, а мусор."""
    if value is None or isinstance(value, bool):
        return None
    try:
        pct = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    if not pct.is_finite() or not (0 <= pct < 100):
        return None
    return int((pct * 100).to_integral_value())


def parse_wb_tariffs(data) -> list[dict]:
    rows = (data or {}).get("report") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise TariffError("ответ WB другой разметки (нет списка «report») — формат изменился?")
    out = []
    for r in rows:
        if not isinstance(r, dict) or r.get("subjectID") in (None, ""):
            continue
        out.append({"category_id": str(r["subjectID"])[:64],
                    "category_name": str(r.get("subjectName") or "")[:200],
                    "parent_name": str(r.get("parentName") or "")[:200] or None,
                    "fbs_bp": _bp(r.get("kgvpMarketplace")),
                    "fbo_bp": _bp(r.get("paidStorageKgvp"))})
    if rows and not out:
        raise TariffError("в ответе WB нет ни одного предмета с subjectID — формат изменился?")
    return out


def fetch_wb(token: str, session: requests.Session | None = None) -> list[dict]:
    http = session or requests.Session()
    http.headers.update({"Authorization": token})

    def call():
        r = http.get(f"{WB_COMMON}{WB_TARIFFS_PATH}", params={"locale": "ru"}, timeout=60)
        r.raise_for_status()
        return r.json()
    try:
        return parse_wb_tariffs(with_retry(call))
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        hint = " — токен не подошёл (просрочен, отозван или вписан с ошибкой)" if code in (401, 403) else ""
        raise TariffError(f"WB ответил {code}{hint}") from e
    except requests.RequestException as e:
        raise TariffError(f"WB недоступен: {e}") from e
    except ValueError as e:
        raise TariffError(f"ответ WB не разобран: {e}") from e


def store(db: Session, platform: str, rows: list[dict]) -> dict:
    """Записать таблицу тарифов. Не коммитит. Предмет, пропавший из ответа, не
    удаляется: на него ещё ссылаются карточки, а тариф «как был» честнее пустоты."""
    known = {t.category_id: t for t in db.query(CategoryTariff).filter(CategoryTariff.platform == platform)}
    now = now_utc()
    stats = {"rows": len(rows), "added": 0, "changed": 0}
    for row in rows:
        tariff = known.get(row["category_id"])
        if tariff is None:
            tariff = CategoryTariff(platform=platform, category_id=row["category_id"])
            db.add(tariff)
            known[row["category_id"]] = tariff
            stats["added"] += 1
        elif tariff.fbs_bp is not None and row["fbs_bp"] is not None and tariff.fbs_bp != row["fbs_bp"]:
            tariff.prev_fbs_bp, tariff.changed_at = tariff.fbs_bp, now
            stats["changed"] += 1
        tariff.category_name, tariff.parent_name = row["category_name"], row["parent_name"]
        tariff.fbs_bp, tariff.fbo_bp, tariff.loaded_at = row["fbs_bp"], row["fbo_bp"], now
    db.flush()
    return stats


def _wb_token(db: Session) -> tuple[PlatformAccount | None, str]:
    """Тарифы у WB общие, не по кабинету: годится токен любого активного кабинета."""
    for account in db.query(PlatformAccount).filter(PlatformAccount.platform == "wb",
                                                    PlatformAccount.is_active.is_(True)) \
            .order_by(PlatformAccount.id):
        token = (accounts.credentials(db, account).get("finance_token") or "").strip()
        if token:
            return account, token
    return None, ""


def refresh(db: Session, session: requests.Session | None = None) -> str:
    """Загрузить тарифы WB сейчас. Возвращает итог для человека; ошибка — тоже
    итог (записывается и показывается «Диагностикой»), а не исключение. Коммитит."""
    account, token = _wb_token(db)
    if not token:
        result = "не загружены: нет активного кабинета WB с токеном («API-ключи»)"
        ok = False
    else:
        try:
            stats = store(db, "wb", fetch_wb(token, session))
        except TariffError as e:
            db.rollback()
            result, ok = f"не загружены ({account.name}): {e}", False
        else:
            result = (f"предметов {stats['rows']}, новых {stats['added']}, "
                      f"сменился тариф FBS: {stats['changed']}")
            ok = True
    stamp = now_utc().isoformat(timespec="seconds")
    settings.put(db, settings.TARIFFS_RESULT, f"{stamp}|{result}")
    if ok:
        settings.put(db, settings.TARIFFS_OK_AT, stamp)
    db.commit()
    (logger.info if ok else logger.warning)("тарифы WB: %s", result)
    return result


def refresh_if_due(db: Session) -> str | None:
    """Для суточного задания: загрузить, если последней удачной загрузке больше
    `REFRESH_EVERY`. Неудачная попытка срок не сдвигает — повтор через час."""
    last = settings.get(db, settings.TARIFFS_OK_AT, "")
    try:
        if last and now_utc() - datetime.fromisoformat(last) < REFRESH_EVERY:
            return None
    except ValueError:
        pass
    return refresh(db)


def status_text(db: Session) -> str:
    raw = settings.get(db, settings.TARIFFS_RESULT, "")
    count = db.query(CategoryTariff).filter(CategoryTariff.platform == "wb").count()
    if not raw:
        return "ещё не загружались — загрузятся суточным заданием или кнопкой ниже"
    stamp, _, result = raw.partition("|")
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        when = None
    ok_at = settings.get(db, settings.TARIFFS_OK_AT, "")
    tail = f" В базе предметов: {count}" + (f", последняя удачная загрузка {ok_at[:10]}" if ok_at else "") + "."
    return (f"{when:%d.%m.%Y %H:%M} UTC — " if when else "") + result + "." + tail


# ---------------------------------------------------------------- предмет карточки

def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "").replace("ё", "е").replace("Ё", "Е")).strip().lower()


def remember_subjects(db: Session, lines) -> int:
    """nmID → предмет из разобранных строк отчёта WB. Не коммитит."""
    pairs: dict[str, str] = {}
    for line in lines:
        nm, subject = (line.get("platform_sku") or "").strip(), (line.get("subject") or "").strip()
        if nm and subject:
            pairs[nm[:32]] = subject[:200]
    if not pairs:
        return 0
    nms = sorted(pairs)
    known: dict[str, WbCard] = {}
    for start in range(0, len(nms), _CHUNK):
        for card in db.query(WbCard).filter(WbCard.nm_id.in_(nms[start:start + _CHUNK])):
            known[card.nm_id] = card
    now = now_utc()
    for nm in nms:
        card = known.get(nm)
        if card is None:
            db.add(WbCard(nm_id=nm, subject_name=pairs[nm], seen_at=now))
        else:
            card.subject_name, card.seen_at = pairs[nm], now
    db.flush()
    return len(pairs)


@dataclass
class CardTariff:
    subject: str
    fbs_bp: int | None
    fbo_bp: int | None
    prev_fbs_bp: int | None = None      # заполнено, только если тариф сменился недавно
    changed_at: datetime | None = None

    @property
    def fbs(self) -> Decimal | None:
        return Decimal(self.fbs_bp) / 100 if self.fbs_bp is not None else None

    @property
    def fbo(self) -> Decimal | None:
        return Decimal(self.fbo_bp) / 100 if self.fbo_bp is not None else None

    @property
    def prev_fbs(self) -> Decimal | None:
        return Decimal(self.prev_fbs_bp) / 100 if self.prev_fbs_bp is not None else None


class TariffBook:
    """Тариф по артикулу WB: nmID → предмет (`WbCard`) → тариф (`CategoryTariff`).

    Предмет ищется по НАЗВАНИЮ: в детализации отчёта WB есть только оно, без
    subjectID. Два предмета с одним названием и разными тарифами — не угадываем."""

    def __init__(self, db: Session, nm_ids):
        nms = sorted({str(n) for n in nm_ids if n})
        self.subject_of: dict[str, str] = {}
        for start in range(0, len(nms), _CHUNK):
            for nm, subject in db.query(WbCard.nm_id, WbCard.subject_name).filter(
                    WbCard.nm_id.in_(nms[start:start + _CHUNK])):
                self.subject_of[nm] = subject
        names = {_norm(s) for s in self.subject_of.values()}
        self.by_name: dict[str, CardTariff | None] = {}
        if names:
            cutoff = now_utc() - CHANGE_VISIBLE
            for t in db.query(CategoryTariff).filter(CategoryTariff.platform == "wb"):
                key = _norm(t.category_name)
                if key not in names:
                    continue
                recent = t.changed_at is not None and t.changed_at >= cutoff
                found = CardTariff(t.category_name, t.fbs_bp, t.fbo_bp,
                                   t.prev_fbs_bp if recent else None, t.changed_at if recent else None)
                if key in self.by_name:
                    other = self.by_name[key]
                    if other is None or (other.fbs_bp, other.fbo_bp) != (found.fbs_bp, found.fbo_bp):
                        self.by_name[key] = None
                    continue
                self.by_name[key] = found

    def for_cards(self, nm_ids) -> CardTariff | None:
        """Тариф позиции: у её карточек один предмет — его тариф; разные предметы
        с разными тарифами (позиция по артикулу 1С на двух карточках) — нет ответа."""
        subjects = {_norm(self.subject_of[n]) for n in nm_ids if n in self.subject_of}
        if not subjects or any(self.by_name.get(s) is None for s in subjects):
            return None
        found = [self.by_name[s] for s in sorted(subjects)]
        rates = {(t.fbs_bp, t.fbo_bp) for t in found}
        return found[0] if len(rates) == 1 else None

    def subject_for(self, nm_ids) -> str:
        subjects = sorted({self.subject_of[n] for n in nm_ids if n in self.subject_of})
        return ", ".join(subjects[:2]) + (" …" if len(subjects) > 2 else "")


def annotate(db: Session, groups) -> None:
    """Проставить позициям юнит-экономики предмет WB и текущий тариф."""
    nms = set()
    for group in groups:
        nms |= group.wb_cards
    if not nms:
        return
    book = TariffBook(db, nms)
    for group in groups:
        if group.wb_cards:
            group.wb_subject = book.subject_for(group.wb_cards)
            group.tariff = book.for_cards(group.wb_cards)
