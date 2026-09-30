"""Коды поставки: заказ в СУЗ, получение, статусы, ввод в оборот (ТЗ, 7.1–7.2).

Порядок по артикулу: заказ → СУЗ готовит коды → коды получены и закреплены
за поставкой → ЧЗ сам формирует отчёт о нанесении (для lp вручную его не
отправить, ошибка 7710) → код «Нанесён» → документ «Ввод в оборот» →
«В обороте». Время до «Нанесён» нестабильно, поэтому никакого «подождать
N минут»: статусы опрашиваются фоном, в документ идут только нанесённые.

Подписывает страница (плагин), в ЧЗ ходит программа. Всё, что получено от
СУЗ, пишется в базу сразу: коды СУЗ повторно не выдаёт.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from markapp import chz_api, chz_auth
from markapp.crypto import encrypt_value
from markapp.labels import FULL_RE, normalize
from markapp.models import (CodeOrder, GtinPair, IntroduceDoc, MarkCode, NkCard, Supply,
                            SupplyStatus)
from markapp.timeutils import now_utc, today_local

logger = logging.getLogger("marking.codes")

# Заказывать можно только под зафиксированную поставку: после перемещения
# состав не меняется, и заказанный код не окажется лишним (ТЗ, 4).
ORDER_STATUSES = (SupplyStatus.moved.value, SupplyStatus.upd_issued.value, SupplyStatus.accepted.value)
OPEN_ORDER = ("new", "sent", "ready")

STATUS_RU = {"EMITTED": "эмитирован", "APPLIED": "нанесён", "INTRODUCED": "в обороте",
             "WRITTEN_OFF": "списан", "RETIRED": "выбыл", "WITHDRAWN": "выведен",
             "UNKNOWN": "нет данных", "": "не проверен"}
ORDER_RU = {"new": "ждёт отправки", "sent": "СУЗ готовит коды", "ready": "коды готовы к получению",
            "done": "коды получены", "rejected": "отклонён СУЗ", "error": "ошибка"}

# Колонки файла поставки с данными документа (как в kiz-tool).
TNVED_HEADERS = ("тнвэд", "тн вэд", "код тн вэд", "код тнвэд")
PERMIT_NO_HEADERS = ("номер разрешительного документа",)
PERMIT_DATE_HEADERS = ("дата начала действия",)
CERT_TYPES = {"CONFORMITY_DECLARATION": "Декларация соответствия",
              "CONFORMITY_CERTIFICATE": "Сертификат соответствия"}


class CodesError(Exception):
    pass


def short_cis(full: str) -> str:
    """Короткий КИ из полного — по шаблону, не «первые 31 символ» (ТЗ, 7.4)."""
    m = FULL_RE.match(normalize(full))
    if not m:
        raise CodesError("код не полный — СУЗ такой не выдаёт")
    return f"01{m.group(1)}21{m.group(2)}"


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
    """
    _check_can_order(supply)
    for line in plan(db, supply):
        if line.gtin and line.deficit > 0:
            db.add(CodeOrder(supply_id=supply.id, organization_id=supply.organization_id,
                             supplier_sku=line.supplier_sku, gtin=line.gtin, quantity=line.deficit,
                             body=chz_api.order_body(line.gtin, line.deficit), created_by=username))
    db.flush()
    return (db.query(CodeOrder).filter(CodeOrder.supply_id == supply.id, CodeOrder.status == "new")
            .order_by(CodeOrder.id).all())


def send_order(db: Session, order: CodeOrder, signature: str) -> None:
    supply = db.get(Supply, order.supply_id)
    _check_can_order(supply)
    if order.status != "new":
        raise CodesError("заказ уже отправлен")
    token = suz_token(supply)
    try:
        order.suz_order_id = chz_api.create_order(supply.organization.oms_id, order.body, token,
                                                  signature)
        order.status, order.error = "sent", ""
    except chz_api.ChzApiError as e:
        # Без номера заказа СУЗ заказа нет — его можно отправить заново.
        order.error = str(e)
        if e.status is not None and e.status < 500 and e.status not in (401, 429):
            order.status = "error"
        raise CodesError(str(e))
    finally:
        order.updated_at = now_utc()


# --- Получение кодов ----------------------------------------------------------------

def steps(db: Session, supply: Supply) -> list[dict]:
    """Что подписать дальше: статус заказа или получение готовых кодов."""
    db.flush()
    oms = supply.organization.oms_id or ""
    out = []
    for o in (db.query(CodeOrder).filter(CodeOrder.supply_id == supply.id,
                                         CodeOrder.status.in_(("sent", "ready")))
              .order_by(CodeOrder.id).all()):
        if o.status == "ready":
            path = chz_api.codes_path(oms, o.suz_order_id, o.gtin, o.quantity - o.received)
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
        raise CodesError(str(e))
    if action == "status":
        _apply_status(order, data)
    else:
        _apply_codes(db, supply, order, data)
    order.updated_at = now_utc()


def _apply_status(order: CodeOrder, data) -> None:
    info = data[0] if isinstance(data, list) and data else (data or {})
    buffer = info.get("bufferStatus", "")
    if buffer == "REJECTED":
        order.status, order.error = "rejected", info.get("rejectionReason") or json.dumps(info, ensure_ascii=False)
    elif buffer == "ACTIVE" and int(info.get("availableCodes") or 0) > 0:
        order.status, order.error = "ready", ""
    elif buffer in ("EXHAUSTED", "DELETED", "CLOSED") and order.received < order.quantity:
        order.status = "error"
        order.error = f"СУЗ закрыл буфер ({buffer}), получено {order.received} из {order.quantity}"
    # PENDING — СУЗ ещё готовит, ждём.


def _apply_codes(db: Session, supply: Supply, order: CodeOrder, data) -> None:
    raw = (data or {}).get("codes") if isinstance(data, dict) else None
    if not raw:
        order.status = "sent"          # буфер опустел раньше ответа — спросить статус снова
        return
    added = 0
    for full in raw:
        full = normalize(str(full))
        cis = short_cis(full)
        if db.query(MarkCode.id).filter(MarkCode.cis == cis).first():
            continue                    # тот же блок второй раз — код уже закреплён
        db.add(MarkCode(cis=cis, full_enc=encrypt_value(full), gtin=order.gtin,
                        supplier_sku=order.supplier_sku, supply_id=supply.id, order_id=order.id))
        added += 1
    order.received += added
    order.status = "done" if order.received >= order.quantity else "sent"
    order.error = ""
    request_backup()


def request_backup() -> None:
    """Внеочередная копия после новых кодов (ТЗ, 7.1 и 11): СУЗ их повторно не выдаст."""
    def run():
        try:
            from markapp import backup
            backup.make_backup()
        except Exception:
            logger.exception("внеочередная копия после кодов не снялась")
    threading.Thread(target=run, name="codes-backup", daemon=True).start()


# --- Статусы кодов (фоном) --------------------------------------------------------------

def refresh_statuses(db: Session, supply: Supply | None = None, limit: int = 1) -> dict:
    """Статусы кодов, ещё не «в обороте», по токену True API — пачками по 1000."""
    db.flush()
    stats = {"requests": 0, "updated": 0, "note": ""}
    q = db.query(MarkCode).filter(MarkCode.status != "INTRODUCED")
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
                if e.status == 401:
                    chz_auth.forget(s.organization)
                stats["note"] = str(e)
                return stats
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
    return stats


# --- Ввод в оборот ---------------------------------------------------------------------

def _extra(supply: Supply, row, names) -> str:
    for i, h in enumerate(supply.extra_headers or []):
        if h.strip().lower() in names and i < len(row.extras or []):
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
    if re.search(r"RU\s+Д-", number or ""):
        return "CONFORMITY_DECLARATION"
    if re.search(r"RU\s+С-", number or ""):
        return "CONFORMITY_CERTIFICATE"
    return ""


def defaults(supply: Supply) -> dict:
    d = dict(supply.intro_attrs or {})
    d.setdefault("production_date", today_local().isoformat())
    for k in ("tnved", "cert_type", "cert_number", "cert_date"):
        d.setdefault(k, "")
    return d


def attrs_by_sku(db: Session, supply: Supply) -> dict[str, dict]:
    """ТН ВЭД и разрешительный документ по артикулу: файл поставки → карточка НК → умолчания."""
    base = defaults(supply)
    pairs = {p.supplier_sku: p.gtin for p in db.query(GtinPair).all()}
    out = {}
    for row in supply.rows:
        if row.supplier_sku in out:
            continue
        card = db.get(NkCard, pairs.get(row.supplier_sku, "")) if pairs.get(row.supplier_sku) else None
        number = _extra(supply, row, PERMIT_NO_HEADERS) or base["cert_number"]
        out[row.supplier_sku] = {
            "tnved": _extra(supply, row, TNVED_HEADERS) or (card.tn_ved if card else "") or base["tnved"],
            "cert_number": number,
            "cert_date": _iso(_extra(supply, row, PERMIT_DATE_HEADERS)) or base["cert_date"],
            "cert_type": guess_cert_type(number) or base["cert_type"],
        }
    return out


def ready_codes(db: Session, supply: Supply) -> list[MarkCode]:
    """Нанесённые коды, которые не лежат в документе на проверке или уже принятом
    (статус кода «в обороте» догонит документ на следующем опросе)."""
    db.flush()
    busy = {d.id for d in db.query(IntroduceDoc).filter(
        IntroduceDoc.supply_id == supply.id,
        IntroduceDoc.status.in_(("new", "sent", "CHECKED_OK"))).all()}
    return [c for c in db.query(MarkCode).filter(MarkCode.supply_id == supply.id,
                                                 MarkCode.status == "APPLIED").order_by(MarkCode.id).all()
            if c.introduce_doc_id not in busy]


def prepare_introduce(db: Session, supply: Supply, username: str) -> IntroduceDoc:
    if supply.is_test:
        raise CodesError("тестовая поставка — в оборот не вводится")
    if chz_auth.token(supply.organization) is None:
        raise CodesError(f"нужен вход в ЧЗ ({supply.organization.name})")
    # Неподписанный документ прошлого раза — выбросить: состав мог измениться.
    for d in db.query(IntroduceDoc).filter(IntroduceDoc.supply_id == supply.id,
                                           IntroduceDoc.status == "new").all():
        d.status, d.error = "error", "не подписан — заменён новым"
        for c in db.query(MarkCode).filter(MarkCode.introduce_doc_id == d.id).all():
            c.introduce_doc_id = None
    db.flush()
    codes = ready_codes(db, supply)
    if not codes:
        raise CodesError("нет кодов со статусом «Нанесён» — ЧЗ ещё не обработал нанесение, подождите")
    attrs = attrs_by_sku(db, supply)
    base = defaults(supply)
    problems = []
    for sku in sorted({c.supplier_sku for c in codes}):
        a = attrs.get(sku) or {}
        missing = [n for k, n in (("tnved", "ТН ВЭД"), ("cert_number", "номер документа"),
                                  ("cert_date", "дата документа"), ("cert_type", "тип документа"))
                   if not a.get(k)]
        if missing:
            problems.append(f"{sku}: нет {', '.join(missing)}")
    if problems:
        raise CodesError("не хватает данных документа — " + "; ".join(problems[:5])
                         + (f" и ещё {len(problems) - 5}" if len(problems) > 5 else ""))
    inn = supply.organization.inn
    prod = base["production_date"]
    doc = {
        "participant_inn": inn, "production_date": prod, "producer_inn": inn, "owner_inn": inn,
        "production_type": "OWN_PRODUCTION",
        "products": [{
            "uit_code": c.cis, "production_date": prod, "tnved_code": attrs[c.supplier_sku]["tnved"],
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
    return row


def send_introduce(db: Session, doc: IntroduceDoc, signature: str) -> None:
    supply = db.get(Supply, doc.supply_id)
    if doc.status != "new":
        raise CodesError("документ уже отправлен")
    token = chz_auth.token(supply.organization)
    if token is None:
        raise CodesError(f"нужен вход в ЧЗ ({supply.organization.name})")
    try:
        doc.doc_id = chz_api.create_document(doc.document, re.sub(r"\s+", "", signature), token)
        doc.status, doc.sent_at, doc.error = "sent", now_utc(), ""
    except chz_api.ChzApiError as e:
        doc.status, doc.error = "error", str(e)
        for c in db.query(MarkCode).filter(MarkCode.introduce_doc_id == doc.id).all():
            c.introduce_doc_id = None
        raise CodesError(str(e))


def refresh_documents(db: Session) -> dict:
    """Итог обработки отправленных документов. Истина — статусы кодов, это для причины отказа."""
    db.flush()
    stats = {"checked": 0, "note": ""}
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
        if status in ("CHECKED_OK", "CHECKED_NOT_OK", "ERROR"):
            doc.status = "CHECKED_OK" if status == "CHECKED_OK" else "CHECKED_NOT_OK"
            doc.error, doc.checked_at = errors, now_utc()
            if doc.status != "CHECKED_OK":
                # Отказ по документу — коды свободны для следующего.
                for c in db.query(MarkCode).filter(MarkCode.introduce_doc_id == doc.id,
                                                   MarkCode.status != "INTRODUCED").all():
                    c.introduce_doc_id = None
    return stats


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
    supply.intro_attrs = {"tnved": tnved.strip(), "cert_type": cert_type, "cert_number": cert_number.strip(),
                          "cert_date": cert_date, "production_date": production_date}
