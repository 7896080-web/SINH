"""Командная строка: awer-bot <команда>. Запуск без установки: python -m awer_sales_bot <команда>."""
from __future__ import annotations

import argparse
import logging
import os
import tempfile
from datetime import date, datetime
from pathlib import Path

from . import config as config_mod
from .db import DB
from .messages import pct


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="awer-bot", description="Планы продавцов AWER и бот MAX")
    ap.add_argument("-c", "--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="создать базу")
    h = sub.add_parser("import-history", help="история из ведомостей ЗП (папка с xlsx)")
    h.add_argument("folder")
    h.add_argument("--admins", nargs="*", default=[], help='"Фамилия Имя" старших продавцов')
    s = sub.add_parser("import-sales", help="импорт выгрузки 1С (файл или папка входящих)")
    s.add_argument("file", nargs="?")
    sub.add_parser("sellers", help="список продавцов")
    inv = sub.add_parser("invite", help="код привязки к боту для продавца")
    inv.add_argument("name")
    role = sub.add_parser("set-role", help="роль: seller | admin | test (тестовая карточка: сообщения есть, в ведомость не идёт)")
    role.add_argument("name")
    role.add_argument("role", choices=["seller", "admin", "test"])
    al = sub.add_parser("alias", help="другое написание продавца в 1С → существующий продавец")
    al.add_argument("alias")
    al.add_argument("name")
    pl = sub.add_parser("plan", help="показать / зафиксировать план месяца")
    pl.add_argument("month")
    pl.add_argument("--freeze", action="store_true")
    dr = sub.add_parser("daily", help="ежедневная рассылка за вчера (вручную)")
    dr.add_argument("--today", help="YYYY-MM-DD (по умолчанию сегодня)")
    dr.add_argument("--dry-run", action="store_true", help="печать вместо отправки")
    dr.add_argument("--force", action="store_true", help="не ждать закрытия дня")
    cl = sub.add_parser("close-month", help="итоги месяца и ставки")
    cl.add_argument("month")
    bt = sub.add_parser("backtest", help="проверка плана на прошлых месяцах: план и факт по продавцам (ничего не пишет)")
    bt.add_argument("first", help="первый месяц, YYYY-MM")
    bt.add_argument("last", nargs="?", help="последний месяц (по умолчанию = первый)")
    sub.add_parser("check-max", help="проверить связь с MAX и токены ботов (ничего не отправляет)")
    sub.add_parser("bot", help="запустить бота (long polling + расписание)")
    sv = sub.add_parser("serve", help="бот + админка в одном процессе (для службы Windows)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8080)
    bk = sub.add_parser("backup", help="снимок базы с проверкой целостности (задача копирования в облако)")
    bk.add_argument("--dest", default="backups")
    bk.add_argument("--keep-days", type=int, default=30)
    bk.add_argument("--log", help="дописать результат в этот файл (UTF-8)")
    dc = sub.add_parser("doctor", help="состояние: данные 1С, план месяца, бот MAX, копии базы")
    dc.add_argument("--backups", default="backups")
    w = sub.add_parser("web", help="админка: настройки месяца, ручные коэффициенты и планы")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8080)

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config_mod.load(a.config)
    db = DB(cfg.db_path)
    from .botcfg import apply
    apply(cfg, db)  # ссылки на ботов и владельцы со страницы «Бот MAX» главнее config.yaml

    if a.cmd == "init-db":
        print(f"База: {cfg.db_path}")
    elif a.cmd == "import-history":
        from .importers.payroll_archive import import_history
        print(import_history(a.folder, db, a.admins))
    elif a.cmd == "import-sales":
        from .importers.onec_sales import import_file, import_inbox, reconcile
        reps = [import_file(a.file, db, cfg)] if a.file else import_inbox(db, cfg)
        kinds = {"lines": "строки ОРП", "receipts": "чеки", "docs": "шапки ОРП", "stock": "остатки",
                 "arrivals": "поступления", "error": "ОШИБКА", "onec_error": "ОШИБКА 1С"}
        for r in reps:
            if r.error:
                print(f"{r.file}: {kinds.get(r.kind, r.kind)} — {r.error}")
                continue
            print(f"{r.file}: {kinds.get(r.kind, r.kind)}, строк {r.lines}, дней {len(r.days)}, пропущено "
                  f"{r.skipped}, новые продавцы: {', '.join(sorted(r.unknown_sellers)) or '—'}")
            for k, v in sorted(r.foreign.items()):
                print(f"   не магазин, не загружено: {k or 'склад не указан'} — {v:,.0f} ₽".replace(",", " "))
        diff = reconcile(db, {d for r in reps if r.kind in ("lines", "docs") for d, _ in r.days})
        for m in diff:
            print(f"Не сходится {m.day} {m.store}: " + m.text(lambda v: f"{v:,.0f}".replace(",", " ")))
        if any(r.kind == "receipts" for r in reps) and not any(r.kind == "lines" for r in reps):
            print("Загружены только чеки: продажи для плана берутся из строк ОРП (sales_*.csv). "
                  "Выгрузите период новой обработкой — она пишет и sales_*, и receipts_*.")
    elif a.cmd == "sellers":
        for r in db.sellers(active_only=False):
            print(f"{r['id']:>3} {r['name']:<28} {r['role']:<7} {'активен' if r['active'] else 'нет':<7} "
                  f"MAX: {r['max_user_id'] or '—'}  код: {r['invite_code'] or ''}")
    elif a.cmd == "invite":
        sid = db.seller_id(a.name, create=False)
        if not sid:
            raise SystemExit("Продавец не найден")
        code = db.new_invite(sid)
        link = f"{cfg.max.bot_link}?start={code}" if cfg.max.bot_link else ""
        print(f"Код: {code} {link}")
    elif a.cmd == "set-role":
        sid = db.seller_id(a.name, create=False)  # опечатка в имени не должна создавать нового продавца (аудит Н4)
        if not sid:
            raise SystemExit("Продавец не найден — имя как в 1С, список: awer-bot sellers")
        with db.tx() as c:
            c.execute("UPDATE sellers SET role=? WHERE id=?", (a.role, sid))
    elif a.cmd == "alias":
        sid = db.seller_id(a.name, create=False)
        if not sid:
            raise SystemExit("Продавец не найден")
        with db.tx() as c:
            c.execute("INSERT OR REPLACE INTO seller_aliases(alias, seller_id) VALUES(?,?)", (a.alias, sid))
    elif a.cmd == "plan":
        from .planning import service
        mp = service.freeze(db, cfg, a.month) if a.freeze else service.compute(db, cfg, a.month)
        sp = mp.store
        print(f"{a.month}: тренд {sp.trend_calc} (вручную {mp.trend_manual}) → {sp.trend}; план магазина {sp.plan:,.0f}; "
              f"коэф. месяца {sp.coef}; новичок {sp.newcomer_plan:,.0f}" if sp.plan else f"{a.month}: нет данных для плана")
        for r in mp.rows:
            print(f"  {r.name:<28} {r.method:<9} {r.plan_final or 0:>12,.0f}")
        print("Зафиксирован" if a.freeze else "Не зафиксирован (добавьте --freeze)")
    elif a.cmd == "daily":
        from .jobs import Sender, daily_run
        from .max_api import MaxClient
        client = None if a.dry_run else MaxClient(cfg.max_token, cfg.max.base_url, cfg.max.ca_file)
        today = date.fromisoformat(a.today) if a.today else cfg.now().date()
        if a.dry_run:  # аудит Р1: проверка не подводит итоги, не фиксирует план и не забирает файлы 1С в настоящей базе
            db = db.copy_to(Path(tempfile.mkdtemp(prefix="awer-dry-")) / "dry.db")
            print("Проверка на копии базы: в настоящую базу ничего не пишется, файлы 1С из inbox не загружаются.\n")
        print(daily_run(db, cfg, Sender(db, cfg, client, dry_run=a.dry_run), today, force=a.force,
                        import_files=not a.dry_run))
    elif a.cmd == "close-month":
        from .planning import service
        rows = service.close_month(db, cfg, a.month)
        print(f"Итоги {a.month} " + ("окончательные" if service.is_final(db, a.month) else
                                     "ПРЕДВАРИТЕЛЬНЫЕ: выгружены не все дни месяца"))
        for r in rows:
            perf = "—" if r["performance"] is None else pct(r["performance"])
            print(f"  {r['name']:<28} {r['sales']:>12,.0f}  {perf:>5}  ставка {r['rate']:,.0f}")
    elif a.cmd == "backtest":
        from .planning import backtest
        for m in backtest.months_between(a.first, a.last or a.first):
            r = backtest.run(db, cfg, m)
            plan = (r.store.plan or 0) * r.coverage
            print(f"{m} ({r.source}): план магазинов {plan:,.0f}, факт {r.store_fact:,.0f}, "
                  f"выполнили план {r.done} из {len(r.rows)}, ставки по схеме {r.rates:,.0f}".replace(",", " "))
            for x in r.rows:
                perf = "—" if x.performance is None else pct(x.performance)
                print(f"  {x.name:<28} план {x.plan or 0:>11,.0f}  отработано {x.fte:4.2f}  факт {x.sales:>11,.0f}  "
                      f"{perf:>5}  ставка {x.rate:,.0f}".replace(",", " "))
    elif a.cmd == "check-max":
        from .max_api import MaxAuthError, MaxClient
        for name, token in (("«План»", os.environ.get("MAX_BOT_TOKEN")), ("«Обучение»", cfg.training_token)):
            if not token:
                print(f"{name}: токен не задан в .env")
                continue
            try:
                me = MaxClient(token, cfg.max.base_url, cfg.max.ca_file).me()
                print(f"{name}: на связи — {me.get('name') or me.get('first_name')} (user_id {me.get('user_id')})")
            except MaxAuthError:
                print(f"{name}: MAX не принял токен — проверьте .env")
            except Exception as e:  # noqa: BLE001
                print(f"{name}: нет связи с {cfg.max.base_url} — {e}")
    elif a.cmd == "bot":
        from .bot import run, single_instance
        if not single_instance(cfg):
            raise SystemExit("Бот уже работает (служба AwerSalesBot). Второй экземпляр слал бы сообщения дважды. "
                             "Перезапуск: Restart-Service AwerSalesBot")
        run(cfg, db)
    elif a.cmd == "serve":
        import uvicorn

        from .bot import Runner
        from .web import create_app
        runner = Runner(cfg)
        if not runner.start():  # токена нет — только админка; бот запустится со страницы «Бот MAX»
            logging.warning("MAX_BOT_TOKEN не задан — запущена только админка")
        uvicorn.run(create_app(cfg, db, runner=runner), host=a.host, port=a.port)
    elif a.cmd == "backup":

        from .ops import backup
        try:
            out = backup(cfg.db_path, Path(a.dest), a.keep_days)
            msg = f"ok {out.name} ({out.stat().st_size / 1e6:.1f} МБ)"
        except Exception as e:  # noqa: BLE001 — задача Windows: причина — в журнал, код возврата — 1
            msg = f"ОШИБКА {e}"
        print(msg)
        if a.log:
            Path(a.log).parent.mkdir(parents=True, exist_ok=True)
            with open(a.log, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
        if msg.startswith("ОШИБКА"):
            raise SystemExit(1)
    elif a.cmd == "doctor":

        from .ops import doctor
        for good, line in doctor(db, cfg, Path(a.backups)):
            print(("  ok  " if good else "  !!  ") + line)
    elif a.cmd == "web":
        import uvicorn

        from .web import create_app
        uvicorn.run(create_app(cfg, db), host=a.host, port=a.port)


if __name__ == "__main__":
    main()
