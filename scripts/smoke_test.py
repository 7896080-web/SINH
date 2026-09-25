#!/usr/bin/env python3
"""
Безголовая проверка админки после деплоя — без браузера, для использования
из Claude Code или CI. Проверяет: /health, вход, что основные страницы
реально открываются авторизованным пользователем.

Использование:
    python3 scripts/smoke_test.py --base-url http://127.0.0.1:8000 \
        --username admin --password ваш_пароль

Код возврата 0 — всё ок, 1 — есть проблемы (список печатается в stdout).
Не создаёт и не меняет никаких данных — только GET-запросы плюс один вход.
"""
import argparse
import sys

import requests


PAGES_TO_CHECK = ["/mapping", "/products", "/anomalies", "/stock-on-date", "/diagnostics",
                  "/testing", "/api-keys", "/returns", "/returns/list"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    session = requests.Session()
    problems = []

    def check(label, ok, detail=""):
        status = "OK  " if ok else "FAIL"
        print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))
        if not ok:
            problems.append(label)

    # 1. /health — не требует авторизации
    try:
        r = session.get(f"{base}/health", timeout=args.timeout)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        check("GET /health отвечает", r.status_code in (200, 503),
              f"HTTP {r.status_code}, ok={data.get('ok')}")
        if r.status_code == 503:
            stale = [w["worker"] for w in data.get("workers", []) if not w.get("ok")]
            if stale:
                print(f"       протухшие воркеры: {', '.join(stale)}")
    except requests.RequestException as e:
        check("GET /health отвечает", False, str(e))

    # 2. Страница входа
    try:
        r = session.get(f"{base}/login", timeout=args.timeout)
        check("GET /login возвращает 200", r.status_code == 200, f"HTTP {r.status_code}")
    except requests.RequestException as e:
        check("GET /login возвращает 200", False, str(e))
        print_summary(problems)
        return 1

    # 3. Вход
    try:
        r = session.post(
            f"{base}/login", data={"username": args.username, "password": args.password},
            timeout=args.timeout, allow_redirects=False,
        )
        logged_in = r.status_code == 303 and "location" in r.headers
        check("Вход выполнен", logged_in, f"HTTP {r.status_code}")
        if not logged_in:
            print_summary(problems)
            return 1
    except requests.RequestException as e:
        check("Вход выполнен", False, str(e))
        print_summary(problems)
        return 1

    # 4. Основные страницы под авторизацией
    for path in PAGES_TO_CHECK:
        try:
            r = session.get(f"{base}{path}", timeout=args.timeout)
            check(f"GET {path}", r.status_code == 200, f"HTTP {r.status_code}")
        except requests.RequestException as e:
            check(f"GET {path}", False, str(e))

    return print_summary(problems)


def print_summary(problems: list[str]) -> int:
    print()
    if problems:
        print(f"ПРОБЛЕМЫ ({len(problems)}): " + "; ".join(problems))
        return 1
    print("Всё в порядке.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
