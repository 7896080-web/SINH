"""Уведомления наружу: единственный путь, по которому система сама зовёт человека.

Аудит 22.09 назвал это главной слабостью на тот момент, и не в коде: внутри
система научилась видеть про себя всё — `/health`, «Диагностика», часовой отчёт
о расхождениях, — но узнать об этом мог только тот, кто пришёл и открыл
страницу. Отчёт писал строку в лог, а лог читают, когда уже что-то случилось.
Ровно тот дефект, который весь день чинили внутри («находка без читателя»),
только на уровне системы целиком: упади обе службы ночью, до утра об этом не
знал бы никто.

ЧТО БУДИТ ЧЕЛОВЕКА. Только две вещи, и обе означают «деньги идут не туда прямо
сейчас»:

  1. `/health` не зелёный — задание не работает. Остатки перестают
     синхронизироваться молча: заказы не принимаются, число на площадку не
     уходит, а площадка продолжает продавать по старому.
  2. КРИТИЧНАЯ находка отчёта — система разошлась с реальностью.

WARNING не будит НИКОГДА, и это осознанно. Жёлтых находок в исправной системе
бывает несколько штук постоянно (отрицательные остатки, расхождения со складом,
несопоставленные карточки) — они требуют разбора, но не ночью. Разбудив человека
на жёлтом один раз, мы научим его не читать и красное; тогда канал перестанет
работать весь, а не наполовину. То же правило, что с подтверждением на массовых
действиях: лишний вопрос на безопасном приучает жать «Да» не глядя.

ТИХИХ ЧАСОВ НЕТ. Оверселл — это реальные деньги и реальный товар, который уже
продан дважды; ждать до утра он не станет. Если это окажется слишком, лечится
сужением того, что считается критичным, а не окном молчания: молчание по
расписанию делает канал ненадёжным, а ненадёжному каналу перестают верить.

НИЧЕГО НЕ ЧИНИТ. Как и отчёт: только читает и рассказывает. Уведомление, которое
может что-то поменять на площадке или в 1С, начнут бояться включать.

НАСТРАИВАЕТСЯ ОКРУЖЕНИЕМ, и ни один канал не включён по умолчанию — токен бота и
пароль почты живут в `.env` рядом с ключами площадок. Не настроено ничего —
задание честно говорит об этом в `/health`, а не молчит: канал, про который никто
не знает, что он выключен, хуже отсутствующего.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from datetime import timedelta

import requests
from sqlalchemy.orm import Session

from app.models import AlertState
from app.report import CRITICAL, collect_findings
from app.routers.health import snapshot
from app.timeutils import now_utc

logger = logging.getLogger("sync_worker")

STATE_KEY = "main"

# Через сколько напомнить о ТОЙ ЖЕ, никуда не девшейся поломке. Шесть часов —
# компромисс: за смену человек получит напоминание один раз, а не привыкнет к
# потоку. Меняется переменной окружения, потому что правильное число зависит от
# того, кто и как дежурит, а не от кода.
REPEAT_AFTER = timedelta(hours=int(os.environ.get("ALERT_REPEAT_HOURS", "6")))

# Сколько ждём ответа от канала. Задание ходит часто, и залипнуть на минуту на
# недоступном Telegram нельзя: следующий цикл всё равно повторит.
TIMEOUT = 10

# Адрес, по которому человек откроет систему. По умолчанию локальный — веб
# слушает 127.0.0.1, и снаружи ссылка не откроется; если перед приложением
# стоит прокси, адрес надо задать.
BASE_URL = os.environ.get("ALERT_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def telegram_configured() -> bool:
    return bool(_env("TELEGRAM_BOT_TOKEN") and _env("TELEGRAM_CHAT_ID"))


def email_configured() -> bool:
    return bool(_env("ALERT_SMTP_HOST") and _env("ALERT_EMAIL_TO"))


def configured_channels() -> list[str]:
    channels = []
    if telegram_configured():
        channels.append("telegram")
    if email_configured():
        channels.append("email")
    return channels


def _send_telegram(subject: str, body: str) -> None:
    token = _env("TELEGRAM_BOT_TOKEN")
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": _env("TELEGRAM_CHAT_ID"),
            "text": f"{subject}\n\n{body}",
            # Без разметки намеренно: в текст попадают артикулы, имена кабинетов
            # и сообщения площадок, где встречается что угодно. Один символ `_`
            # в артикуле — и Telegram отклонит ВСЁ сообщение как битую разметку,
            # то есть тревога не доедет ровно тогда, когда она есть.
            "disable_web_page_preview": True,
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("ok"):
        # 200 с `ok: false` — обычный для Telegram способ отказать. Считать это
        # доставкой значит молча потерять тревогу.
        raise RuntimeError(f"telegram: {payload.get('description') or payload}")


def _send_email(subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = _env("ALERT_EMAIL_FROM") or _env("ALERT_SMTP_USER") or _env("ALERT_EMAIL_TO")
    message["To"] = _env("ALERT_EMAIL_TO")
    message.set_content(body)

    port = int(_env("ALERT_SMTP_PORT") or "587")
    host = _env("ALERT_SMTP_HOST")
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT)
    else:
        server = smtplib.SMTP(host, port, timeout=TIMEOUT)
    try:
        if port != 465 and _env("ALERT_SMTP_TLS") != "0":
            server.starttls()
        if _env("ALERT_SMTP_USER"):
            server.login(_env("ALERT_SMTP_USER"), _env("ALERT_SMTP_PASSWORD"))
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:                            # noqa: BLE001
            pass


def deliver(subject: str, body: str) -> tuple[list[str], list[str]]:
    """Разослать по всем настроенным каналам. Возвращает (доехало, не доехало).

    Канал, который упал, НЕ роняет остальные и не роняет задание: тревога,
    потерянная из-за недоступного SMTP, — это тревога, которой не было.
    """
    sent, failed = [], []
    for name, fn in (("telegram", _send_telegram), ("email", _send_email)):
        if name == "telegram" and not telegram_configured():
            continue
        if name == "email" and not email_configured():
            continue
        try:
            fn(subject, body)
            sent.append(name)
        except Exception as e:                       # noqa: BLE001 — см. docstring
            failed.append(f"{name}: {type(e).__name__}: {e}"[:200])
            logger.warning("уведомление не ушло (%s): %s", name, e)
    return sent, failed


def build_alarm(db: Session) -> tuple[list[str], list[str]]:
    """Что сейчас не так: (отпечаток картины, строки для человека).

    Отпечаток — не для чтения, а для сравнения с прошлым разом: пока он тот же,
    повторяться незачем. Поэтому в него идут только ключи и имена, без чисел,
    которые скачут сами по себе (возраст, счётчики). Иначе «та же самая
    поломка» выглядела бы новой на каждом цикле, и мы бы слали каждые пять минут
    ровно то, от чего эта функция защищает.
    """
    signature: list[str] = []
    lines: list[str] = []

    body, _status = snapshot(db)
    if not body["ok"]:
        broken = [w["worker"] for w in body["workers"] if not w["ok"]]
        missing = list(body.get("missing_workers") or [])
        for name in broken:
            signature.append(f"worker:{name}")
        for name in missing:
            signature.append(f"missing:{name}")
        if body.get("reason"):
            signature.append("scheduler:down")
            lines.append(f"• {body['reason']}")
        if broken:
            lines.append("• Задания не работают: " + ", ".join(sorted(broken)))
        if missing:
            lines.append("• Задания не запускались вовсе: " + ", ".join(sorted(missing)))
        lines.append("  → Остатки не синхронизируются: заказы могут не приниматься, "
                     "а новое число не уходить на площадки.")

    # Находки берём у отчёта, а не считаем заново: разойдись они, уведомление
    # говорило бы не то, что человек увидит, открыв страницу.
    for finding in collect_findings(db):
        if finding.level != CRITICAL:
            continue
        signature.append(f"finding:{finding.key}")
        lines.append(f"• {finding.title}")
        lines.append(f"  → {finding.consequence}")

    return signature, lines


def run_alert_cycle(db: Session) -> dict:
    """Один проход: посмотреть, решить, надо ли говорить, сказать.

    Возвращает статистику для лога и heartbeat. Ничего не шлёт, если картина не
    изменилась и таймер напоминания не вышел.
    """
    stats = {"channels": len(configured_channels()), "sent": 0, "failed": 0,
             "action": "none"}
    if not configured_channels():
        stats["action"] = "not_configured"
        return stats

    signature, lines = build_alarm(db)
    fingerprint = "|".join(sorted(signature))

    state = db.query(AlertState).filter(AlertState.key == STATE_KEY).first()
    if state is None:
        state = AlertState(key=STATE_KEY, level="clear")
        db.add(state)
        db.flush()

    now = now_utc()
    was_alarm = state.level == "alarm"

    if not signature:
        # Всё в порядке. Отбой шлём, только если до него была тревога: сообщение
        # «всё хорошо» на системе, которая и так была в порядке, — это спам,
        # который обесценивает настоящее.
        if not was_alarm:
            return stats
        subject = "[Sync Admin] Отбой — система в порядке"
        body = ("Проблемы, о которых сообщали раньше, больше не видны.\n\n"
                f"Проверить: {BASE_URL}/diagnostics")
        sent, failed = deliver(subject, body)
        stats["sent"], stats["failed"] = len(sent), len(failed)
        stats["action"] = "clear"
        if sent:
            # Состояние двигаем ТОЛЬКО на удачной отправке: иначе отбой,
            # не доехавший из-за сети, был бы потерян навсегда, и человек
            # остался бы с тревогой, которая давно кончилась.
            state.level = "clear"
            state.signature = ""
            state.last_sent_at = now
        return stats

    changed = fingerprint != (state.signature or "")
    stale = (state.last_sent_at is None
             or now - state.last_sent_at >= REPEAT_AFTER)
    if was_alarm and not changed and not stale:
        stats["action"] = "quiet"
        return stats

    head = "[Sync Admin] Требует внимания"
    if was_alarm and not changed:
        head = "[Sync Admin] Всё ещё требует внимания"
    body = "\n".join(lines) + f"\n\nРазобрать: {BASE_URL}/report"
    sent, failed = deliver(head, body)
    stats["sent"], stats["failed"] = len(sent), len(failed)
    stats["action"] = "alarm"
    if sent:
        # Тот же принцип: не доехало — не запоминаем, повторим следующим циклом.
        state.level = "alarm"
        state.signature = fingerprint
        state.last_sent_at = now
    return stats
