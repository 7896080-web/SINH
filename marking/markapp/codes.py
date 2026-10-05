"""Коды поставки: заказ в СУЗ, получение, статусы, ввод в оборот (ТЗ, 7.1–7.2).

Порядок по артикулу: заказ → СУЗ готовит коды → коды получены и закреплены
за поставкой → ЧЗ сам формирует отчёт о нанесении (для lp вручную его не
отправить, ошибка 7710) → код «Нанесён» → документ «Ввод в оборот» →
«В обороте». Время до «Нанесён» нестабильно, поэтому никакого «подождать
N минут»: статусы опрашиваются фоном, в документ идут только нанесённые.

Подписывает страница (плагин), в ЧЗ ходит программа. Два правила, на
которых держится всё остальное:
- **Действие с внешним эффектом захватывается в базе ДО запроса** (new →
  sending, коммит). Вторая вкладка его не получит. Нет ответа (таймаут, 5xx)
  — состояние `unknown`: запрос МОГ выполниться, и повтор без решения
  человека значил бы двойной заказ денег или второй документ.
- **Ответ СУЗ с кодами сначала ложится на диск** (журнал, зашифрованно), потом
  разбирается: СУЗ выданные коды повторно не отдаёт.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from markapp import chz_api, chz_auth, config, settings
from markapp.crypto import decrypt_value, encrypt_value
from markapp.labels import FULL_RE, normalize
from markapp.models import (CodeOrder, GtinPair, IntroduceDoc, MarkCode, NkCard, Supply,
                            SupplyStatus)
from markapp.timeutils import now_utc, today_local

logger = logging.getLogger("marking.codes")

# Заказывать можно только под зафиксированную поставку: после перемещения
# состав не меняется, и заказанный код не окажется лишним (ТЗ, 4).
ORDER_STATUSES = (SupplyStatus.moved.value, SupplyStatus.upd_issued.value, SupplyStatus.accepted.value)
# Заказ, который занимает количество: повторно на это количество не заказываем.
# `incomplete` — буфер СУЗ закрыт, а получено меньше заказанного (блок мог
# потеряться по дороге). Занимает своё количество, как `unknown`: дозаказ — только
# решением человека, иначе каждый потерянный ответ превращался бы в платный дубль.
OPEN_ORDER = ("new", "sending", "unknown", "sent", "ready", "incomplete")
# Документ, чьи коды заняты: в новый документ они не идут.
BUSY_DOC = ("new", "sending", "unknown", "sent", "CHECKED_OK")
# Конечные статусы кода: опрашивать дальше незачем.
FINAL_CIS = ("INTRODUCED", "WRITTEN_OFF", "RETIRED", "WITHDRAWN")
SENDING_STALE = timedelta(minutes=3)
# Статусы документа в ЧЗ. Отказом считаем только названные: незнакомый статус
# коды не освобождает (их могли уже ввести), а держит документ в проверке.
DOC_REFUSED = ("CHECKED_NOT_OK", "ERROR", "PROCESSING_ERROR", "PARSE_ERROR", "CANCELLED")
DOC_IN_PROGRESS = ("", "IN_PROGRESS", "WAIT_ACCEPTANCE", "UNDEFINED")
DOC_SENT_STALE = timedelta(days=1)

STATUS_RU = {"EMITTED": "эмитирован", "APPLIED": "нанесён", "INTRODUCED": "в обороте",
             "WRITTEN_OFF": "списан", "RETIRED": "выбыл", "WITHDRAWN": "выведен",
             "UNKNOWN": "нет данных", "": "не проверен"}
ORDER_RU = {"new": "ждёт отправки", "sending": "отправляется", "unknown": "НЕИЗВЕСТНО, создан ли заказ",
            "sent": "СУЗ готовит коды", "ready": "коды готовы к получению", "done": "коды получены",
            "rejected": "отклонён СУЗ", "error": "ошибка",
            "incomplete": "СУЗ закрыл буфер, получены не все коды"}
DOC_RU = {"new": "ждёт подписи", "sending": "отправляется", "unknown": "НЕИЗВЕСТНО, принят ли",
          "sent": "проверяется ЧЗ", "CHECKED_OK": "принят", "CHECKED_NOT_OK": "отклонён",
          "error": "не отправлен"}

# Колонки файла поставки с данными документа (как в kiz-tool).
TNVED_HEADERS = ("тнвэд", "тн вэд", "код тн вэд", "код тнвэд")
PERMIT_NO_HEADERS = ("номер разрешительного документа",)
PERMIT_DATE_HEADERS = ("дата начала действия",)
CERT_TYPES = {"CONFORMITY_DECLARATION": "Декларация соответствия",
              "CONFORMITY_CERTIFICATE": "Сертификат соответствия"}

CHZ_PAUSE_UNTIL = "chz_pause_until"           # 429: опрос встаёт до этого момента (UTC, ISO)
CODES_BACKUP_AT = "codes_backup_at"           # последняя внеочередная копия после кодов
CODES_BACKUP_ERROR = "codes_backup_error"


class CodesError(Exception):
    pass


def short_cis(full: str) -> str:
    """Короткий КИ из полного — по шаблону, не «первые 31 символ» (ТЗ, 7.4)."""
    m = FULL_RE.match(normalize(full))
    if not m:
        raise CodesError("код не полный — СУЗ такой не выдаёт")
    return f"01{m.group(1)}21{m.group(2)}"


def _claim(db: Session, model, obj_id: int, frm: str, to: str) -> bool:
    """Атомарно перевести запись из `frm` в `to` и закоммитить. False — её уже
    взяли (вторая вкладка, двойной клик): SQLite пускает одного писателя.

    Вместе со статусом ставится отметка начала отправки. Без неё
    `expire_sending` мерил бы «сколько висит» от создания записи, и заказ,
    подготовленный раньше, чем за SENDING_STALE до нажатия, объявлялся бы
    `unknown` прямо посреди запроса — из соседней вкладки."""
    stamp = model.updated_at if model is CodeOrder else model.sent_at
    n = (db.query(model).filter(model.id == obj_id, model.status == frm)
         .update({model.status: to, stamp: now_utc()}, synchronize_session=False))
    db.commit()
    return n == 1


# --- План по артикулам --------------------------------------------------------------

@dataclass
class Line:
    supplier_sku: str
    gtin: str
    need: int
    codes: int = 0
    ordering: int = 0              # в открытых заказах, ещё не получено
    statuses: Counter = field(default_factory=Counter)
    orders: list = field(default_factory=list)

    @property
    def deficit(self) -> int:
        return max(0, self.need - self.codes - self.ordering)

    @property
    def introduced(self) -> int:
        return self.statuses.get("INTRODUCED", 0)

    @property
    def state(self) -> str:
        if not self.gtin:
            return "нет GTIN в справочнике"
        if self.need and self.introduced >= self.need:
            return "в обороте"
        if any(o.status == "unknown" for o in self.orders):
            return "ошибка заказа: исход неизвестен"
        if self.codes < self.need:
            if any(o.status in ("rejected", "error") for o in self.orders) and not self.ordering:
                return "ошибка заказа"
            return "заказ кодов" if self.ordering else "коды не заказаны"
        if self.statuses.get("APPLIED"):
            return "готово к вводу"
        return "ждём «Нанесён»"


def plan(db: Session, supply: Supply) -> list[Line]:
    db.flush()      # autoflush выключен: правки этого же действия иначе не видны
    need: dict[str, int] = {}
    for r in supply.rows:
        need[r.supplier_sku] = need.get(r.supplier_sku, 0) + r.qty
    pairs = {p.supplier_sku: p.gtin for p in
             db.query(GtinPair).filter(GtinPair.supplier_sku.in_(list(need))).all()}
    lines = {sku: Line(sku, pairs.get(sku, ""), qty) for sku, qty in need.items()}
    for code in db.query(MarkCode).filter(MarkCode.supply_id == supply.id).all():
        line = lines.get(code.supplier_sku)
        if line is not None:
            line.codes += 1
            line.statuses[code.status] += 1
    for o in db.query(CodeOrder).filter(CodeOrder.supply_id == supply.id).order_by(CodeOrder.id).all():
        line = lines.get(o.supplier_sku)
        if line is None:
            continue
        line.orders.append(o)
        if o.status in OPEN_ORDER:
            line.ordering += max(0, o.quantity - o.received)
    return list(lines.values())


def summary(lines: list[Line]) -> dict:
    need = sum(line.need for line in lines)
    codes = sum(line.codes for line in lines)
    introduced = sum(min(line.introduced, line.need) for line in lines)
    st = Counter()
    for line in lines:
        st.update(line.statuses)
    return {"need": need, "codes": codes, "introduced": introduced, "statuses": st,
            "done": bool(need) and introduced >= need}


def expire_sending(db: Session) -> int:
    """«Отправляется» дольше нескольких минут — процесс оборвался посреди
    запроса. Дошёл ли он, неизвестно: такое же `unknown`, как при таймауте."""
    old = now_utc() - SENDING_STALE
    n = 0
    for model in (CodeOrder, IntroduceDoc):
        stamp = model.updated_at if model is CodeOrder else model.sent_at
        for row in db.query(model).filter(model.status == "sending", stamp < old).all():
            row.status = "unknown"
            row.error = "отправка оборвалась — неизвестно, дошла ли"
            n += 1
    return n


# --- Заказ -------------------------------------------------------------------------

def _check_can_order(supply: Supply) -> None:
    if supply.is_test:
        raise CodesError("тестовая поставка — настоящие коды под неё не заказываются")
    if supply.status not in ORDER_STATUSES:
        raise CodesError("коды заказываются после перемещения в 1С: до него состав поставки может измениться")
    org = supply.organization
    if not org.oms_id:
        raise CodesError(f"у {org.name} не задан OMS ID")
    if not org.connection_id:
        raise CodesError(f"у {org.name} не задан ID соединения СУЗ — без него нет входа в СУЗ")


def suz_token(supply: Supply) -> str:
    token = chz_auth.token(supply.organization, "suz")
    if token is None:
        raise CodesError(f"нужен вход в ЧЗ ({supply.organization.name}): токена СУЗ нет или истёк")
    return token


def prepare_orders(db: Session, supply: Supply, username: str) -> list[CodeOrder]:
    """Заказы на недостающее по каждому артикулу — в базу ДО отправки.

    Неотправленные заказы прошлого раза возвращаются снова, а не дублируются.
    Заказ с неизвестным исходом занимает своё количество: пока человек не
    решит, был ли он, нового на это количество нет.
    """
    _check_can_order(supply)
    # Блокировка записи ДО расчёта недостающего. pysqlite читает вне транзакции,
    # и две вкладки, нажавшие «Заказать» одновременно, обе видели бы дефицит и
    # обе вставили бы заказы — платный дубль. Пустой UPDATE берёт блокировку
    # писателя: вторая вкладка ждёт коммита первой и считает план уже с её
    # заказами.
    db.query(Supply).filter(Supply.id == supply.id).update(
        {Supply.id: Supply.id}, synchronize_session=False)
    for line in plan(db, supply):
        if line.gtin and line.deficit > 0:
            db.add(CodeOrder(supply_id=supply.id, organization_id=supply.organization_id,
                             supplier_sku=line.supplier_sku, gtin=line.gtin, quantity=line.deficit,
                             oms_id=supply.organization.oms_id,
                             body=chz_api.order_body(line.gtin, line.deficit), created_by=username))
    db.flush()
    return (db.query(CodeOrder).filter(CodeOrder.supply_id == supply.id, CodeOrder.status == "new")
            .order_by(CodeOrder.id).all())


def send_order(db: Session, order: CodeOrder, signature: str) -> None:
    """Захват → запрос → итог. Вызывающий коммитит итог."""
    supply = db.get(Supply, order.supply_id)
    _check_can_order(supply)
    token = suz_token(supply)
    if not _claim(db, CodeOrder, order.id, "new", "sending"):
        raise CodesError("заказ уже отправляется или отправлен (другая вкладка?)")
    db.refresh(order)
    try:
        order.suz_order_id = chz_api.create_order(order.oms_id or supply.organization.oms_id,
                                                  order.body, token, signature)
        # Номер — в журнал СРАЗУ, до коммита: упади запись в базу, заказ стал бы
        # `unknown`, и номер пришлось бы искать в ЛК СУЗ по времени и GTIN.
        logger.info("заказ %s (%s, %s шт.): СУЗ создал заказ %s", order.id, order.gtin,
                    order.quantity, order.suz_order_id)
        order.status, order.error = "sent", ""
    except chz_api.ChzApiError as e:
        order.error = str(e)
        # 408 и 409 — не «заказа нет»: таймаут на стороне сервера или повтор уже
        # принятого запроса. Объяви мы их ошибкой, недостающее заказалось бы снова.
        if e.outcome_unknown or e.status in (408, 409) or "не вернул номер" in str(e):
            order.status = "unknown"
        elif e.status in (401, 429):
            # Не принят: можно повторить после входа / паузы.
            order.status = "new"
            if e.status == 401:
                chz_auth.forget(supply.organization, "suz", token)
        else:
            order.status = "error"     # 4xx: СУЗ заказ не создал
        raise CodesError(str(e))
    finally:
        order.updated_at = now_utc()


UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def resolve_unknown_order(db: Session, order: CodeOrder, suz_order_id: str) -> None:
    """Решение человека по заказу с неизвестным исходом (по ЛК СУЗ): номер
    заказа есть — продолжаем получать коды; нет — заказа не было."""
    if order.status not in ("unknown", "incomplete"):
        raise CodesError("решать нужно только заказ с неизвестным исходом или неполным получением")
    suz_order_id = (suz_order_id or "").strip()
    if order.status == "incomplete":
        if suz_order_id:
            raise CodesError("у этого заказа номер уже есть — решение только «дозаказать недостающее»")
        order.status = "error"
        order.error = (f"человек подтвердил: получено {order.received} из {order.quantity}, "
                       "остальные коды не придут — недостающее можно заказать заново")
        order.updated_at = now_utc()
        return
    if suz_order_id:
        if not UUID_RE.match(suz_order_id):
            raise CodesError("номер заказа СУЗ — это UUID вида 1b2c3d4e-…, проверьте, что скопирован целиком")
        db.flush()            # autoflush=False: иначе запрос не увидит несохранённые решения
        taken = (db.query(CodeOrder.id).filter(CodeOrder.suz_order_id == suz_order_id,
                                               CodeOrder.id != order.id).first())
        if taken:
            # Два заказа программы на один буфер СУЗ: план посчитал бы его дважды,
            # один из них упёрся бы в исчерпание и попросил дозаказ.
            raise CodesError(f"этот номер уже записан за заказом №{taken[0]} — у каждого заказа свой номер")
        order.suz_order_id, order.status = suz_order_id, "sent"
        order.error = "номер заказа указан человеком по ЛК СУЗ"
    else:
        order.status, order.error = "error", "человек подтвердил: в СУЗ заказа нет"
    order.updated_at = now_utc()


# --- Получение кодов ----------------------------------------------------------------

def steps(db: Session, supply: Supply) -> list[dict]:
    """Что подписать дальше: статус заказа или получение готовых кодов."""
    db.flush()
    out = []
    for o in (db.query(CodeOrder).filter(CodeOrder.supply_id == supply.id,
                                         CodeOrder.status.in_(("sent", "ready")))
              .order_by(CodeOrder.id).all()):
        oms = o.oms_id or supply.organization.oms_id or ""
        if o.status == "ready":
            left = o.quantity - o.received
            path = chz_api.codes_path(oms, o.suz_order_id, o.gtin,
                                      min(left, o.available) if o.available else left)
            action = "codes"
        else:
            path = chz_api.status_path(oms, o.suz_order_id, o.gtin)
            action = "status"
        out.append({"order_id": o.id, "action": action, "path": path, "sign": chz_api.signed_text(path),
                    "sku": o.supplier_sku})
    return out


def run_step(db: Session, order: CodeOrder, action: str, path: str, signature: str) -> None:
    supply = db.get(Supply, order.supply_id)
    expected = {s["order_id"]: s for s in steps(db, supply)}.get(order.id)
    # Путь строит программа; страница подписывает и возвращает его же.
    if expected is None or expected["action"] != action or expected["path"] != path:
        raise CodesError("шаг устарел — обновите страницу")
    token = suz_token(supply)
    try:
        data = chz_api.suz_get(path, token, signature)
    except chz_api.ChzApiError as e:
        order.error, order.updated_at = str(e), now_utc()
        if action == "codes" and e.outcome_unknown:
            # СУЗ мог выдать блок, а ответ не дошёл. Повторный запрос вернёт
            # СЛЕДУЮЩИЙ блок, этот на нашей стороне потерян. Если коды так и не
            # доберутся до количества, заказ станет `incomplete` и дозаказ решит
            # человек — по ЛК СУЗ, где выданные блоки видны.
            order.error = (f"{e} — ответ на получение кодов не пришёл: блок мог быть выдан. "
                           "Если коды не доберутся до заказанного, сверьте с ЛК СУЗ")
            logger.error("заказ %s (%s): получение кодов без ответа: %s", order.id, order.suz_order_id, e)
        if e.status == 401:
            chz_auth.forget(supply.organization, "suz", token)
        raise CodesError(str(e))
    if action == "status":
        # Коды из журнала — до того, как исчерпанный буфер объявит недополучение:
        # они могли прийти и не записаться в базу.
        recover_journal(db, order_id=order.id)
        _apply_status(order, data)
    else:
        _apply_codes(db, supply, order, data)
    order.updated_at = now_utc()


def _apply_status(order: CodeOrder, data) -> None:
    info = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
    buffer = info.get("bufferStatus", "")
    available = int(info.get("availableCodes") or 0)
    if buffer == "REJECTED":
        order.status, order.error = "rejected", info.get("rejectionReason") or json.dumps(info, ensure_ascii=False)
    elif buffer == "ACTIVE" and available > 0:
        order.status, order.error, order.available = "ready", "", available
    elif buffer in ("EXHAUSTED", "DELETED", "CLOSED") and order.received < order.quantity:
        # Не `error`: `error` освобождает количество под новый заказ, а коды могли
        # быть выданы и потеряться по дороге. Дозаказ — решение человека.
        order.status = "incomplete"
        order.error = (f"СУЗ закрыл буфер ({buffer}), получено {order.received} из {order.quantity}. "
                       "Сверьте с ЛК СУЗ, прежде чем дозаказывать")
    # PENDING — СУЗ ещё готовит, ждём.


def journal_dir() -> Path:
    return Path(config.BACKUP_DIR) / "codes_journal"


def _journal(order: CodeOrder, data) -> Path | None:
    """Сырой ответ СУЗ — на диск ДО разбора, зашифрованно. Упал разбор или
    запись в базу — коды восстанавливаются отсюда (`recover_journal`)."""
    try:
        d = journal_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"order{order.id}-{now_utc():%Y%m%d-%H%M%S%f}.enc"
        payload = json.dumps({"order_id": order.id, "supply_id": order.supply_id, "gtin": order.gtin,
                              "sku": order.supplier_sku, "data": data}, ensure_ascii=False)
        with open(path, "w", encoding="ascii") as f:
            f.write(encrypt_value(payload))
            f.flush()
            os.fsync(f.fileno())
        return path
    except Exception:
        logger.exception("журнал кодов не записан (заказ %s)", order.id)
        return None


def _apply_codes(db: Session, supply: Supply, order: CodeOrder, data, recovering: bool = False) -> None:
    if data is not None and not isinstance(data, dict):
        # 200 с телом не того вида: могли прийти коды, которые мы не поняли.
        # Сохраняем как есть и не делаем вид, что буфер пуст.
        journal = _journal(order, data)
        raise CodesError(f"ответ СУЗ на получение кодов не разобран — сохранён в журнале "
                         f"{journal.name if journal else '(НЕ записан!)'}")
    raw = (data or {}).get("codes")
    if not raw:
        if not recovering:
            order.status = "sent"      # буфер опустел раньше ответа — спросить статус снова
        return
    journal = None if recovering else _journal(order, data)
    if journal is None and not recovering:
        # Журнал — единственная копия кодов до коммита. Не записался — коды всё
        # равно применяем (база их сохранит), но говорим об этом громко.
        logger.error("заказ %s: журнал кодов НЕ записан — при сбое записи в базу коды не восстановить",
                     order.id)
    bad = []
    for n, full in enumerate(raw, 1):
        full = normalize(str(full))
        try:
            cis = short_cis(full)
        except CodesError:
            bad.append(f"№{n}: не полный код")
            continue
        if cis[2:16] != order.gtin:
            bad.append(f"№{n}: GTIN {cis[2:16]} вместо {order.gtin}")
            continue
        if db.query(MarkCode.id).filter(MarkCode.cis == cis).first():
            continue                    # тот же блок второй раз — код уже закреплён
        db.add(MarkCode(cis=cis, full_enc=encrypt_value(full), gtin=order.gtin,
                        supplier_sku=order.supplier_sku, supply_id=supply.id, order_id=order.id))
        db.flush()
    # По факту, а не «+= добавлено»: две вкладки не потеряют приращение.
    order.received = db.query(MarkCode).filter(MarkCode.order_id == order.id).count()
    if recovering:
        # Восстановление только добавляет коды и пересчитывает. Статус не трогаем:
        # заказ в `error` или `rejected` не должен снова открываться.
        if order.received >= order.quantity and order.status in OPEN_ORDER:
            order.status, order.error = "done", ""
        return
    order.available = None
    order.status = "done" if order.received >= order.quantity else "sent"
    order.error = "" if journal is not None else "журнал кодов не записан (см. лог) — коды в базе"
    if bad:
        order.error = (f"не принято кодов: {len(bad)} ({'; '.join(bad[:3])}) — ответ СУЗ сохранён в "
                       f"журнале {journal.name if journal else '(НЕ записан!)'}")
        logger.error("заказ %s: %s", order.id, order.error)


def recover_journal(db: Session, order_id: int | None = None) -> int:
    """Коды из журнала, которых нет в базе (сбой записи после ответа СУЗ).

    Зовётся при каждом открытии «Кодов ЧЗ» и перед разбором статуса заказа:
    без вызова журнал был бы мёртвым грузом, а исчерпанный буфер объявил бы
    недополучение по кодам, которые лежат на диске. Идемпотентно — по `cis`.
    Не коммитит."""
    added = 0
    pattern = f"order{order_id}-*.enc" if order_id is not None else "order*.enc"
    for path in sorted(journal_dir().glob(pattern)) if journal_dir().exists() else []:
        try:
            rec = json.loads(decrypt_value(path.read_text(encoding="ascii")))
        except Exception:
            logger.exception("журнал кодов не читается: %s", path.name)
            continue
        order = db.get(CodeOrder, rec["order_id"])
        # Номер заказа — не опознание: после восстановления базы из копии номера
        # выдаются заново, и журнал старого заказа №5 лёг бы на новый №5 —
        # чужие коды в чужую поставку. Сверяем всё, что записано рядом.
        if (order is None or rec.get("supply_id") != order.supply_id or rec.get("gtin") != order.gtin
                or rec.get("sku") != order.supplier_sku):
            continue
        before = order.received
        try:
            _apply_codes(db, db.get(Supply, order.supply_id), order, rec["data"], recovering=True)
        except CodesError:
            continue          # неразобранный ответ — его разбирает человек, не мы
        added += order.received - before
    return added


_bk_lock = threading.Lock()
_bk_state = {"running": False, "again": False}


def request_backup() -> None:
    """Внеочередная копия после новых кодов (ТЗ, 7.1 и 11): СУЗ их повторно не
    выдаст. Звать ПОСЛЕ коммита. Один поток; запросы во время копии склеиваются
    в ещё одну копию после неё, а не в десяток параллельных."""
    with _bk_lock:
        if _bk_state["running"]:
            _bk_state["again"] = True
            return
        _bk_state["running"] = True

    def run():
        from markapp import backup
        from markapp.database import SessionLocal
        while True:
            error = ""
            try:
                res = backup.make_backup()
                error = res.error or res.remote_error or ""
            except Exception as e:
                logger.exception("внеочередная копия после кодов не снялась")
                error = f"{type(e).__name__}: {e}"
            try:
                db = SessionLocal()
                try:
                    settings.put(db, CODES_BACKUP_AT, now_utc().isoformat(timespec="seconds"))
                    settings.put(db, CODES_BACKUP_ERROR, error[:500])
                    db.commit()
                finally:
                    db.close()
            except Exception:
                logger.exception("отметка копии после кодов не записана")
            with _bk_lock:
                if not _bk_state["again"]:
                    _bk_state["running"] = False
                    return
                _bk_state["again"] = False

    threading.Thread(target=run, name="codes-backup", daemon=True).start()


def backup_state(db: Session, supply: Supply) -> dict:
    """Попали ли последние коды поставки в копию — видно на странице (ТЗ, 11.1)."""
    from markapp import backup
    last_code = (db.query(MarkCode.received_at).filter(MarkCode.supply_id == supply.id)
                 .order_by(MarkCode.received_at.desc()).first())
    last_backup = backup.last_backup()
    return {"last_code": last_code[0] if last_code else None, "last_backup": last_backup,
            "covered": not last_code or (last_backup is not None and last_backup >= last_code[0]),
            "error": settings.get(db, CODES_BACKUP_ERROR)}


# --- Статусы кодов (фоном) --------------------------------------------------------------

def _paused(db: Session) -> bool:
    until = settings.get(db, CHZ_PAUSE_UNTIL)
    return bool(until) and until > now_utc().isoformat(timespec="seconds")


def refresh_statuses(db: Session, supply: Supply | None = None, limit: int = 1) -> dict:
    """Статусы кодов не в конечном статусе, по токену True API — пачками по 1000.

    Запрос в ЧЗ идёт без открытой записи в базу: итог пачки пишется и
    коммитится сразу, и долгий ответ ЧЗ не держит SQLite занятой.
    """
    db.commit()
    stats = {"requests": 0, "updated": 0, "note": ""}
    if _paused(db):
        stats["note"] = "ЧЗ ответил 429 — опрос на паузе"
        return stats
    q = db.query(MarkCode).filter(MarkCode.status.notin_(FINAL_CIS))
    if supply is not None:
        q = q.filter(MarkCode.supply_id == supply.id)
    by_supply: dict[int, list[MarkCode]] = {}
    for c in q.order_by(MarkCode.status_at.is_(None).desc(), MarkCode.status_at).all():
        by_supply.setdefault(c.supply_id, []).append(c)
    for supply_id, codes in by_supply.items():
        s = db.get(Supply, supply_id)
        token = chz_auth.token(s.organization)
        if token is None:
            stats["note"] = f"нужен вход в ЧЗ ({s.organization.name}) — статусы кодов не обновляются"
            continue
        for i in range(0, len(codes), chz_api.CISES_PER_REQUEST):
            if stats["requests"] >= limit:
                return stats
            chunk = codes[i:i + chz_api.CISES_PER_REQUEST]
            try:
                got = chz_api.cises_info([c.cis for c in chunk], token)
            except chz_api.ChzApiError as e:
                stats["note"] = str(e)
                if e.status == 401:
                    chz_auth.forget(s.organization, "true_api", token)
                elif e.status == 429:
                    settings.put(db, CHZ_PAUSE_UNTIL, (now_utc() + timedelta(seconds=e.retry_after or 60))
                                 .isoformat(timespec="seconds"))
                if e.status in (401, 429):
                    db.commit()
                    return stats
                # Любая другая ошибка — про эту пачку, а не про ЧЗ целиком. Отметка
                # времени отодвигает пачку в конец очереди: иначе каждый проход
                # начинался бы с неё же, и статусы ВСЕХ остальных поставок (а за
                # ними и закрытие документов) не обновлялись бы никогда.
                stats["requests"] += 1
                stats["failed"] = stats.get("failed", 0) + 1
                for c in chunk:
                    c.status_at = now_utc()
                db.commit()
                break
            stats["requests"] += 1
            now = now_utc()
            for c in chunk:
                st = got.get(c.cis)
                if st:
                    if st == "APPLIED" and c.applied_at is None:
                        c.applied_at = now
                    if st != c.status:
                        stats["updated"] += 1
                    c.status = st
                    c.status_at = now
            if chunk and not any(got.get(c.cis) for c in chunk):
                stats["note"] = "ЧЗ не вернул статусов ни по одному коду пачки"
            db.commit()
    return stats


# --- Ввод в оборот ---------------------------------------------------------------------

def _extra(supply: Supply, row, names) -> str:
    for i, h in enumerate(supply.extra_headers or []):
        if " ".join(h.split()).lower() in names and i < len(row.extras or []):
            v = str(row.extras[i] or "").strip()
            if v:
                return v
    return ""


def _iso(text: str) -> str:
    t = (text or "").strip()
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})", t)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", t)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def guess_cert_type(number: str) -> str:
    # «С» в номере сертификата пишут и кириллицей, и латиницей — на глаз не различить.
    if re.search(r"RU\s*Д-", number or ""):
        return "CONFORMITY_DECLARATION"
    if re.search(r"RU\s*[СC]-", number or ""):
        return "CONFORMITY_CERTIFICATE"
    return ""


def defaults(supply: Supply) -> dict:
    # Дата производства — без умолчания «сегодня»: это утверждение в документе
    # для ЧЗ, и подставленное программой число человек не вводил и не видел.
    d = dict(supply.intro_attrs or {})
    for k in ("tnved", "cert_type", "cert_number", "cert_date", "production_date"):
        d.setdefault(k, "")
    return d


def attrs_by_sku(db: Session, supply: Supply) -> tuple[dict[str, dict], list[str]]:
    """Данные документа по артикулу и список проблем.

    - ТН ВЭД: карточка НК (первоисточник, ТЗ 5.3) → колонка файла → умолчание
      поставки; расхождение НК и файла — проблема, а не тихий выбор.
    - Разрешительный документ берётся ЦЕЛИКОМ из одного источника: номер из
      файла — значит и дата из файла (номер одного документа с датой другого
      ЧЗ примет или отвергнет, но верным это не станет).
    - Строки одного артикула с разными данными — проблема.
    """
    base = defaults(supply)
    pairs = {p.supplier_sku: p.gtin for p in db.query(GtinPair).all()}
    from_file: dict[str, set] = {}
    for row in supply.rows:
        rec = (_extra(supply, row, TNVED_HEADERS), _extra(supply, row, PERMIT_NO_HEADERS),
               _iso(_extra(supply, row, PERMIT_DATE_HEADERS)))
        from_file.setdefault(row.supplier_sku, set())
        if any(rec):
            from_file[row.supplier_sku].add(rec)
    out, problems = {}, []
    for sku, recs in from_file.items():
        if len(recs) > 1:
            problems.append(f"{sku}: в строках файла разные ТН ВЭД или документы")
            continue
        f_tnved, f_number, f_date = next(iter(recs)) if recs else ("", "", "")
        gtin = pairs.get(sku)
        card = db.get(NkCard, gtin) if gtin else None
        nk_tnved = card.tn_ved if card is not None and card.status == "ok" else ""
        if nk_tnved and f_tnved and nk_tnved != f_tnved:
            problems.append(f"{sku}: ТН ВЭД в файле {f_tnved}, в Нацкаталоге {nk_tnved}")
        tnved = nk_tnved or f_tnved or base["tnved"]
        tnved_src = "Нацкаталог" if nk_tnved else ("файл поставки" if f_tnved else "общее для поставки")
        if f_number:
            number, cdate, cert_src = f_number, f_date, "файл поставки"
            ctype = guess_cert_type(f_number)
            if not ctype:
                # Номер из файла с типом из умолчаний поставки — документ из двух
                # источников: сертификат ушёл бы в ЧЗ декларацией.
                problems.append(f"{sku}: по номеру «{f_number}» не понять, декларация это или сертификат")
            if not f_date:
                problems.append(f"{sku}: в файле номер документа без даты начала действия")
        else:
            number, cdate, ctype = base["cert_number"], base["cert_date"], base["cert_type"]
            cert_src = "общее для поставки"
        if tnved and not re.fullmatch(r"\d{10}", tnved):
            problems.append(f"{sku}: ТН ВЭД «{tnved}» — нужно ровно 10 цифр")
        out[sku] = {"tnved": tnved, "cert_number": number, "cert_date": cdate, "cert_type": ctype,
                    "tnved_src": tnved_src, "cert_src": cert_src}
    return out, problems


def ready_codes(db: Session, supply: Supply) -> list[MarkCode]:
    """Нанесённые коды, не лежащие в документе, который ещё идёт, принят или
    с неизвестным исходом (статус кода «в обороте» догонит документ)."""
    db.flush()
    busy = {d.id for d in db.query(IntroduceDoc).filter(IntroduceDoc.supply_id == supply.id,
                                                          IntroduceDoc.status.in_(BUSY_DOC)).all()}
    return [c for c in db.query(MarkCode).filter(MarkCode.supply_id == supply.id,
                                                 MarkCode.status == "APPLIED").order_by(MarkCode.id).all()
            if c.introduce_doc_id not in busy]


def prepare_introduce(db: Session, supply: Supply, username: str) -> IntroduceDoc:
    if supply.is_test:
        raise CodesError("тестовая поставка — в оборот не вводится")
    if chz_auth.token(supply.organization) is None:
        raise CodesError(f"нужен вход в ЧЗ ({supply.organization.name})")
    # Неподписанный документ прошлого раза — выбросить: состав мог измениться.
    # Условно (`status == "new"` в самом UPDATE): документ, который соседняя
    # вкладка как раз захватила в отправку, не превратится в «ошибку» с
    # освобождёнными кодами посреди запроса.
    stale = [d.id for d in db.query(IntroduceDoc.id).filter(IntroduceDoc.supply_id == supply.id,
                                                             IntroduceDoc.status == "new").all()]
    for doc_id in stale:
        n = (db.query(IntroduceDoc).filter(IntroduceDoc.id == doc_id, IntroduceDoc.status == "new")
             .update({IntroduceDoc.status: "error", IntroduceDoc.error: "не подписан — заменён новым"},
                     synchronize_session=False))
        if n:
            for c in db.query(MarkCode).filter(MarkCode.introduce_doc_id == doc_id).all():
                c.introduce_doc_id = None
    db.flush()
    codes = ready_codes(db, supply)
    if not codes:
        raise CodesError("нет кодов со статусом «Нанесён» — ЧЗ ещё не обработал нанесение, подождите")
    attrs, problems = attrs_by_sku(db, supply)
    skus = sorted({c.supplier_sku for c in codes})
    problems = [p for p in problems if p.split(":")[0] in skus]
    for sku in skus:
        a = attrs.get(sku) or {}
        missing = [n for k, n in (("tnved", "ТН ВЭД"), ("cert_number", "номер документа"),
                                  ("cert_date", "дата документа"), ("cert_type", "тип документа"))
                   if not a.get(k)]
        if missing:
            problems.append(f"{sku}: нет {', '.join(missing)}")
    if problems:
        raise CodesError("документ не собран — " + "; ".join(problems[:5])
                         + (f" и ещё {len(problems) - 5}" if len(problems) > 5 else ""))
    inn = supply.organization.inn
    prod = defaults(supply)["production_date"]
    _check_production_date(prod)
    applied = today_local().isoformat()
    doc = {
        "participant_inn": inn, "production_date": prod, "producer_inn": inn, "owner_inn": inn,
        # Подтверждено заказчиком 05.10.2026: производство собственное,
        # производитель — сам ИП. Контрактного производства нет; появится —
        # нужен CONTRACT_PRODUCTION и ИНН фабрики, это не догадка программы.
        "production_type": "OWN_PRODUCTION",
        "products": [{
            "uit_code": c.cis, "production_date": prod, "application_date": applied,
            "tnved_code": attrs[c.supplier_sku]["tnved"],
            "certificate_document_data": [{
                "certificate_type": attrs[c.supplier_sku]["cert_type"],
                "certificate_number": attrs[c.supplier_sku]["cert_number"],
                "certificate_date": attrs[c.supplier_sku]["cert_date"],
            }],
        } for c in codes],
    }
    encoded = base64.b64encode(json.dumps(doc, ensure_ascii=False).encode("utf-8")).decode("ascii")
    row = IntroduceDoc(supply_id=supply.id, organization_id=supply.organization_id, document=encoded,
                       codes_count=len(codes), created_by=username)
    db.add(row)
    db.flush()
    for c in codes:
        c.introduce_doc_id = row.id
    # Сводка для человека ПЕРЕД подписью: что именно утверждает документ. Без
    # неё подтверждение называло только число кодов, а ТН ВЭД и разрешительный
    # документ, взятые из общих умолчаний поставки, уходили в ЧЗ невиденными.
    per_sku = Counter(c.supplier_sku for c in codes)
    lines = [f"Дата производства: {date.fromisoformat(prod):%d.%m.%Y}, производство собственное, "
             f"производитель и владелец — ИНН {inn}."]
    for sku in skus:
        a = attrs[sku]
        lines.append(f"{sku} — {per_sku[sku]} шт.: ТН ВЭД {a['tnved']} ({a['tnved_src']}); "
                     f"{CERT_TYPES.get(a['cert_type'], a['cert_type'])} {a['cert_number']} от "
                     f"{a['cert_date']} ({a['cert_src']})")
    row.summary = "\n".join(lines)
    return row


def _release(db: Session, doc: IntroduceDoc) -> None:
    for c in db.query(MarkCode).filter(MarkCode.introduce_doc_id == doc.id,
                                       MarkCode.status != "INTRODUCED").all():
        c.introduce_doc_id = None


def send_introduce(db: Session, doc: IntroduceDoc, signature: str) -> None:
    supply = db.get(Supply, doc.supply_id)
    if supply.is_test:
        raise CodesError("тестовая поставка — в оборот не вводится")
    token = chz_auth.token(supply.organization)
    if token is None:
        raise CodesError(f"нужен вход в ЧЗ ({supply.organization.name})")
    if not _claim(db, IntroduceDoc, doc.id, "new", "sending"):
        raise CodesError("документ уже отправляется или отправлен")
    db.refresh(doc)
    try:
        doc.doc_id = chz_api.create_document(doc.document, re.sub(r"\s+", "", signature), token)
        doc.sent_at, doc.error = now_utc(), ""
        # Без номера проверить итог нельзя — ждём статусов кодов, коды заняты.
        doc.status = "sent" if doc.doc_id else "unknown"
        if not doc.doc_id:
            doc.error = "ЧЗ принял документ без номера — итог будет виден по статусам кодов"
    except chz_api.ChzApiError as e:
        doc.error = str(e)
        if e.outcome_unknown:
            doc.status = "unknown"      # мог уйти: коды заняты до решения
        elif e.status in (401, 429):
            doc.status = "new"
            if e.status == 401:
                chz_auth.forget(supply.organization, "true_api", token)
        else:
            doc.status = "error"
            _release(db, doc)
        raise CodesError(str(e))


def release_unknown_doc(db: Session, doc: IntroduceDoc) -> None:
    """Решение человека: документ с неизвестным исходом в ЛК ЧЗ не найден."""
    if doc.status != "unknown":
        raise CodesError("освободить можно только документ с неизвестным исходом")
    codes = db.query(MarkCode).filter(MarkCode.introduce_doc_id == doc.id).all()
    if any(c.status == "INTRODUCED" for c in codes):
        raise CodesError("часть кодов документа уже в обороте — документ дошёл, освобождать нельзя")
    since = doc.sent_at or doc.created_at
    stale = [c for c in codes if c.status_at is None or c.status_at <= since]
    if stale:
        # Статус, снятый ДО отправки, ничего не говорит о её исходе: документ мог
        # дойти, а коды показывали бы «нанесён» — и ушли бы во второй документ.
        raise CodesError(f"сначала «Обновить статусы»: по {len(stale)} кодам статус не проверялся "
                         "после отправки документа")
    doc.status, doc.error = "error", "человек подтвердил: в ЧЗ документа нет"
    _release(db, doc)


def refresh_documents(db: Session) -> dict:
    """Итог отправленных документов. Истина — статусы кодов, это для причины отказа."""
    db.commit()
    stats = {"checked": 0, "note": ""}
    # Документ с неизвестным исходом или в проверке закрывается сам, когда все его
    # коды в обороте: статус кода — истина, ответ /doc/list — только причина отказа.
    for doc in db.query(IntroduceDoc).filter(IntroduceDoc.status.in_(("unknown", "sent"))).all():
        codes = db.query(MarkCode).filter(MarkCode.introduce_doc_id == doc.id).all()
        if codes and all(c.status == "INTRODUCED" for c in codes):
            doc.status, doc.checked_at = "CHECKED_OK", now_utc()
            doc.error = "принят — все его коды в обороте"
    db.commit()
    for doc in db.query(IntroduceDoc).filter(IntroduceDoc.status == "sent").all():
        supply = db.get(Supply, doc.supply_id)
        token = chz_auth.token(supply.organization)
        if token is None or not doc.doc_id:
            continue
        try:
            status, errors = chz_api.document_status(doc.doc_id, token)
        except chz_api.ChzApiError as e:
            stats["note"] = str(e)
            continue
        stats["checked"] += 1
        if status == "CHECKED_OK":
            doc.status, doc.error, doc.checked_at = "CHECKED_OK", errors, now_utc()
        elif status in DOC_REFUSED:
            doc.status, doc.error, doc.checked_at = "CHECKED_NOT_OK", f"{status}: {errors}".strip(": "), now_utc()
            _release(db, doc)           # отказ по документу — коды свободны для следующего
        elif status and status not in DOC_IN_PROGRESS:
            doc.error = f"ЧЗ вернул незнакомый статус документа «{status}» — ждём статусов кодов"
        elif doc.sent_at and now_utc() - doc.sent_at > DOC_SENT_STALE:
            # Сутки без итога (документ не находится по номеру, статус не меняется):
            # не держим коды вечно — решение человеку, как при неизвестном исходе.
            doc.status = "unknown"
            doc.error = (f"итога нет больше {DOC_SENT_STALE.days} сут. — проверьте документ в ЛК ЧЗ")
        db.commit()
    return stats


def _check_production_date(value: str) -> None:
    if not value:
        raise CodesError("укажите дату производства в «Данных документа» — программа её не подставляет")
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise CodesError("дата производства — ГГГГ-ММ-ДД")
    if d > today_local():
        raise CodesError(f"дата производства {d:%d.%m.%Y} — в будущем")


def save_defaults(supply: Supply, tnved: str, cert_type: str, cert_number: str, cert_date: str,
                  production_date: str) -> None:
    for v, name in ((cert_date, "дата документа"), (production_date, "дата производства")):
        if v:
            try:
                date.fromisoformat(v)
            except ValueError:
                raise CodesError(f"{name} — ГГГГ-ММ-ДД")
    if cert_type and cert_type not in CERT_TYPES:
        raise CodesError("тип документа — сертификат или декларация")
    if tnved.strip() and not re.fullmatch(r"\d{10}", tnved.strip()):
        raise CodesError("ТН ВЭД — ровно 10 цифр")
    if production_date:
        _check_production_date(production_date)
    supply.intro_attrs = {"tnved": tnved.strip(), "cert_type": cert_type, "cert_number": cert_number.strip(),
                          "cert_date": cert_date, "production_date": production_date}


# --- Выдача кодов: этикетки и файл ------------------------------------------------------

def full_codes(db: Session, supply: Supply, only_introduced: bool = False) -> list[str]:
    """Полные коды поставки в порядке строк поставки, внутри — по получению."""
    order = {}
    for r in supply.rows:
        order.setdefault(r.supplier_sku, len(order))
    q = db.query(MarkCode).filter(MarkCode.supply_id == supply.id)
    codes = sorted(q.all(), key=lambda c: (order.get(c.supplier_sku, 10 ** 6), c.id))
    if only_introduced and any(c.status != "INTRODUCED" for c in codes):
        raise CodesError("не все коды поставки в обороте — файл для Lamoda выдаётся только с кодами «в обороте»")
    return [decrypt_value(c.full_enc) for c in codes]
