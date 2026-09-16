import io, os
ROOT = r"C:\sync_admin"
EDITS = []
EDITS.append((r"app\models.py",
'    quantity = Column(Integer, nullable=True)\n    order_id = Column(String(128), nullable=False)\n    account_id = Column(Integer, ForeignKey("platform_accounts.id"), nullable=False)\n    status = Column(Enum(FtpTaskStatus), default=FtpTaskStatus.pending, nullable=False)',
'    quantity = Column(Integer, nullable=True)\n    order_id = Column(String(128), nullable=False)\n    account_id = Column(Integer, ForeignKey("platform_accounts.id"), nullable=False)\n    movement_date = Column(Date, nullable=True)\n    status = Column(Enum(FtpTaskStatus), default=FtpTaskStatus.pending, nullable=False)'))
EDITS.append((r"app\workers\order_poller.py",
'def process_new_order(db: Session, order: PlatformOrder, account: PlatformAccount, warehouse_pending: str,\n                       is_test: bool = False) -> dict:',
'def process_new_order(db: Session, order: PlatformOrder, account: PlatformAccount, warehouse_pending: str,\n                       is_test: bool = False, order_date=None) -> dict:'))
EDITS.append((r"app\workers\order_poller.py",
'            quantity=order.quantity, order_id=order.order_id, account_id=account.id,\n            is_test=is_test,\n        )',
'            quantity=order.quantity, order_id=order.order_id, account_id=account.id,\n            movement_date=order_date,\n            is_test=is_test,\n        )'))
EDITS.append((r"app\workers\ftp_channel.py",
'        platform_value = t.account.platform.value\n        if t.command == "CREATE_MOVEMENT":\n            lines.append("|".join([\n                "CREATE_MOVEMENT", t.barcode, t.warehouse_from or "ЦС Склад", t.warehouse_to or "",\n                str(t.quantity or 0), t.order_id, platform_value,\n            ]))',
'        platform_value = t.account.platform.value\n        mdate = t.movement_date.strftime("%Y%m%d") if t.movement_date else ""\n        if t.command == "CREATE_MOVEMENT":\n            lines.append("|".join([\n                "CREATE_MOVEMENT", t.barcode, t.warehouse_from or "ЦС Склад", t.warehouse_to or "",\n                str(t.quantity or 0), t.order_id, platform_value, mdate,\n            ]))'))
EDITS.append((r"app\workers\ftp_channel.py",
'                "CONFIRM_MOVEMENT", t.barcode, t.warehouse_from or "", t.warehouse_to or "",\n                str(t.quantity or 0), t.order_id, platform_value,\n            ]))',
'                "CONFIRM_MOVEMENT", t.barcode, t.warehouse_from or "", t.warehouse_to or "",\n                str(t.quantity or 0), t.order_id, platform_value, mdate,\n            ]))'))

def ap(p, o, n):
    t = io.open(p, encoding="utf-8").read()
    if n in t:
        return "skip"
    if o in t:
        t = t.replace(o, n, 1)
    else:
        o2, n2 = o.replace(chr(10), chr(13)+chr(10)), n.replace(chr(10), chr(13)+chr(10))
        if o2 in t:
            t = t.replace(o2, n2, 1)
        else:
            return "MISS"
    io.open(p, "w", encoding="utf-8", newline="").write(t)
    return "ok"
for r, o, n in EDITS:
    print(ap(os.path.join(ROOT, r), o, n), r)
print("DONE")
