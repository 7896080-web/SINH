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

НАСТРАИВАЕТСЯ СО СТРАНИЦЫ «Уведомления», и ни один канал не включён по
умолчанию. Значения читаются через `settings_store`: строка в базе, а если её
нет — `.env`. Хранить их только в файле было хуже по трём причинам — `.env`
читается один раз на импорте (правка без перезапуска службы не действует, а про
перезапуск забывают), токен лежал бы открытым текстом рядом с
`SECRETS_ENCRYPTION_KEY`, и правку файла никто не видит. Не настроено ничего —
задание честно говорит об этом в `/health`, а не молчит: канал, про который никто
не знает, что он выключен, хуже отсутствующего.
"""

from __future__ import annotations

import logging
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
# потоку. Настраивается, потому что правильное число зависит от того, кто и как
# дежурит, а не от кода.
DEFAULT_REPEAT_HOURS = 6

# Сколько ждём ответа от канала. Задание ходит часто, и залипнуть на минуту на
# недоступном Telegram нельзя: следующий цикл всё равно повторит.
TIMEOUT = 10

# Адрес, по которому человек откроет систему. По умолчанию локальный — веб
# слушает 127.0.0.1, и снаружи ссылка не откроется; если перед приложением
# стоит прокси, адрес надо задать.
DEFAULT_BASE_URL = "http://127.0.0.1:8000"


def _env(db: Session, name: str) -> str:
    """Значение настройки: страница «Уведомления», иначе `.env`.

    Вся развилка живёт в `settings_store` — здесь её повторять нельзя: разойдись
    два места, страница показывала бы одно, а уведомление уходило бы по другому.
    """
    from app import settings_store

    return settings_store.get(db, name)


def repeat_after(db: Session) -> timedelta:
    raw = _env(db, "ALERT_REPEAT_HOURS")
    try:
        hours = int(raw)
    except (TypeError, ValueError):
        hours = DEFAULT_REPEAT_HOURS
    # Ноль и отрицательное — это «напоминать каждый цикл», то есть каждые пять
    # минут: ровно тот поток, от которого дедупликация и защищает. Опечатка в
    # поле не должна уметь отключить защиту.
    return timedelta(hours=hours if hours > 0 else DEFAULT_REPEAT_HOURS)


def base_url(db: Session) -> str:
    return (_env(db, "ALERT_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def ping_alive(db: Session) -> str:
    """Дёрнуть внешнего сторожа: «воркер жив». Пустая строка — всё хорошо.

    ЭТО ЗАКРЫВАЕТ ГЛАВНУЮ ДЫРУ ОСТАЛЬНОГО МЕХАНИЗМА. Задание `alerts` живёт
    ВНУТРИ воркера: умер воркер — умерли и уведомления, и именно тот сценарий,
    ради которого всё затевалось («ночью обе службы легли»), остался бы
    непокрытым. Изнутри эту задачу решить нельзя в принципе: процесс, которого
    нет, не может сообщить, что его нет.

    Поэтому обратная полярность — «сторож мёртвого человека». Мы не сообщаем о
    беде, а регулярно сообщаем, что живы; перестали — внешний сервис пишет
    человеку САМ. Подходит любой, кто умеет ждать пинг по URL (healthchecks.io,
    Better Stack, push-монитор Uptime Kuma): адрес задаётся в `.env`, и ни один
    конкретный сервис в код не зашит.

    Дёргаем на КАЖДОМ прогоне, независимо от того, всё ли в порядке внутри:
    сторож отвечает ровно на один вопрос — «жив ли воркер». Смешай мы сюда
    второй смысл («и всё ли хорошо»), человек получал бы от сторожа сигнал,
    неотличимый от падения службы, а о настоящей беде ему и так скажет обычное
    уведомление.
    """
    url = _env(db, "ALERT_HEARTBEAT_URL")
    if not url:
        return ""
    try:
        requests.get(url, timeout=TIMEOUT).raise_for_status()
        return ""
    except Exception as e:                           # noqa: BLE001
        # Не доехало — молча не глотаем: сторож, о котором мы думаем, что он
        # сторожит, хуже отсутствующего. Но и задание не роняем: его дело —
        # уведомления, а не пинг.
        logger.warning("сторож не отвечает (%s): %s", url, e)
        return f"{type(e).__name__}: {e}"[:200]


def telegram_configured(db: Session) -> bool:
    return bool(_env(db, "TELEGRAM_BOT_TOKEN") and _env(db, "TELEGRAM_CHAT_ID"))


def email_configured(db: Session) -> bool:
    return bool(_env(db, "ALERT_SMTP_HOST") and _env(db, "ALERT_EMAIL_TO"))


def configured_channels(db: Session) -> list[str]:
    channels = []
    if telegram_configured(db):
        channels.append("telegram")
    if email_configured(db):
        channels.append("email")
    return channels


def _send_telegram(cfg: dict, subject: str, body: str) -> None:
    token = cfg["TELEGRAM_BOT_TOKEN"]
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": cfg["TELEGRAM_CHAT_ID"],
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


def _send_email(cfg: dict, subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = (cfg["ALERT_EMAIL_FROM"] or cfg["ALERT_SMTP_USER"]
                       or cfg["ALERT_EMAIL_TO"])
    message["To"] = cfg["ALERT_EMAIL_TO"]
    message.set_content(body)

    # Порт с опечаткой не должен ронять отправку молча: непонятное значение
    # трактуем как умолчание, а не как повод не отправить ничего.
    try:
        port = int(cfg["ALERT_SMTP_PORT"] or "587")
    except (TypeError, ValueError):
        port = 587
    host = cfg["ALERT_SMTP_HOST"]
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT)
    else:
        server = smtplib.SMTP(host, port, timeout=TIMEOUT)
    try:
        if port != 465 and cfg["ALERT_SMTP_TLS"] != "0":
            server.starttls()
        if cfg["ALERT_SMTP_USER"]:
            server.login(cfg["ALERT_SMTP_USER"], cfg["ALERT_SMTP_PASSWORD"])
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:                            # noqa: BLE001
            pass


# Что нужно каждому каналу. Читаем ОДНИМ заходом перед отправкой, а не по полю
# из недр `_send_*`: иначе правка настройки посреди отправки собрала бы письмо
# наполовину из старых значений, наполовину из новых.
CHANNEL_KEYS = [
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "ALERT_SMTP_HOST", "ALERT_SMTP_PORT", "ALERT_SMTP_USER",
    "ALERT_SMTP_PASSWORD", "ALERT_EMAIL_TO", "ALERT_EMAIL_FROM", "ALERT_SMTP_TLS",
]


def deliver(db: Session, subject: str, body: str) -> tuple[list[str], list[str]]:
    """Разослать по всем настроенным каналам. Возвращает (доехало, не доехало).

    Канал, который упал, НЕ роняет остальные и не роняет задание: тревога,
    потерянная из-за недоступного SMTP, — это тревога, которой не было.
    """
    cfg = {name: _env(db, name) for name in CHANNEL_KEYS}
    sent, failed = [], []
    for name, fn in (("telegram", _send_telegram), ("email", _send_email)):
        if name == "telegram" and not (cfg["TELEGRAM_BOT_TOKEN"] and cfg["TELEGRAM_CHAT_ID"]):
            continue
        if name == "email" and not (cfg["ALERT_SMTP_HOST"] and cfg["ALERT_EMAIL_TO"]):
            continue
        try:
            fn(cfg, subject, body)
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
    channels = configured_channels(db)
    stats = {"channels": len(channels), "sent": 0, "failed": 0,
             "action": "none"}
    if not channels:
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
                f"Проверить: {base_url(db)}/diagnostics")
        sent, failed = deliver(db, subject, body)
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
             or now - state.last_sent_at >= repeat_after(db))
    if was_alarm and not changed and not stale:
        stats["action"] = "quiet"
        return stats

    head = "[Sync Admin] Требует внимания"
    if was_alarm and not changed:
        head = "[Sync Admin] Всё ещё требует внимания"
    body = "\n".join(lines) + f"\n\nРазобрать: {base_url(db)}/report"
    sent, failed = deliver(db, head, body)
    stats["sent"], stats["failed"] = len(sent), len(failed)
    stats["action"] = "alarm"
    if sent:
        # Тот же принцип: не доехало — не запоминаем, повторим следующим циклом.
        state.level = "alarm"
        state.signature = fingerprint
        state.last_sent_at = now
    return stats
