"""Сообщение о результате действия — через сессию, до следующей страницы.

Cookie сессии браузер отбрасывает ЦЕЛИКОМ и молча, если она длиннее 4096
байт, а кириллица в JSON сессии занимает по шесть байт на букву. У sync_admin
на этом пропадали итоги импорта. Поэтому длина ограничена, а режется конец:
в начале итог, в конце примеры.
"""
MAX_FLASH_CHARS = 440


def flash(request, text: str, level: str = "info") -> None:
    if len(text) > MAX_FLASH_CHARS:
        text = text[:MAX_FLASH_CHARS - 20].rstrip() + " … (обрезано)"
    request.session["flash"] = {"text": text, "level": level}


def pop_flash(request):
    return request.session.pop("flash", None)
