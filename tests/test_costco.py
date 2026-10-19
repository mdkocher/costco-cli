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


# ---------- warehouse price drops found in your own receipts ----------

def wh_line(num, desc, units, amount, dept=14):
    l = line(num, desc, units, amount)
    l["itemDescription01"] = desc
    l["itemDepartmentNumber"] = dept
    return l


def wh_receipt(when, lines, barcode, warehouse=100):
    r = receipt(when, lines, barcode)
    r["warehouseNumber"] = warehouse
    return r


def test_drop_from_a_later_receipt_of_your_own():
    """The shelf price fell between two trips, which only your receipts record."""
    today = date(2026, 10, 20)
    con = db.connect(":memory:")
    bought = (today - timedelta(days=20)).isoformat() + "T12:00:00"
    cheaper = (today - timedelta(days=6)).isoformat() + "T12:00:00"
    db.save_warehouse_receipt(con, wh_receipt(bought, [wh_line("1707952", "KSOXI", 4, 59.96)], "FIRST"))
    db.save_warehouse_receipt(con, wh_receipt(cheaper, [wh_line("1707952", "KSOXI", 1, 11.99)], "LATER"))

    drops = prices.find_drops(con, today)
    assert len(drops) == 1
    d = drops[0]
    assert d.purchase_id == "FIRST" and d.from_history is True
    assert d.paid_unit == 14.99 and d.now_unit == 11.99
    assert d.per_unit == 3.0 and d.total == 12.0        # x4 units on the first receipt
    assert d.reason == "you paid $11.99 for it on " + (today - timedelta(days=6)).isoformat()
    assert d.alertable is True


def test_no_drop_when_the_later_receipt_is_dearer_or_same_day():
    today = date(2026, 10, 20)
    con = db.connect(":memory:")
    first = (today - timedelta(days=10)).isoformat() + "T09:00:00"
    db.save_warehouse_receipt(con, wh_receipt(first, [wh_line("1707952", "KSOXI", 1, 11.99)], "CHEAP"))
    # Same day, higher price: a second trip, not a price change.
    db.save_warehouse_receipt(con, wh_receipt(first, [wh_line("1707952", "KSOXI", 1, 14.99)], "SAMEDAY"))
    # Later and dearer: a price rise is not a claim.
    db.save_warehouse_receipt(con, wh_receipt((today - timedelta(days=2)).isoformat() + "T12:00:00",
                                              [wh_line("1707952", "KSOXI", 1, 16.99)], "RISE"))
    assert prices.find_drops(con, today) == []


def test_later_receipt_outside_the_window_is_not_a_claim():
    """A price that falls on day 31 is past the adjustment deadline."""
    today = date(2026, 10, 20)
    con = db.connect(":memory:")
    db.save_warehouse_receipt(con, wh_receipt((today - timedelta(days=29)).isoformat() + "T12:00:00",
                                              [wh_line("1707952", "KSOXI", 1, 14.99)], "OLD"))
    db.save_warehouse_receipt(con, wh_receipt(today.isoformat() + "T12:00:00",
                                              [wh_line("1707952", "KSOXI", 1, 11.99)], "NOW"))
    # 29 days + 30-day window still reaches today, so this one does count.
    assert [d.purchase_id for d in prices.find_drops(con, today)] == ["OLD"]
    # Push the purchase to exactly 31 days back and it drops out of the query entirely.
    con.execute("UPDATE purchases SET purchased_at = ? WHERE id = 'OLD'",
                ((today - timedelta(days=31)).isoformat() + "T12:00:00",))
    assert prices.find_drops(con, today) == []


def test_prices_are_not_compared_across_warehouses():
    """Costco prices regionally; a Bend claim cannot cite a Culver City receipt."""
    today = date(2026, 10, 20)
    con = db.connect(":memory:")
    db.save_warehouse_receipt(con, wh_receipt((today - timedelta(days=9)).isoformat() + "T12:00:00",
                                              [wh_line("1707952", "KSOXI", 1, 14.99)], "BEND", warehouse=1696))
    db.save_warehouse_receipt(con, wh_receipt((today - timedelta(days=3)).isoformat() + "T12:00:00",
                                              [wh_line("1707952", "KSOXI", 1, 11.99)], "CULVER", warehouse=479))
    assert prices.find_drops(con, today) == []


def test_seasonal_departments_are_recorded_but_not_alertable():
    today = date(2026, 10, 20)
    con = db.connect(":memory:")
    bought = (today - timedelta(days=12)).isoformat() + "T12:00:00"
    cheaper = (today - timedelta(days=4)).isoformat() + "T12:00:00"
    # 65 is fresh produce, 16 alcohol, 14 household.
    db.save_warehouse_receipt(con, wh_receipt(bought, [
        wh_line("251680", "RASPBERRIES", 1, 8.49, dept=65),
        wh_line("1169418", "KS CABERNET", 1, 16.99, dept=16),
        wh_line("1707952", "KSOXI", 1, 14.99, dept=14),
    ], "FIRST"))
    db.save_warehouse_receipt(con, wh_receipt(cheaper, [
        wh_line("251680", "RASPBERRIES", 1, 5.49, dept=65),
        wh_line("1169418", "KS CABERNET", 1, 15.99, dept=16),
        wh_line("1707952", "KSOXI", 1, 11.99, dept=14),
    ], "LATER"))

    drops = {d.item_number: d for d in prices.find_drops(con, today)}
    assert set(drops) == {"251680", "1169418", "1707952"}   # all three are found
    assert drops["251680"].alertable is False               # produce: listed, not texted
    assert drops["1169418"].alertable is False              # alcohol: listed, not texted
    assert drops["1707952"].alertable is True               # household: texted


def test_a_coupon_in_a_seasonal_department_still_alerts():
    """A coupon is a published markdown, so the department gate does not apply."""
    today = date(2026, 10, 20)
    con = db.connect(":memory:")
    db.save_warehouse_receipt(con, wh_receipt((today - timedelta(days=2)).isoformat() + "T12:00:00",
                                              [wh_line("1707952", "KSOXI", 1, 14.99, dept=65)], "R"))
    db.save_coupons(con, savings.parse(SAVINGS_HTML))
    drops = prices.find_drops(con, today)
    assert len(drops) == 1 and drops[0].from_history is False
    assert drops[0].alertable is True


def test_the_better_of_coupon_and_receipt_price_is_claimed():
    today = date(2026, 10, 20)
    con = db.connect(":memory:")
    db.save_warehouse_receipt(con, wh_receipt((today - timedelta(days=8)).isoformat() + "T12:00:00",
                                              [wh_line("1707952", "KSOXI", 2, 29.98)], "FIRST"))
    # A later receipt at $8.99 beats the coupon's $9.99 price.
    db.save_warehouse_receipt(con, wh_receipt((today - timedelta(days=1)).isoformat() + "T12:00:00",
                                              [wh_line("1707952", "KSOXI", 1, 8.99)], "LATER"))
    db.save_coupons(con, savings.parse(SAVINGS_HTML))
    drops = [d for d in prices.find_drops(con, today) if d.purchase_id == "FIRST"]
    assert len(drops) == 1
    assert drops[0].now_unit == 8.99 and drops[0].from_history is True
    assert drops[0].total == 12.0   # (14.99 - 8.99) x 2


def test_coupon_without_a_stated_price_uses_the_gross_unit_price():
    """Regression: subtracting a coupon from an already-discounted price
    reported the same instant saving a second time."""
    today = date(2026, 10, 20)
    con = db.connect(":memory:")
    db.save_warehouse_receipt(con, wh_receipt((today - timedelta(days=1)).isoformat() + "T12:00:00", [
        wh_line("1819379", "HEAD GLOVES", 1, 16.99),
        wh_line("388420", "/ 1819379", -1, -4.00),
    ], "R"))
    db.save_coupons(con, savings.parse(SAVINGS_HTML))   # Head Gloves: $4 off, price_after $12.99
    assert prices.find_drops(con, today) == []


def test_today_does_not_consult_later_receipts():
    """Regression: find_drops had no upper date bound, so a replayed date saw
    receipts from after it and reported drops that had not happened yet."""
    today = date(2026, 10, 20)
    con = db.connect(":memory:")
    db.save_warehouse_receipt(con, wh_receipt((today - timedelta(days=3)).isoformat() + "T12:00:00",
                                              [wh_line("1707952", "KSOXI", 1, 14.99)], "BOUGHT"))
    db.save_warehouse_receipt(con, wh_receipt((today + timedelta(days=4)).isoformat() + "T12:00:00",
                                              [wh_line("1707952", "KSOXI", 1, 11.99)], "FUTURE"))
    assert prices.find_drops(con, today) == []
    # The drop appears once that later receipt is in the past.
    assert [d.purchase_id for d in prices.find_drops(con, today + timedelta(days=5))] == ["BOUGHT"]
