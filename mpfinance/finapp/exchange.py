"""Транспорт обмена с 1С — локальные папки сервера (1С стоит на нём же).

Правила те же, что у sync_admin и «Репрайсера»:
- задание появляется под финальным именем ЦЕЛЫМ (`.part` + переименование) —
  обработка не должна прочитать недописанный файл;
- ответ уходит в архив ПОСЛЕ того, как его разбор закоммичен (`onec.py`);
- свои имена: задания `task_fin_*.txt`, ответы `result_fin_*.txt` (и рядом
  `cost_fin_*`, `barcodes_fin_*`) в своём подкаталоге `results\\finance`.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from finapp import config

TASK_PREFIX = "task_fin_"
RESULT_PREFIX = "result_fin_"
ARCHIVE_KEEP_DAYS = 60


class LocalExchange:
    def __init__(self, tasks: Path, results: Path, archive: Path):
        self.tasks, self.results, self.archive = Path(tasks), Path(results), Path(archive)

    def put_task(self, name: str, text: str) -> None:
        self.tasks.mkdir(parents=True, exist_ok=True)
        tmp = self.tasks / (name + ".part")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.tasks / name)

    def task_exists(self, name: str) -> bool:
        return (self.tasks / name).exists()

    def result_names(self) -> list[str]:
        if not self.results.exists():
            return []
        return sorted(p.name for p in self.results.glob(f"{RESULT_PREFIX}*.txt"))

    def read_result(self, name: str) -> str | None:
        p = self.results / name
        return p.read_text(encoding="utf-8-sig") if p.exists() else None

    def archive_result(self, name: str) -> None:
        p = self.results / name
        if p.exists():
            self.archive.mkdir(parents=True, exist_ok=True)
            os.replace(p, self.archive / name)


def current() -> LocalExchange:
    return LocalExchange(config.ONEC_TASKS_DIR, config.ONEC_RESULTS_DIR, config.ONEC_ARCHIVE_DIR)


def prune_archive(root: Path, days: int = ARCHIVE_KEEP_DAYS) -> int:
    """Удалить из своего архива обмена файлы старше `days` суток. Справочник
    баркодов — мегабайты в сутки на ОБЩЕМ диске с базами и 1С; кончится место —
    разом встанут все программы сервера. Сбой на файле не мешает остальным."""
    if not root.exists():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for f in root.rglob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed
