"""Повтор баркода ВНУТРИ файла «Мэппинг → Импорт» не должен ронять импорт.

Поиск существующей привязки идёт запросом, а сессия живёт с `autoflush=False`:
только что добавленный баркод запрос не видит, и второй такой же в файле уходил
вторым `INSERT`. На коммите это `UNIQUE constraint failed: barcodes.barcode` —
500 и потеря ВСЕГО импорта, включая переподвязки, сделанные выше по файлу.
Оператор при этом видит страницу ошибки и не знает, что применилось, а что нет.

Повтор в файле — вещь обычная: выгрузка с площадки, где один баркод стоит у
нескольких строк, или файл, склеенный из двух.
"""
import io

from openpyxl import Workbook

from app.models import Barcode, Product


def _file(rows, headers=("Баркод", "ID_1С")):
    wb = Workbook(); ws = wb.active
    ws.append(list(headers))
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()


def _import(client, content, repoint=False):
    data = {"repoint": "1"} if repoint else {}
    return client.post("/mapping/import", files={"file": ("m.xlsx", content)},
                       data=data, follow_redirects=False)


def _product(db, uid):
    db.add(Product(uid_1c=uid, article=f"A-{uid}", name="Товар", stock_on_hand=1))
    db.commit()


def test_the_same_pair_twice_is_not_an_error(logged_in_client, web_db):
    _product(web_db, "u1")

    r = _import(logged_in_client, _file([("111", "u1"), ("111", "u1")]))

    assert r.status_code == 303, "импорт не должен падать в 500"
    web_db.expire_all()
    assert web_db.query(Barcode).count() == 1


def test_one_barcode_on_two_products_in_one_file_is_reported(logged_in_client, web_db):
    """Взять любой молча значило бы решить за человека, на какой товар спишется
    заказ по этому баркоду."""
    _product(web_db, "u1")
    _product(web_db, "u2")

    r = _import(logged_in_client, _file([("111", "u1"), ("111", "u2")]),)

    assert r.status_code == 303
    web_db.expire_all()
    rows = web_db.query(Barcode).all()
    assert len(rows) == 1 and rows[0].uid_1c == "u1"


def test_a_duplicate_does_not_swallow_the_rest_of_the_file(logged_in_client, web_db):
    """Главное последствие было не в самой строке, а в том, что с ней пропадал
    весь файл."""
    for uid in ("u1", "u2", "u3"):
        _product(web_db, uid)

    r = _import(logged_in_client, _file([
        ("111", "u1"), ("111", "u1"), ("222", "u2"), ("333", "u3")]))

    assert r.status_code == 303
    web_db.expire_all()
    assert {b.barcode for b in web_db.query(Barcode).all()} == {"111", "222", "333"}
