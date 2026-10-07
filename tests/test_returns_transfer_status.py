"""Живая строка: что СЕЙЧАС с вещами, переданными в 1С.

После «Передать в 1С» страница коробки про судьбу пачки не говорила НИЧЕГО.
Вещь уходит в «ждём 1С», файл едет минутным циклом, ответ разбирается позже — а
кладовщик видит опустевшую пачку и уходит. Вернулась вещь в остаток или
застряла, он узнавал, только открыв список и посмотрев статусы по одной; а
застрявшее задание возврата не чинится само вовсе — автоповтора у возвратов нет
(`repost_stuck_movements` берёт только `CREATE_MOVEMENT`).

Главное, что проверяется ниже, — СТАДИЯ ЗАДАНИЯ, а не статус вещи. У вещи
статус один на всё ожидание, а стоят за ним три разных положения: файл ещё у
нас, файл у 1С, 1С молчит дольше срока. Первые два пройдут сами, третье — нет,
и сказать про них одним словом «в пути» значит спрятать единственное, что
требует человека.
"""
from datetime import timedelta

import pytest

from app import returns as R
from app.models import (Barcode, FtpTask, FtpTaskStatus, Platform, Product,
                        ReturnItem, ReturnStatus, User, UserRole)
from app.security import hash_password
from app.timeutils import now_utc

WB_WH = "Wildberries_Склад_FBO"


@pytest.fixture()
def warehouse_client(client, web_db):
    """Вход под складом: строка живёт на ЕГО странице, ему и смотреть."""
    web_db.add(User(username="sklad", password_hash=hash_password("secret123"),
                    role=UserRole.warehouse))
    web_db.commit()
    client.post("/login", data={"username": "sklad", "password": "secret123"})
    return client


@pytest.fixture()
def goods(web_db):
    web_db.add(Product(uid_1c="uid-1", name="Свитшот", article="СВ-01",
                       size="62", stock_on_hand=5))
    web_db.add(Barcode(barcode="2000000000017", uid_1c="uid-1"))
    web_db.commit()


def _waiting(web_db, task_status, *, minutes_ago=1, is_test=False,
             status=ReturnStatus.awaiting_1c):
    """Вещь, ждущая 1С, с заданием на нужной стадии."""
    task = FtpTask(command=R.RETURN_COMMAND, barcode="2000000000017",
                   warehouse_from=WB_WH, warehouse_to=R.TARGET_WAREHOUSE,
                   quantity=1, order_id="RET-1", status=task_status,
                   is_test=is_test)
    web_db.add(task)
    web_db.flush()
    item = ReturnItem(barcode="2000000000017", uid_1c="uid-1",
                      platform=Platform.wb, status=status,
                      status_changed_at=now_utc() - timedelta(minutes=minutes_ago),
                      ftp_task_id=task.id, is_test=is_test)
    web_db.add(item)
    web_db.commit()
    return item


def _done(web_db, status, *, hours_ago=1):
    item = ReturnItem(barcode="2000000000017", uid_1c="uid-1",
                      platform=Platform.wb, status=status,
                      status_changed_at=now_utc() - timedelta(hours=hours_ago))
    web_db.add(item)
    web_db.commit()
    return item


# ------------------------------------------------------ сам подсчёт

def test_the_stage_of_the_task_is_what_counts(web_db, goods):
    """Три положения за одним статусом вещи — и считаются они порознь.

    «Ждём 1С» у вещи одно, а файл при этом либо ещё у нас, либо уже у 1С, либо
    потерян. Слить их в одно число значит спрятать застрявшее — единственное,
    что здесь требует человека.
    """
    _waiting(web_db, FtpTaskStatus.pending)
    _waiting(web_db, FtpTaskStatus.sent)
    _waiting(web_db, FtpTaskStatus.timeout)

    status = R.transfer_status(web_db)

    assert status["queued"] == 1
    assert status["at_1c"] == 1
    assert status["stuck"] == 1
    assert status["waiting"] == 3


def test_a_long_wait_counts_as_stuck(web_db, goods):
    """Файл у 1С третий час — для человека это тот же тупик.

    Статус задания при этом «sent», то есть формально всё идёт как надо. Но
    круг «файл собрался — 1С забрала — ответ разобран» идёт минутами, и
    молчание дольше срока означает, что ответа может не быть вовсе: по
    возвратам автоповтора нет, и вещь не вернётся в остаток сама.
    """
    _waiting(web_db, FtpTaskStatus.sent, minutes_ago=180)

    status = R.transfer_status(web_db)

    assert status["stuck"] == 1 and status["at_1c"] == 0
    assert status["oldest_minutes"] >= 180


def test_a_missing_task_is_stuck_too(web_db, goods):
    """Вещь ждёт ответа, а задания нет вовсе — ответ прислать некому."""
    item = ReturnItem(barcode="2000000000017", uid_1c="uid-1",
                      platform=Platform.wb, status=ReturnStatus.awaiting_scrap,
                      status_changed_at=now_utc(), ftp_task_id=None)
    web_db.add(item)
    web_db.commit()

    assert R.transfer_status(web_db)["stuck"] == 1


def test_training_things_are_counted_apart(web_db, goods):
    """Тренировочные считаются ОТДЕЛЬНО, а не вместе и не молча.

    Их задания помечены `is_test` и в файл для 1С не попадают вовсе, то есть
    ответа по ним не будет никогда. Смешай мы их с боевыми, строка показывала
    бы «ждут 1С: 1» в установке, из которой наружу не ушло ничего, — и человек
    ждал бы ответа до конца смены.
    """
    _waiting(web_db, FtpTaskStatus.pending, is_test=True)

    status = R.transfer_status(web_db)

    assert status["waiting"] == 0
    assert status["test_waiting"] == 1


def test_what_1c_answered_is_shown_apart_from_a_refusal(web_db, goods):
    """«Проведено» и «отказ 1С» — разные вещи, и смешивать их нельзя.

    Отказ значит, что вещь вернулась в разбор и решение по ней нужно принять
    заново; посчитай мы его проведённым, человек считал бы дело законченным.
    """
    _done(web_db, ReturnStatus.back_to_sale)
    _done(web_db, ReturnStatus.scrapped)
    _done(web_db, ReturnStatus.rejected_1c)

    status = R.transfer_status(web_db)

    assert status["back_to_sale"] == 1
    assert status["scrapped"] == 1
    assert status["rejected"] == 1


def test_yesterdays_work_does_not_fill_the_line(web_db, goods):
    """Окно — сутки: иначе строка вечно показывала бы старые числа."""
    _done(web_db, ReturnStatus.back_to_sale, hours_ago=30)

    assert R.transfer_status(web_db)["back_to_sale"] == 0


def test_a_quiet_installation_says_so(web_db, goods):
    """Пусто — это тоже ответ, и он обязан быть внятным."""
    status = R.transfer_status(web_db)

    assert status["waiting"] == 0 and status["stuck"] == 0


# ------------------------------------------------------ сама страница

def test_the_page_shows_the_line_at_once(logged_in_client, web_db, goods):
    """Строка есть сразу, а не только после первого автообновления.

    Иначе первые пятнадцать секунд на её месте пусто — ровно тогда, когда
    человек, передавший пачку, смотрит на страницу и ждёт ответа про коробку.
    """
    _waiting(web_db, FtpTaskStatus.sent)

    body = logged_in_client.get("/returns/box").text

    assert "Ждут ответа 1С: 1" in body, body[:400]
    assert "/returns/box/status" in body, "строка не обновляет себя сама"


def test_the_fragment_answers_on_its_own(logged_in_client, web_db, goods):
    """У строки СВОЙ адрес — она не часть куска пачки.

    Кусок пачки подменяется на каждый скан и держит внутри поле скана: живи
    строка там, автообновление каждые пятнадцать секунд перерисовывало бы поле,
    то есть крало бы фокус посреди коробки.
    """
    _waiting(web_db, FtpTaskStatus.timeout)

    r = logged_in_client.get("/returns/box/status")

    assert r.status_code == 200
    assert "Застряло: 1" in r.text, r.text
    assert "позовите администратора" in r.text, (
        "складу «Диагностика» закрыта — строка обязана сказать, к кому идти\n"
        + r.text)
    # Шапки и меню в куске быть не должно: подмена вставила бы страницу внутрь
    # страницы.
    assert "<nav" not in r.text and "</html>" not in r.text, r.text


def test_the_line_is_not_swapped_as_a_whole(web_db):
    """Подмена ВНУТРЕННЯЯ, иначе строка обновится ровно один раз.

    При `outerHTML` ответ заменил бы сам контейнер вместе с `hx-get` и
    `hx-trigger` — строка замерла бы навсегда, не сказав ни слова. Отказ
    молчаливый: с виду всё на месте, просто числа больше не меняются.
    """
    from pathlib import Path
    page = Path("app/templates/returns_box.html").read_text(encoding="utf-8")

    block = page[page.index('id="transfer-status"'):]
    block = block[:block.index("</div>")]
    assert 'hx-swap="innerHTML"' in block, block


def test_the_scan_guard_is_per_target(web_db):
    """Защиты скана считаются ПО ЦЕЛИ подмены, а не на всю страницу.

    Их писали, когда подмена была одна. С появлением живой строки общий счётчик
    очереди означал бы, что ответ статуса запрещает подмену пачки (счёт
    отсканированного соврал бы), а общий возврат фокуса утаскивал бы курсор в
    поле скана каждые пятнадцать секунд — закрывая открытый список складов.
    """
    from pathlib import Path
    page = Path("app/templates/returns_box.html").read_text(encoding="utf-8")

    assert 'var BATCH = "box-live"' in page, "цель подмены пачки не названа"
    assert "if (target(e) !== BATCH) return;" in page, (
        "фокус возвращается после ЛЮБОЙ подмены — значит и после обновления "
        "строки статуса")


def test_the_warehouse_role_sees_the_fragment(web_db, warehouse_client):
    """Складу строка доступна: её адрес под `/returns/`.

    Положи мы фрагмент по чужому адресу, роль `warehouse` получила бы отказ, и
    строка просто не появлялась бы — без единого слова почему.
    """
    r = warehouse_client.get("/returns/box/status")

    assert r.status_code == 200, r.status_code
