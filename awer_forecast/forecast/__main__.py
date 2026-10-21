"""Командная строка AWER Forecast.

    python -m forecast migrate                 # накатить миграции (alembic upgrade head)
    python -m forecast import-catalog карта.xlsx
    python -m forecast daily                   # ежедневный цикл (для Планировщика Windows)
    python -m forecast run <шаг>               # один шаг: lamoda_orders, onec_stock, recalc, …
    python -m forecast serve [--host 127.0.0.1 --port 8010]
    python -m forecast set-password            # хеш пароля для FORECAST_PASSWORD_HASH в .env
    python -m forecast demo [--serve]          # демо на тестовых данных
"""
from __future__ import annotations

import argparse
import getpass
import logging
import sys
from pathlib import Path


def _alembic_upgrade(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    from forecast.config import PROJECT_DIR
    cfg = Config(str(PROJECT_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(cfg, "head")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m forecast", description="AWER Forecast")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="накатить миграции БД")
    imp = sub.add_parser("import-catalog", help="импорт Excel-карты мэппинга")
    imp.add_argument("path", type=Path)
    sub.add_parser("daily", help="ежедневный цикл целиком")
    run = sub.add_parser("run", help="один шаг цикла")
    run.add_argument("job")
    serve = sub.add_parser("serve", help="веб-интерфейс")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8010)
    sub.add_parser("set-password", help="хеш пароля для .env")
    demo = sub.add_parser("demo", help="демо на тестовых данных")
    demo.add_argument("--dir", type=Path, default=None)
    demo.add_argument("--serve", action="store_true")
    demo.add_argument("--port", type=int, default=8010)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)     # в URL запросов нет секретов, но и шум не нужен

    if args.cmd == "set-password":
        from forecast.security import hash_password
        first = getpass.getpass("Новый пароль: ")
        if len(first) < 8:
            print("Слишком короткий: нужно не меньше 8 символов.")
            return 1
        if getpass.getpass("Ещё раз: ") != first:
            print("Пароли не совпали.")
            return 1
        print("Впишите в .env строку:\nFORECAST_PASSWORD_HASH=" + hash_password(first))
        return 0

    if args.cmd == "demo":
        from forecast.config import PROJECT_DIR
        from forecast.demo import build_demo
        settings = build_demo(args.dir or (PROJECT_DIR / "demo"))
        if args.serve:
            import uvicorn
            from forecast.web.app import create_app
            print(f"Страницы: http://127.0.0.1:{args.port}  (логин demo, пароль demo)")
            uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port)
        return 0

    from forecast.config import get_settings
    from forecast.db import make_engine
    settings = get_settings()

    if args.cmd == "migrate":
        _alembic_upgrade(settings.database_url)
        return 0
    if args.cmd == "serve":
        import uvicorn
        from forecast.web.app import create_app
        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return 0

    engine = make_engine(settings.database_url)
    if args.cmd == "import-catalog":
        from forecast.catalog.excel_import import CatalogFileError, import_catalog
        try:
            with engine.begin() as conn:
                report = import_catalog(conn, args.path.read_bytes(),
                                        default_supplier_lead_days=settings.planning.supplier_lead_days_default)
        except CatalogFileError as exc:
            print(f"Файл не принят: {exc}")
            return 1
        print(f"лист «{report.sheet}», заголовки в строке {report.header_row}: {report.columns}")
        print(f"строк {report.rows_imported}/{report.rows_total}, моделей +{report.models_created}, "
              f"размеров +{report.sizes_created}, баркодов +{report.barcodes_added} (уже были {report.barcodes_known})")
        for line in report.suppliers_created:
            print("  поставщик создан:", line)
        for line in report.conflicts[:50] + report.errors[:50]:
            print("  !", line)
        return 0

    from forecast.jobs import run_daily, run_step
    if args.cmd == "daily":
        results = run_daily(engine, settings)
    else:
        results = [run_step(engine, settings, args.job, started_by="cli")]
    for r in results:
        print(f"[{r.status}] {r.job}: {r.message}")
    return 1 if any(r.status == "error" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
