"""Сквозной прогон живого приложения: цепочка «заказ -> 1С -> сверка -> площадка».

Проверяются не отдельные функции, а причинно-следственные связи между блоками:
что реально уходит на площадку, что попадает в файл задания для 1С, как ответ 1С
закрывает задание, как сверка выравнивает остаток и не задваивает его.

Каждый шаг печатает ОК/ПРОВАЛ и фактические значения. Ничего не мокается кроме
HTTP-клиентов площадок (сети нет) и файлов обмена (локальный каталог).
"""
import os
import shutil
import sys
import tempfile
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# --- Своя база, и только своя. ДО первого импорта из `app`. ---------------
#
# Скрипт не мокает базу: он заводит боевые по типу объекты (`PlatformAccount`,
# `Product`, заказы через `process_new_order`, записи очереди) и зовёт настоящие
# функции. `DATABASE_URL` раньше не подменялся вовсе, а `app/__init__.py` вне
# pytest читает `.env` — на боевом сервере это живая `C:/sync_admin/sync_admin.db`.
#
# Дальше начиналось непоправимое: `build_task_batch` забирает ВСЕ задания в
# `pending` с `is_test=False`, до пятисот строк, помечает их `sent` и коммитит, а
# содержимое скрипт никуда не публикует — каталог обмена у него временный. Для
# `CANCEL_MOVEMENT` это без обратного хода: автоповтор их не берёт, второго
# задания `existing_cancel_task` не даст, обратного документа в 1С не будет
# никогда. То есть один запуск «аудита» на сервере тихо съедал бы настоящие
# задания 1С.
#
# Отсюда два предохранителя. Свой файл базы задаём принудительно; а если кто-то
# указал `DATABASE_URL` руками — требуем, чтобы он был явно тестовым, тем же
# правилом, что и `tests/conftest.py`. Отказ, а не молчаливая подмена: человек,
# задавший переменную, должен узнать, что его не послушались.
_own_url = "sqlite:///./e2e_audit.db"
_given = os.environ.get("DATABASE_URL")
if _given and _given != _own_url:
    _p = _given[len("sqlite:///"):].replace("\\", "/").lower() \
        if _given.startswith("sqlite:///") else ""
    if not (_p and (_p == ":memory:" or "test" in os.path.basename(_p))):
        sys.exit(
            "DATABASE_URL указывает на НЕ тестовую базу: " + _given + "\n"
            "Этот скрипт заводит боевые по типу объекты и ЗАБИРАЕТ настоящие "
            "задания 1С из очереди, никуда их не публикуя. Уберите переменную "
            "(в PowerShell: Remove-Item Env:DATABASE_URL) и запустите заново."
        )
else:
    os.environ["DATABASE_URL"] = _own_url

FAIL = []
STEP = [0]


def check(name, ok, detail=""):
    STEP[0] += 1
    mark = "ОК     " if ok else "ПРОВАЛ "
    print(f"{mark} {STEP[0]:>2}. {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAIL.append((STEP[0], name, detail))


from app.database import SessionLocal, Base, engine
from app.models import (Product, Barcode, PlatformAccount, Platform, SyncSetting,
                        ProcessedOrder, OrderProcessStatus, FtpTask, FtpTaskStatus,
                        DispatchQueueItem, DispatchStatus, SyncAnomaly, PlatformCatalogItem)
from app.workers.platform_clients.base import PlatformOrder
from app.workers.order_poller import poll_new_orders, poll_cancellations, process_new_order
from app.workers.dispatch import run_dispatch_cycle
from app.workers.ftp_channel import (LocalExchange, build_task_batch, apply_result_batch,
                                     fetch_stock_export_rows, detect_timed_out_tasks)
from app.workers.reconciliation import run_reconciliation, import_product_master
from app.transmit import explain, quantity_for_account

Base.metadata.create_all(bind=engine)


class FakeClient:
    """Площадка без сети. Запоминает ВСЁ, что ей реально отправили."""

    def __init__(self, new_orders=None, cancelled=None):
        self._new = new_orders or []
        self._cancelled = cancelled or []
        self.pushed = []          # [(warehouse_id, [(barcode, qty, external_id, article)])]

    def get_orders_awaiting_confirmation(self):
        return self._new

    def get_cancelled_orders(self, order_ids):
        return [o for o in self._cancelled if o.order_id in order_ids]

    def get_confirmed_orders(self, order_ids):
        return []

    def push_stock(self, warehouse_id, items):
        self.pushed.append((warehouse_id, [(i.barcode, i.quantity, i.external_id, i.article) for i in items]))
        return {"ok": [i.barcode for i in items], "errors": []}

    def get_catalog_items(self):
        return []


db = SessionLocal()
SYNC = tempfile.mkdtemp(prefix="sync_e2e_")
exchange = LocalExchange(f"{SYNC}/tasks", f"{SYNC}/results", f"{SYNC}/archive")
exchange._ensure_dirs()

print("=" * 78)
print("СКВОЗНОЙ ПРОГОН: заказ -> списание -> площадка -> 1С -> сверка -> отмена")
print("=" * 78)

# --------------------------------------------------------------- подготовка
wb = PlatformAccount(platform=Platform.wb, name="ИП Яворская", warehouse_id="wh-wb")
kit = PlatformAccount(platform=Platform.kit, name="КИТ", warehouse_id="wh-kit")
ozon = PlatformAccount(platform=Platform.ozon, name="ОЗОН", warehouse_id="wh-oz")
db.add_all([wb, kit, ozon])
db.commit()

UID = "sku-1"
db.add(Product(uid_1c=UID, article="27643", name="Свитшот", size="XL", color="LACIVERT/RED",
               stock_on_hand=43, broadcast_enabled=True))
db.add(Barcode(barcode="2000932153735", uid_1c=UID))
db.add(Barcode(barcode="2000932153742", uid_1c=UID))       # второй баркод того же SKU
db.add(SyncSetting(uid_1c=UID, account_id=wb.id, enabled=True))
db.add(SyncSetting(uid_1c=UID, account_id=kit.id, enabled=True))
db.add(SyncSetting(uid_1c=UID, account_id=ozon.id, enabled=False))   # кабинет не отмечен
db.add(PlatformCatalogItem(account_id=kit.id, barcode="2000932153735",
                           external_id="kit-variant-uuid", article="27643-XL", name="Свитшот"))
db.commit()
product = db.query(Product).filter(Product.uid_1c == UID).first()

# --------------------------------------------------------------- 1. порог трансляции
product.broadcast_offset = 43 - 32          # пересчёт: учёт 43, реально 32
db.commit()
r = explain(product, db.query(SyncSetting).filter(SyncSetting.account_id == wb.id).first(), wb)
check("порог трансляции считается от пересчёта", product.broadcast_offset == 11 and r.quantity == 32,
      f"порог={product.broadcast_offset}, уйдёт={r.quantity}")

# --------------------------------------------------------------- 2. живой заказ
order = PlatformOrder(order_id="WB-1001", barcode="2000932153735", quantity=3, raw_status="new")
client_wb = FakeClient(new_orders=[order])
stats = poll_new_orders(db, client_wb, wb, "Wildberries_Склад_FBO")
db.expire_all()
product = db.query(Product).filter(Product.uid_1c == UID).first()
check("заказ списал остаток ЦС", product.stock_on_hand == 40, f"43 - 3 = {product.stock_on_hand}")
check("заказ отмечен обработанным (идемпотентность)",
      db.query(ProcessedOrder).filter(ProcessedOrder.order_id == "WB-1001").count() == 1)

# повторный опрос того же заказа
stats2 = poll_new_orders(db, client_wb, wb, "Wildberries_Склад_FBO")
db.expire_all()
product = db.query(Product).filter(Product.uid_1c == UID).first()
check("повторный тот же заказ НЕ списывает второй раз",
      product.stock_on_hand == 40 and stats2["already_processed"] == 1,
      f"остаток={product.stock_on_hand}, already={stats2['already_processed']}")

# --------------------------------------------------------------- 3. рассылка на площадки
queued = db.query(DispatchQueueItem).filter(DispatchQueueItem.status == DispatchStatus.pending).all()
check("рассылка поставлена только на ДРУГИЕ отмеченные кабинеты (не на источник)",
      {q.account_id for q in queued} == {kit.id},
      f"кабинеты в очереди: {sorted(q.account_id for q in queued)} (источник {wb.id}, ozon {ozon.id} не отмечен)")

client_kit = FakeClient()
run_dispatch_cycle(db, {wb.id: client_wb, kit.id: client_kit, ozon.id: FakeClient()},
                   active_accounts=[wb, kit, ozon])
sent_kit = [it for _, items in client_kit.pushed for it in items]
check("на площадку ушло остаток минус порог, а не сырой остаток",
      len(sent_kit) == 1 and sent_kit[0][1] == 29, f"отправлено {sent_kit}")
check("для Kit взят variant_id из каталога кабинета, а не баркод",
      sent_kit and sent_kit[0][2] == "kit-variant-uuid", f"external_id={sent_kit[0][2] if sent_kit else '—'}")

# --------------------------------------------------------------- 4. файл задания для 1С
batch = build_task_batch(db, request_stock_export=True)
check("задание для 1С собрано", batch is not None)
fname, content = batch
lines = [l for l in content.splitlines() if l.strip()]
move = [l for l in lines if l.startswith("CREATE_MOVEMENT")]
check("в задании ровно одно перемещение по заказу", len(move) == 1, f"строк: {len(lines)}")
fields = move[0].split("|")
check("строка перемещения имеет 8 полей (8-е — дата документа)", len(fields) == 8, f"полей: {len(fields)}: {fields}")
check("склад-источник ЦС, склад-приёмник площадки, количество заказа",
      fields[2] == "ЦС Склад" and fields[3] == "Wildberries_Склад_FBO" and fields[4] == "3",
      f"{fields[2]} -> {fields[3]}, кол-во {fields[4]}")
check("запрос выгрузки остатков попал в то же задание", "EXPORT_STOCK_ON_HAND" in lines)

exchange.upload_task_file(fname, content)
check("файл задания опубликован атомарно (без .part)",
      os.path.exists(f"{SYNC}/tasks/{fname}") and not os.path.exists(f"{SYNC}/tasks/{fname}.part"))

# --------------------------------------------------------------- 5. ответ 1С с BOM
result = "﻿" + f"WB-1001|OK|ЦБ000000186"
open(f"{SYNC}/results/result_1.txt", "wb").write(result.encode("utf-8"))
stats_r = apply_result_batch(db, exchange.download_and_archive_result("result_1.txt"))
task = db.query(FtpTask).filter(FtpTask.order_id == "WB-1001").first()
check("ответ 1С с BOM закрывает задание из ПЕРВОЙ строки",
      stats_r["ok"] == 1 and task.status == FtpTaskStatus.done and task.result_detail == "ЦБ000000186",
      f"статус={task.status.value}, документ={task.result_detail}")

# --------------------------------------------------------------- 6. опоздавший ответ
late = FtpTask(command="CREATE_MOVEMENT", barcode="2000932153735", quantity=1, order_id="WB-LATE",
               account_id=wb.id, status=FtpTaskStatus.sent)
db.add(late)
db.commit()
late.sent_at = late.created_at - timedelta(hours=2)
db.commit()
detect_timed_out_tasks(db)
db.expire_all()
late = db.query(FtpTask).filter(FtpTask.order_id == "WB-LATE").first()
check("задание без ответа уходит в timeout", late.status == FtpTaskStatus.timeout)
apply_result_batch(db, "WB-LATE|OK|ЦБ000000999")
db.expire_all()
late = db.query(FtpTask).filter(FtpTask.order_id == "WB-LATE").first()
check("опоздавший ответ всё равно закрывает просроченное задание",
      late.status == FtpTaskStatus.done and late.result_detail == "ЦБ000000999",
      f"статус={late.status.value}")

# --------------------------------------------------------------- 7. выгрузка остатков и сверка
open(f"{SYNC}/results/stock_1.txt", "wb").write(
    ("﻿" + f"{UID}|27643|Свитшот|40|2000932153735,2000932153742|XL|LACIVERT/RED").encode("utf-8"))
rows = fetch_stock_export_rows(exchange)
check("выгрузка остатков разобрана с размером и цветом",
      len(rows) == 1 and rows[0].get("size") == "XL" and rows[0].get("color") == "LACIVERT/RED",
      f"{rows}")

stock_from_1c = {"2000932153735": 40, "2000932153742": 40}
stats_rec = run_reconciliation(db, stock_from_1c)
db.expire_all()
product = db.query(Product).filter(Product.uid_1c == UID).first()
check("сверка при совпадении не двигает остаток",
      product.stock_on_hand == 40 and stats_rec["normal"] == 1,
      f"остаток={product.stock_on_hand}, {stats_rec}")
check("порог трансляции сверка не трогает", product.broadcast_offset == 11)

# движение склада мимо площадок: в 1С списали 5
stats_rec2 = run_reconciliation(db, {"2000932153735": 35, "2000932153742": 35})
db.expire_all()
product = db.query(Product).filter(Product.uid_1c == UID).first()
check("стороннее движение склада подтянулось из 1С", product.stock_on_hand == 35)
r = explain(product, db.query(SyncSetting).filter(SyncSetting.account_id == kit.id).first(), kit)
check("после движения склада трансляция пересчиталась от порога, порог не сдвинулся",
      r.quantity == 24 and product.broadcast_offset == 11, f"уйдёт={r.quantity}, порог={product.broadcast_offset}")

# --------------------------------------------------------------- 8. in_flight: заказ в пути
before = db.query(Product).filter(Product.uid_1c == UID).first().stock_on_hand
o2 = PlatformOrder(order_id="WB-1002", barcode="2000932153735", quantity=2, raw_status="new")
poll_new_orders(db, FakeClient(new_orders=[o2]), wb, "Wildberries_Склад_FBO")
db.expire_all()
product = db.query(Product).filter(Product.uid_1c == UID).first()
check("новый заказ списал локально", product.stock_on_hand == before - 2, f"{before} -> {product.stock_on_hand}")
# 1С ещё НЕ провела это перемещение: отдаёт прежние 35
stats_rec3 = run_reconciliation(db, {"2000932153735": 35, "2000932153742": 35})
db.expire_all()
product = db.query(Product).filter(Product.uid_1c == UID).first()
check("сверка учла «в пути» и НЕ вернула списанные 2 обратно",
      product.stock_on_hand == 33 and stats_rec3["normal"] == 1,
      f"остаток={product.stock_on_hand} (ожидалось 33), {stats_rec3}")

# --------------------------------------------------------------- 9. отмена
cancel = PlatformOrder(order_id="WB-1002", barcode="2000932153735", quantity=2,
                       raw_status="cancel", is_cancellation=True)
poll_cancellations(db, FakeClient(cancelled=[cancel]), wb)
db.expire_all()
product = db.query(Product).filter(Product.uid_1c == UID).first()
check("отмена вернула ровно списанное", product.stock_on_hand == 35, f"остаток={product.stock_on_hand}")
rec = db.query(ProcessedOrder).filter(ProcessedOrder.order_id == "WB-1002").first()
check("отменённый заказ помечен cancelled", rec.status == OrderProcessStatus.cancelled)
poll_cancellations(db, FakeClient(cancelled=[cancel]), wb)
db.expire_all()
product = db.query(Product).filter(Product.uid_1c == UID).first()
check("повторная отмена того же заказа не возвращает второй раз", product.stock_on_hand == 35,
      f"остаток={product.stock_on_hand}")

# --------------------------------------------------------------- 10. изоляция is_test
before = db.query(Product).filter(Product.uid_1c == UID).first().stock_on_hand
test_order = PlatformOrder(order_id="TEST-xyz", barcode="2000932153735", quantity=4, raw_status="test")
process_new_order(db, test_order, wb, "Wildberries_Склад_FBO", is_test=True)
db.commit()
client_kit2 = FakeClient()
run_dispatch_cycle(db, {wb.id: FakeClient(), kit.id: client_kit2, ozon.id: FakeClient()},
                   active_accounts=[wb, kit, ozon])
check("тестовый заказ НЕ ушёл на площадку", client_kit2.pushed == [], f"отправлено: {client_kit2.pushed}")
batch2 = build_task_batch(db)
test_lines = [l for l in (batch2[1].splitlines() if batch2 else []) if "TEST-xyz" in l]
check("тестовый заказ НЕ попал в файл задания для 1С", test_lines == [], f"строк: {test_lines}")
check("при этом тестовые записи в БД созданы (для страницы тестирования)",
      db.query(FtpTask).filter(FtpTask.order_id == "TEST-xyz", FtpTask.is_test.is_(True)).count() == 1)

# --------------------------------------------------------------- 11. выключатели
product = db.query(Product).filter(Product.uid_1c == UID).first()
product.broadcast_enabled = False
db.commit()
check("выключенная трансляция SKU обнуляет отправку на всех кабинетах",
      quantity_for_account(db, UID, wb.id, product.stock_on_hand) == 0
      and quantity_for_account(db, UID, kit.id, product.stock_on_hand) == 0)
product.broadcast_enabled = True
db.commit()
kit.dispatch_enabled = False
db.commit()
client_kit3 = FakeClient()
db.add(DispatchQueueItem(uid_1c=UID, account_id=kit.id, quantity=product.stock_on_hand, reason="audit"))
db.commit()
run_dispatch_cycle(db, {kit.id: client_kit3}, active_accounts=[kit])
check("пауза кабинета полностью останавливает отправку на эту площадку",
      client_kit3.pushed == [], f"отправлено: {client_kit3.pushed}")
kit.dispatch_enabled = True
db.commit()

# --------------------------------------------------------------- 12. отрицательный остаток
product = db.query(Product).filter(Product.uid_1c == UID).first()
product.stock_on_hand = -4
product.broadcast_offset = None
db.commit()
check("отрицательный остаток из 1С не уходит на площадку как отрицательный",
      quantity_for_account(db, UID, wb.id, -4) == 0)

# --------------------------------------------------------------- 13. неизвестный баркод
stats_u = poll_new_orders(db, FakeClient(new_orders=[
    PlatformOrder(order_id="WB-UNK", barcode="9999999999999", quantity=1, raw_status="new")]),
    wb, "Wildberries_Склад_FBO")
check("заказ с неизвестным баркодом не ломает опрос и не списывает остаток",
      stats_u["unmatched"] == 1, f"{stats_u}")

print("=" * 78)
if FAIL:
    print(f"ПРОВАЛЕНО ШАГОВ: {len(FAIL)} из {STEP[0]}")
    for n, name, d in FAIL:
        print(f"  шаг {n}: {name} — {d}")
else:
    print(f"ВСЕ {STEP[0]} ШАГОВ ПРОЙДЕНЫ")
print("=" * 78)
db.close()
shutil.rmtree(SYNC, ignore_errors=True)
sys.exit(1 if FAIL else 0)
