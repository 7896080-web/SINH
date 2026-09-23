"""Отбор строк для восстановления порогов по копии базы.

23.09 массовая кнопка схлопнула пороги у 62 товаров: факт стал равен учёту,
расхождение обнулилось, порог упал до брони — и наружу пошло больше, чем есть.
Прежний факт из текущей базы не восстановить, выручает только суточная копия.

Цена ошибки здесь вся в ОТБОРЕ, поэтому он и вынесен отдельной функцией: лишняя
строка в файле вернёт порог там, где человек изменил его намеренно, а
недостающая оставит оверселл. Обе ошибки молчаливые — файл выглядит одинаково.
"""

from scripts.restore_offsets_from_backup import (direction_note, outgoing,
                                                 plan_restore, to_rows)


def _row(uid, offset, *, stock=22, reserve=0, base=43, broadcasting=True):
    return {"uid_1c": uid, "article": "27643", "size": "XL", "color": "LACIVERT/RED",
            "name": "Свитшот", "stock_on_hand": stock, "reserve": reserve,
            "broadcast_enabled": 1 if broadcasting else 0,
            "broadcast_offset": offset, "offset_base_date": "2026-08-06",
            "offset_base_stock": base, "fact_at_date": None}


def _uids(items):
    return [uid for uid, *_ in items]


# ------------------------------------------------------ направление оверселла

def test_a_flattened_threshold_is_the_one_that_oversells():
    """Ровно случай 27643: порог 11 → 0, наружу пошло 22 вместо 11."""
    plan = plan_restore({"u1": _row("u1", 11)}, {"u1": _row("u1", 0)})

    assert _uids(plan["dropped"]) == ["u1"]
    assert _uids(plan["to_file"]) == ["u1"]


def test_a_raised_threshold_waits_unless_exact_is_asked():
    """Вырос порог — наружу уходит МЕНЬШЕ, чем есть. Не оверселл, не срочно.

    Но «точное восстановление» значит «как в копии», поэтому по просьбе он тоже
    возвращается."""
    was, now = {"u1": _row("u1", 1)}, {"u1": _row("u1", 5)}

    assert _uids(plan_restore(was, now)["to_file"]) == []
    assert _uids(plan_restore(was, now, exact=True)["to_file"]) == ["u1"]
    assert _uids(plan_restore(was, now)["raised"]) == ["u1"]


def test_an_unchanged_threshold_is_not_touched():
    plan = plan_restore({"u1": _row("u1", 11)}, {"u1": _row("u1", 11)}, exact=True)

    assert plan["to_file"] == []


def test_a_product_added_after_the_copy_is_skipped():
    """Восстанавливать нечего: в копии его нет вовсе."""
    plan = plan_restore({}, {"новый": _row("новый", 3)}, exact=True)

    assert plan["to_file"] == [] and plan["manual"] == []


# ------------------------------------------------- только транслируемые товары

def test_only_broadcasting_products_when_asked():
    """Оверселл возможен только там, где остаток уходит наружу прямо сейчас.

    Выключенную строку можно спокойно разобрать потом, а в файле она лишь
    отвлекает."""
    was = {"on": _row("on", 11), "off": _row("off", 11, broadcasting=False)}
    now = {"on": _row("on", 0), "off": _row("off", 0, broadcasting=False)}

    assert _uids(plan_restore(was, now, only_broadcasting=True)["to_file"]) == ["on"]
    assert sorted(_uids(plan_restore(was, now)["to_file"])) == ["off", "on"]


def test_broadcasting_is_read_from_the_live_base_not_the_copy():
    """Трансляцию могли включить после копии — важно сегодняшнее состояние."""
    was = {"u1": _row("u1", 11, broadcasting=False)}
    now = {"u1": _row("u1", 0, broadcasting=True)}

    assert _uids(plan_restore(was, now, only_broadcasting=True)["to_file"]) == ["u1"]


# ------------------------------------- чего файл не закрывает, о том он говорит

def test_a_threshold_that_did_not_exist_in_the_copy_goes_to_manual():
    """Снять порог у строки с датой файлом нельзя — это «Сбросить порог».

    Молча выбросить такую строку значило бы отчитаться «восстановлено всё»,
    когда часть работы осталась человеку."""
    plan = plan_restore({"u1": _row("u1", None)}, {"u1": _row("u1", 4)}, exact=True)

    assert _uids(plan["manual"]) == ["u1"]
    assert plan["to_file"] == []


def test_a_zero_threshold_where_there_was_none_raises_what_goes_out_when_a_reserve_exists():
    """«Порога не было» и «порог 0» — НЕ одно и то же при ненулевой брони.

    Без порога лестница считает `остаток − бронь`, с порогом 0 — весь остаток.
    23.09 таких строк оказалось 223, и от ответа «а есть ли у них бронь» зависело,
    беда это или безобидная перемена записи. Корзина обязана называть следствие,
    а не только себя."""
    row_with_reserve = _row("u1", 0, stock=10, reserve=3)

    assert outgoing(row_with_reserve, None) == 7      # было: остаток − бронь
    assert outgoing(row_with_reserve, 0) == 10        # стало: весь остаток

    row_without = _row("u2", 0, stock=10, reserve=0)
    assert outgoing(row_without, None) == outgoing(row_without, 0)


def test_the_script_says_whether_those_rows_changed_anything():
    """Иначе 223 строки в выводе — просто число, из которого ничего не следует."""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "scripts"
              / "restore_offsets_from_backup.py").read_text(encoding="utf-8")

    assert "ПОДНЯЛИ отправку (бронь перестала вычитаться)" in source
    assert "отправку не изменили" in source, \
        "безобидный случай тоже надо называть — молчание читается как «не считали»"


def test_an_impossible_threshold_is_reported_instead_of_written():
    """Импорт подбирает под порог факт, а склад отрицательным не бывает.

    Такую строку файл бы не применил — импорт отклонил бы её с номером строки, —
    поэтому её место в списке для разбора, а не в файле."""
    was = {"u1": _row("u1", 50)}
    now = {"u1": _row("u1", 0, base=4)}          # 4 − 50 + 0 < 0

    plan = plan_restore(was, now, exact=True)

    assert _uids(plan["impossible"]) == ["u1"]
    assert plan["to_file"] == []


def test_a_threshold_cleared_in_the_live_base_is_still_restorable():
    """Порог сняли кнопкой — вернуть его файлом можно, это обычное падение."""
    plan = plan_restore({"u1": _row("u1", 11)}, {"u1": _row("u1", None)})

    assert _uids(plan["dropped"]) == ["u1"]


# --------------------------------------------------------- что уходит в файл

def test_restoring_a_negative_threshold_increases_what_goes_out():
    """Отрицательный порог значит «на складе больше, чем числится в 1С».

    23.09 на бою такие нашлись у 25 транслируемых товаров, и один вернул бы 227
    штук при учёте 14: `max(0, 14 − (−213))`. То есть «точное восстановление»
    по этим строкам ведёт В СТОРОНУ ОВЕРСЕЛЛА, а не от него, — и корзина, куда
    они падают, раньше называлась «остаток занижен, не срочно».
    """
    was = {"u1": _row("u1", -213, stock=14)}
    now = {"u1": _row("u1", 0, stock=14)}

    plan = plan_restore(was, now, exact=True)

    assert _uids(plan["raised"]) == ["u1"]
    assert outgoing(now["u1"], 0) == 14
    assert outgoing(now["u1"], -213) == 227, "возврат отправил бы 227 при учёте 14"


def test_the_script_warns_when_the_file_would_raise_what_goes_out():
    """Направление важнее числа, и сказать о нём надо ДО заливки.

    Скрипт заканчивается словами «залейте на странице», поэтому нейтральное
    «после восстановления будет 435» читается как отчёт об успехе — а это
    предложение увеличить отправку на 288 штук."""
    raises = direction_note(147, 435)

    # «на 288 шт», а не просто «288»: перепутанный знак даёт «на -288 шт», и
    # проверка на подстроку «288» его пропускает. Поймано мутацией.
    assert "УВЕЛИЧИТ" in raises and "на 288 шт" in raises
    assert "оверселл" in raises


def test_the_note_names_the_safe_direction_too():
    """Снижение — это как раз починка, и назвать её надо своими словами."""
    lowers = direction_note(996, 578)

    assert "СНИЗИТ" in lowers and "на 418 шт" in lowers
    assert direction_note(100, 100) == "", "на равных числах сказать нечего"


def test_the_script_actually_prints_the_note():
    """Сама функция мало что значит, если её вывод никуда не попадает.

    Читатель у сигнала обязан быть: без этой проверки `main` мог перестать
    печатать `note`, а поведенческий тест выше остался бы зелёным."""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "scripts"
              / "restore_offsets_from_backup.py").read_text(encoding="utf-8")

    assert "note = direction_note(now_total, back_total)" in source
    assert "print(note)" in source


def test_a_negative_threshold_in_the_copy_is_counted_out_loud():
    """Число таких строк — единственное, по чему видно масштаб опасности."""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "scripts"
              / "restore_offsets_from_backup.py").read_text(encoding="utf-8")

    assert "ОТРИЦАТЕЛЬНЫМ порогом в копии" in source
    assert "порог вырос (остаток занижен)" not in source, \
        "вернулась формулировка «не срочно» про направление оверселла"


def test_the_file_carries_exactly_the_two_columns_the_import_reads():
    """Импорт разбирает «ID_1С» и «Порог трансляции»; остальное — для глаз.

    Колонка порога несёт значение ИЗ КОПИИ, а не текущее: иначе файл вернул бы
    ровно то, что уже стоит."""
    from scripts.restore_offsets_from_backup import HEADERS

    rows = to_rows(plan_restore({"u1": _row("u1", 11)},
                                {"u1": _row("u1", 0)})["to_file"])

    assert HEADERS[:2] == ["ID_1С", "Порог трансляции"]
    assert rows[0][0] == "u1"
    assert rows[0][1] == 11, "в файл уехал текущий порог вместо прежнего"


def test_the_numbers_shown_are_what_will_actually_go_out():
    """«Уходит сейчас → станет» — по той же формуле, что у рассылки."""
    row = _row("u1", 0, stock=22)

    assert outgoing(row, 0) == 22
    assert outgoing(row, 11) == 11
