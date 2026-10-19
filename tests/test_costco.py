from datetime import date, timedelta

from costco_cli import db, prices, savings

SAVINGS_HTML = """
<div>Pricing shown is warehouse pricing</div><div>Valid 10/19/26 - 11/15/26</div>
<div><span>Laundry</span><a>Kirkland Oxi Powder</a><span>Warehouse</span><span>&amp;</span><span>Online</span>
<p>Kirkland Oxi Powder</p><p>11 lbs</p><p>Item 1707952</p><p>Limit 10.</p>
<span>$</span><span>9</span><span>.</span><span>99</span><span>After $3 OFF</span></div>
<div><a>Head Gloves</a><span>Warehouse</span><span>Only</span>
<p>Head Gloves</p><p>Item 1819379, 1819383</p><span>$</span><span>12</span><span>.</span><span>99</span>
<span>After $4 OFF</span></div>
"""


def test_parse_coupon_book():
    c = savings.parse(SAVINGS_HTML)
    assert [x["item_number"] for x in c] == ["1707952", "1819379", "1819383"]
    oxi = c[0]
    assert oxi == {"item_number": "1707952", "valid_from": "2026-10-19", "valid_to": "2026-11-15",
                   "discount": 3.0, "price_after": 9.99, "name": "Kirkland Oxi Powder 11 lbs"}
    assert c[1]["name"] == "Head Gloves" and c[1]["discount"] == 4.0


def receipt(when, lines, barcode="R1"):
    return {"transactionBarcode": barcode, "transactionDateTime": when, "receiptType": "In-Warehouse",
            "documentType": "WarehouseReceiptDetail", "warehouseName": "SAMPLE", "warehouseNumber": 100,
            "transactionType": "Sales", "total": sum(l["amount"] for l in lines), "itemArray": lines}


def line(num, desc, units, amount):
    return {"itemNumber": num, "itemDescription01": desc, "unit": units, "amount": amount,
            "itemUnitPriceAmount": 0, "itemDepartmentNumber": 14}


def test_discount_lines_and_unit_price():
    con = db.connect(":memory:")
    db.save_warehouse_receipt(con, receipt("2026-10-10T10:00:00", [
        line("1707952", "KSOXIPOWDER", 3, 44.97),
        line("388420", "/ 1707952", -3, -7.50),
    ]))
    rows = con.execute("SELECT item_number, unit_price, discount_for FROM lines ORDER BY line_no").fetchall()
    assert tuple(rows[0]) == ("1707952", 14.99, None)
    assert rows[1]["discount_for"] == "1707952"


def test_drops_coupon_and_online():
    today = date(2026, 10, 20)
    con = db.connect(":memory:")
    recent = (today - timedelta(days=5)).isoformat() + "T12:00:00"
    old = (today - timedelta(days=45)).isoformat() + "T12:00:00"
    db.save_warehouse_receipt(con, receipt(recent, [line("1707952", "KSOXIPOWDER", 2, 29.98)], "NEW"))
    db.save_warehouse_receipt(con, receipt(old, [line("1707952", "KSOXIPOWDER", 1, 14.99)], "OLD"))
    db.save_online_order(con, {"orderNumber": "O1", "orderPlacedDate": recent, "orderTotal": 50, "status": "Delivered",
                               "orderShipTos": [{"orderLineItems": [
                                   {"itemNumber": "555", "itemDescription": "Vacuum", "quantity": 1, "price": 50,
                                    "merchandiseTotalAmount": 50, "isFeeItem": False}]}]})
    db.save_coupons(con, savings.parse(SAVINGS_HTML))
    db.record_online_price(con, "555", 42.0, today.isoformat())

    drops = {d.purchase_id: d for d in prices.find_drops(con, today)}
    assert set(drops) == {"NEW", "O1"}  # the 45-day-old receipt is outside the window
    assert drops["NEW"].per_unit == 5.0 and drops["NEW"].total == 10.0   # 14.99 -> 9.99, x2
    assert drops["O1"].total == 8.0


def test_no_drop_when_coupon_already_applied():
    today = date(2026, 10, 20)
    con = db.connect(":memory:")
    when = (today - timedelta(days=1)).isoformat() + "T12:00:00"
    db.save_warehouse_receipt(con, receipt(when, [
        line("1707952", "KSOXIPOWDER", 1, 12.99),
        line("388420", "/ 1707952", -1, -3.00),
    ]))
    db.save_coupons(con, savings.parse(SAVINGS_HTML))
    assert prices.find_drops(con, today) == []


def test_twilio_send(monkeypatch):
    from costco_cli import notify
    con = db.connect(":memory:")
    for k, v in (("twilio_sid", "AC123"), ("twilio_from", "+15550000001"), ("twilio_to", "+15550000002"),
                 ("notify_channel", "twilio")):
        db.set_meta(con, k, v)
    sent = {}

    class Resp:
        status_code = 201

    def fake_post(url, auth, data, timeout):
        sent.update(url=url, auth=auth, data=data)
        return Resp()

    monkeypatch.setattr(notify, "_token", lambda sid: "tok")
    monkeypatch.setattr(notify.requests, "post", fake_post)
    assert notify.send(con, "Costco price drop", "$5.00 back") == "twilio"
    assert sent["url"].endswith("/Accounts/AC123/Messages.json")
    assert sent["auth"] == ("AC123", "tok")
    assert sent["data"] == {"From": "+15550000001", "To": "+15550000002", "Body": "Costco price drop: $5.00 back"}
