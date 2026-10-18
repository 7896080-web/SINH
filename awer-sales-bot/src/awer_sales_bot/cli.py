"""Командная строка: awer-bot <команда>. Запуск без установки: python -m awer_sales_bot <команда>."""
from __future__ import annotations

import argparse
import logging
from datetime import date

from . import config as config_mod
from .db import DB


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
    role = sub.add_parser("set-role", help="роль: seller | admin")
    role.add_argument("name")
    role.add_argument("role", choices=["seller", "admin"])
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
    sub.add_parser("bot", help="запустить бота (long polling + расписание)")
    sv = sub.add_parser("serve", help="бот + админка в одном процессе (для службы Windows)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8080)
    w = sub.add_parser("web", help="админка: настройки месяца, ручные коэффициенты и планы")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8080)

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config_mod.load(a.config)
    db = DB(cfg.db_path)

    if a.cmd == "init-db":
        print(f"База: {cfg.db_path}")
    elif a.cmd == "import-history":
        from .importers.payroll_archive import import_history
        print(import_history(a.folder, db, a.admins))
    elif a.cmd == "import-sales":
        from .importers.onec_sales import import_file, import_inbox
        reps = [import_file(a.file, db, cfg)] if a.file else import_inbox(db, cfg)
        for r in reps:
            print(f"{r.file}: строк {r.lines}, дней {len(r.days)}, пропущено {r.skipped}, новые продавцы: "
                  f"{', '.join(sorted(r.unknown_sellers)) or '—'}")
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
        with db.tx() as c:
            c.execute("UPDATE sellers SET role=? WHERE id=?", (a.role, db.seller_id(a.name)))
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
        client = None if a.dry_run else MaxClient(cfg.max_token, cfg.max.base_url, cfg.max.auth)
        today = date.fromisoformat(a.today) if a.today else date.today()
        print(daily_run(db, cfg, Sender(db, cfg, client, dry_run=a.dry_run), today, force=a.force))
    elif a.cmd == "close-month":
        from .planning import service
        for r in service.close_month(db, cfg, a.month):
            perf = "—" if r["performance"] is None else f"{r['performance']:.0%}"
            print(f"  {r['name']:<28} {r['sales']:>12,.0f}  {perf:>5}  ставка {r['rate']:,.0f}")
    elif a.cmd == "bot":
        from .bot import run
        run(cfg, db)
    elif a.cmd == "serve":
        import threading

        import uvicorn

        from .bot import run
        from .web import create_app
        threading.Thread(target=run, args=(cfg, DB(cfg.db_path)), daemon=True, name="max-bot").start()
        uvicorn.run(create_app(cfg, db), host=a.host, port=a.port)
    elif a.cmd == "web":
        import uvicorn

        from .web import create_app
        uvicorn.run(create_app(cfg, db), host=a.host, port=a.port)


if __name__ == "__main__":
    main()
