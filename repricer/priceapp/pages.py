"""Общая отрисовка страниц: у каждой есть пользователь, раздел меню и флеш."""
from fastapi import Request

from priceapp.flash import pop_flash
from priceapp.templating import templates


def render(request: Request, template: str, user, active: str | None = None, **ctx):
    ctx.update(request=request, current_user=user, active_page=active,
               flash=pop_flash(request) if template.endswith(".html") and not template.endswith("_rows.html") else None)
    return templates.TemplateResponse(request, template, ctx)
