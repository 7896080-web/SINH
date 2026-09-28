"""Аномалия, причина которой исчезла, не должна висеть в списке.

Аномалия — это запись о том, что заказ пришёл на товар, с которым мы работать
не могли: трансляция на этот кабинет не включена, или у товара нет баркода.
Пока причина держится, строка нужна. Но причину устраняют не только кнопкой
«Синхронизировать» на этой же странице: товар включают на «Товарах», массовой
правкой, импортом из Excel, баркод приезжает справочником из 1С. Во всех этих
случаях аномалия оставалась в списке навсегда — и список рос, хотя разбирать в
нём было нечего.

20.09 на бою их накопилось 93, почти все — «заказ на невключённый товар» по
каталогу, который ещё ведёт вторая система. Список, который не пустеет от
работы, перестают открывать: в нём не видно, сделано что-то или нет.

Гасим ровно то, чего больше нет:
  * «заказ на невключённый товар» — пара товар+кабинет включена;
  * «нет баркода при включённой галочке» — у товара появился баркод.

Остальное трогать нельзя: закрытая аномалия исчезает из работы, и закрыть её
по ошибке значит потерять заказ, который никто не разнёс.
"""

from sqlalchemy.orm import Session

from app.models import (AnomalyReason, AnomalyStatus, Barcode, SyncAnomaly,
                        SyncSetting)


def close_fixed_anomalies(db: Session) -> int:
    """Закрыть аномалии, причина которых устранена. Возвращает сколько закрыто.

    Коммит НЕ делает: вызывающий сам решает, когда фиксировать — страница
    аномалий делает это одной транзакцией со своим ответом.
    """
    rows = db.query(SyncAnomaly).filter(
        SyncAnomaly.status == AnomalyStatus.new,
        SyncAnomaly.is_test.is_(False),
    ).all()
    if not rows:
        return 0

    # Два запроса на всю пачку вместо запроса на строку: аномалий бывают сотни,
    # а страница открывается часто и обновляет себя сама.
    enabled = {
        (s.uid_1c, s.account_id)
        for s in db.query(SyncSetting).filter(SyncSetting.enabled.is_(True)).all()
    }
    with_barcode = {
        b.uid_1c for b in
        db.query(Barcode.uid_1c).filter(
            Barcode.uid_1c.in_({r.uid_1c for r in rows})).distinct().all()
    }

    closed = 0
    for row in rows:
        if row.reason == AnomalyReason.order_on_disabled:
            fixed = (row.uid_1c, row.account_id) in enabled
        elif row.reason == AnomalyReason.missing_barcode:
            fixed = row.uid_1c in with_barcode
        else:
            fixed = False
        if fixed:
            row.status = AnomalyStatus.resolved
            closed += 1
    return closed
