"""Страницы возвратов: то, что увидит и нажмёт кладовщик.

Домен закрыт в `test_returns.py`. Здесь — стык со страницей, потому что дефекты
этого проекта живут именно там: запрос верен, а зовёт его страница не так.
"""

import re
from datetime import timedelta

import pytest

from app.models import (Barcode, FtpTask, FtpTaskStatus, Platform, Product,
                        ReturnItem, ReturnStatus)
from app import returns as R
from app.timeutils import now_utc


@pytest.fixture()
def goods(web_db):
    web_db.add(Product(uid_1c="uid-1", name="Свитшот", article="СВ-01", size="62",
                       stock_on_hand=5))
    web_db.add(Barcode(barcode="2000000000017", uid_1c="uid-1"))
    web_db.commit()


def _scan(client, code, **extra):
    return client.post("/returns/scan", data={"code": code, **extra},
                       follow_redirects=False)


def test_a_scan_creates_an_item_and_offers_its_label(logged_in_client, web_db, goods):
    _scan(logged_in_client, "2000000000017")

    item = web_db.query(ReturnItem).one()
    assert item.uid_1c == "uid-1"
    assert item.status is ReturnStatus.accepted

    label = logged_in_client.get(f"/returns/item/{item.id}/label")
    assert label.status_code == 200
    assert R.label_number(item) in label.text
    assert "<svg" in label.text, "наклейка без штрихкода бесполезна: её не отсканировать"
    assert "window.print()" in label.text


def test_the_same_field_opens_an_existing_item(logged_in_client, web_db, goods):
    _scan(logged_in_client, "2000000000017")
    item = web_db.query(ReturnItem).one()

    r = _scan(logged_in_client, R.label_number(item))

    assert r.headers["location"] == f"/returns/item/{item.id}"


def test_the_platform_of_the_box_lives_in_the_session(logged_in_client, web_db, goods):
    """Площадка выбирается КОРОБКОЙ: кладовщик едет в один ПВЗ. Спрашивать её на
    каждый скан и медленнее, и ошибочнее."""
    logged_in_client.post("/returns/platform", data={"platform": "ozon"})
    _scan(logged_in_client, "2000000000017")

    assert web_db.query(ReturnItem).one().platform is Platform.ozon


def test_the_box_counter_is_a_local_day_and_not_a_utc_one(logged_in_client, web_db,
                                                          goods, monkeypatch):
    """Боевой сервер в Москве. С полуночи до трёх часов UTC-шные сутки ещё
    вчерашние, и счётчик показывал бы вчерашнюю коробку вместе с сегодняшней —
    а утренняя приёмка идёт как раз в эти часы.

    Проверяем через СЛЕДСТВИЕ: вещь, принятая до местной полуночи, в сегодняшний
    счёт не идёт. Подменяем границу дня функцией, а не переменной окружения:
    `time.tzset()` есть только на Unix, и тест с ним валит `pytest` на боевом
    Windows — то есть останавливает накат.
    """
    from app.routers import returns as page

    _scan(logged_in_client, "2000000000017")
    yesterday = web_db.query(ReturnItem).one()
    yesterday.created_at = now_utc() - timedelta(hours=2)
    web_db.commit()

    monkeypatch.setattr(page, "local_day_start_utc",
                        lambda day: now_utc() - timedelta(hours=1))

    html = logged_in_client.get("/returns").text
    shown = re.search(r"принято сегодня:.*?<strong[^>]*>(\d+)</strong>", html, re.S)
    assert shown is not None, "счётчик коробки исчез со страницы"
    assert shown.group(1) == "0", "счёт коробки берёт UTC-шные сутки вместо местных"


def test_the_recent_list_shows_only_this_box(logged_in_client, web_db, goods):
    """Полоса наверху утверждает площадку. Чужой скан под ней читается как
    «принял не туда» и заставляет отменять то, что в порядке."""
    logged_in_client.post("/returns/platform", data={"platform": "ozon"})
    _scan(logged_in_client, "2000000000017")
    ozon = web_db.query(ReturnItem).one()

    logged_in_client.post("/returns/platform", data={"platform": "wb"})
    page = logged_in_client.get("/returns").text

    assert R.label_number(ozon) not in page


def test_a_repeated_barcode_asks_before_making_a_second_item(logged_in_client, web_db,
                                                             goods):
    """Две одинаковые вещи подряд — норма, и молча отбросить вторую значит
    потерять настоящий товар. Но и молча завести её нельзя: чаще это повтор."""
    _scan(logged_in_client, "2000000000017")
    first = web_db.query(ReturnItem).one()
    first.created_at = now_utc() - R.SCANNER_BOUNCE * 10
    web_db.commit()

    _scan(logged_in_client, "2000000000017")
    assert web_db.query(ReturnItem).count() == 1, "вторая вещь заведена без вопроса"
    assert "2000000000017" in logged_in_client.get("/returns").text

    _scan(logged_in_client, "2000000000017", confirm_second="yes")
    assert web_db.query(ReturnItem).count() == 2, "подтверждённый повтор не завёлся"


def test_a_scanner_bounce_makes_nothing_at_all(logged_in_client, web_db, goods):
    """Дребезг — один жест. Спросить о нём значит приучить жать «да» не глядя, и
    тогда вопрос про НАСТОЯЩИЙ повтор тоже перестанет работать."""
    _scan(logged_in_client, "2000000000017")
    _scan(logged_in_client, "2000000000017")

    assert web_db.query(ReturnItem).count() == 1


def test_the_item_page_offers_only_the_transitions_that_exist(logged_in_client,
                                                              web_db, goods):
    """Предложить переход, который не состоится, — тот же немой отказ: человек
    жмёт, ничего не происходит, и он решает, что кнопка сломана."""
    _scan(logged_in_client, "2000000000017")
    item = web_db.query(ReturnItem).one()

    page = logged_in_client.get(f"/returns/item/{item.id}").text

    assert 'value="cleaning"' in page
    assert 'value="back_to_sale"' not in page, \
        "страница предлагает статус, который ставит только 1С"


def test_sending_to_1c_from_the_page_makes_the_task(logged_in_client, web_db, goods):
    from app.workers.scheduler import PENDING_WAREHOUSE_NAME

    logged_in_client.post("/returns/platform", data={"platform": "kit"})
    _scan(logged_in_client, "2000000000017")
    item = web_db.query(ReturnItem).one()

    logged_in_client.post(f"/returns/item/{item.id}/to-sale")

    task = web_db.query(FtpTask).filter(FtpTask.command == R.RETURN_COMMAND).one()
    assert task.warehouse_from == PENDING_WAREHOUSE_NAME[Platform.kit]
    assert task.status is FtpTaskStatus.pending
    web_db.refresh(item)
    assert item.status is ReturnStatus.awaiting_1c


def test_the_page_refuses_to_send_what_1c_cannot_receive(logged_in_client, web_db):
    """Баркода 1С не знает — проводить перемещение не на что. Отказ обязан
    сказать это словами: молчаливая кнопка читается как сломанная."""
    _scan(logged_in_client, "9999999999999")
    item = web_db.query(ReturnItem).one()

    # Читаем ОТВЕТ на нажатие: TestClient идёт по редиректу сам, и флеш
    # съедается уже им — отдельный GET увидел бы пустую страницу и молчал бы,
    # даже если сообщение пропадает.
    page = logged_in_client.post(f"/returns/item/{item.id}/to-sale").text

    assert web_db.query(FtpTask).count() == 0
    # Именно ОТКАЗ, слово в слово из домена, а не похожая строка в шаблоне:
    # страница и так пишет про мэппинг у неопознанного баркода, и проверка на
    # это слово молчала бы, даже если отказ пропадал бы целиком.
    assert "Товар 1С по этому баркоду не определён — сначала мэппинг" in page


def test_an_unknown_number_says_so_instead_of_breaking(logged_in_client):
    """Наклейка мнётся и читается неверно. Пятисотая на этом месте оставила бы
    кладовщика с вещью в руках и без единой подсказки."""
    r = logged_in_client.get("/returns/item/999999")

    assert r.status_code == 200
    assert "999999" in r.text


def test_the_list_counts_the_whole_selection_and_not_the_page(logged_in_client,
                                                              web_db, goods):
    """Счётчик по длине показанного — дефект, который уже случался дважды:
    страница молчит о том, что список обрезан, и человек считает его полным."""
    from app.routers import returns as page

    for _ in range(5):
        web_db.add(ReturnItem(barcode="2000000000017", uid_1c="uid-1",
                              platform=Platform.wb, status=ReturnStatus.accepted,
                              status_changed_at=now_utc()))
    web_db.commit()
    page.LIST_LIMIT = 2
    try:
        html = logged_in_client.get("/returns/list").text
    finally:
        page.LIST_LIMIT = 300

    assert "Показаны первые 2 из 5" in html, \
        "страница молчит о том, что список обрезан"


def test_the_list_finds_an_item_by_its_label_number(logged_in_client, web_db, goods):
    _scan(logged_in_client, "2000000000017")
    item = web_db.query(ReturnItem).one()

    html = logged_in_client.get(f"/returns/list?q={R.label_number(item)}").text

    assert R.label_number(item) in html


def test_the_label_window_opens_once_and_not_on_every_reload(logged_in_client,
                                                             web_db, goods):
    """Иначе печать срабатывает на КАЖДУЮ загрузку страницы, а страница у
    кладовщика открыта весь день: одна наклейка превращается в пачку, и на
    вещах оказываются номера от других вещей — ровно то, ради чего наклейка и
    заводилась."""
    _scan(logged_in_client, "2000000000017")
    item = web_db.query(ReturnItem).one()

    def auto_print(html):
        # Именно САМООТКРЫТИЕ, а не ссылка «печать» в строке: ссылка есть у
        # каждой принятой вещи и остаётся на месте по построению.
        return f"window.open('/returns/item/{item.id}/label'" in html

    assert auto_print(logged_in_client.get("/returns").text), "наклейка не предложена вовсе"
    assert not auto_print(logged_in_client.get("/returns").text), \
        "печать повторяется на каждой перезагрузке"


def test_skipping_a_repeated_scan_actually_clears_the_question(logged_in_client,
                                                               web_db, goods):
    """Вопрос, не исчезающий по «пропустить», читается как сломанная кнопка —
    и следующий скан оператор делает, не понимая, к чему относится плашка."""
    _scan(logged_in_client, "2000000000017")
    web_db.query(ReturnItem).one().created_at = now_utc() - R.SCANNER_BOUNCE * 10
    web_db.commit()
    _scan(logged_in_client, "2000000000017")
    assert "ВТОРАЯ вещь" in logged_in_client.get("/returns").text

    logged_in_client.post("/returns/skip-repeat", follow_redirects=False)

    assert "ВТОРАЯ вещь" not in logged_in_client.get("/returns").text


def test_the_repeat_question_survives_a_reload(logged_in_client, web_db, goods):
    """Снимай его отрисовка, и F5 по привычке молча стёр бы решение по реальной
    второй вещи: она не завелась бы, и никто бы об этом не узнал."""
    _scan(logged_in_client, "2000000000017")
    web_db.query(ReturnItem).one().created_at = now_utc() - R.SCANNER_BOUNCE * 10
    web_db.commit()
    _scan(logged_in_client, "2000000000017")

    logged_in_client.get("/returns")
    assert "ВТОРАЯ вещь" in logged_in_client.get("/returns").text


def test_the_label_date_is_local_and_not_utc(logged_in_client, web_db, goods,
                                             monkeypatch):
    """Дата на наклейке — КАЛЕНДАРНОЕ число, а его называет человек. У Москвы
    UTC+3, и у вещи, принятой до трёх ночи, UTC-шное число вчерашнее. Наклейку
    потом сверяют с коробкой и накладной ПВЗ глазами, и расхождение в день
    объясняют чем угодно, кроме часового пояса.

    Пояс подменяем ФУНКЦИЕЙ: `time.tzset()` есть только на Unix, и тест с ним
    валит `pytest` на боевом Windows — то есть останавливает накат.
    """
    from datetime import timedelta as _td
    from app.routers import returns as page

    _scan(logged_in_client, "2000000000017")
    item = web_db.query(ReturnItem).one()

    monkeypatch.setattr(page, "local_date_of",
                        lambda moment: (moment + _td(days=1)).date())
    html = logged_in_client.get(f"/returns/item/{item.id}/label").text

    tomorrow = (item.created_at + _td(days=1)).strftime("%d.%m.%Y")
    assert tomorrow in html, "дата наклейки взята из UTC-времени"


def test_the_item_page_explains_the_current_status(logged_in_client, web_db, goods):
    """Пояснение берётся из `RETURN_HINTS` — того же места, что и легенда в
    списке. Два текста про один статус разошлись бы, и человек, читающий их
    буквально, перестал бы верить обоим."""
    _scan(logged_in_client, "2000000000017")
    item = web_db.query(ReturnItem).one()
    means, do = R.RETURN_HINTS[ReturnStatus.accepted]

    page = logged_in_client.get(f"/returns/item/{item.id}").text

    assert means in page, "страница не говорит, что значит текущий статус"
    assert do in page, "страница не говорит, что делать дальше"


def test_the_awaiting_page_does_not_offer_what_it_cannot_do(logged_in_client,
                                                            web_db, goods):
    """Из «ждём 1С» руками не выйти, и страница обязана сказать это словами, а
    не просто убрать кнопки: пустой экран читается как сбой."""
    _scan(logged_in_client, "2000000000017")
    item = web_db.query(ReturnItem).one()
    logged_in_client.post(f"/returns/item/{item.id}/to-sale")

    page = logged_in_client.get(f"/returns/item/{item.id}").text

    assert R.RETURN_HINTS[ReturnStatus.awaiting_1c][1] in page


def test_the_list_legend_covers_every_status(logged_in_client):
    """Легенда с дырой хуже, чем её отсутствие: статус, которого в ней нет,
    человек считает ошибкой программы."""
    page = logged_in_client.get("/returns/list").text

    for status, (means, _do) in R.RETURN_HINTS.items():
        assert R.RETURN_LABELS[status] in page, f"нет статуса {status.value}"
        assert means in page, f"нет пояснения к {status.value}"


def test_each_page_says_what_to_do_on_it(logged_in_client):
    """Инструкция живёт на самой странице, а не в бумажке рядом с монитором:
    бумажку теряют, а новый человек садится за эту работу без неё."""
    assert "Что делать на этой странице" in logged_in_client.get("/returns").text
    assert "Что делать на этой странице" in logged_in_client.get("/returns/list").text


# --------------------------------------------------------------- тренировка

def _mode_on(client):
    return client.post("/returns/test-mode", data={"on": "1"}, follow_redirects=False)


def _mode_off(client):
    return client.post("/returns/test-mode", data={"on": "0"}, follow_redirects=False)


def test_the_strip_shouts_on_every_page_of_the_section(logged_in_client, goods):
    """Увидь человек полосу один раз на входе — через час он про неё не
    вспомнит. Неверное представление о том, в каком ты режиме, и есть вся
    опасность этой затеи."""
    assert "ТРЕНИРОВОЧНЫЙ РЕЖИМ" not in logged_in_client.get("/returns").text

    _mode_on(logged_in_client)

    for url in ("/returns", "/returns/list"):
        assert "ТРЕНИРОВОЧНЫЙ РЕЖИМ" in logged_in_client.get(url).text, \
            f"{url} не говорит, что идёт тренировка"


def test_scanning_in_training_mode_marks_the_item(logged_in_client, web_db, goods):
    _mode_on(logged_in_client)
    _scan(logged_in_client, "2000000000017")

    assert web_db.query(ReturnItem).one().is_test is True


def test_the_mode_is_one_for_the_whole_installation(client, logged_in_client,
                                                    web_db, goods):
    """Склад работает под ОБЩЕЙ учётной записью. Режим, живущий в браузерной
    сессии, означал бы, что один человек тренируется, а второй в соседнем окне
    принимает настоящие возвраты, считая, что тоже тренируется."""
    _mode_on(logged_in_client)

    # Второй браузер, тот же сервер.
    from app.models import User
    from app.security import hash_password
    web_db.add(User(username="второй", password_hash=hash_password("secret123")))
    web_db.commit()
    client.post("/login", data={"username": "второй", "password": "secret123"})

    assert "ТРЕНИРОВОЧНЫЙ РЕЖИМ" in client.get("/returns").text


def test_leaving_the_mode_clears_what_it_made_and_keeps_the_rest(
        logged_in_client, web_db, goods):
    _scan(logged_in_client, "2000000000017")          # боевая, до тренировки
    _mode_on(logged_in_client)
    _scan(logged_in_client, "2000000000017", confirm_second="1")

    assert web_db.query(ReturnItem).count() == 2
    _mode_off(logged_in_client)

    rows = web_db.query(ReturnItem).all()
    assert len(rows) == 1 and rows[0].is_test is False
    assert "ТРЕНИРОВОЧНЫЙ РЕЖИМ" not in logged_in_client.get("/returns").text


def test_leaving_says_how_many_it_erased(logged_in_client, web_db, goods):
    """Число — не украшение: в тренировочном режиме мог быть принят НАСТОЯЩИЙ
    возврат, и это единственный шанс заметить до того, как запись исчезнет."""
    _mode_on(logged_in_client)
    _scan(logged_in_client, "2000000000017")

    page = logged_in_client.post("/returns/test-mode", data={"on": "0"}).text

    assert "стёрто: 1" in page


def test_the_confirm_names_the_number_before_erasing(logged_in_client, web_db, goods):
    _mode_on(logged_in_client)
    _scan(logged_in_client, "2000000000017")

    page = logged_in_client.get("/returns").text

    assert "тренировочных вещей: 1" in page, "вопрос не называет число"


def test_the_training_item_can_be_answered_for_1c_from_the_page(logged_in_client,
                                                                web_db, goods):
    _mode_on(logged_in_client)
    _scan(logged_in_client, "2000000000017")
    item = web_db.query(ReturnItem).one()
    logged_in_client.post(f"/returns/item/{item.id}/to-sale")

    assert "1С провела документ" in logged_in_client.get(f"/returns/item/{item.id}").text
    logged_in_client.post(f"/returns/item/{item.id}/simulate-1c", data={"ok": "1"})

    web_db.refresh(item)
    assert item.status is ReturnStatus.back_to_sale


def test_a_real_item_is_never_offered_the_fake_answer(logged_in_client, web_db, goods):
    """Кнопка, которой нет на экране, — половина защиты. Вторая половина —
    отказ в домене, и она закрыта в test_returns.py."""
    _scan(logged_in_client, "2000000000017")
    item = web_db.query(ReturnItem).one()
    logged_in_client.post(f"/returns/item/{item.id}/to-sale")

    page = logged_in_client.get(f"/returns/item/{item.id}").text
    assert "1С провела документ" not in page

    logged_in_client.post(f"/returns/item/{item.id}/simulate-1c", data={"ok": "1"})
    web_db.refresh(item)
    assert item.status is ReturnStatus.awaiting_1c, "ответ за 1С подделан по HTTP"


def test_a_training_item_is_marked_in_the_lists(logged_in_client, web_db, goods):
    """Вещь открывают по ссылке из списка и после выхода из режима — полоса
    наверху тогда ничего не скажет."""
    _mode_on(logged_in_client)
    _scan(logged_in_client, "2000000000017")
    item = web_db.query(ReturnItem).one()

    assert "ТРЕНИРОВКА" in logged_in_client.get("/returns").text
    assert "ТРЕНИРОВКА" in logged_in_client.get("/returns/list").text
    assert "ТРЕНИРОВОЧНАЯ" in logged_in_client.get(f"/returns/item/{item.id}").text


def test_the_warehouse_may_switch_the_mode_itself(warehouse_client_for_returns):
    """Тренируется именно кладовщик. Оставь переключатель администратору — его
    придётся звать ради каждой тренировки, то есть тренироваться не будут."""
    r = warehouse_client_for_returns.post("/returns/test-mode", data={"on": "1"},
                                          follow_redirects=False)

    assert r.status_code == 303
    assert "ТРЕНИРОВОЧНЫЙ РЕЖИМ" in warehouse_client_for_returns.get("/returns").text


@pytest.fixture()
def warehouse_client_for_returns(client, web_db):
    from app.models import User, UserRole
    from app.security import hash_password

    web_db.add(User(username="sklad", password_hash=hash_password("secret123"),
                    role=UserRole.warehouse))
    web_db.commit()
    client.post("/login", data={"username": "sklad", "password": "secret123"})
    return client


def test_a_refusal_in_this_section_actually_reaches_the_screen(logged_in_client,
                                                               web_db, goods):
    """Раздел рисует шапку общим `base.html`, а тот берёт флеш ИЗ КОНТЕКСТА.
    Забудь его положить — и каждый отказ уходит в никуда: человек нажимает,
    экран не меняется, и он решает, что кнопка сломана. Проверяем на отказе,
    текст которого нигде в шаблонах не написан."""
    _scan(logged_in_client, "2000000000017")
    item = web_db.query(ReturnItem).one()
    page = logged_in_client.post(f"/returns/item/{item.id}/recall").text

    assert "Отменять нечего" in page
