"""Национальный каталог: карточки по GTIN (ТЗ, 5.3).

`GET <True API>/nk/product?gtins=a;b;…&apikey=…` — по API-ключу, без подписи,
значит фоном из воркера. Лимит жёсткий: 10 запросов за 5 минут и не больше 25
GTIN в запросе. Работаем на 70% (7 за 5 минут, настройка) через ограничитель в
базе — веб и воркер два процесса. Этикетки и проверки читают кэш (`NkCard`).

Разбор атрибутов одежды — НАСТРОЙКА, а не догадка в коде: документация
показывает `good_attrs` только на примере сметаны, а имена атрибутов («Цвет»,
«Размер одежды»…) известны лишь по живому ответу. Сырой ответ хранится
целиком, страница карточки показывает все атрибуты, и нужные имена человек
вписывает в настройки. Карточки после этого перечитывать не надо —
`reparse_all` разбирает сохранённое заново.

Владельца карточки (ИНН) и признаки `goodMarkFlag`/`goodTurnFlag` этот метод
не отдаёт — это `/product/info`, ему нужен токен ЧЗ (этап 4).
"""
from __future__ import annotations

import json
import os
from datetime import timedelta

import requests
from sqlalchemy.orm import Session

from markapp import settings
from markapp.crypto import decrypt_value
from markapp.models import GtinPair, NkCard, NkRequest, Organization
from markapp.timeutils import now_utc

TRUE_API_URL = os.environ.get("MARKING_TRUE_API_URL", "https://markirovka.crpt.ru/api/v3/true-api")
# Необязательный HTTP(S)-прокси до ЧЗ (ТЗ, 10.5). С рабочего компьютера ЧЗ
# доступен напрямую; прокси — на случай другой сети. CORS-прокси kiz-tool тут
# не нужен: в ЧЗ ходит программа, а не страница браузера.
CHZ_PROXY = os.environ.get("MARKING_CHZ_PROXY", "")

MAX_GTINS_PER_REQUEST = 25
WINDOW = timedelta(minutes=5)
RETRY_ERRORS_AFTER = timedelta(hours=1)

NK_LIMIT = settings.NK_LIMIT
NK_BLOCKED_UNTIL = settings.NK_BLOCKED_UNTIL
ATTR_COLOR = settings.NK_ATTR_COLOR
ATTR_SIZE = settings.NK_ATTR_SIZE
ATTR_TNVED = settings.NK_ATTR_TNVED


def attr_names(db: Session, key: str) -> list[str]:
    return [n.strip() for n in settings.get(db, key).split(";") if n.strip()]


def pick_attr(attrs: list[dict], names: list[str]) -> str:
    """Первое непустое значение атрибута из списка имён (без учёта регистра).
    Порядок имён — приоритет: «Размер одежды» раньше просто «Размер»."""
    by_name: dict[str, str] = {}
    for a in attrs:
        name = str(a.get("attr_name", "")).strip().lower()
        value = str(a.get("attr_value", "") or "").strip()
        if name and value and name not in by_name:
            by_name[name] = value
    for n in names:
        if n.lower() in by_name:
            return by_name[n.lower()]
    return ""


def gtin_of_item(item: dict) -> str:
    for ident in item.get("identified_by") or []:
        if ident.get("type") == "gtin" and ident.get("value"):
            return str(ident["value"]).zfill(14)
    return str(item.get("gtin", "")).zfill(14) if item.get("gtin") else ""


def apply_item(db: Session, card: NkCard, item: dict) -> None:
    attrs = item.get("good_attrs") or []
    card.status = "ok"
    card.error = ""
    card.good_id = str(item.get("good_id", "") or "")
    card.name = str(item.get("good_name", "") or "")[:500]
    card.card_status = str(item.get("good_status", "") or "")[:64]
    card.attrs = [{k: a.get(k) for k in ("attr_id", "attr_name", "attr_value", "attr_group_name")}
                  for a in attrs]
    card.raw = json.dumps(item, ensure_ascii=False)
    card.color = pick_attr(attrs, attr_names(db, ATTR_COLOR))[:200]
    card.size = pick_attr(attrs, attr_names(db, ATTR_SIZE))[:100]
    card.tn_ved = pick_attr(attrs, attr_names(db, ATTR_TNVED))[:20]
    card.fetched_at = now_utc()


def reparse_all(db: Session) -> int:
    """Разобрать сохранённые ответы заново — после правки имён атрибутов."""
    n = 0
    for card in db.query(NkCard).filter(NkCard.status == "ok").all():
        try:
            apply_item(db, card, json.loads(card.raw))
            n += 1
        except ValueError:
            continue
    return n


# --- Очередь и ограничитель -------------------------------------------------------

def queue_missing(db: Session) -> int:
    """Карточку ждёт каждый GTIN справочника, которого ещё нет в кэше.

    flush обязателен: сессия живёт с autoflush=False, и пары, добавленные тем же
    действием (импорт, выгрузка FBO), запрос иначе не увидел бы — очередь
    карточек молча осталась бы пустой.
    """
    db.flush()
    have = {g for (g,) in db.query(NkCard.gtin).all()}
    added = 0
    for (g,) in db.query(GtinPair.gtin).all():
        if g not in have:
            db.add(NkCard(gtin=g, status="pending"))
            have.add(g)
            added += 1
    return added


def request_refresh(db: Session, gtin: str) -> None:
    card = db.get(NkCard, gtin)
    if card is None:
        db.add(NkCard(gtin=gtin, status="pending"))
    else:
        card.status = "pending"
        card.requested_at = now_utc()


def budget(db: Session, org_id: int) -> int:
    """Сколько запросов можно сделать прямо сейчас."""
    blocked = settings.get(db, NK_BLOCKED_UNTIL)
    if blocked and blocked > now_utc().isoformat(timespec="seconds"):
        return 0
    limit = int(settings.get(db, NK_LIMIT) or 7)
    used = (db.query(NkRequest).filter(NkRequest.organization_id == org_id,
                                       NkRequest.at > now_utc() - WINDOW).count())
    return max(0, limit - used)


def _due(db: Session, limit: int) -> list[NkCard]:
    retry_before = now_utc() - RETRY_ERRORS_AFTER
    pending = (db.query(NkCard).filter(NkCard.status == "pending")
               .order_by(NkCard.requested_at).limit(limit).all())
    if len(pending) < limit:
        pending += (db.query(NkCard).filter(NkCard.status == "error",
                                            NkCard.requested_at < retry_before)
                    .order_by(NkCard.requested_at).limit(limit - len(pending)).all())
    return pending


class NkClient:
    def __init__(self, api_key: str, base_url: str = TRUE_API_URL, proxy: str = CHZ_PROXY):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.proxies = {"http": proxy, "https": proxy} if proxy else None

    def product(self, gtins: list[str]) -> requests.Response:
        # Ключ — в параметре apikey; токен в заголовке НЕ передаём: методы НК
        # отвечают 400, если указаны оба.
        return requests.get(f"{self.base_url}/nk/product",
                            params={"gtins": ";".join(gtins), "apikey": self.api_key},
                            headers={"accept": "application/json"},
                            proxies=self.proxies, timeout=30)


def fetch_due(db: Session, client_factory=NkClient) -> dict:
    """Один проход: сколько позволяет ограничитель, пачками по 25."""
    stats = {"requests": 0, "ok": 0, "not_found": 0, "error": 0, "note": ""}
    queue_missing(db)
    db.commit()
    org = settings.lamoda_org(db)
    if org is None or not org.nk_api_key_enc:
        waiting = db.query(NkCard).filter(NkCard.status == "pending").count()
        if waiting:
            stats["note"] = f"нет API-ключа Нацкаталога у ИП для Lamoda — ждут карточки: {waiting}"
        return stats
    client = client_factory(decrypt_value(org.nk_api_key_enc))
    while budget(db, org.id) > 0:
        batch = _due(db, MAX_GTINS_PER_REQUEST)
        if not batch:
            break
        gtins = [c.gtin for c in batch]
        try:
            resp = client.product(gtins)
            status = resp.status_code
        except requests.RequestException as e:
            status, resp = None, None
            err = f"сеть: {type(e).__name__}"
        db.add(NkRequest(organization_id=org.id, http_status=status))
        stats["requests"] += 1
        now = now_utc()
        for c in batch:
            c.requested_at = now
            c.organization_id = org.id
        if status == 200:
            try:
                items = resp.json().get("result") or []
            except ValueError:
                items = None
            if items is None:
                for c in batch:
                    c.status, c.error = "error", "ответ не JSON"
                stats["error"] += len(batch)
            else:
                found = {}
                for item in items:
                    g = gtin_of_item(item)
                    if g:
                        found[g] = item
                for c in batch:
                    if c.gtin in found:
                        apply_item(db, c, found[c.gtin])
                        stats["ok"] += 1
                    else:
                        c.status, c.error, c.fetched_at = "not_found", "карточки в НК нет", now
                        stats["not_found"] += 1
        elif status == 404:
            for c in batch:
                c.status, c.error, c.fetched_at = "not_found", "карточки в НК нет", now
            stats["not_found"] += len(batch)
        elif status == 429:
            # Наша оценка предела неверна — молотить дальше значит идти к
            # блокировке участника. Пауза на окно, запись в отметку задания.
            settings.put(db, NK_BLOCKED_UNTIL, (now + WINDOW).isoformat(timespec="seconds"))
            for c in batch:
                c.status, c.error = "pending", "429: превышен лимит НК, пауза"
            stats["note"] = "НК ответил 429 — пауза 5 минут"
            db.commit()
            break
        else:
            text = err if status is None else f"HTTP {status}: {resp.text[:200]}"
            if status in (401, 403):
                text = f"HTTP {status}: ключ Нацкаталога не принят — проверьте его на странице организации"
            for c in batch:
                c.status, c.error = "error", text
            stats["error"] += len(batch)
            stats["note"] = text
            db.commit()
            break
        db.commit()
    return stats


def card_for(db: Session, gtin: str) -> NkCard | None:
    return db.get(NkCard, gtin) if gtin else None


def set_api_key(db: Session, org: Organization, key: str) -> None:
    from markapp.crypto import encrypt_value
    org.nk_api_key_enc = encrypt_value(key.strip()) if key.strip() else None
