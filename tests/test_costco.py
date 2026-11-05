from datetime import date, timedelta

import pytest

from costco_cli import db, notify, prices, savings

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


class _FakeSMTP:
    """Stands in for smtplib.SMTP_SSL, recording what a send would have done."""

    log = {}

    def __init__(self, host, port, timeout=None):
        _FakeSMTP.log.update(host=host, port=port, timeout=timeout)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        _FakeSMTP.log.update(user=user, password=password)

    def send_message(self, msg):
        _FakeSMTP.log.update(msg=msg)


def _email_con(**overrides):
    con = db.connect(":memory:")
    settings = {"email_from": "me@example.com", "email_to": "you@example.com", "notify_channel": "email"}
    settings.update(overrides)
    for k, v in settings.items():
        db.set_meta(con, k, v)
    return con


def test_macos_is_the_default_channel():
    assert notify.channel(db.connect(":memory:")) == "macos"


def test_email_send(monkeypatch):
    con = _email_con()
    _FakeSMTP.log.clear()
    monkeypatch.setattr(notify, "_password", lambda address: "apppw")
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", _FakeSMTP)

    assert notify.send(con, "Costco price drop", "$5.00 back") == "email"
    log = _FakeSMTP.log
    assert (log["host"], log["port"]) == (notify.DEFAULT_SMTP_HOST, notify.DEFAULT_SMTP_PORT)
    assert (log["user"], log["password"]) == ("me@example.com", "apppw")
    assert log["msg"]["Subject"] == "Costco price drop"
    assert log["msg"]["From"] == "me@example.com"
    assert log["msg"]["To"] == "you@example.com"
    assert log["msg"].get_content().strip() == "$5.00 back"


def test_email_honours_a_custom_host_and_port(monkeypatch):
    con = _email_con(smtp_host="mail.fastmail.com", smtp_port="587")
    _FakeSMTP.log.clear()
    monkeypatch.setattr(notify, "_password", lambda address: "apppw")
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", _FakeSMTP)

    notify.send(con, "t", "m")
    assert (_FakeSMTP.log["host"], _FakeSMTP.log["port"]) == ("mail.fastmail.com", 587)


def test_a_failed_email_still_shows_a_macos_notification(monkeypatch):
    """An alert is never silently lost: an SMTP failure falls back to the desktop."""
    con = _email_con()
    shown = {}
    monkeypatch.setattr(notify, "_macos", lambda title, message: shown.update(title=title, message=message))
    monkeypatch.setattr(notify, "_password", lambda address: "apppw")

    def refuse(*args, **kwargs):
        raise notify.smtplib.SMTPException("server said no")

    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", refuse)
    with pytest.raises(notify.NotifyError):
        notify.send(con, "Costco price drop", "$5.00 back")
    assert "email failed" in shown["message"]
    assert "$5.00 back" in shown["message"]


def test_email_not_configured_is_an_error(monkeypatch):
    con = db.connect(":memory:")
    db.set_meta(con, "notify_channel", "email")
    monkeypatch.setattr(notify, "_macos", lambda title, message: None)
    with pytest.raises(notify.NotifyError, match="not set up"):
        notify.send(con, "t", "m")


def test_a_non_numeric_smtp_port_is_an_error():
    con = _email_con(smtp_port="not-a-port")
    with pytest.raises(notify.NotifyError, match="not a number"):
        notify.email_settings(con)


def test_save_password_rejects_a_bad_address():
    with pytest.raises(notify.NotifyError, match="email address"):
        notify.save_password("not an address", "abc123")


def test_save_password_rejects_a_non_alphanumeric_password():
    with pytest.raises(notify.NotifyError, match="letters and digits"):
        notify.save_password("me@example.com", "has;semicolon")


def test_migrating_away_from_sms_clears_the_twilio_settings(monkeypatch):
    """Leftover SMS config is cleared once, and the Keychain item goes with it."""
    con = db.connect(":memory:")
    for k, v in (("twilio_sid", "AC123"), ("twilio_from", "+15550000001"),
                 ("twilio_to", "+15550000002"), ("notify_channel", "twilio")):
        db.set_meta(con, k, v)
    commands = []

    class Done:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(notify.subprocess, "run", lambda cmd, **kw: commands.append(cmd) or Done())

    assert notify.migrate_legacy_channel(con) is True
    assert notify.channel(con) == "macos"
    assert all(db.get_meta(con, k) is None for k in notify.LEGACY_KEYS)
    assert any(notify.LEGACY_KEYCHAIN_SERVICE in cmd and "AC123" in cmd for cmd in commands)
    # Idempotent: nothing left to clean up on a second run.
    assert notify.migrate_legacy_channel(con) is False


def test_migration_is_a_no_op_on_a_fresh_database(monkeypatch):
    monkeypatch.setattr(notify.subprocess, "run", lambda *a, **k: pytest.fail("should not touch the Keychain"))
    assert notify.migrate_legacy_channel(db.connect(":memory:")) is False
