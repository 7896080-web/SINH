"""Оперативное изменение остатка ЦС: 1С кладёт файл сама, не дожидаясь часовой выгрузки.

Полный снимок — это 152 тыс. товаров, поэтому его просят раз в час, и правка в 1С
доезжала до страницы до часа с четвертью. Дельта сокращает это до минуты.

Три вещи, без которых механизм опаснее пользы, и все три здесь проверяются:
  1. частичный файл НЕ обнуляет то, чего в нём нет — иначе первое же сообщение
     обнулило бы весь каталог;
  2. свои же документы (источник `sync`) отбрасываются — их эффект уже учтён в
     момент приёма заказа, применить второй раз значит списать дважды;
  3. документ, применённый однажды, второй раз не применяется — файл приезжает
     повторно при переотправке, повторном проведении, ручном перезапуске.
"""
from datetime import timedelta

from app.models import Barcode, FtpTask, FtpTaskStatus, Platform, PlatformAccount, Product, StockDeltaDocument
from app.timeutils import now_utc
from app.workers.ftp_channel import (LocalExchange, SYNC_SOURCE, collect_stock_delta,
                                     finalize_stock_delta,
                                     parse_stock_delta_file)
from app.workers.reconciliation import run_reconciliation


def _exchange(tmp_path) -> LocalExchange:
    for d in ("t", "r", "a"):
        (tmp_path / d).mkdir(exist_ok=True)
    return LocalExchange(str(tmp_path / "t"), str(tmp_path / "r"), str(tmp_path / "a"))


def _delta(ex, name, body):
    (tmp := ex.dir_results / name).write_text(body, encoding="utf-8")
    return tmp


def _product(db, uid="u1", barcode="111", stock=10):
    db.add(Product(uid_1c=uid, article="A", name="Товар", stock_on_hand=stock, reserve=0))
    db.add(Barcode(barcode=barcode, uid_1c=uid))
    db.commit()


# ------------------------------------------------------------------ разбор строки

def test_the_line_carries_barcode_quantity_source_and_document():
    rows = parse_stock_delta_file("111|7|Реализация|ЦБ000000123")

    assert rows == [{"barcode": "111", "quantity": 7,
                     "source": "реализация", "document_id": "ЦБ000000123"}]


def test_a_negative_stock_survives_parsing():
    """Пересортица хранится как есть — на площадку всё равно уходит max(0, …)."""
    assert parse_stock_delta_file("111|-3|Списание|ЦБ1")[0]["quantity"] == -3


def test_broken_lines_are_skipped_not_guessed():
    rows = parse_stock_delta_file(
        "мусор\n"
        "111|нечисло|Реализация|ЦБ1\n"
        "|5|Реализация|ЦБ2\n"
        "222|4|Реализация\n"            # нет идентификатора — полей мало
        "333|9|Реализация|ЦБ3\n"
    )

    assert [r["barcode"] for r in rows] == ["333"]


# ------------------------------------------------------------------ два предохранителя

def test_our_own_documents_are_dropped(db, tmp_path):
    """Эхо собственных действий: наше перемещение уже списало остаток при приёме
    заказа, применить его второй раз — списать дважды."""
    ex = _exchange(tmp_path)
    _delta(ex, "delta_20260919020000.txt",
           f"111|7|{SYNC_SOURCE}|ЦБ1\n222|4|Реализация|ЦБ2")

    mapping, stats = collect_stock_delta(db, ex)

    assert mapping == {"222": 4}
    assert stats["ours_skipped"] == 1


def test_a_document_is_applied_only_once(db, tmp_path):
    ex = _exchange(tmp_path)
    _delta(ex, "delta_20260919020000.txt", "111|7|Реализация|ЦБ1")
    _collect_and_finalize(db, ex)

    _delta(ex, "delta_20260919021000.txt", "111|5|Реализация|ЦБ1")   # тот же документ
    mapping, stats = _collect_and_finalize(db, ex)

    assert mapping == {}
    assert stats["already_applied"] == 1


def test_a_document_is_not_marked_applied_before_the_stock_moves(db, tmp_path):
    """Пометка «применён» ставится только при закреплении.

    Раньше она коммитилась при чтении: падение на применении остатка оставляло
    документ помеченным, а остаток нетронутым — правка 1С пропадала насовсем, и
    повторная присылка того же файла была бы отброшена как «уже применён».
    """
    ex = _exchange(tmp_path)
    _delta(ex, "delta_20260919020000.txt", "111|7|Реализация|ЦБ1")

    mapping, stats = collect_stock_delta(db, ex)

    assert mapping == {"111": 7}
    assert db.query(StockDeltaDocument).count() == 0, "документ помечен до применения"

    finalize_stock_delta(db, ex, stats)
    assert db.query(StockDeltaDocument).count() == 1


def test_a_repeat_inside_one_file_is_counted_once(db, tmp_path):
    ex = _exchange(tmp_path)
    _delta(ex, "delta_20260919020000.txt", "111|7|Реализация|ЦБ1\n222|4|Реализация|ЦБ1")

    mapping, _ = _collect_and_finalize(db, ex)

    assert mapping == {"111": 7, "222": 4}          # один документ, две позиции
    assert db.query(StockDeltaDocument).count() == 1


def test_a_line_without_a_document_id_is_skipped(db, tmp_path):
    """Без идентификатора повтор неотличим от новости. Часовая выгрузка всё равно
    принесёт этот остаток — не позже чем через час."""
    ex = _exchange(tmp_path)
    _delta(ex, "delta_20260919020000.txt", "111|7|Реализация|")

    mapping, stats = collect_stock_delta(db, ex)

    assert mapping == {} and stats["no_document_id"] == 1


def _collect_and_finalize(db, ex):
    """Прочитать дельту и ЗАКРЕПИТЬ её — так, как это делает планировщик.

    Закрепление (пометка документов применёнными + архив файла) вынесено из
    чтения намеренно: раньше оно делалось сразу, и сбой на ПРИМЕНЕНИИ остатка
    оставлял файл в архиве, а документ помеченным — то есть правка 1С пропадала
    насовсем, и повторная присылка того же файла была бы отброшена как «уже
    применён». Теперь порядок такой: прочитали → применили остаток → закрепили.
    """
    mapping, stats = collect_stock_delta(db, ex)
    finalize_stock_delta(db, ex, stats)
    return mapping, stats


def test_the_file_is_archived_after_it_is_applied(db, tmp_path):
    """Файл уезжает в архив ПОСЛЕ закрепления, а не при чтении."""
    ex = _exchange(tmp_path)
    _delta(ex, "delta_20260919020000.txt", "111|7|Реализация|ЦБ1")

    mapping, stats = collect_stock_delta(db, ex)

    assert (tmp_path / "r" / "delta_20260919020000.txt").exists(), (
        "файл убран до применения — сбой на применении потерял бы правку 1С")

    finalize_stock_delta(db, ex, stats)

    assert (tmp_path / "a" / "delta_20260919020000.txt").exists()
    assert not (tmp_path / "r" / "delta_20260919020000.txt").exists()




# ------------------------------------------------------------------ частичность

def test_a_delta_never_zeroes_what_it_does_not_mention(db, tmp_path):
    """Самый опасный случай: полный снимок обнуляет отсутствующих намеренно, и
    дельта, применённая как снимок, обнулила бы весь каталог."""
    _product(db, uid="u1", barcode="111", stock=10)
    _product(db, uid="u2", barcode="222", stock=20)
    ex = _exchange(tmp_path)
    _delta(ex, "delta_20260919020000.txt", "111|7|Реализация|ЦБ1")

    mapping, _ = collect_stock_delta(db, ex)
    run_reconciliation(db, mapping, missing_means_zero=False)

    db.expire_all()
    assert db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand == 7
    assert db.query(Product).filter(Product.uid_1c == "u2").first().stock_on_hand == 20


def test_the_delta_respects_goods_in_flight(db, tmp_path):
    """Формула одна и та же: остаток = число 1С минус «в пути». Отдельной копии
    расчёта у дельты нет — иначе они разъедутся, как уже бывало."""
    _product(db, uid="u1", barcode="111", stock=10)
    acc = PlatformAccount(platform=Platform.wb, name="ИП", warehouse_id="wh")
    db.add(acc)
    db.commit()
    db.add(FtpTask(command="CREATE_MOVEMENT", barcode="111", quantity=1, order_id="o1",
                   account_id=acc.id, status=FtpTaskStatus.sent, is_test=False,
                   sent_at=now_utc() - timedelta(minutes=1)))
    db.commit()

    ex = _exchange(tmp_path)
    _delta(ex, "delta_20260919020000.txt", "111|9|Реализация|ЦБ1")
    mapping, _ = collect_stock_delta(db, ex)
    run_reconciliation(db, mapping, missing_means_zero=False, snapshot_at=now_utc())

    db.expire_all()
    assert db.query(Product).first().stock_on_hand == 8      # 9 из 1С минус 1 в пути


# ------------------------------------------------------------------ соседство файлов

def test_a_delta_file_is_never_taken_for_a_full_snapshot(tmp_path):
    """Полный снимок обнуляет отсутствующих. Файл дельты, попавший в список
    снимков, обнулил бы каталог — поэтому имя снимка сужено до `stock_<цифры>`."""
    ex = _exchange(tmp_path)
    _delta(ex, "delta_20260919020000.txt", "111|7|Реализация|ЦБ1")
    _delta(ex, "stock_delta_20260919020000.txt", "111|7|Реализация|ЦБ1")
    _delta(ex, "stock_20260919020000.txt", "u1|A|Т|8|111")

    assert ex.list_stock_files() == ["stock_20260919020000.txt"]
    assert ex.list_stock_delta_files() == ["delta_20260919020000.txt"]
