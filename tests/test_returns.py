"""Возвраты: статусы, отправка в 1С и её цена для остатка.

Главный тест здесь — `test_a_return_task_is_not_counted_as_in_flight`, и он про
ноль. Сверка считает «в пути» так: `CREATE_MOVEMENT` со знаком плюс,
`CANCEL_MOVEMENT` со знаком минус, остальные команды — ноль. Для возврата ноль
ровно верен: пока вещь в разборе, её нет НИГДЕ — ни в нашем `stock_on_hand`, ни
в снимке 1С, — и подстраивать сверке нечего. Но ноль здесь получается не
решением, а тем, что команда не попала ни в одну ветку `if`, то есть выглядит
как пропуск. Допиши кто-нибудь `RETURN_TO_STOCK` в ту же цепочку — «возврат же
это движение», — и сверка посчитала бы его уходом с ЦС и занизила остаток вдвое
против правды. Тест обязан упасть в этот момент, а не через месяц на бою.
"""

import pytest

from app.models import (
    Barcode, FtpTask, FtpTaskStatus, Platform, Product, ReturnItem, ReturnItemLog,
    ReturnStatus, ScrapReason, RETURN_BY_1C, RETURN_TRANSITIONS,
)
from app import returns
from app.timeutils import now_utc
from app.workers.reconciliation import _in_flight_adjustment


@pytest.fixture()
def product(db):
    p = Product(uid_1c="uid-1", name="Свитшот", article="СВ-01", size="62",
                stock_on_hand=5)
    db.add(p)
    db.add(Barcode(barcode="2000000000017", uid_1c="uid-1"))
    db.commit()
    return p


# --------------------------------------------------------------- приёмка

def test_an_unknown_barcode_is_still_accepted(db):
    """Вещь физически существует независимо от нашего мэппинга.

    Отказать в скане значит заставить человека отложить её в сторону и забыть —
    а это уже потерянный товар, не потерянная строка.
    """
    item = returns.accept(db, "9999999999999", Platform.wb)
    db.commit()

    assert item.status is ReturnStatus.accepted
    assert item.uid_1c is None, "неопознанный баркод не выдумывает товар"


def test_a_known_barcode_brings_its_product(db, product):
    item = returns.accept(db, "2000000000017", Platform.ozon)
    db.commit()

    assert item.uid_1c == "uid-1"


def test_acceptance_writes_the_first_line_of_history(db):
    item = returns.accept(db, "111", Platform.kit)
    db.commit()

    log = db.query(ReturnItemLog).filter(ReturnItemLog.return_id == item.id).all()
    assert [(l.from_status, l.to_status) for l in log] == [(None, ReturnStatus.accepted)]


def test_an_empty_barcode_is_refused(db):
    with pytest.raises(returns.ReturnError):
        returns.accept(db, "   ", Platform.wb)


def test_the_label_number_survives_the_round_trip(db):
    item = returns.accept(db, "111", Platform.wb)
    db.commit()

    assert returns.parse_label(returns.label_number(item)) == item.id
    assert returns.parse_label("  ret-0000%d  " % item.id) == item.id
    assert returns.parse_label("2000000000017") is None, \
        "товарный баркод не должен читаться как номер наклейки"


# --------------------------------------------------------------- повторный скан

def test_a_scanner_bounce_is_not_a_second_item(db):
    """Сканер повторяет один жест дважды. Спрашивать про это — учить жать «да»
    не читая, и тогда вопрос про НАСТОЯЩИЙ повтор тоже перестанет работать."""
    first = returns.accept(db, "2000000000017", Platform.wb)
    db.commit()

    assert returns.is_scanner_bounce(first)


def test_the_same_barcode_much_later_is_a_question_and_not_a_bounce(db):
    first = returns.accept(db, "2000000000017", Platform.wb)
    first.created_at = now_utc() - returns.SCANNER_BOUNCE * 10
    db.commit()

    assert not returns.is_scanner_bounce(first)
    assert returns.recent_same_barcode(db, "2000000000017", Platform.wb) is first, \
        "две одинаковые вещи подряд — норма, но спросить надо"


def test_the_same_barcode_on_another_platform_is_another_box(db):
    """Кладовщик едет в один ПВЗ и привозит коробку одной площадки. Тот же
    баркод из другой коробки — другая вещь, а не повтор."""
    returns.accept(db, "2000000000017", Platform.wb)
    db.commit()

    assert returns.recent_same_barcode(db, "2000000000017", Platform.ozon) is None


# --------------------------------------------------------------- переходы

def test_the_transition_table_is_the_only_truth(db):
    item = returns.accept(db, "111", Platform.wb)
    db.commit()

    returns.change_status(db, item, ReturnStatus.cleaning)
    db.commit()

    assert item.status is ReturnStatus.cleaning
    with pytest.raises(returns.ReturnError):
        # Из химчистки — не «принят»: назад по цепочке ходить незачем, а
        # разрешив это, мы потеряли бы, что вещь уже смотрели.
        returns.change_status(db, item, ReturnStatus.accepted)


def test_scrapping_demands_a_reason(db):
    """«Утилизировано 40» — число без смысла. «Из них 12 подмена» — повод для
    претензии площадке."""
    item = returns.accept(db, "111", Platform.wb)
    db.commit()

    with pytest.raises(returns.ReturnError):
        returns.change_status(db, item, ReturnStatus.scrapped)

    returns.change_status(db, item, ReturnStatus.scrapped,
                          scrap_reason=ScrapReason.swapped)
    db.commit()
    assert item.scrap_reason is ScrapReason.swapped


@pytest.mark.parametrize("status", RETURN_BY_1C)
def test_a_1c_status_cannot_be_set_by_hand(db, monkeypatch, status):
    """Нажать «возвращён в продажу», пока задание в пути, значило бы заявить,
    что остаток вырос, хотя 1С этого не говорила.

    Таблицу переходов здесь подменяем НАРОЧНО, разрешая этот переход. Сегодня
    она его и так не разрешает, то есть отказ пришёл бы от неё, и проверка
    молчала бы об исчезновении самого запрета — а он именно второй слой: таблица
    живая, в неё дописывают статусы, и однажды кто-то допишет этот. Смысл
    запрета в том, чтобы пережить такую правку.
    """
    item = returns.accept(db, "111", Platform.wb)
    db.commit()
    monkeypatch.setitem(returns.RETURN_TRANSITIONS, ReturnStatus.accepted,
                        RETURN_TRANSITIONS[ReturnStatus.accepted] + (status,))

    with pytest.raises(returns.ReturnError):
        returns.change_status(db, item, status)
    assert item.status is ReturnStatus.accepted


def test_there_is_no_way_out_of_awaiting_1c_by_hand():
    assert RETURN_TRANSITIONS[ReturnStatus.awaiting_1c] == ()


def test_every_status_is_in_the_transition_table():
    """Статус без строки в таблице тупик: `can_change` вернёт False на всё, а
    страница покажет вещь, с которой нельзя ничего сделать, не объяснив почему."""
    assert set(RETURN_TRANSITIONS) == set(ReturnStatus)


def test_every_status_has_a_russian_label():
    """Статус без подписи выводится латиницей enum'а — человек читает это как
    сбой, а не как состояние."""
    assert set(returns.RETURN_LABELS) == set(ReturnStatus)
    assert set(returns.SCRAP_LABELS) == set(ScrapReason)


def test_cancelling_acceptance_works_only_while_untouched(db):
    """Дальше — только решение с причиной: запись уже что-то утверждает о
    физическом мире, и стереть её молча значит соврать."""
    item = returns.accept(db, "111", Platform.wb)
    db.commit()
    returns.change_status(db, item, ReturnStatus.cleaning)
    db.commit()

    with pytest.raises(returns.ReturnError):
        returns.cancel_acceptance(db, item)


# --------------------------------------------------------------- 1С

def test_the_task_goes_from_the_platform_warehouse_to_the_central_one(db, product):
    """Перемещение зеркально приёму заказа, и склад-источник берётся ПО
    ПЛОЩАДКЕ: ИП к документу отношения не имеет."""
    from app.workers.scheduler import PENDING_WAREHOUSE_NAME

    item = returns.accept(db, "2000000000017", Platform.ozon)
    db.commit()
    task = returns.send_to_1c(db, item)
    db.commit()

    assert task.command == returns.RETURN_COMMAND
    assert task.warehouse_from == PENDING_WAREHOUSE_NAME[Platform.ozon]
    assert task.warehouse_to == returns.TARGET_WAREHOUSE
    assert task.quantity == 1
    assert task.account_id is None, "кабинет в документе не участвует"
    assert task.platform is Platform.ozon
    assert item.status is ReturnStatus.awaiting_1c


def test_a_return_without_a_1c_product_cannot_be_sent(db):
    """1С опознаёт строку по баркоду, а этого баркода она не знает: проводить
    перемещение не на что."""
    item = returns.accept(db, "9999999999999", Platform.wb)
    db.commit()

    with pytest.raises(returns.ReturnError):
        returns.send_to_1c(db, item)
    assert item.status is ReturnStatus.accepted


def test_a_return_task_is_not_counted_as_in_flight(db, product):
    """ГЛАВНЫЙ тест раздела. Ноль — это ЗНАЧЕНИЕ, а не пропуск.

    Пока вещь в разборе, её нет ни у нас, ни в снимке 1С — числа сходятся, и
    подстраивать сверке нечего. Посчитай она возврат как `CREATE_MOVEMENT`
    (соблазн есть: «это же перемещение»), остаток оказался бы занижен вдвое
    против правды; как `CANCEL_MOVEMENT` — завышен, а это прямой оверселл.
    """
    item = returns.accept(db, "2000000000017", Platform.wb)
    db.commit()
    returns.send_to_1c(db, item)
    db.commit()

    assert _in_flight_adjustment(db, "uid-1") == 0

    # И для сравнения — обычное перемещение по тому же баркоду видно.
    db.add(FtpTask(command="CREATE_MOVEMENT", barcode="2000000000017",
                   warehouse_from="ЦС Склад", warehouse_to="OZON_Склад",
                   quantity=1, order_id="X-1", status=FtpTaskStatus.pending))
    db.commit()
    assert _in_flight_adjustment(db, "uid-1") == 1, \
        "проверка слепа: она не увидела бы и настоящее движение"


def test_only_the_1c_answer_opens_the_terminal_status(db, product):
    item = returns.accept(db, "2000000000017", Platform.wb)
    db.commit()
    task = returns.send_to_1c(db, item)
    db.commit()

    returns.apply_1c_result(db, task, ok=True)
    db.commit()

    assert item.status is ReturnStatus.back_to_sale


def test_a_refusal_from_1c_lands_in_manual_work(db, product):
    """Отказ 1С — не конец: вещь физически лежит на складе, и решение по ней
    ещё предстоит принять человеку."""
    item = returns.accept(db, "2000000000017", Platform.wb)
    db.commit()
    task = returns.send_to_1c(db, item)
    task.result_detail = "нет такой номенклатуры"
    db.commit()

    returns.apply_1c_result(db, task, ok=False)
    db.commit()

    assert item.status is ReturnStatus.rejected_1c
    assert ReturnStatus.awaiting_1c in RETURN_TRANSITIONS[item.status], \
        "после отказа обязана быть возможность повторить"


def test_the_1c_answer_moves_only_its_own_item(db, product):
    """Ответ по чужому заданию не должен двигать соседнюю вещь."""
    mine = returns.accept(db, "2000000000017", Platform.wb)
    other = returns.accept(db, "2000000000017", Platform.wb)
    db.commit()
    task = returns.send_to_1c(db, mine)
    db.commit()

    returns.apply_1c_result(db, task, ok=True)
    db.commit()

    assert other.status is ReturnStatus.accepted


def test_the_send_can_be_recalled_only_before_the_task_leaves(db, product):
    """`pending` — файл ещё не собран, отмена ничего не стоит. `sent` — задание
    у 1С, и клик здесь его не вернёт; говорим это прямо."""
    item = returns.accept(db, "2000000000017", Platform.wb)
    db.commit()
    task = returns.send_to_1c(db, item)
    db.commit()

    returns.recall_before_send(db, item)
    db.commit()
    assert item.status is ReturnStatus.accepted
    assert db.query(FtpTask).filter(FtpTask.id == task.id).first() is None

    task = returns.send_to_1c(db, item)
    task.status = FtpTaskStatus.sent
    db.commit()
    with pytest.raises(returns.ReturnError):
        returns.recall_before_send(db, item)
    assert item.status is ReturnStatus.awaiting_1c


def test_a_test_return_marks_its_task_as_test(db, product):
    """`is_test` — граница безопасности: тестовое задание не должно создать
    документ в боевой базе 1С и не должно искажать сверку."""
    item = returns.accept(db, "2000000000017", Platform.wb, is_test=True)
    db.commit()
    task = returns.send_to_1c(db, item)
    db.commit()

    assert task.is_test is True
    assert _in_flight_adjustment(db, "uid-1") == 0


def test_the_platform_warehouse_map_covers_every_platform():
    """Разойдись карта с перечислением площадок — возврат по новой площадке
    упал бы KeyError уже в руках у кладовщика."""
    for platform in Platform:
        assert returns.source_warehouse(platform)


# --------------------------------------------------------------- ручной разбор

def test_a_stuck_return_is_not_described_in_the_words_of_a_movement(db, product):
    """Третий случай не похож ни на один из двух, и это про СЛЕДСТВИЕ.

    Возврат в «в пути» не участвует вовсе: остаток не занижен и не завышен — он
    просто ещё не вырос. Описать это словами создания («остаток занижен, наружу
    уходит меньше») значило бы пообещать, что оно рассосётся сверкой само, — а
    оно не рассосётся никогда: вещь лежит на складе, 1С её не приходовала, и
    продавать её нечем, пока человек не решит.
    """
    from app.workers.ftp_channel import review_effect

    item = returns.accept(db, "2000000000017", Platform.wb)
    db.commit()
    task = returns.send_to_1c(db, item)
    db.commit()

    effect = review_effect(task)

    assert "занижен" not in effect["effect"], "текст создания приписан возврату"
    assert "завышен" not in effect["effect"], "текст отмены приписан возврату"
    assert effect["oversell"] is False
    # Решение «документа нет» по возврату остаток НЕ двигает: 1С ничего не
    # приходовала, двигать нечего.
    assert "ВЫРАСТЕТ" not in effect["no_document_effect"]
    assert "УМЕНЬШИТСЯ" not in effect["no_document_effect"]


def test_every_command_has_its_own_wording(db):
    """Три команды — три разных следствия. Совпади два текста, один из них
    заведомо неправда: направления у них разные."""
    from app.workers.ftp_channel import review_effect

    texts = set()
    for command in ("CREATE_MOVEMENT", "CANCEL_MOVEMENT", returns.RETURN_COMMAND):
        effect = review_effect(FtpTask(command=command, barcode="111", quantity=1,
                                       order_id="X", status=FtpTaskStatus.timeout))
        assert set(effect) == {"effect", "no_document_effect", "warning", "oversell"}
        texts.add((effect["effect"], effect["no_document_effect"], effect["warning"]))
    assert len(texts) == 3


def test_manual_review_moves_the_return_too(db, product):
    """Не сдвинь мы вещь здесь, она осталась бы в «ждём 1С» навсегда — молча,
    при закрытом задании, и увидеть это можно было бы только глазами."""
    from app.workers.ftp_channel import resolve_stuck_task

    item = returns.accept(db, "2000000000017", Platform.wb)
    db.commit()
    task = returns.send_to_1c(db, item)
    task.status = FtpTaskStatus.timeout
    db.commit()

    resolve_stuck_task(db, task, document_exists=True, actor="оператор")

    assert item.status is ReturnStatus.back_to_sale


def test_manual_review_without_a_document_returns_the_item_to_work(db, product):
    """«Документа нет» по возврату — не конец: вещь физически на складе, и
    решение по ней ещё предстоит принять."""
    from app.workers.ftp_channel import resolve_stuck_task

    item = returns.accept(db, "2000000000017", Platform.wb)
    db.commit()
    task = returns.send_to_1c(db, item)
    task.status = FtpTaskStatus.timeout
    db.commit()

    resolve_stuck_task(db, task, document_exists=False, actor="оператор")

    assert item.status is ReturnStatus.rejected_1c


# --------------------------------------------------------------- хранение

def test_in_work_and_terminal_split_every_status():
    """Статус, не попавший ни в один список, не чистился бы НИКОГДА и не
    показывался бы в сводке — молча, без единого признака."""
    assert set(returns.IN_WORK) | set(returns.TERMINAL) == set(ReturnStatus)
    assert not set(returns.IN_WORK) & set(returns.TERMINAL)


def test_awaiting_1c_is_in_work_and_not_terminal():
    """Пустая строка в таблице переходов значит «руками не выйти», а не «выхода
    нет»: `awaiting_1c` двигает ответ 1С. Выведи кто-нибудь терминальные ИЗ
    ТАБЛИЦЫ, чистка удаляла бы вещи, прямо сейчас ждущие 1С, — ответ пришёл бы
    на запись, которой уже нет."""
    assert RETURN_TRANSITIONS[ReturnStatus.awaiting_1c] == ()
    assert ReturnStatus.awaiting_1c in returns.IN_WORK
    assert ReturnStatus.awaiting_1c not in returns.TERMINAL


def test_retention_removes_finished_returns_with_their_history(db, product):
    from app import retention
    from app.timeutils import now_utc as _now

    old = returns.accept(db, "2000000000017", Platform.wb)
    db.commit()
    returns.change_status(db, old, ReturnStatus.scrapped,
                          scrap_reason=ScrapReason.defect)
    db.commit()
    old.status_changed_at = _now() - retention.RETURN_KEEP * 2
    db.commit()
    old_id = old.id

    retention.apply_retention(db)

    assert db.query(ReturnItem).filter(ReturnItem.id == old_id).first() is None
    assert db.query(ReturnItemLog).filter(
        ReturnItemLog.return_id == old_id).count() == 0, \
        "история осиротела: массовый DELETE каскад не поднимает"


def test_retention_never_touches_an_unfinished_return(db, product):
    """«Ждём 1С» прямо сейчас означает вещь, которой нет ни у нас, ни в 1С.
    Удалить такую строку значит потерять её насовсем: ответ придёт на запись,
    которой уже нет, а вещь так и останется лежать на складе."""
    from app import retention
    from app.timeutils import now_utc as _now

    item = returns.accept(db, "2000000000017", Platform.wb)
    db.commit()
    returns.send_to_1c(db, item)
    db.commit()
    item.status_changed_at = _now() - retention.RETURN_KEEP * 5
    db.commit()

    retention.apply_retention(db)

    assert db.query(ReturnItem).filter(ReturnItem.id == item.id).first() is not None


# --------------------------------------------------------------- пояснения

def test_every_status_explains_itself_and_the_next_step():
    """Подпись «Отложен» не говорит человеку ни что это значит, ни что делать, а
    он видит её через неделю после того, как сам её поставил, и уже не помнит
    почему. Статус без пояснения — тот же немой отказ."""
    assert set(returns.RETURN_HINTS) == set(ReturnStatus)
    for status, hint in returns.RETURN_HINTS.items():
        means, do = hint
        assert means.strip() and do.strip(), f"пустое пояснение у {status.value}"


def test_a_terminal_status_says_there_is_nothing_left_to_do():
    """«Что делать» у законченного дела обязано отвечать «ничего»: иначе человек
    ищет кнопку, не находит и решает, что страница сломана."""
    for status in returns.TERMINAL:
        assert "ничего" in returns.RETURN_HINTS[status][1].lower(), \
            f"{status.value} предлагает работу, которой нет"


def test_awaiting_1c_does_not_promise_a_button_it_will_not_give():
    """Из «ждём 1С» руками не выйти вовсе, и подсказка обязана говорить это
    прямо — иначе она советует то, чего страница не даст."""
    assert RETURN_TRANSITIONS[ReturnStatus.awaiting_1c] == ()
    do = returns.RETURN_HINTS[ReturnStatus.awaiting_1c][1].lower()
    assert "руками этот статус не меняется" in do


# --------------------------------------------------------------- тренировка

def test_the_training_mode_marks_what_it_creates(db):
    assert returns.test_mode(db) is False, "по умолчанию — боевой режим"

    returns.set_test_mode(db, True)
    db.commit()

    assert returns.test_mode(db) is True
    item = returns.accept(db, "111", Platform.wb, is_test=returns.test_mode(db))
    db.commit()
    assert item.is_test is True


def test_a_training_return_never_makes_a_live_task(db, product):
    """Вся затея держится на этом: `build_task_batch` забирает только задания с
    `is_test=False`, значит тренировочное в файл для 1С не поедет никогда.
    Проверяем не флаг, а СЛЕДСТВИЕ — что задание в пачку не попало."""
    from app.workers.ftp_channel import build_task_batch

    item = returns.accept(db, "2000000000017", Platform.wb, is_test=True)
    db.commit()
    task = returns.send_to_1c(db, item)
    db.commit()

    assert task.is_test is True
    batch = build_task_batch(db)
    # Смотрим на САМ ФАЙЛ, который уедет в 1С, а не на выборку: предмет проверки
    # — то, что окажется на диске, а не то, что вернула функция.
    assert batch is None or returns.label_number(item) not in batch[1], \
        "тренировочное задание уехало бы в боевую 1С"


def test_a_live_task_does_reach_the_batch(db, product):
    """Иначе предыдущая проверка слепа: она молчала бы и на сломанной выборке."""
    from app.workers.ftp_channel import build_task_batch

    item = returns.accept(db, "2000000000017", Platform.wb, is_test=False)
    db.commit()
    task = returns.send_to_1c(db, item)
    db.commit()

    batch = build_task_batch(db)
    assert batch is not None and returns.label_number(item) in batch[1]


def test_the_flag_lives_on_the_thing_and_not_on_the_moment(db, product):
    """НАСТОЯЩАЯ вещь, отправленная во время тренировки, обязана уехать в 1С
    по-настоящему. Считай `send_to_1c` режим на момент отправки, боевой возврат,
    отправленный при включённой тренировке, молча стал бы тренировочным: в 1С он
    не поехал бы никогда, а при выходе из режима его бы ещё и стёрли."""
    real = returns.accept(db, "2000000000017", Platform.wb, is_test=False)
    db.commit()
    returns.set_test_mode(db, True)
    db.commit()

    task = returns.send_to_1c(db, real)
    db.commit()

    assert task.is_test is False


def test_answering_for_1c_is_refused_on_a_real_return(db, product):
    """Граница безопасности. Разреши мы это, кнопка объявила бы остаток
    выросшим, хотя 1С молчала: вещь считалась бы возвращённой в продажу, её
    отправили бы на площадки, а в 1С её нет."""
    real = returns.accept(db, "2000000000017", Platform.wb, is_test=False)
    db.commit()
    returns.send_to_1c(db, real)
    db.commit()

    with pytest.raises(returns.ReturnError):
        returns.simulate_1c(db, real, ok=True)
    assert real.status is ReturnStatus.awaiting_1c


def test_a_real_item_is_refused_even_if_its_task_looks_like_a_training_one(db, product):
    """Рубежа ДВА, и этот проверяет первый — по самой ВЕЩИ.

    Предыдущая проверка его не видит: у боевой вещи и задание боевое, так что
    отказ приходит от второго рубежа, и убери кто-нибудь первый — тест смолчал
    бы. Поэтому собираем состояние, невозможное по построению: боевая вещь с
    тренировочным заданием. Раз оно возникло, значит мы уже где-то ошиблись, и
    единственный верный ответ — отказ, а не «задание же тренировочное».
    """
    real = returns.accept(db, "2000000000017", Platform.wb, is_test=False)
    db.commit()
    task = returns.send_to_1c(db, real)
    task.is_test = True                      # так не бывает — и потому проверяем
    db.commit()

    with pytest.raises(returns.ReturnError):
        returns.simulate_1c(db, real, ok=True)
    assert real.status is ReturnStatus.awaiting_1c


def test_a_training_item_is_refused_if_its_task_is_live(db, product):
    """Зеркальный случай и второй рубеж: тренировочная вещь с БОЕВЫМ заданием.
    Подделать ответ на него — худшее из возможных продолжений: в 1С по такому
    заданию документ вполне может быть."""
    fake = returns.accept(db, "2000000000017", Platform.wb, is_test=True)
    db.commit()
    task = returns.send_to_1c(db, fake)
    task.is_test = False
    db.commit()

    with pytest.raises(returns.ReturnError):
        returns.simulate_1c(db, fake, ok=True)
    assert fake.status is ReturnStatus.awaiting_1c


def test_the_training_answer_walks_the_whole_way(db, product):
    """Без него тренировочная вещь зависла бы в «ждём 1С» навсегда, и человек не
    увидел бы ни одного из двух исходов — ровно тех экранов, ради которых
    тренировка и затевается."""
    item = returns.accept(db, "2000000000017", Platform.wb, is_test=True)
    db.commit()
    returns.send_to_1c(db, item)
    db.commit()

    returns.simulate_1c(db, item, ok=True)
    db.commit()

    assert item.status is ReturnStatus.back_to_sale


def test_the_training_refusal_lands_where_a_real_one_would(db, product):
    item = returns.accept(db, "2000000000017", Platform.wb, is_test=True)
    db.commit()
    returns.send_to_1c(db, item)
    db.commit()

    returns.simulate_1c(db, item, ok=False)
    db.commit()

    assert item.status is ReturnStatus.rejected_1c


def test_clearing_takes_the_training_data_and_nothing_else(db, product):
    """Единственное, чего нельзя сделать здесь ни при каких обстоятельствах, —
    задеть настоящий возврат: он описывает вещь, лежащую на складе, и
    восстановить его будет неоткуда."""
    real = returns.accept(db, "2000000000017", Platform.wb, is_test=False)
    fake = returns.accept(db, "2000000000017", Platform.wb, is_test=True)
    db.commit()
    real_task = returns.send_to_1c(db, real)
    fake_task = returns.send_to_1c(db, fake)
    db.commit()
    real_id, fake_id = real.id, fake.id
    real_task_id, fake_task_id = real_task.id, fake_task.id

    assert returns.test_items_count(db) == 1
    assert returns.clear_test_data(db) == 1
    db.commit()

    assert db.query(ReturnItem).filter(ReturnItem.id == real_id).first() is not None
    assert db.query(ReturnItem).filter(ReturnItem.id == fake_id).first() is None
    assert db.query(ReturnItemLog).filter(
        ReturnItemLog.return_id == fake_id).count() == 0, "история осиротела"
    assert db.query(ReturnItemLog).filter(
        ReturnItemLog.return_id == real_id).count() > 0, "стёрта чужая история"
    assert db.query(FtpTask).filter(FtpTask.id == real_task_id).first() is not None
    assert db.query(FtpTask).filter(FtpTask.id == fake_task_id).first() is None


def test_clearing_an_empty_installation_is_harmless(db, product):
    returns.accept(db, "2000000000017", Platform.wb, is_test=False)
    db.commit()

    assert returns.clear_test_data(db) == 0
    assert db.query(ReturnItem).count() == 1
