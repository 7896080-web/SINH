import io, os
ROOT = r"C:\sync_admin"
EDITS = []
EDITS.append((r"app\routers\testing.py",
'    request: Request, uid_1c: str = Form(...), account_id: int = Form(...), quantity: int = Form(1),\n    db: Session = Depends(get_db), user: User = Depends(get_current_user),\n):',
'    request: Request, uid_1c: str = Form(...), account_id: int = Form(...), quantity: int = Form(1),\n    order_date: str = Form(""),\n    db: Session = Depends(get_db), user: User = Depends(get_current_user),\n):'))
EDITS.append((r"app\routers\testing.py",
'    if quantity < 1:\n        quantity = 1\n\n    order = PlatformOrder(\n        order_id=_new_test_order_id(), barcode=barcode_row.barcode, quantity=quantity, raw_status="test",\n    )',
'    if quantity < 1:\n        quantity = 1\n\n    parsed_date = None\n    raw_date = (order_date or "").strip()\n    if raw_date:\n        try:\n            parsed_date = datetime.strptime(raw_date, "%Y-%m-%d").date()\n        except ValueError:\n            set_flash(request, "Дата должна быть ГГГГ-ММ-ДД.", "warn")\n            db.commit()\n            return RedirectResponse(f"/testing?uid_1c={uid_1c}&account_id={account_id}", status_code=303)\n\n    order = PlatformOrder(\n        order_id=_new_test_order_id(), barcode=barcode_row.barcode, quantity=quantity, raw_status="test",\n    )'))
EDITS.append((r"app\routers\testing.py",
'    result = process_new_order(db, order, account, warehouse_pending, is_test=True)',
'    result = process_new_order(db, order, account, warehouse_pending, is_test=True, order_date=parsed_date)'))
EDITS.append((r"app\templates\testing.html",
'                    {{ p.article }} — {{ p.name }} (остаток {{ p.stock_on_hand }})',
"                    {{ p.article }} — {{ p.name }}{% if p.size or p.color %} · {{ p.size or '' }}{% if p.color %}/{{ p.color }}{% endif %}{% endif %} (остаток {{ p.stock_on_hand }})"))
EDITS.append((r"app\templates\testing.html",
'            <input type="number" id="quantity" name="quantity" value="1" min="1" style="width:80px;">\n        </div>\n        <button type="submit" class="btn-primary" {% if not barcode %}disabled{% endif %}>Симулировать поступление заказа</button>',
'            <input type="number" id="quantity" name="quantity" value="1" min="1" style="width:80px;">\n        </div>\n        <div class="field">\n            <label for="order_date">Дата начала (задним числом)</label>\n            <input type="date" id="order_date" name="order_date">\n        </div>\n        <button type="submit" class="btn-primary" {% if not barcode %}disabled{% endif %}>Симулировать поступление заказа</button>'))

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
