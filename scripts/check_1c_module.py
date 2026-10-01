"""Цел ли НУЖНЫЙ НАМ функционал в обработке 1С — только чтение.

Запуск:

    C:\\sync_admin\\.venv\\Scripts\\python.exe C:\\sync_admin\\scripts\\check_1c_module.py C:\\путь\\модуль.txt
    ... check_1c_module.py          # без аргумента — копия из репозитория, 1c/

Как достать текст доработанной `.epf`: Конфигуратор -> открыть обработку ->
Модуль объекта -> Ctrl+A, Ctrl+C -> вставить в файл .txt и указать его здесь.
Распаковать `.epf` снаружи нечем: это двоичный контейнер 1С, а компилятора в
нашей среде нет вовсе.

Зачем это нужно. Обработку правят в Конфигураторе, и правки бывают не наши:
кто-то добавляет свою команду, трогает соседнюю процедуру, переносит строку.
Отказ при этом НЕМОЙ — обмен продолжает работать, просто перестаёт делать что-то
одно: не публикует файл, не называет команду в ответе, не ищет проведённый
документ перед повтором. Узнать об этом можно было бы только по следствию, то
есть неделями позже и уже на остатках.

Требования скрипт берёт ИЗ НАШЕГО КОДА, а не держит у себя списком, где это
возможно: команды задания — из `app`, имена файлов ответов — из глобов
`ftp_channel`. Разойдись список с кодом, проверка однажды пропустила бы ровно
то, ради чего написана. То, что списком всё же перечислено (три команды
перемещения и две выгрузки), проверяется на присутствие в исходниках — список,
отставший от кода, скажет об этом сам.

Проверки грубые, по тексту: это не компилятор и не замена синтаксическому
контролю (Ctrl+F7 в Конфигураторе обязателен). Они ловят ровно тот класс, что
порождает правка чужими руками, — исчезнувшую возможность.

Ничего не меняет и не публикует.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MODULE = os.path.join(ROOT, "1c", "ОбменССайтом_МодульОбъекта.txt")
FTP_CHANNEL = os.path.join(ROOT, "app", "workers", "ftp_channel.py")

# Консоль боевого сервера пишет в cp1251, а сюда печатаются куски ЧУЖОГО текста:
# строки модуля 1С, имена процедур. Один символ, которого в кодировке нет, уронил
# бы весь вывод посреди строки — человек получил бы трассировку вместо ответа.
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):     # перенаправленный вывод, старый Python
    pass

# Команды перемещения и выгрузок перечислены здесь, потому что в коде они живут
# разрозненно: часть литералами в `order_poller`, часть в `ftp_channel`. Чтобы
# список не отстал молча, `check_commands` проверяет каждую на присутствие в
# исходниках — отставший список скажет об этом сам.
MOVEMENT_COMMANDS = ("CREATE_MOVEMENT", "CONFIRM_MOVEMENT", "CANCEL_MOVEMENT")
EXPORT_COMMANDS = ("EXPORT_STOCK_ON_HAND", "EXPORT_BARCODES")


def _our_commands() -> list[str]:
    """Всё, что умеет отправить наша сторона. Импортом, а не списком."""
    from app.returns import RETURN_COMMAND, SCRAP_COMMAND
    from app.workers.ftp_channel import STOCK_ON_DATE_COMMAND

    return sorted({*MOVEMENT_COMMANDS, *EXPORT_COMMANDS,
                   RETURN_COMMAND, SCRAP_COMMAND, STOCK_ON_DATE_COMMAND})


def _our_result_prefixes() -> list[str]:
    """Префиксы файлов, которые наша сторона ИЩЕТ в каталоге ответов.

    Берём из самих глобов `ftp_channel`: повтори мы их списком, обработка могла
    бы перестать публиковать файл, который мы ждём, а проверка — согласиться.
    """
    text = open(FTP_CHANNEL, encoding="utf-8").read()
    out = []
    for pattern in re.findall(r'\.glob\("([^"]+)"\)', text):
        out.append(re.split(r"[\[*]", pattern)[0])
    return sorted(set(out))


def _code(text: str) -> str:
    """Текст без комментариев: в них встречаются и «Если», и кавычки."""
    return "\n".join(ln.split("//")[0] for ln in text.splitlines())


# ------------------------------------------------------------- проверки

def check_commands(text: str):
    """Обработка обязана разбирать КАЖДУЮ команду, которую мы отправляем."""
    stale = []
    sources = ""
    for folder, _dirs, files in os.walk(os.path.join(ROOT, "app")):
        for name in files:
            if name.endswith(".py"):
                sources += open(os.path.join(folder, name), encoding="utf-8").read()
    for command in MOVEMENT_COMMANDS + EXPORT_COMMANDS:
        if command not in sources:
            stale.append(command)
    if stale:
        return False, (f"список проверки отстал от кода: {', '.join(stale)} "
                       f"в `app` больше не встречается")

    known = set(re.findall(r'Команда = "([A-Z_]+)"', text))
    missing = [c for c in _our_commands() if c not in known]
    if missing:
        return False, (f"обработка не разбирает: {', '.join(missing)}; "
                       f"разбирает: {', '.join(sorted(known)) or '(ничего)'}")
    return True, f"разбираются все {len(_our_commands())}"


def check_result_files(text: str):
    """Публикуется всё, что мы ищем в каталоге ответов."""
    published = set(re.findall(r'ОпубликоватьФайл\([^,]+,\s*"([a-z_]+)"', text))
    missing = [p for p in _our_result_prefixes() if p not in published]
    if missing:
        return False, (f"не публикуется: {', '.join(missing)}; "
                       f"публикуется: {', '.join(sorted(published)) or '(ничего)'}")
    return True, f"публикуются все {len(_our_result_prefixes())}"


def check_answer_names_command(text: str):
    """Ответ несёт имя команды четвёртым полем."""
    known = set(re.findall(r'Команда = "([A-Z_]+)"', text))
    answers = [ln.strip() for ln in text.splitlines()
               if "СтрокиРезультата.Добавить(" in ln]
    if not answers:
        return False, "в обработке нет ни одной строки ответа"
    for line in answers:
        if '+ "|" + Команда' in line:
            continue                      # имя подставляется переменной ветки
        literal = re.search(r'\|([A-Z_]+)"\)', line)
        if literal is None:
            return False, f"ответ без имени команды: {line[:90]}"
        if literal.group(1) not in known:
            return False, (f"ответ называет команду {literal.group(1)}, которой "
                           f"обработка не разбирает")
    return True, f"строк ответа {len(answers)}, у каждой есть имя команды"


def check_field_count_guard(text: str):
    """Длина строки задания проверяется ДО обращения по индексу."""
    missing = [g for g in ("Если Поля.Количество() < 7 Тогда",
                           "Если Поля.Количество() < 2 Тогда") if g not in text]
    if missing:
        return False, f"нет проверки длины: {missing}"
    return True, "длина проверяется до индексации"


def check_structure(text: str):
    """Баланс блоков: замена компилятору, которого здесь нет."""
    words = re.findall(r"[А-Яа-яЁё]+", _code(text))
    pairs = {"Если": "КонецЕсли", "Цикл": "КонецЦикла", "Попытка": "КонецПопытки",
             "Функция": "КонецФункции", "Процедура": "КонецПроцедуры"}
    broken = []
    for start, end in pairs.items():
        a, b = words.count(start), words.count(end)
        if a != b:
            broken.append(f"{start} {a} / {end} {b}")
    if broken:
        return False, "; ".join(broken)
    return True, "блоки сбалансированы"


def check_transactions(text: str):
    """Транзакция без отката оставит первый документ проведённым."""
    body = _code(text)
    begins = body.count("НачатьТранзакцию()")
    commits = body.count("ЗафиксироватьТранзакцию()")
    rollbacks = body.count("ОтменитьТранзакцию()")
    if not begins:
        return False, "транзакций нет вовсе — утилизация делает два документа порознь"
    if begins != commits or begins != rollbacks:
        return False, (f"начато {begins}, зафиксировано {commits}, откатов "
                       f"{rollbacks}")
    return True, f"транзакций {begins}, у каждой фиксация и откат"


def check_no_hardcoded_operation(text: str):
    """Наименование хоз. операции списания не зашито в обработке."""
    hardcoded = re.findall(r'"(Утилизация[^"]*)"', _code(text))
    if hardcoded:
        return False, f"зашито: {', '.join(sorted(set(hardcoded)))}"
    return True, "приезжает полем задания"


def check_scrap_indexes(text: str):
    """Поля строки утилизации читаются теми же номерами, что мы кладём."""
    if 'ИначеЕсли Команда = "SCRAP_RETURN"' not in text:
        return False, "ветки SCRAP_RETURN нет"
    block = text[text.index('ИначеЕсли Команда = "SCRAP_RETURN"'):]
    block = block[:block.index('ИначеЕсли Команда = "CANCEL_MOVEMENT"')]
    expected = {
        "7 (дата документа)": "ДатаДок = ?(Поля.Количество() > 7, Поля[7]",
        "8 (хоз. операция)": "ХозОперация = ?(Поля.Количество() > 8, СокрЛП(Поля[8])",
        "9 (ответственный)": "Ответственный = ?(Поля.Количество() > 9, СокрЛП(Поля[9])",
    }
    missing = [name for name, needle in expected.items() if needle not in block]
    if missing:
        return False, f"поле читается не тем номером: {', '.join(missing)}"
    return True, "поля 7, 8, 9 читаются как мы их кладём"


def check_scrap_document(text: str):
    """Реквизиты, без которых списание не проведётся или уйдёт не туда."""
    if "Функция СписатьВозврат" not in text:
        return False, "функции СписатьВозврат нет"
    block = text[text.index("Функция СписатьВозврат"):]
    block = block[:block.index("КонецФункции")]
    required = ("Документ.Склад =", "Документ.Магазин =",
                "Документ.ХозяйственнаяОперация =", "Документ.Ответственный =",
                "СтрокаТовара.Номенклатура =",
                "СтрокаТовара.ХарактеристикаНоменклатуры =",
                "СтрокаТовара.Количество =", "СтрокаТовара.ЕдиницаИзмерения =",
                "СтрокаТовара.Коэффициент =", "СтрокаТовара.СтатьяЗатрат =",
                "СтрокаТовара.КлючСтроки =")
    missing = [a for a in required if a not in block]
    if missing:
        return False, f"не заполняется: {', '.join(missing)}"
    return True, f"заполняются все {len(required)}"


def check_create_idempotent(text: str):
    """Повтор зависшего создания не должен списать остаток ВТОРОЙ раз."""
    if "НайтиПроведённоеПеремещение" not in text:
        return False, ("нет поиска проведённого перемещения: при включённом "
                       "MOVEMENT_REPOST_ENABLED каждый повтор спишет остаток заново")
    return True, "повтор находит свой документ и второго не создаёт"


def check_scrap_idempotent(text: str):
    """Идемпотентность спрашивают у ПОСЛЕДНЕГО документа цепочки."""
    if "НайтиПроведённоеСписаниеВозврата" not in text:
        return False, ("нет поиска проведённого списания: повтор утилизации "
                       "сделал бы вторую пару документов")
    return True, "повтор смотрит на списание, а не на перемещение"


def check_reverse_excluded(text: str):
    """Отмена не должна находить собственные реверсы."""
    if "sync REVERSE" not in text:
        return False, ("реверсы не исключены: вторая отмена отменила бы и возврат, "
                       "вернув на ЦС вдвое больше")
    return True, "реверсы исключены по шаблону"


def check_answer_mark_from_task(text: str):
    """Имя файла ответа берётся из имени задания, а не из текущего времени."""
    if 'СтрЗаменить(ФайлЗадания.Имя, "task_", "")' not in text:
        return False, ("метка не из имени задания: два файла в одну секунду "
                       "получат одно имя ответа, и второй затрёт первый")
    return True, "метка из имени задания — ответы не затирают друг друга"


def check_archive_right_after_answer(text: str):
    """Задание архивируется сразу после ответа, до тяжёлых выгрузок."""
    for needle in ("АрхивироватьЗадание(Пар, ФайлЗадания)", '"result_"', '"stock_"'):
        if needle not in text:
            return False, f"в модуле нет {needle}"
    answer = text.index('"result_"')
    archive = text.index("АрхивироватьЗадание(Пар, ФайлЗадания)",
                         text.index("Процедура ОбработатьФайлЗадания"))
    stock = text.index('"stock_"')
    if not (answer < archive < stock):
        return False, ("архивирование стоит не между ответом и выгрузками: "
                       "исключение на выгрузке оставит задание в tasks, и "
                       "следующий запуск создаст ДУБЛИ документов")
    return True, "ответ -> архив -> выгрузки"


def check_catalog_by_metadata(text: str):
    """Имя справочника не угадывается, а спрашивается у метаданных."""
    if 'ОписаниеТипа.СодержитТип(Тип("СправочникСсылка.' not in text:
        return False, ("справочник ищется не по метаданным: угаданное имя либо не "
                       "пройдёт контроль, либо найдёт не то")
    return True, "справочник ищется по описанию типа реквизита"


CHECKS = (
    ("команды задания", check_commands),
    ("файлы ответов", check_result_files),
    ("имя команды в ответе", check_answer_names_command),
    ("длина строки до индексации", check_field_count_guard),
    ("баланс блоков", check_structure),
    ("транзакции", check_transactions),
    ("хоз. операция не зашита", check_no_hardcoded_operation),
    ("номера полей утилизации", check_scrap_indexes),
    ("реквизиты списания", check_scrap_document),
    ("идемпотентность создания", check_create_idempotent),
    ("идемпотентность утилизации", check_scrap_idempotent),
    ("реверсы исключены", check_reverse_excluded),
    ("метка ответа", check_answer_mark_from_task),
    ("порядок архивирования", check_archive_right_after_answer),
    ("справочник по метаданным", check_catalog_by_metadata),
)


def run(text: str) -> list[tuple[str, bool, str]]:
    """Прогнать все проверки. Упавшая проверка — своя строка, не обрыв всего."""
    out = []
    for title, func in CHECKS:
        try:
            ok, detail = func(text)
        except Exception as e:                       # noqa: BLE001
            ok, detail = False, f"проверка сломалась: {type(e).__name__}: {e}"
        out.append((title, ok, detail))
    return out


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MODULE
    if not os.path.exists(path):
        print(f"Файла нет: {path}")
        return 2
    text = open(path, encoding="utf-8-sig").read()

    print("=== ПРОВЕРКА ОБРАБОТКИ 1С ===")
    print(f"файл: {path}")
    print(f"строк: {len(text.splitlines())}")
    if len(sys.argv) <= 1:
        # Сказать это обязательно: копия в репозитории на работу 1С не влияет
        # вовсе, и зелёный отчёт по ней про живую .epf не говорит ничего.
        print("ВНИМАНИЕ: это копия из репозитория, а не текст живой .epf.")
        print("Выгрузите модуль Конфигуратором и укажите файл аргументом.")
    print()

    results = run(text)
    for title, ok, detail in results:
        print(f"  [{'OK ' if ok else 'НЕТ'}] {title}: {detail}")

    bad = [t for t, ok, _ in results if not ok]
    print()
    if bad:
        print(f"ПОТЕРЯНО ИЛИ ИЗМЕНЕНО: {len(bad)} из {len(results)} — {', '.join(bad)}")
        print("Это не косметика: каждая строка выше называет, что перестанет работать.")
        return 1
    print(f"Все {len(results)} проверок прошли: нужный нам функционал на месте.")
    print("Это НЕ замена синтаксическому контролю (Ctrl+F7) в Конфигураторе.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
