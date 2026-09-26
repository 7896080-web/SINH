"""Отдельное пространство данных для каждого пользователя.

Каждый пользователь (Telegram id из ALLOWED_USER_IDS) получает свою папку
data/users/<id>/ со своей базой SQLite и своими скриншотами. Разделение —
физическое, а не фильтром в запросах: код одного пользователя в принципе не
может прочитать или изменить данные другого, даже при ошибке в каком-то
запросе. Карты, статьи, правила, записи, выписки и отчёты — у каждого свои.
"""

import logging
import os
import shutil

from .flow import Flow
from .storage import Storage

log = logging.getLogger(__name__)


def parse_user_ids(raw: str) -> list[int]:
    """'111, 222' → [111, 222] (порядок важен: первому достаются старые данные)."""
    ids: list[int] = []
    for part in raw.replace(",", " ").split():
        if not part.isdigit():
            raise ValueError(f"ALLOWED_USER_IDS: «{part}» — не Telegram id (нужны только цифры)")
        if int(part) not in ids:
            ids.append(int(part))
    return ids


class UserSpaces:
    def __init__(self, data_dir: str, recognizer, user_ids: list[int], **flow_options):
        self.data_dir = data_dir
        self.recognizer = recognizer
        self.user_ids = list(user_ids)
        self.flow_options = flow_options
        self._flows: dict[int, Flow] = {}

    def user_dir(self, user_id: int) -> str:
        return os.path.join(self.data_dir, "users", str(int(user_id)))

    def flow(self, user_id: int) -> Flow:
        """Flow пользователя (создаётся при первом обращении). Только для разрешённых."""
        if user_id not in self.user_ids:
            raise PermissionError(f"пользователь {user_id} не в ALLOWED_USER_IDS")
        if user_id not in self._flows:
            folder = self.user_dir(user_id)
            os.makedirs(folder, mode=0o700, exist_ok=True)
            os.chmod(folder, 0o700)
            storage = Storage(os.path.join(folder, "finance.db"))
            self._flows[user_id] = Flow(storage, self.recognizer, os.path.join(folder, "receipts"),
                                        **self.flow_options)
        return self._flows[user_id]

    def migrate_shared_data(self) -> str | None:
        """Прежняя версия хранила всё в одной базе data/finance.db. Переносим её
        первому пользователю из списка — только если у него ещё нет своей базы."""
        old_db = os.path.join(self.data_dir, "finance.db")
        if not os.path.exists(old_db) or not self.user_ids:
            return None
        owner = self.user_ids[0]
        folder = self.user_dir(owner)
        new_db = os.path.join(folder, "finance.db")
        if os.path.exists(new_db):
            log.warning("Есть и общая база %s, и база пользователя %s — общую не трогаю",
                        old_db, owner)
            return None
        os.makedirs(folder, mode=0o700, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(old_db + suffix):
                shutil.move(old_db + suffix, new_db + suffix)
        old_receipts = os.path.join(self.data_dir, "receipts")
        if os.path.isdir(old_receipts):
            shutil.move(old_receipts, os.path.join(folder, "receipts"))
            # Пути к скриншотам в базе — абсолютные на старое место; поправляем.
            storage = Storage(new_db)
            for table in ("expenses", "transfers"):
                storage.conn.execute(
                    f"UPDATE {table} SET receipt_path = replace(receipt_path, ?, ?)"
                    " WHERE receipt_path LIKE ?",
                    (old_receipts, os.path.join(folder, "receipts"), old_receipts + "%"))
            storage.conn.commit()
            storage.close()
        log.warning("Общая база прежней версии перенесена пользователю %s", owner)
        return new_db
