"""Общая отрисовка страниц: у каждой есть пользователь, раздел меню и флеш."""
from fastapi import Request

from markapp.flash import pop_flash
from markapp.templating import templates


def render(request: Request, template: str, user, active: str | None = None, **ctx):
    ctx.update(request=request, current_user=user, active_page=active, flash=pop_flash(request))
    return templates.TemplateResponse(request, template, ctx)
