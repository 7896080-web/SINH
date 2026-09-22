"""Страница «Уведомления» — куда система зовёт человека, когда сама не справилась.

Сделана по образцу «API-ключей» и ровно по той же причине: доступ, от которого
зависит работа, человек должен заводить в интерфейсе, а не в файле на сервере.
Разница только в том, куда ведут ключи — там в кабинет площадки, здесь в чат
или ящик дежурного.

Три вещи, которые страница обязана делать и без которых она была бы вредна.

ПОКАЗЫВАТЬ, ОТКУДА ВЗЯТО ЗНАЧЕНИЕ. Поле, пришедшее из `.env`, правится не
здесь: запись со страницы его перекроет, а вот стереть файл она не может.
Молчи страница об этом, человек стёр бы поле, увидел бы его снова заполненным
после перезагрузки — и решил бы, что интерфейс не работает.

ГОВОРИТЬ, ЧТО КАНАЛ ВЫКЛЮЧЕН. Ненастроенный канал выглядит точно так же, как
исправный и молчащий, — в этом вся беда уведомлений. Пустая страница обязана
читаться как «вас никто не позовёт», а не как «всё в порядке».

ВЕСТИ К ПРОВЕРКЕ. Опечатка в токене неотличима от исправной тишины и
обнаружилась бы в худший из возможных часов. Поэтому с формы — прямая ссылка на
кнопку «Отправить пробное уведомление», а не упоминание, что такая где-то есть.
"""

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import settings_store
from app.database import get_db
from app.templating import templates as shared_templates
from app.dependencies import get_current_user
from app.models import User
from app.audit import log_action
from app.flash import set_flash, pop_flash

router = APIRouter()
templates = shared_templates


@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    from app.alerts import telegram_configured, email_configured

    return templates.TemplateResponse(request, "notifications.html", {
        "request": request, "current_user": user, "active_page": "notifications",
        "cards": settings_store.as_cards(db),
        "telegram_on": telegram_configured(db),
        "email_on": email_configured(db),
        "flash": pop_flash(request),
    })


@router.post("/notifications/save")
def save_settings(
    request: Request,
    name: str = Form(...), value: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Сохранить одну настройку.

    По одному полю на форму, как на «API-ключах»: общая кнопка «Сохранить всё»
    заставляла бы отправлять секреты, которых на странице нет (мы показываем
    маску), и первое же сохранение затёрло бы токен маской.
    """
    field = settings_store.BY_NAME.get(name)
    if field is None:
        set_flash(request, f"Неизвестная настройка «{name}».", "warn")
        return RedirectResponse("/notifications", status_code=303)

    changed = settings_store.set_value(db, name, value)
    if changed:
        # В журнал — ФАКТ и имя поля, но НЕ значение: токен бота и пароль почты
        # в журнале действий не место, он открыт любому пользователю админки.
        # Для открытых полей значение полезно (по нему видно, куда перестали
        # уходить тревоги), но длину всё равно режем.
        detail = field.label
        if not field.secret:
            detail += f": «{(value or '').strip()[:80] or '—'}»"
        log_action(db, user.username,
                   "alert_setting_changed" if (value or "").strip()
                   else "alert_setting_cleared", detail)
    db.commit()

    set_flash(request, (f"«{field.label}» сохранено." if changed
                        else f"«{field.label}» — без изменений."), "good")
    return RedirectResponse("/notifications", status_code=303)
