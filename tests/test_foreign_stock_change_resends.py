"""Остаток ЦС изменили НЕ МЫ — значит по этим позициям сразу идёт переотправка.

Правило, и оно про единственный случай, когда площадка узнаёт о складе не от
нашего заказа. Наши собственные движения площадка получает сама: приём заказа
списывает остаток и тут же ставит отправку. А склад меняется и мимо нас —
розницей, перемещением, списанием, пересортицей, поставкой, — и узнаём мы об
этом ровно двумя способами: минутной дельтой (`delta_*.txt`, 1С кладёт её сама)
и часовым полным снимком. Оба обязаны поставить переотправку по ИЗМЕНИВШИМСЯ
позициям, и немедленно.

Почему немедленно и почему это вообще надо закреплять. Рассылка СОБЫТИЙНАЯ: не
поставив запись сейчас, мы не поставим её никогда — следующая отправка будет,
только когда остаток изменится снова, а у медленного размера это месяцы. То
есть пропущенная переотправка — это не «уедет позже», а «на площадке навсегда
осталось старое число». В сторону занижения это упущенные продажи, в сторону
завышения — прямой оверселл: склад опустел розницей, а карточка продолжает
продавать по нашему прежнему числу.

Тремя тестами закрыты три половины правила, и третья не менее важна первых:
переотправка идёт ТОЛЬКО по изменившимся. Ставь мы её на каждую строку снимка,
каждый час в очередь уезжал бы весь каталог — сто пятьдесят тысяч записей на
кабинет, и рассылка перестала бы успевать за настоящими событиями.
"""
import pytest

from app.models import (Barcode, DispatchQueueItem, Platform, Product,
                        SyncSetting)
from app.workers.reconciliation import run_reconciliation
from tests.factories import make_account


@pytest.fixture()
def ready(db):
    """Товар, доведённый до состояния, в котором остаток реально уходит наружу.

    Трансляция включена и кабинет покрыт расчётом — иначе `enqueue_full_resend`
    справедливо откажет (на непокрытый кабинет ушёл бы ноль), и тест проверял бы
    не правило, а гейт.
    """
    account = make_account(db)
    product = Product(uid_1c="u1", article="A1", name="Свитшот",
                      stock_on_hand=10, broadcast_enabled=True,
                      recalc_account_ids=str(account.id))
    db.add(product)
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()
    return product, account


def _queued(db, uid_1c="u1"):
    return (db.query(DispatchQueueItem)
            .filter(DispatchQueueItem.uid_1c == uid_1c)
            .order_by(DispatchQueueItem.id).all())


def test_a_partial_delta_from_1c_enqueues_a_resend(ready, db):
    """Минутная дельта — это и есть «склад изменился мимо нас».

    Применяется она тем же `run_reconciliation`, что и часовой снимок, только с
    `missing_means_zero=False`. Разведи кто-нибудь эти два пути (соблазн прямой:
    «дельта — это же просто обновить остаток»), и переотправка исчезла бы молча
    именно на самом быстром канале, по которому приходят правки 1С.
    """
    product, account = ready

    # 1С сообщила: на складе стало 7 — три штуки ушли не нашей продажей.
    run_reconciliation(db, {"111": 7}, missing_means_zero=False)

    db.refresh(product)
    assert product.stock_on_hand == 7
    rows = _queued(db)
    assert len(rows) == 1, "по изменившейся позиции обязана быть переотправка"
    assert rows[0].account_id == account.id
    assert rows[0].quantity == 7, "в очередь кладётся НОВОЕ число"
    assert rows[0].reason == "reconciliation"


def test_a_product_zeroed_by_the_full_snapshot_is_withdrawn(ready, db):
    """Товар исчез из полного снимка — остаток в ноль, и ноль обязан уехать.

    Это самый дорогой случай правила: склад опустел, а на карточке осталось
    наше прежнее число, и площадка продолжает продавать то, чего нет. Молчание
    здесь — прямой оверселл, и исправиться само оно не может: остаток уже ноль,
    значит следующего события по строке не будет никогда.
    """
    product, account = ready
    # Второй товар в снимке есть — иначе сработает предохранитель «снимок покрыл
    # меньше половины прежних ненулевых» и обнуления не будет вовсе.
    db.add(Product(uid_1c="u2", article="A2", name="Джемпер", stock_on_hand=5))
    db.add(Barcode(barcode="222", uid_1c="u2"))
    db.commit()

    run_reconciliation(db, {"222": 5}, missing_means_zero=True)

    db.refresh(product)
    assert product.stock_on_hand == 0, "в снимке товара нет — склад пуст"
    rows = _queued(db)
    assert len(rows) == 1, "отзыв обязан уехать: площадка держит прежнее число"
    assert rows[0].quantity == 0


def test_a_matching_snapshot_enqueues_nothing(ready, db):
    """Переотправка идёт ТОЛЬКО по изменившимся — и это половина правила.

    Ставь мы её на каждую строку снимка, каждый час в очередь уезжал бы весь
    каталог: сто пятьдесят тысяч записей на кабинет, и рассылка перестала бы
    успевать за настоящими событиями. Сошлось — значит площадка и так держит
    наше число.
    """
    product, _ = ready

    run_reconciliation(db, {"111": 10}, missing_means_zero=False)

    db.refresh(product)
    assert product.stock_on_hand == 10
    assert _queued(db) == [], "по сошедшейся позиции отправлять нечего"


def test_every_marked_account_gets_the_change(ready, db):
    """Позиция изменилась — переотправка идёт на ВСЕ отмеченные кабинеты.

    Остаток один на товар, а карточек у него столько, сколько кабинетов. Поставь
    мы запись на один, второй остался бы с прежним числом — и это тот же
    оверселл, просто на кабинете, которым давно не занимались.
    """
    product, first = ready
    second = make_account(db, platform=Platform.ozon, name="Озон")
    db.add(SyncSetting(uid_1c="u1", account_id=second.id, enabled=True))
    product.recalc_account_ids = f"{first.id},{second.id}"
    db.commit()

    run_reconciliation(db, {"111": 4}, missing_means_zero=False)

    rows = _queued(db)
    assert {r.account_id for r in rows} == {first.id, second.id}, rows
    assert all(r.quantity == 4 for r in rows)
