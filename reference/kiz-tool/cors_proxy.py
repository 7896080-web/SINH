#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Локальный CORS-прокси для онлайн-инструмента заказа КИЗ.

Зачем: сервер Честного знака (markirovka.crpt.ru / suzgrpc.crpt.ru) не
разрешает браузеру обращаться к себе напрямую с произвольного адреса
(CORS-политика на их стороне — вне нашего контроля). Этот скрипт запускается
у вас на компьютере, сам делает запрос к Честному знаку (для сервера это
обычный серверный запрос, CORS тут ни при чём), и возвращает браузеру ответ
с добавленными CORS-заголовками, которые снимают блокировку.

Использует только стандартную библиотеку Python — ничего дополнительно
устанавливать не нужно.

ЗАПУСК:
    python cors_proxy.py [порт]
    (по умолчанию порт 8787)

Оставьте окно с запущенным скриптом открытым, пока пользуетесь онлайн-
инструментом — прокси должен работать всё это время.

После запуска в самом HTML-инструменте включите галочку
"Через локальный CORS-прокси" в настройках подключения — тогда запросы
пойдут на http://localhost:8787/trueapi/... и http://localhost:8787/suz/...
вместо прямых адресов crpt.ru.
"""
import sys
import http.server
import urllib.request
import urllib.error

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8787

UPSTREAMS = {
    '/trueapi': 'https://markirovka.crpt.ru/api/v3/true-api',
    '/suz': 'https://suzgrid.crpt.ru/api/v3',
}


class ProxyHandler(http.server.BaseHTTPRequestHandler):

    def _cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, clientToken, X-Signature')
        self.send_header('Access-Control-Max-Age', '86400')

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def _find_upstream(self):
        for prefix, upstream in UPSTREAMS.items():
            if self.path.startswith(prefix):
                return upstream + self.path[len(prefix):]
        return None

    def _proxy(self, method):
        target = self._find_upstream()
        if not target:
            self.send_response(404)
            self._cors_headers()
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(
                ('Неизвестный префикс пути: ' + self.path +
                 '\nИспользуйте /trueapi/... или /suz/...').encode('utf-8')
            )
            return

        length = int(self.headers.get('Content-Length', 0) or 0)
        body = self.rfile.read(length) if length else None

        req = urllib.request.Request(target, data=body, method=method)
        for h in ('Content-Type', 'Authorization', 'clientToken', 'X-Signature'):
            if h in self.headers:
                req.add_header(h, self.headers[h])

        print(f'[proxy] {method} {self.path} -> {target}')

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                self.send_response(resp.status)
                self._cors_headers()
                self.send_header('Content-Type', resp.headers.get('Content-Type', 'application/json'))
                self.end_headers()
                self.wfile.write(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read()
            print(f'[proxy] upstream HTTP error {e.code}: {body[:300]}')
            self.send_response(e.code)
            self._cors_headers()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            print(f'[proxy] error: {e}')
            self.send_response(502)
            self._cors_headers()
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(('Ошибка прокси: ' + str(e)).encode('utf-8'))

    def do_GET(self):
        self._proxy('GET')

    def do_POST(self):
        self._proxy('POST')

    def log_message(self, fmt, *args):
        pass  # печатаем сами в _proxy, стандартный лог отключаем


if __name__ == '__main__':
    print(f'CORS-прокси запущен: http://localhost:{PORT}')
    print(f'  /trueapi/*  ->  {UPSTREAMS["/trueapi"]}')
    print(f'  /suz/*      ->  {UPSTREAMS["/suz"]}')
    print('Оставьте это окно открытым. Остановить — Ctrl+C.')
    http.server.HTTPServer(('localhost', PORT), ProxyHandler).serve_forever()
