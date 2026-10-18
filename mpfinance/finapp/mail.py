"""Отчёты Lamoda с почты: письма от b2b@lamoda.ru, вложения .xlsx.

Ящик — по IMAP (SSL), ТОЛЬКО ЧТЕНИЕ: папка открывается `readonly`, письма
читаются `BODY.PEEK` — ничего не помечается прочитанным, не перемещается и не
удаляется. Что уже разобрано, программа помнит сама (`MailMessage`, по
Message-ID), поэтому человек может читать эту почту как обычно.

Вложение разбирается тем же импортом, что и файл со страницы «Отчёты площадок»:
еженедельный отчёт ложится предварительными неделями, отчёт комиссионера —
итоговым отчётом с коррекцией (`finance/lamoda.py`).

Пароль хранится зашифрованным ключом программы. Для Gmail и Яндекс 360 нужен
пароль приложения (обычный пароль по IMAP они не принимают).
"""
from __future__ import annotations

import email
import email.policy
import hashlib
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime

from cryptography.fernet import InvalidToken
from sqlalchemy.orm import Session

from finapp import progress, settings
from finapp.crypto import decrypt_value, encrypt_value
from finapp.models import MailMessage, PlatformAccount
from finapp.timeutils import now_utc, today_local

logger = logging.getLogger("mpfinance.mail")

HOST, PORT, USER, PASSWORD = "mail_host", "mail_port", "mail_user", "mail_password"
FOLDER, SENDER, ACCOUNT = "mail_folder", "mail_sender", "mail_account_id"
LAST_CHECK, LAST_RESULT = "mail_last_check_at", "mail_last_result"
DEFAULT_SENDER = "b2b@lamoda.ru"
LOOKBACK_DAYS = 45
ATTACHMENT_RE = re.compile(r"\.(xlsx|zip)$", re.IGNORECASE)
# Месяцы для SINCE — по-английски всегда: `%b` зависит от локали, а на русской
# Windows дал бы «окт», и сервер почты отверг бы запрос.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class MailError(RuntimeError):
    pass


@dataclass
class MailSettings:
    host: str
    port: int
    user: str
    password: str
    folder: str
    sender: str
    account_id: int | None

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.password)


def load(db: Session) -> MailSettings:
    raw_password = settings.get(db, PASSWORD)
    try:
        password = decrypt_value(raw_password) if raw_password else ""
    except InvalidToken:
        password = ""
    port = settings.get(db, PORT, "993")
    account = settings.get(db, ACCOUNT)
    return MailSettings(host=settings.get(db, HOST).strip(), port=int(port) if port.isdigit() else 993,
                        user=settings.get(db, USER).strip(), password=password,
                        folder=settings.get(db, FOLDER, "INBOX").strip() or "INBOX",
                        sender=settings.get(db, SENDER, DEFAULT_SENDER).strip() or DEFAULT_SENDER,
                        account_id=int(account) if account.isdigit() else None)


def save(db: Session, *, host: str, port: str, user: str, password: str, folder: str, sender: str,
         account_id: str) -> None:
    """Пустой пароль сохранённый не стирает (поле на странице не показывается)."""
    settings.put(db, HOST, host.strip())
    settings.put(db, PORT, port.strip() if port.strip().isdigit() else "993")
    settings.put(db, USER, user.strip())
    if password.strip():
        settings.put(db, PASSWORD, encrypt_value(password.strip()))
    settings.put(db, FOLDER, folder.strip() or "INBOX")
    settings.put(db, SENDER, sender.strip() or DEFAULT_SENDER)
    settings.put(db, ACCOUNT, account_id.strip() if account_id.strip().isdigit() else "")


def _since(days: int) -> str:
    day = today_local() - timedelta(days=days)
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


def _header(raw: bytes) -> email.message.EmailMessage:
    return email.message_from_bytes(raw, policy=email.policy.default)


def _message_id(msg) -> str:
    mid = (msg.get("Message-ID") or "").strip()
    if mid:
        return mid[:255]
    # Без Message-ID — отпечаток по дате, теме и отправителю.
    basis = f"{msg.get('Date')}|{msg.get('Subject')}|{msg.get('From')}"
    return "sha:" + hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()


def _fetch_bytes(imap, num, what: str) -> bytes:
    typ, data = imap.fetch(num, f"(BODY.PEEK[{what}])")
    if typ != "OK":
        raise MailError(f"письмо {num!r} не прочитано: {typ}")
    return b"".join(part[1] for part in data if isinstance(part, tuple))


def fetch_new(db: Session, connect=None, days: int = LOOKBACK_DAYS) -> dict:
    """Разобрать новые письма с отчётами. Коммитит по письму."""
    from finapp.finance.importer import import_report

    cfg = load(db)
    stats = {"configured": cfg.configured, "messages": 0, "imported": 0, "failed": 0, "skipped": 0}
    if not cfg.configured:
        return stats
    if cfg.account_id is not None and db.get(PlatformAccount, cfg.account_id) is None:
        cfg.account_id = None
    progress.report(0, None, f"вход в почту {cfg.host}")
    try:
        imap = (connect or (lambda h, p: imaplib.IMAP4_SSL(h, p, timeout=60)))(cfg.host, cfg.port)
        imap.login(cfg.user, cfg.password)
    except (imaplib.IMAP4.error, OSError) as e:
        raise MailError(f"почта {cfg.host}: вход не удался ({e})") from e
    try:
        typ, _ = imap.select(f'"{cfg.folder}"', readonly=True)
        if typ != "OK":
            raise MailError(f"нет папки «{cfg.folder}» в ящике")
        typ, data = imap.search(None, "FROM", f'"{cfg.sender}"', "SINCE", _since(days))
        if typ != "OK":
            raise MailError(f"поиск писем не удался: {typ}")
        numbers = (data[0] or b"").split()
        progress.report(0, len(numbers), f"писем от {cfg.sender}: {len(numbers)}")
        for i, num in enumerate(numbers, start=1):
            progress.report(i - 1, len(numbers), f"письмо {i} из {len(numbers)}")
            head = _header(_fetch_bytes(imap, num, "HEADER"))
            mid = _message_id(head)
            if db.query(MailMessage.id).filter(MailMessage.message_id == mid).first():
                continue
            stats["messages"] += 1
            msg = _header(_fetch_bytes(imap, num, ""))
            record = MailMessage(message_id=mid, sender=str(msg.get("From") or "")[:255],
                                 subject=str(msg.get("Subject") or "")[:500])
            try:
                record.received_at = parsedate_to_datetime(msg.get("Date")).astimezone(
                    timezone.utc).replace(tzinfo=None) if msg.get("Date") else None
            except (TypeError, ValueError):
                record.received_at = None
            names, notes, imported, repeated = [], [], 0, 0
            for part in msg.iter_attachments():
                name = part.get_filename() or ""
                if not ATTACHMENT_RE.search(name):
                    continue
                names.append(name)
                try:
                    outcome = import_report(db, platform="lamoda",
                                            file_bytes=part.get_payload(decode=True) or b"",
                                            filename=name, user="почта", account_id=cfg.account_id,
                                            source="email")
                except Exception as e:           # noqa: BLE001 — одно письмо не уносит очередь
                    # Неожиданный сбой разбора одного вложения записывается в
                    # письмо, а не роняет проход: иначе это письмо и все более
                    # новые не разбирались бы до выпадения из окна поиска (45 дней).
                    db.rollback()
                    logger.exception("письмо %s: вложение %s не разобрано", mid, name)
                    notes.append(f"{name}: не разобран ({type(e).__name__}: {e})"[:400])
                    continue
                if outcome.report is not None:
                    imported += 1
                    notes.append(f"{name}: " + (" ".join(outcome.warnings) or "загружен"))
                else:
                    db.rollback()
                    # Тот же отчёт прислали второй раз — не ошибка.
                    if outcome.errors and all("уже загружен" in e for e in outcome.errors):
                        repeated += 1
                    notes.append(f"{name}: " + " ".join(outcome.errors)[:400])
            record.attachments = "; ".join(names)[:2000] or None
            failed = bool(names) and not imported and repeated < len(names)
            record.status = "imported" if imported else ("failed" if failed else "skipped")
            record.note = "\n".join(notes)[:4000] or "в письме нет вложений .xlsx/.zip"
            db.add(record)
            db.commit()
            stats[record.status] += 1
    finally:
        try:
            imap.logout()
        except Exception:                        # noqa: BLE001 — выход из ящика не важен
            pass
    settings.put(db, LAST_CHECK, now_utc().isoformat(timespec="seconds"))
    settings.put(db, LAST_RESULT, f"писем {stats['messages']}: загружено {stats['imported']}, "
                                  f"с ошибкой {stats['failed']}, без отчётов {stats['skipped']}")
    db.commit()
    return stats
