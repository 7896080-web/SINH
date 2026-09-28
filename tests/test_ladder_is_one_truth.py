"""Страница и рассылка обязаны считать ОДНО И ТО ЖЕ — на всех сочетаниях сразу.

`explain()` рисует оператору, сколько уйдёт на площадку, а
`quantity_for_account()` решает, сколько уйдёт на самом деле. Разойдись они —
строка показывает одно, на карточку уезжает другое, и узнать об этом неоткуда:
ошибка видна только тому, кто сравнит страницу с кабинетом площадки руками.
Так уже было: рассылка не дублировала ступени 1–2, считая, что «очередь копится
только по отмеченным кабинетам», и запись, пережившая снятие галочки, уносила
на отключённый кабинет полный остаток.

Перебор ВНУТРИ одного теста, а не параметризацией: фикстура базы стоит дороже
самой проверки, и полторы тысячи отдельных тестов превратили бы полсекунды
работы в полминуты прогона набора.
"""
import itertools

from app.models import Product, PlatformAccount, SyncSetting
from app.transmit import explain, quantity_for_account, blocked_by_switch


COMBOS = list(itertools.product(
    [True, False],      # трансляция товара
    [True, False],      # кабинет отмечен
    [True, False],      # рассылка на кабинет не на паузе
    [True, False],      # кабинет покрыт расчётом
    [True, False],      # покрытие вообще отслеживается
    [0, 5, 20],         # остаток ЦС
    [0, 3],             # бронь
    [None, 4],          # порог трансляции
    [None, 7],          # ручной остаток (legacy)
    [0, 5],             # порог кабинета
))


def test_the_page_and_the_dispatcher_never_disagree(db):
    account = PlatformAccount(platform="wb", name="ИП А", warehouse_id="1",
                              is_active=True, dispatch_enabled=True)
    db.add(account)
    db.flush()
    product = Product(uid_1c="u1", article="a", name="n", stock_on_hand=0)
    setting = SyncSetting(uid_1c="u1", account_id=account.id, enabled=True)
    db.add_all([product, setting])
    db.flush()

    for combo in COMBOS:
        (broadcast, enabled, dispatch, covered, tracked,
         stock, reserve, offset, override, threshold) = combo
        product.broadcast_enabled = broadcast
        product.stock_on_hand = stock
        product.reserve = reserve
        product.broadcast_offset = offset
        product.transmit_override = override
        product.recalc_account_ids = (
            (str(account.id) if covered else "") if tracked else None)
        setting.enabled = enabled
        setting.min_threshold = threshold
        account.dispatch_enabled = dispatch
        db.flush()
        db.expire_all()

        shown = explain(product, setting, account)
        sent = quantity_for_account(db, "u1", account.id, stock)
        assert shown.quantity == sent, (
            f"страница показывает {shown.quantity}, рассылка отправит {sent}; "
            f"условия: {combo}")

        # И вторая половина: «ноль от выключателя» рассылка обязана распознавать
        # ровно там, где его называет страница. Ошибись она в одну сторону —
        # отзыв не уйдёт и площадка продолжит продавать по нашему числу; в
        # другую — ноль уедет на карточку, которой мы ни разу не касались.
        switch_off = (not broadcast or not enabled or not dispatch
                      or (tracked and not covered))
        assert blocked_by_switch(db, "u1", account.id) == switch_off, (
            f"выключатель: рассылка говорит "
            f"{blocked_by_switch(db, 'u1', account.id)}, ожидалось {switch_off}; "
            f"условия: {combo}")
