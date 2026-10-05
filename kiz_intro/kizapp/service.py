"""Логика: PDF из ЛК ЧЗ → коды → статусы → ввод в оборот → txt.

Полный код с криптохвостом есть только в картинках DataMatrix (текстом в PDF —
короткий КИ, разорванный переносом), поэтому страницы рендерятся и читаются
zxing-cpp в режиме Plain — он отдаёт код с настоящим GS.

Правила, перенесённые из «Маркировки» (аудит 01.10.2026):
- действие с внешним эффектом захватывается в базе ДО запроса (new → sending);
  нет ответа — `unknown`: документ МОГ уйти, повтор — только решением человека;
- в документ идут только коды «Нанесён»; итог — по статусам кодов;
- ТН ВЭД и разрешительный документ — из карточки Нацкаталога.
"""
from __future__ import annotations

import base64
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from kizapp import chz
from kizapp.crypto import decrypt, encrypt
from kizapp.models import Batch, Card, Code, Doc, Journal, Org, now_utc

GS = "\x1d"
FULL_RE = re.compile(r"^01(\d{14})21(.{13})\x1d91(.{4})\x1d92(.{44})$", re.S)
QTY_RE = re.compile(r"quantity_(\d+)", re.I)
MAX_PDF = 30 * 1024 * 1024
RENDER_SCALE = 3
FINAL = ("INTRODUCED", "WRITTEN_OFF", "RETIRED", "WITHDRAWN")
BUSY_DOC = ("new", "sending", "unknown", "sent", "CHECKED_OK")
EXPIRY_MARGIN = timedelta(minutes=5)

STATUS_RU = {"EMITTED": "эмитирован", "APPLIED": "нанесён", "INTRODUCED": "в обороте",
             "WRITTEN_OFF": "списан", "RETIRED": "выбыл", "WITHDRAWN": "выведен", "": "не проверен"}
DOC_RU = {"new": "ждёт подписи", "sending": "отправляется", "unknown": "НЕИЗВЕСТНО, принят ли",
          "sent": "проверяется ЧЗ", "CHECKED_OK": "принят", "CHECKED_NOT_OK": "отклонён", "error": "не отправлен"}
CERT_RU = {"CONFORMITY_DECLARATION": "Декларация", "CONFORMITY_CERTIFICATE": "Сертификат"}


class KizError(Exception):
    pass


def log(db: Session, action: str, details: str = "") -> None:
    db.add(Journal(action=action, details=details[:2000]))


# --- Организации и вход --------------------------------------------------------------
# ИП несколько: у каждого свой вход в ЧЗ (токен), свой справочник карточек НК,
# свои партии. Операция берёт ИП своей партии, а не «текущий вход».

def org_of(db: Session, batch: Batch) -> Org:
    return db.get(Org, batch.org_id)


def save_org(db: Session, org_id: int | None, name: str, inn: str) -> Org:
    inn = inn.strip()
    if not (inn.isdigit() and len(inn) in (10, 12)):
        raise KizError("ИНН — 10 или 12 цифр")
    clash = db.query(Org).filter(Org.inn == inn, Org.id != (org_id or 0)).first()
    if clash:
        raise KizError(f"ИП с ИНН {inn} уже есть")
    o = db.get(Org, org_id) if org_id else None
    if org_id and o is None:
        raise KizError("организация не найдена")
    if o is None:
        o = Org(inn=inn)
        db.add(o)
    elif o.inn != inn:
        o.token_enc, o.token_until = None, None      # токен прежнего ИНН — чужой
        db.query(Card).filter(Card.org_id == o.id).delete()   # и справочник — чужой
    o.name, o.inn = name.strip() or f"ИНН {inn}", inn
    db.flush()
    return o


def token(o: Org) -> str | None:
    if not o.token_enc or o.token_until is None or o.token_until - EXPIRY_MARGIN <= now_utc():
        return None
    return decrypt(o.token_enc)


def forget(o: Org, rejected: str) -> None:
    if token(o) in (None, rejected):
        o.token_enc, o.token_until = None, None


def login(db: Session, o: Org, uuid: str, signature: str, cert_inn: str) -> datetime:
    inn = chz.signer_inn(signature) or "".join(ch for ch in cert_inn if ch.isdigit()) or None
    if inn is None:
        raise KizError("ИНН сертификата не прочитан — вход не выполнен")
    if not chz.inn_matches(o.inn, inn):
        raise KizError(f"сертификат ИНН {inn}, а у организации {o.inn} — вход не выполнен")
    try:
        t = chz.sign_in(uuid, signature)
    except chz.ChzError as e:
        raise KizError(str(e))
    o.token_enc, o.token_until = encrypt(t), chz.token_expires(t)
    log(db, "login", f"{o.name}, ИНН {inn}")
    return o.token_until


# --- PDF и загрузка ---------------------------------------------------------------------

def normalize(raw: str) -> str:
    return raw.strip().replace("_x001D_", GS).replace("_x001d_", GS).replace("\\u001d", GS)


def short_cis(full: str) -> str:
    m = FULL_RE.match(normalize(full))
    if not m:
        raise KizError("код не полный")
    return f"01{m.group(1)}21{m.group(2)}"


def codes_from_pdf(data: bytes) -> list[str]:
    import pypdfium2 as pdfium
    import zxingcpp
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception as e:
        raise KizError(f"файл не читается как PDF: {e}")
    out = []
    for i in range(len(pdf)):
        img = pdf[i].render(scale=RENDER_SCALE).to_pil()
        for r in zxingcpp.read_barcodes(img, formats=zxingcpp.BarcodeFormat.DataMatrix,
                                        text_mode=zxingcpp.TextMode.Plain):
            out.append(normalize(r.text))
    return out


def load_pdf(db: Session, o: Org, data: bytes, filename: str) -> Batch:
    if len(data) > MAX_PDF:
        raise KizError("файл больше 30 МБ")
    found = codes_from_pdf(data)
    if not found:
        raise KizError("в файле не найдено ни одного кода DataMatrix")
    problems = []
    bad = [n for n, c in enumerate(found, 1) if not FULL_RE.match(c)]
    if bad:
        problems.append(f"не полные коды: №{', №'.join(map(str, bad[:5]))}")
    full = [c for c in found if FULL_RE.match(c)]
    if len(set(full)) != len(full):
        problems.append("коды в файле повторяются")
    cis = [short_cis(c) for c in full]
    taken = {x for (x,) in db.query(Code.cis).filter(Code.cis.in_(cis)).all()}
    if taken:
        problems.append(f"уже загружены: {len(taken)} кодов (этот файл загружали?)")
    m = QTY_RE.search(filename or "")
    expected = int(m.group(1)) if m else None
    if expected is not None and expected != len(found):
        problems.append(f"в имени файла {expected} шт., прочитано {len(found)}")
    if problems:
        raise KizError("; ".join(problems))
    b = Batch(org_id=o.id, title=(filename or "")[:300], expected=expected)
    db.add(b)
    db.flush()
    for code, s in zip(full, cis):
        db.add(Code(batch_id=b.id, cis=s, full_enc=encrypt(code), gtin=s[2:16]))
    log(db, "load", f"партия #{b.id} ({o.name}): {filename}, кодов {len(full)}")
    db.flush()
    return b


# --- Справочник карточек НК (свой у каждого ИП) -------------------------------------------

def card(db: Session, org_id: int, gtin: str) -> Card | None:
    return db.query(Card).filter(Card.org_id == org_id, Card.gtin == gtin).first()


def refresh_cards(db: Session, batch: Batch, force: bool = False) -> str:
    """Карточки НК для GTIN партии — в справочник ИП партии. Пусто — всё в порядке."""
    gtins = sorted({g for (g,) in db.query(Code.gtin).filter(Code.batch_id == batch.id).distinct()})
    return fetch_cards(db, org_of(db, batch), gtins, force)


def fetch_cards(db: Session, o: Org, gtins: list[str], force: bool = True) -> str:
    """Запросить карточки (≤25 GTIN за запрос) токеном ИП и положить в его справочник."""
    t = token(o)
    need = [g for g in gtins if force or card(db, o.id, g) is None or not card(db, o.id, g).fetched_at]
    if not need:
        return ""
    if t is None:
        return f"нужен вход в ЧЗ ({o.name}) — карточки НК не запрошены"
    for i in range(0, len(need), 25):
        part = need[i:i + 25]
        try:
            got = chz.nk_products(part, t)
        except chz.ChzError as e:
            if e.status == 401:
                forget(o, t)
            return str(e)
        for g in part:
            c = card(db, o.id, g) or Card(org_id=o.id, gtin=g)
            db.add(c)
            d = got.get(g)
            if d is None:
                c.error, c.fetched_at = "карточки в НК нет", now_utc()
                continue
            c.name, c.tnved = d["name"], d["tnved"]
            c.permit_type, c.permit_number, c.permit_date = d["permit_type"], d["permit_number"], d["permit_date"]
            c.error, c.fetched_at = "", now_utc()
    return ""


# --- Статусы ------------------------------------------------------------------------------

def refresh_statuses(db: Session, batch: Batch) -> str:
    db.commit()           # запрос в ЧЗ — без открытой записи в базу
    o = org_of(db, batch)
    t = token(o)
    if t is None:
        return "нужен вход в ЧЗ — статусы не обновлены"
    codes = db.query(Code).filter(Code.batch_id == batch.id, Code.status.notin_(FINAL)).all()
    for i in range(0, len(codes), chz.CISES_PER_REQUEST):
        chunk = codes[i:i + chz.CISES_PER_REQUEST]
        try:
            got = chz.cises_info([c.cis for c in chunk], t)
        except chz.ChzError as e:
            if e.status == 401:
                forget(o, t)
            db.commit()
            return str(e)
        now = now_utc()
        for c in chunk:
            if got.get(c.cis):
                c.status, c.status_at = got[c.cis], now
        db.commit()
    refresh_documents(db, batch)
    return ""


def refresh_documents(db: Session, batch: Batch) -> str:
    o = org_of(db, batch)
    t = token(o)
    for d in db.query(Doc).filter(Doc.batch_id == batch.id, Doc.status == "unknown").all():
        codes = db.query(Code).filter(Code.doc_id == d.id).all()
        if codes and all(c.status == "INTRODUCED" for c in codes):
            d.status, d.error = "CHECKED_OK", "принят — все его коды в обороте"
    db.commit()
    if t is None:
        return ""
    for d in db.query(Doc).filter(Doc.batch_id == batch.id, Doc.status == "sent").all():
        if not d.chz_doc_id:
            continue
        try:
            st, errs = chz.document_status(d.chz_doc_id, t)
        except chz.ChzError as e:
            return str(e)
        if st in ("CHECKED_OK", "CHECKED_NOT_OK", "ERROR"):
            d.status = "CHECKED_OK" if st == "CHECKED_OK" else "CHECKED_NOT_OK"
            d.error = errs
            if d.status != "CHECKED_OK":
                _release(db, d)
        db.commit()
    return ""


# --- Сводка ---------------------------------------------------------------------------------

def summary(db: Session, batch: Batch) -> dict:
    db.flush()
    codes = db.query(Code).filter(Code.batch_id == batch.id).all()
    st = Counter(c.status for c in codes)
    by_gtin = defaultdict(Counter)
    for c in codes:
        by_gtin[c.gtin][c.status] += 1
    rows = [{"gtin": g, "count": sum(cnt.values()), "statuses": cnt, "card": card(db, batch.org_id, g)}
            for g, cnt in sorted(by_gtin.items())]
    total = len(codes)
    return {"total": total, "statuses": st, "rows": rows, "introduced": st.get("INTRODUCED", 0),
            "done": bool(total) and st.get("INTRODUCED", 0) == total, "ready": len(ready_codes(db, batch))}


# --- Ввод в оборот --------------------------------------------------------------------------

def ready_codes(db: Session, batch: Batch) -> list[Code]:
    db.flush()
    busy = {d.id for d in db.query(Doc).filter(Doc.batch_id == batch.id, Doc.status.in_(BUSY_DOC))}
    return [c for c in db.query(Code).filter(Code.batch_id == batch.id, Code.status == "APPLIED")
            .order_by(Code.id) if c.doc_id not in busy]


def set_production_date(batch: Batch, value: str) -> None:
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise KizError("дата производства — ГГГГ-ММ-ДД")
    if d > date.today():
        raise KizError("дата производства не может быть в будущем")
    batch.production_date = d.isoformat()


def prepare(db: Session, batch: Batch) -> Doc:
    o = org_of(db, batch)
    if token(o) is None:
        raise KizError("нужен вход в ЧЗ")
    if not batch.production_date:
        raise KizError("не указана дата производства")
    for d in db.query(Doc).filter(Doc.batch_id == batch.id, Doc.status == "new"):
        d.status, d.error = "error", "не подписан — заменён новым"
        _release(db, d)
    db.flush()
    codes = ready_codes(db, batch)
    if not codes:
        raise KizError("нет кодов «Нанесён» — ЧЗ ещё не обработал нанесение, подождите")
    problems, attrs = [], {}
    for g in sorted({c.gtin for c in codes}):
        cd = card(db, o.id, g)
        if cd is None or not cd.fetched_at or cd.error:
            problems.append(f"GTIN {g}: нет карточки НК в справочнике {o.name} ({cd.error if cd else 'не запрошена'})")
            continue
        if not re.fullmatch(r"\d{10}", cd.tnved or ""):
            problems.append(f"GTIN {g}: в карточке НК нет ТН ВЭД из 10 цифр")
        if not (cd.permit_number and cd.permit_date and cd.permit_type):
            problems.append(f"GTIN {g}: в карточке НК нет разрешительного документа с датой")
        attrs[g] = cd
    if problems:
        raise KizError("документ не собран — " + "; ".join(problems))
    prod = batch.production_date
    doc = {
        "participant_inn": o.inn, "production_date": prod, "producer_inn": o.inn, "owner_inn": o.inn,
        "production_type": "OWN_PRODUCTION",
        "products": [{
            "uit_code": c.cis, "production_date": prod, "tnved_code": attrs[c.gtin].tnved,
            "certificate_document_data": [{"certificate_type": attrs[c.gtin].permit_type,
                                           "certificate_number": attrs[c.gtin].permit_number,
                                           "certificate_date": attrs[c.gtin].permit_date}],
        } for c in codes],
    }
    row = Doc(batch_id=batch.id, codes_count=len(codes),
              document=base64.b64encode(json.dumps(doc, ensure_ascii=False).encode()).decode("ascii"))
    db.add(row)
    db.flush()
    for c in codes:
        c.doc_id = row.id
    return row


def doc_summary(doc: Doc) -> list[str]:
    body = json.loads(base64.b64decode(doc.document))
    cnt = Counter((p["tnved_code"], p["certificate_document_data"][0]["certificate_number"],
                   p["certificate_document_data"][0]["certificate_date"]) for p in body["products"])
    return ([f"{n} шт.: ТН ВЭД {t}; {num} от {d}" for (t, num, d), n in cnt.items()]
            + [f"дата производства {body['production_date']}; производитель и владелец — ИНН {body['owner_inn']}"])


def _release(db: Session, doc: Doc) -> None:
    for c in db.query(Code).filter(Code.doc_id == doc.id, Code.status != "INTRODUCED"):
        c.doc_id = None


def send(db: Session, doc: Doc, signature: str) -> None:
    o = org_of(db, db.get(Batch, doc.batch_id))
    t = token(o)
    if t is None:
        raise KizError("нужен вход в ЧЗ")
    n = db.query(Doc).filter(Doc.id == doc.id, Doc.status == "new").update({Doc.status: "sending"},
                                                                          synchronize_session=False)
    db.commit()
    if n != 1:
        raise KizError("документ уже отправляется или отправлен")
    db.refresh(doc)
    try:
        doc.chz_doc_id = chz.create_document(doc.document, signature, t)
        doc.sent_at, doc.error = now_utc(), ""
        doc.status = "sent" if doc.chz_doc_id else "unknown"
        if not doc.chz_doc_id:
            doc.error = "ЧЗ принял документ без номера — итог будет виден по статусам кодов"
        log(db, "introduce", f"партия #{doc.batch_id}: {doc.codes_count} кодов, документ {doc.chz_doc_id or '?'}")
    except chz.ChzError as e:
        doc.error = str(e)
        if e.outcome_unknown:
            doc.status = "unknown"
        elif e.status in (401, 429):
            doc.status = "new"
            if e.status == 401:
                forget(o, t)
        else:
            doc.status = "error"
            _release(db, doc)
        log(db, "introduce_failed", f"партия #{doc.batch_id}: {doc.status} — {e}")
        raise KizError(str(e))


def release_unknown(db: Session, doc: Doc) -> None:
    if doc.status != "unknown":
        raise KizError("освободить можно только документ с неизвестным исходом")
    doc.status, doc.error = "error", "человек подтвердил: в ЧЗ документа нет"
    _release(db, doc)


# --- Выдача --------------------------------------------------------------------------------

def txt(db: Session, batch: Batch) -> bytes:
    """Полный код на строку, настоящий GS внутри, CRLF после каждой, без BOM — как СУЗ."""
    codes = db.query(Code).filter(Code.batch_id == batch.id).order_by(Code.id).all()
    if not codes:
        raise KizError("в партии нет кодов")
    if any(c.status != "INTRODUCED" for c in codes):
        raise KizError("не все коды партии в обороте")
    log(db, "txt", f"партия #{batch.id}: {len(codes)} кодов")
    return "".join(decrypt(c.full_enc) + "\r\n" for c in codes).encode("ascii")


def full_codes(db: Session, batch: Batch) -> list[str]:
    """Полные коды партии в порядке загрузки — для этикеток (печать по желанию,
    до ввода в оборот тоже: этикетку клеят раньше, чем ЧЗ ставит «в обороте»)."""
    codes = db.query(Code).filter(Code.batch_id == batch.id).order_by(Code.id).all()
    if not codes:
        raise KizError("в партии нет кодов")
    return [decrypt(c.full_enc) for c in codes]
