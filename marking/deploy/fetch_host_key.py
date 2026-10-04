"""Спросить у сервера его ключ ed25519 и напечатать строку для known_hosts.

    python fetch_host_key.py <сервер> <порт>

Вместо ssh-keyscan: клиент OpenSSH, встроенный в Windows (9.5), не
договаривается об обмене ключами с OpenSSH 10 на сервере («unsupported KEX
method sntrup761x25519»). paramiko — та же библиотека, которой программа потом
ходит по SFTP. Ключ ничем не подтверждён: отпечаток сверяет человек
(SFTP_1C.md, шаг 5).

Всё печатается в stdout, код выхода 0 — ключ получен, 1 — нет: PowerShell 5.1
при ErrorActionPreference=Stop падает на любом выводе native-команды в stderr.
"""
import socket
import sys

import paramiko


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 1
    host, port = argv[1], int(argv[2])
    try:
        sock = socket.create_connection((host, port), timeout=15)
        transport = paramiko.Transport(sock)
        try:
            transport.get_security_options().key_types = ["ssh-ed25519"]
            transport.start_client(timeout=15)
            key = transport.get_remote_server_key()
        finally:
            transport.close()
    except Exception as exc:
        print(f"нет ответа SSH от {host}:{port}: {exc}")
        return 1
    name = host if port == 22 else f"[{host}]:{port}"
    print(f"{name} {key.get_name()} {key.get_base64()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
