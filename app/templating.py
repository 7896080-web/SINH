"""Один шаблонизатор на всё приложение — и версия статики в ссылках.

Зачем понадобилось. 20.09 после наката оператор открыл страницу и увидел
разъехавшуюся вёрстку: разметка пришла новая, а `style.css` браузер взял из
кэша — вчерашний. Иконки меню без своих правил растянулись во всю ширину, а
сворачивание не сработало вовсе. Само по себе это лечится `Ctrl+F5`, но
рассчитывать на то, что каждый оператор после каждого наката догадается нажать
две кнопки, нельзя: он увидит сломанную страницу и решит, что сломали мы.

Поэтому ссылка на статику несёт версию: `/static/style.css?v=<метка>`. Метка
считается из времени правки самих файлов — накат их переписывает, метка
меняется, браузер идёт за новой версией. Пока файлы те же, адрес не меняется, и
кэш работает как задумано.

Файлы читаются ОДИН РАЗ при импорте. Считать на каждый запрос — значит ходить
в файловую систему ради числа, которое меняется только вместе с перезапуском:
накат всё равно перезапускает службы, иначе новый код не подхватится.
"""

import hashlib
import os

from fastapi.templating import Jinja2Templates

STATIC_DIR = "app/static"

# Что версионируем. Ровно то, что грузит `base.html`: остальное в статике —
# файлы, на которые страницы не ссылаются.
VERSIONED = ("style.css", "htmx.min.js")


def _static_version() -> str:
    """Короткая метка, меняющаяся вместе с файлами статики."""
    marks = []
    for name in VERSIONED:
        path = os.path.join(STATIC_DIR, name)
        try:
            marks.append(f"{name}:{os.path.getmtime(path):.0f}")
        except OSError:
            # Файла нет — версия просто не будет его учитывать. Падать здесь
            # нельзя: без шаблонизатора не поднимется всё приложение.
            marks.append(f"{name}:0")
    return hashlib.sha256("|".join(marks).encode()).hexdigest()[:10]


STATIC_VERSION = _static_version()


def static_url(name: str) -> str:
    """Адрес файла статики с версией: `/static/style.css?v=abc123`."""
    return f"/static/{name}?v={STATIC_VERSION}"


templates = Jinja2Templates(directory="app/templates")
templates.env.globals["static_url"] = static_url
templates.env.globals["static_version"] = STATIC_VERSION
