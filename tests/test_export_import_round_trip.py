"""АУДИТ-ПРОБА: выгрузил и тут же залил обратно — не изменилось НИЧЕГО.

Это штатный круг оператора: «выгрузка → правка одной колонки → импорт». Если
файл, вернувшийся без единой правки, что-то меняет, значит любая правка тащит за
собой ещё и это — молча, сразу по всему файлу и без следа в сообщении. Два таких
дефекта проект уже ловил (колонка «Трансляция» гасила отложенную просьбу; ячейка
порога откатывала правку факта), поэтому проверяем не отдельные колонки, а
инвариант целиком — на строках в РАЗНЫХ состояниях.
"""
import io
from datetime import date, timedelta

import pytest
from openpyxl import load_workbook

from app.models import Product, PlatformAccount, SyncSetting, StockDateSnapshot
from app.timeutils import now_utc, today_local


FIELDS = ("stock_on_hand", "reserve", "broadcast_offset", "stock_discrepancy",
          "fact_at_date", "offset_base_date", "offset_base_stock",
          "broadcast_enabled", "broadcast_requested_at", "recalc_done_at",
          "recalc_account_ids", "transmit_override")


def _snapshot(db, uids):
    out = {}
    for uid in uids:
        p = db.query(Product).filter(Product.uid_1c == uid).first()
        out[uid] = {f: getattr(p, f) for f in FIELDS}
        out[uid]["settings"] = sorted(
            (s.account_id, s.enabled, s.min_threshold)
            for s in db.query(SyncSetting).filter(SyncSetting.uid_1c == uid).all())
    return out


@pytest.fixture()
def catalog(web_db):
    acc = PlatformAccount(platform="wb", name="ИП А", warehouse_id="1",
                          is_active=True, dispatch_enabled=True)
    web_db.add(acc)
    web_db.flush()
    day = today_local() - timedelta(days=30)
    from app.models import StockDateStatus
    web_db.add(StockDateSnapshot(snapshot_date=day, status=StockDateStatus.done,
                                 requested_by="t"))
    cases = [
        # (uid, расхождение, факт, дата, остаток на дату, трансляция, покрытие)
        ("u-ничего",      None, None, None, None, False, None),
        ("u-измерен",     11,   32,   day,  43,   True,  None),
        ("u-склад-сошёлся", 0,  43,   day,  43,   True,  None),
        ("u-минус",       -7,   50,   day,  43,   True,  None),
        ("u-без-факта",   None, None, day,  43,   False, None),
        ("u-ждёт-1с",     None, None, day,  None, False, None),
    ]
    for uid, gap, fact, d, base, on, cov in cases:
        p = Product(uid_1c=uid, article=uid, name=uid, stock_on_hand=100, reserve=2,
                    stock_discrepancy=gap, fact_at_date=fact,
                    offset_base_date=d, offset_base_stock=base,
                    broadcast_enabled=on,
                    broadcast_offset=(gap + 2) if gap is not None else None,
                    recalc_done_at=now_utc() if on else None,
                    recalc_account_ids=str(acc.id) if on else None)
        web_db.add(p)
        web_db.add(SyncSetting(uid_1c=uid, account_id=acc.id, enabled=on,
                               min_threshold=0))
    # Строка с НЕПОГАШЕННОЙ просьбой включить трансляцию — тот самый случай,
    # на котором круг уже ломался.
    p = Product(uid_1c="u-просит", article="x", name="x", stock_on_hand=100, reserve=0,
                offset_base_date=day, offset_base_stock=43,
                broadcast_enabled=False, broadcast_requested_at=now_utc())
    web_db.add(p)
    web_db.add(SyncSetting(uid_1c="u-просит", account_id=acc.id, enabled=True,
                           min_threshold=0))
    web_db.commit()
    return [c[0] for c in cases] + ["u-просит"]


def test_a_file_returned_unedited_changes_nothing(logged_in_client, web_db, catalog):
    before = _snapshot(web_db, catalog)

    dump = logged_in_client.get("/products/export")
    assert dump.status_code == 200

    web_db.expire_all()
    back = logged_in_client.post(
        "/products/import",
        files={"file": ("товары.xlsx", io.BytesIO(dump.content),
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet")},
        follow_redirects=False)
    assert back.status_code == 303

    web_db.expire_all()
    after = _snapshot(web_db, catalog)
    for uid in catalog:
        assert after[uid] == before[uid], (
            f"круговой импорт изменил {uid}: "
            + "; ".join(f"{k}: {before[uid][k]!r} -> {after[uid][k]!r}"
                        for k in before[uid] if before[uid][k] != after[uid][k]))
