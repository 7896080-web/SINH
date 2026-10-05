"""Почему погас кабинет — ПОЛНЫЙ текст причины. Только чтение.

Запуск:
    C:\\sync_admin\\.venv\\Scripts\\python.exe C:\\sync_admin\\scripts\\probe_accounts.py
    ... probe_accounts.py --off          # только погашенные
    ... probe_accounts.py 3              # один кабинет по номеру

Предохранитель гасит кабинет на пятом сбое ПОДРЯД и причину записывает:
`PlatformAccount.last_error` (до 2000 символов) и запись журнала
`account_auto_disabled` с именем, числом сбоёв и текстом. То есть ответ на
вопрос «что случилось» в базе есть ВСЕГДА.

Читателя у него, однако, почти не было. На «Диагностике» текст висит в
`title` — всплывающей подсказке у жёлтого бейджа «сбоев подряд: N», — а это
не читатель: глазами не видно, по RDP наводить мышкой мучительно, скопировать
в переписку нельзя, и длинный ответ площадки подсказка обрежет. Человек видит
«отключён» и число сбоёв, а чем именно ответила площадка — нет. Поэтому текст
печатается здесь ЦЕЛИКОМ, и здесь же лежит то, чего на странице нет вовсе:
когда именно погас (из журнала) и что в последний раз сказали per-account
задания этого кабинета.

**Три кабинета, погасшие разом, — это отдельный вопрос, и он главный.** У
каждого ИП свой токен, значит одновременный отказ пяти попыток подряд по всем
трём почти наверняка общий: лёг сам WB, кончилась сеть, забанен поставщик,
сменилось имя ручки. Поэтому вывод заканчивается сводкой: если причина у
нескольких кабинетов ОДНА, скрипт говорит это прямо — иначе человек идёт
менять ключи по одному там, где чинить надо не ключи.

Ничего не меняет и не включает: включение кабинета — действие наружу (он
сразу начнёт опрашивать площадку и рассылать остатки), и делается оно на
странице «API-ключи», где заодно сбрасывается счётчик.
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal                       # noqa: E402
from app.models import (AuditLog, PlatformAccount,           # noqa: E402
                        WorkerHeartbeat)
from app.workers.circuit_breaker import FAILURE_THRESHOLD    # noqa: E402

# Консоль боевого сервера пишет в cp1251, и один символ, которого в ней нет,
# роняет ВЕСЬ вывод посреди строки. Тело ответа площадки из `last_error` —
# ровно те чужие данные, где может оказаться что угодно: «?» вместо символа
# несравнимо лучше, чем оборванный ответ на вопрос «что случилось».
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):  # перенаправленный вывод, старый Python
    pass

# Задания, которые заводятся ПО КАБИНЕТУ: их отметка — последнее, что кабинет
# успел сказать до того, как его сняли. У погашенного кабинета задания больше
# нет вовсе, значит отметка застыла на моменте поломки — именно это и нужно.
ACCOUNT_WORKERS = ("poll_orders_account_", "catalog_poll_account_")


def _error_head(text: str, limit: int = 300) -> str:
    """Начало причины для СВОДКИ — не для разбора.

    Полный текст печатается у самого кабинета; здесь нужен только отпечаток,
    по которому видно, что у трёх кабинетов причина одна и та же.
    """
    return " ".join((text or "").split())[:limit]


def main() -> int:
    args = [a for a in (arg.strip() for arg in sys.argv[1:]) if a]
    only_off = "--off" in args
    wanted = [a for a in args if a != "--off"]

    db = SessionLocal()
    try:
        accounts = db.query(PlatformAccount).order_by(PlatformAccount.id).all()
        if wanted:
            picked = []
            for value in wanted:
                rows = [a for a in accounts
                        if str(a.id) == value
                        or value.upper() in (a.name or "").upper()]
                if not rows:
                    print(f"«{value}» — ни номер, ни имя кабинета. Есть:")
                    for a in accounts:
                        print(f"  {a.id}  {a.platform.value:5} {a.name}")
                    return 1
                picked.extend(rows)
            accounts = picked
        if only_off:
            accounts = [a for a in accounts if not a.is_active]

        if not accounts:
            print("подходящих кабинетов нет"
                  + (" — ни один не отключён" if only_off else ""))
            return 0

        print("=" * 78)
        print(f"КАБИНЕТЫ: {len(accounts)}   порог предохранителя: "
              f"{FAILURE_THRESHOLD} сбоев подряд")
        print("Время везде UTC. Боевой сервер живёт по Москве (UTC+3):")
        print("прибавьте 3 часа, чтобы сойтись с логами и журналом.")
        print("=" * 78)

        same_cause = defaultdict(list)
        for account in accounts:
            state = "РАБОТАЕТ" if account.is_active else "ОТКЛЮЧЁН"
            print()
            print("-" * 78)
            print(f"[{account.id}] {account.name}   {account.platform.value.upper()}"
                  f"   {state}")
            print("-" * 78)
            print(f"  сбоев подряд: {account.consecutive_failures}"
                  f"   склад: {account.warehouse_id or '-'}"
                  f"   рассылка: {'вкл' if account.dispatch_enabled else 'ПАУЗА'}")

            # Полный текст, без обрезки: он и есть ответ на вопрос.
            print()
            if account.last_error:
                print("  ПРИЧИНА (последняя ошибка, как ответила площадка):")
                for line in str(account.last_error).splitlines() or [""]:
                    print(f"    {line}")
                same_cause[_error_head(account.last_error)].append(account.name)
            else:
                print("  ПРИЧИНА: пусто — поле очищено успешным опросом"
                      " (кабинет гасили не сбоями, а руками)")

            if account.last_connection_check_at:
                mark = "ок" if account.last_connection_ok else "ОТКАЗ"
                print()
                print(f"  проверка подключения {account.last_connection_check_at}"
                      f": {mark}")
                print(f"    {account.last_connection_message or ''}")

            # КОГДА погас — этого на странице нет вовсе, а именно по времени
            # и сходятся три кабинета между собой и с логом.
            events = (db.query(AuditLog)
                      .filter(AuditLog.action == "account_auto_disabled",
                              AuditLog.details.contains(account.name))
                      .order_by(AuditLog.id.desc()).limit(5).all())
            print()
            if events:
                print("  КОГДА ГАСИЛ ПРЕДОХРАНИТЕЛЬ (последние 5):")
                for e in reversed(events):
                    print(f"    {e.created_at}  {e.details}")
            else:
                print("  предохранитель этот кабинет не гасил ни разу"
                      " (журнал хранится год)")

            marks = (db.query(WorkerHeartbeat)
                     .filter(WorkerHeartbeat.worker_name.in_(
                         [p + str(account.id) for p in ACCOUNT_WORKERS]))
                     .all())
            print()
            print("  ЧТО В ПОСЛЕДНИЙ РАЗ СКАЗАЛИ ЕГО ЗАДАНИЯ:")
            for hb in sorted(marks, key=lambda h: h.worker_name):
                ok = "успех" if hb.last_success else "ОШИБКА"
                print(f"    {hb.worker_name:28} {hb.last_run_at}  {ok}")
                if hb.last_error:
                    print(f"      {_error_head(hb.last_error, 600)}")
            if not marks:
                print("    отметок нет — задания по этому кабинету не стартовали")

        # Сводка. Три кабинета, погасшие разом, — это про ОБЩУЮ причину, и
        # сказать это надо вслух: иначе человек пойдёт менять ключи по одному
        # там, где чинить надо не ключи.
        shared = {cause: names for cause, names in same_cause.items()
                  if len(names) > 1}
        print()
        print("=" * 78)
        print("ИТОГО")
        print("=" * 78)
        off = [a for a in accounts if not a.is_active]
        print(f"  отключено: {len(off)} из {len(accounts)}"
              + (" — " + ", ".join(a.name for a in off) if off else ""))
        if shared:
            for cause, names in shared.items():
                print()
                print(f"  ОДНА И ТА ЖЕ ПРИЧИНА у {len(names)} кабинетов: "
                      + ", ".join(names))
                print(f"    {cause}")
            print()
            print("  У каждого кабинета свой токен, значит дело почти наверняка"
                  " НЕ в ключах:")
            print("  смотрите сторону площадки и сеть — лёг сам API, забанен"
                  " поставщик,")
            print("  кончился лимит, сменилось имя ручки. Ключи по одному"
                  " менять незачем.")
        elif len(off) > 1:
            print()
            print("  причины РАЗНЫЕ — разбирайте каждый кабинет отдельно")
        print()
        print("  Включают кабинет на «API-ключах» (там же сбрасывается"
              " счётчик сбоев).")
        print("  Этот скрипт только читает и ничего не включает: включённый"
              " кабинет")
        print("  сразу опрашивает площадку и рассылает остатки.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
