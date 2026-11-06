import pathlib
import argparse
import re
from datetime import date, timedelta

import pytest

from costco_cli import db, lh, notify, prices, savings

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


# ---------- claims ----------

def test_record_claim_is_idempotent_per_purchase_line():
    """A redelivered task must not create a second row for the same item."""
    con = db.connect(":memory:")
    db.record_claim(con, "RCPT1", "1707952", 14.40, "claimed", decided_at="2026-11-05T09:00:00")
    db.record_claim(con, "RCPT1", "1707952", 14.40, "claimed", decided_at="2026-11-05T09:00:01")
    rows = db.claims(con)
    assert len(rows) == 1
    assert rows[0]["decided_at"] == "2026-11-05T09:00:01"      # last write wins


def test_record_claim_can_correct_an_outcome():
    con = db.connect(":memory:")
    db.record_claim(con, "RCPT1", "1707952", 14.40, "expired")
    db.record_claim(con, "RCPT1", "1707952", 14.40, "claimed")
    assert db.claim_outcomes(con) == {("RCPT1", "1707952"): "claimed"}


def test_record_claim_rejects_an_unknown_outcome():
    con = db.connect(":memory:")
    with pytest.raises(ValueError, match="outcome must be one of"):
        db.record_claim(con, "RCPT1", "1707952", 1.0, "mailed-it-in")


def test_two_lines_of_the_same_item_are_separate_claims():
    """The same item on two receipts is two adjustments, keyed independently."""
    con = db.connect(":memory:")
    db.record_claim(con, "RCPT1", "1707952", 3.00, "claimed")
    db.record_claim(con, "RCPT2", "1707952", 4.00, "expired")
    assert db.claim_outcomes(con) == {("RCPT1", "1707952"): "claimed",
                                      ("RCPT2", "1707952"): "expired"}


def test_claims_are_newest_first_and_limitable():
    con = db.connect(":memory:")
    for i, when in enumerate(["2026-11-01T09:00:00", "2026-11-03T09:00:00", "2026-11-02T09:00:00"]):
        db.record_claim(con, f"RCPT{i}", "1707952", 1.0, "claimed", decided_at=when)
    assert [r["decided_at"] for r in db.claims(con)] == [
        "2026-11-03T09:00:00", "2026-11-02T09:00:00", "2026-11-01T09:00:00"]
    assert len(db.claims(con, limit=2)) == 2


def test_claim_outcomes_is_empty_on_a_fresh_database():
    assert db.claim_outcomes(db.connect(":memory:")) == {}


def test_the_claims_table_is_added_to_an_existing_database(tmp_path):
    """connect() applies the schema every time, so old databases gain the table."""
    path = tmp_path / "old.db"
    con = db.connect(path)
    con.execute("DROP TABLE claims")
    con.commit()
    con.close()
    con = db.connect(path)                      # reconnecting re-applies the schema
    assert db.claim_outcomes(con) == {}
    db.record_claim(con, "RCPT1", "1707952", 2.0, "claimed")
    assert len(db.claims(con)) == 1


# ---------- the expired guard ----------

def test_expired_does_not_bury_a_claim_you_already_made():
    """Claim it in the shop from a machine with no server access, and days later
    the deadline fires. Your answer must win."""
    con = db.connect(":memory:")
    db.record_claim(con, "RCPT1", "1707952", 12.0, "claimed")
    assert db.record_claim(con, "RCPT1", "1707952", 12.0, "expired") == "claimed"
    assert db.claim_outcomes(con) == {("RCPT1", "1707952"): "claimed"}


def test_expired_does_not_bury_a_deliberate_skip():
    con = db.connect(":memory:")
    db.record_claim(con, "RCPT1", "1707952", 12.0, "skipped")
    assert db.record_claim(con, "RCPT1", "1707952", 12.0, "expired") == "skipped"


def test_claiming_late_still_overwrites_expired():
    """The reverse is legitimate: the window closed, then you got it anyway."""
    con = db.connect(":memory:")
    db.record_claim(con, "RCPT1", "1707952", 12.0, "expired")
    db.record_claim(con, "RCPT1", "1707952", 12.0, "claimed")
    assert db.claim_outcomes(con) == {("RCPT1", "1707952"): "claimed"}


def test_expired_is_recorded_when_nothing_was_decided():
    con = db.connect(":memory:")
    assert db.record_claim(con, "RCPT1", "1707952", 12.0, "expired") == "expired"


# ---------- reconciliation, without the SDK ----------

class _FakeDrop:
    def __init__(self, purchase_id, item_number):
        self.purchase_id = purchase_id
        self.item_number = item_number


class _FakeEnsureRun:
    """Stands in for lh.ensure_run, which would otherwise need a live server."""

    def __init__(self, fail=None):
        self.started = []
        self.fail = fail

    def __call__(self, drop):
        if self.fail:
            raise self.fail
        self.started.append((drop.purchase_id, drop.item_number))
        return f"claim-{drop.purchase_id}"


def test_reconciliation_starts_a_run_for_each_unsettled_drop(monkeypatch):
    from costco_cli import cli
    fake = _FakeEnsureRun()
    monkeypatch.setattr(cli.lh, "ensure_run", fake)
    con = db.connect(":memory:")
    drops = [_FakeDrop("R1", "1707952"), _FakeDrop("R2", "1819379")]
    assert cli.start_claim_runs(con, drops) == (2, None)
    assert fake.started == [("R1", "1707952"), ("R2", "1819379")]


def test_reconciliation_skips_drops_already_settled(monkeypatch):
    from costco_cli import cli
    fake = _FakeEnsureRun()
    monkeypatch.setattr(cli.lh, "ensure_run", fake)
    con = db.connect(":memory:")
    db.record_claim(con, "R1", "1707952", 12.0, "claimed")
    started, status = cli.start_claim_runs(con, [_FakeDrop("R1", "1707952"),
                                                 _FakeDrop("R2", "1819379")])
    assert (started, status) == (1, None)
    assert fake.started == [("R2", "1819379")]


def test_nothing_to_reconcile_is_reported_as_unchecked(monkeypatch):
    """With nothing waiting the server is never contacted, so the status must
    not claim it is running -- that would assert a health check that never
    happened, in the one email whose job is to say whether this works."""
    from costco_cli import cli
    monkeypatch.setattr(cli.lh, "ensure_run",
                        lambda d: pytest.fail("should not contact the server"))
    con = db.connect(":memory:")
    db.record_claim(con, "R1", "1707952", 12.0, "claimed")
    assert cli.start_claim_runs(con, [_FakeDrop("R1", "1707952")]) == (0, cli.UNCHECKED)


def test_a_down_server_is_reported_but_does_not_fail_the_sync(monkeypatch):
    """Claim tracking is required, so this is a fault worth mailing -- but the
    receipt sync already succeeded and must not be undone by it."""
    from costco_cli import cli
    monkeypatch.setattr(cli.lh, "ensure_run",
                        _FakeEnsureRun(fail=ConnectionError("connection refused")))
    started, status = cli.start_claim_runs(db.connect(":memory:"), [_FakeDrop("R1", "1707952")])
    assert started == 0
    assert status.startswith("DOWN - ")
    assert "ConnectionError" in status and "connection refused" in status


# ---------- summary content and cadence ----------

def _summary_con():
    con = db.connect(":memory:")
    today = date.today()
    bought = (today - timedelta(days=3)).isoformat() + "T12:00:00"
    db.save_warehouse_receipt(con, receipt(bought, [line("1707952", "KSOXIPOWDER", 4, 59.96)], "R1"))
    db.save_coupons(con, [{"item_number": "1707952", "valid_from": today.isoformat(),
                           "valid_to": (today + timedelta(days=10)).isoformat(),
                           "discount": 3.0, "price_after": 11.99, "name": "Kirkland Oxi"}])
    db.set_meta(con, "last_sync", today.isoformat())
    return con, today


def test_the_summary_shows_what_is_waiting_with_its_deadline():
    from costco_cli import cli
    con, today = _summary_con()
    text = cli.summary_text(con, "running", today=today, days=7)
    assert "Waiting   1 adjustment," in text
    assert "KSOXIPOWDER" in text
    assert "Settle an adjustment: costco claim" in text


def test_the_summary_reports_purchases_and_tracking():
    from costco_cli import cli
    con, today = _summary_con()
    text = cli.summary_text(con, "idle: server down", today=today, days=7)
    assert "1 receipt," in text and "1 warehouse" in text
    assert "Tracking  idle: server down" in text


def test_the_summary_surfaces_refunds_which_nothing_else_shows():
    from costco_cli import cli
    con, today = _summary_con()
    when = (today - timedelta(days=2)).isoformat() + "T12:00:00"
    r = receipt(when, [line("1949384", "CHEFMAN", -1, -49.99)], "REF1")
    r["transactionType"] = "Refund"
    db.save_warehouse_receipt(con, r)
    text = cli.summary_text(con, "running", today=today, days=7)
    assert "Refunds   1" in text
    assert "-$49.99" in text


def test_an_empty_week_still_reads_sensibly():
    from costco_cli import cli
    con = db.connect(":memory:")
    text = cli.summary_text(con, "running", today=date(2026, 11, 5), days=7)
    for expected in ("Bought    nothing", "Waiting   nothing to claim",
                     "Settled   nothing this period", "Refunds   none"):
        assert expected in text, expected


def test_the_interval_is_configurable():
    from costco_cli import cli
    con = db.connect(":memory:")
    assert cli.summary_interval(con) == cli.DEFAULT_SUMMARY_DAYS
    db.set_meta(con, "summary_days", "1")
    assert cli.summary_interval(con) == 1


def test_a_nonsense_interval_falls_back_to_the_default():
    from costco_cli import cli
    con = db.connect(":memory:")
    db.set_meta(con, "summary_days", "every tuesday")
    assert cli.summary_interval(con) == cli.DEFAULT_SUMMARY_DAYS


def test_summaries_can_be_turned_off(monkeypatch):
    from costco_cli import cli, notify
    con = db.connect(":memory:")
    db.set_meta(con, "summary_days", "0")
    db.set_meta(con, "last_summary", "2020-01-01")
    monkeypatch.setattr(notify, "send", lambda *a: pytest.fail("should not send"))
    assert cli.maybe_summary(con, "running") is False


def test_a_daily_interval_sends_the_next_day(monkeypatch):
    from costco_cli import cli, notify
    con, today = _summary_con()
    db.set_meta(con, "summary_days", "1")
    db.set_meta(con, "last_summary", today.isoformat())
    sent = {}
    monkeypatch.setattr(notify, "send", lambda c, t, m: sent.update(message=m))
    assert cli.maybe_summary(con, "running", today=today + timedelta(days=1)) is True
    assert "Costco summary, 1 days" in sent["message"]


def test_refunds_are_not_counted_as_purchases():
    """A refund receipt would otherwise inflate the receipt count and net its
    negative total into what you spent."""
    from costco_cli import cli
    con, today = _summary_con()
    r = receipt((today - timedelta(days=2)).isoformat() + "T12:00:00",
                [line("1949384", "CHEFMAN", -1, -49.99)], "REF1")
    r["transactionType"] = "Refund"
    db.save_warehouse_receipt(con, r)
    text = cli.summary_text(con, "running", today=today, days=7)
    assert "Bought    1 receipt, $59.96" in text     # the refund is excluded
    assert "Refunds   1" in text                      # and reported on its own


# ---------- the LittleHorse layer ----------

HOSTNAME_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


# ---------- run ids (decision 1A) ----------

def test_run_id_is_a_valid_hostname_label():
    """The server rejects anything else: "Optional argument 'id' must be a
    valid hostname". A colon separator fails outright."""
    assert HOSTNAME_LABEL.match(lh.run_id_for("R1", "1707952"))


def test_run_id_is_deterministic():
    """Idempotency rests on this: the same purchase line must always map to the
    same run, so starting it every morning is a no-op server-side."""
    assert lh.run_id_for("R1", "1707952") == lh.run_id_for("R1", "1707952")


def test_run_ids_differ_per_purchase_line():
    assert lh.run_id_for("R1", "1707952") != lh.run_id_for("R2", "1707952")
    assert lh.run_id_for("R1", "1707952") != lh.run_id_for("R1", "1819379")


@pytest.mark.parametrize("purchase_id", [
    "R1", "0070120281234", "ORDER/123", "a b c", "WC_99.8", "ÜMLAUT", "!!!", "",
])
def test_awkward_purchase_ids_still_make_a_valid_hostname(purchase_id):
    """Receipt barcodes and online order numbers are not our format to choose."""
    run_id = lh.run_id_for(purchase_id, "1707952")
    assert HOSTNAME_LABEL.match(run_id), run_id


def test_ids_that_slugify_identically_do_not_collide():
    """"ORDER/1" and "ORDER-1" slug the same way; the hash keeps them apart."""
    assert lh.run_id_for("ORDER/1", "1") != lh.run_id_for("ORDER-1", "1")


# ---------- outcome decision ----------

@pytest.mark.parametrize("window_closed,claimed,expected", [
    (True, False, "expired"),
    (True, True, "expired"),      # deadline wins: the task was never answered
    (False, True, "claimed"),
    (False, False, "skipped"),
])
def test_outcome_for(window_closed, claimed, expected):
    assert lh.outcome_for(window_closed, claimed) == expected


def test_every_outcome_is_one_the_schema_allows():
    for wc in (True, False):
        for c in (True, False):
            assert lh.outcome_for(wc, c) in db.CLAIM_OUTCOMES


# ---------- nudge timing ----------

def test_seconds_until_is_never_negative():
    """A drop discovered after its deadline must expire at once, not schedule a
    timer in the past."""
    from datetime import date, timedelta
    assert lh.seconds_until(date.today() - timedelta(days=5)) == 0


def test_seconds_until_grows_with_distance():
    from datetime import date, timedelta
    today = date.today()
    assert lh.seconds_until(today + timedelta(days=7)) > lh.seconds_until(today + timedelta(days=1))


# ---------- workflow variables ----------

class _VarDrop:
    purchase_id = "R1"
    item_number = "1707952"
    name = "Kirkland Oxi Powder"
    quantity = 4
    total = 12.0
    source = "warehouse"
    from datetime import date as _d, timedelta as _td
    deadline = _d.today() + _td(days=20)


def test_variables_for_covers_every_workflow_input():
    v = lh.variables_for(_VarDrop())
    assert set(v) == {"purchase_id", "item_number", "name", "dollars_back", "deadline",
                      "where", "first_nudge_seconds", "final_nudge_seconds",
                      "expires_in_seconds"}


def test_nudges_land_before_the_deadline_in_the_right_order():
    v = lh.variables_for(_VarDrop())
    assert v["first_nudge_seconds"] < v["final_nudge_seconds"] < v["expires_in_seconds"]


def test_where_depends_on_where_you_bought_it():
    warehouse = lh.variables_for(_VarDrop())
    online = _VarDrop()
    online.source = "online"
    assert warehouse["where"] == lh.WHERE_WAREHOUSE
    assert lh.variables_for(online)["where"] == lh.WHERE_ONLINE


def test_an_unnamed_item_falls_back_to_its_number():
    d = _VarDrop()
    d.name = None
    assert lh.variables_for(d)["name"] == "1707952"


# ---------- readiness gate ----------

class _FakeRpcError(Exception):
    def __init__(self, code):
        self._code = code

    def code(self):
        return self._code


class _FakeStub:
    """Fails with UNAVAILABLE a few times, as a rebalancing server does."""

    def __init__(self, unavailable_times=0, code=None):
        import grpc
        self.left = unavailable_times
        self.code = code or grpc.StatusCode.UNAVAILABLE
        self.calls = 0

    def SearchWfSpec(self, _request):
        self.calls += 1
        if self.left > 0:
            self.left -= 1
            raise _FakeRpcError(self.code)
        return object()


class _FakeConfig:
    def __init__(self, stub):
        self._stub = stub

    def stub(self):
        return self._stub


def _patch_rpc_error(monkeypatch):
    """wait_until_ready catches grpc.RpcError, so the fake must be one."""
    monkeypatch.setattr(lh.grpc, "RpcError", _FakeRpcError)


def test_wait_until_ready_returns_at_once_when_the_server_is_up(monkeypatch):
    _patch_rpc_error(monkeypatch)
    stub = _FakeStub(unavailable_times=0)
    assert lh.wait_until_ready(_FakeConfig(stub)) is True
    assert stub.calls == 1


def test_wait_until_ready_retries_while_kafka_streams_warms_up(monkeypatch):
    """The real symptom: UNAVAILABLE "Kafka Streams not ready yet" for ~30s
    after the container starts, while `lhctl version` already answers."""
    _patch_rpc_error(monkeypatch)
    stub = _FakeStub(unavailable_times=3)
    assert lh.wait_until_ready(_FakeConfig(stub), poll=0) is True
    assert stub.calls == 4


def test_wait_until_ready_gives_up_eventually(monkeypatch):
    _patch_rpc_error(monkeypatch)
    stub = _FakeStub(unavailable_times=10_000)
    with pytest.raises(_FakeRpcError):
        lh.wait_until_ready(_FakeConfig(stub), timeout=0, poll=0)


def test_wait_until_ready_does_not_mask_a_real_failure(monkeypatch):
    """A wrong address or a refused connection must surface, not spin."""
    import grpc
    _patch_rpc_error(monkeypatch)
    stub = _FakeStub(unavailable_times=1, code=grpc.StatusCode.PERMISSION_DENIED)
    with pytest.raises(_FakeRpcError):
        lh.wait_until_ready(_FakeConfig(stub), poll=0)


def test_the_delay_variables_have_defaults():
    """A run started by hand (the dashboard's Run button) passes no variables.
    An unset INT fails the User Task node with "Delay for User Task Action was
    not an INT, got a DEFINEDTYPE_NOT_SET" before the task is even created."""
    assert lh.DEFAULT_FIRST_NUDGE_SECONDS < lh.DEFAULT_FINAL_NUDGE_SECONDS
    assert lh.DEFAULT_FINAL_NUDGE_SECONDS < lh.DEFAULT_EXPIRES_IN_SECONDS


def test_the_defaults_match_the_real_adjustment_window():
    from costco_cli import config
    assert lh.DEFAULT_EXPIRES_IN_SECONDS == config.PRICE_ADJUST_DAYS * 24 * 60 * 60


# ---------- hand-started runs ----------

def test_a_run_with_no_purchase_line_records_nothing(tmp_path, monkeypatch):
    """The dashboard's Run button starts a run with every variable NULL. There
    is no purchase to record, and claims.purchase_id is NOT NULL, so writing
    anyway fails the task and leaves the run in ERROR."""
    monkeypatch.setenv("COSTCO_CLI_HOME", str(tmp_path))
    from costco_cli import config, db
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "costco.db")

    for missing in (None, ""):
        out = lh._write_claim(missing, "1707952", 12.0, "skipped")
        assert "not recorded" in out
        out = lh._write_claim("RCPT1", missing, 12.0, "skipped")
        assert "not recorded" in out

    con = db.connect()
    assert db.claim_outcomes(con) == {}


def test_a_real_purchase_line_is_still_recorded(tmp_path, monkeypatch):
    from costco_cli import config, db
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "costco.db")

    assert lh._write_claim("RCPT1", "1707952", 12.0, "claimed") == "claimed"
    con = db.connect()
    assert db.claim_outcomes(con) == {("RCPT1", "1707952"): "claimed"}


# ---------- refunds view ----------

def _refund_con():
    con = db.connect(":memory:")
    t = date.today()
    db.save_warehouse_receipt(con, receipt((t - timedelta(days=20)).isoformat() + "T12:00:00",
                                           [line("1707952", "KSOXIPOWDER", 4, 59.96)], "BUY"))
    return con, t


def test_a_full_price_refund_reads_as_a_return(capsys):
    """Both refunds in two years of real data are returns, so this is the case
    that must not be mislabelled."""
    from costco_cli import cli
    con, t = _refund_con()
    r = receipt((t - timedelta(days=5)).isoformat() + "T12:00:00",
                [line("1707952", "KSOXIPOWDER", -1, -14.99)], "REF")
    r["transactionType"] = "Refund"
    db.save_warehouse_receipt(con, r)
    cli.cmd_refunds(argparse.Namespace(days=90), con)
    assert "return" in capsys.readouterr().out


def test_a_partial_refund_reads_as_an_adjustment(capsys):
    """The whole point: a refund of the difference is a price adjustment, and
    is what the auto-detection would eventually key on."""
    from costco_cli import cli
    con, t = _refund_con()
    r = receipt((t - timedelta(days=5)).isoformat() + "T12:00:00",
                [line("1707952", "KSOXIPOWDER", -1, -3.00)], "REF")
    r["transactionType"] = "Refund"
    db.save_warehouse_receipt(con, r)
    cli.cmd_refunds(argparse.Namespace(days=90), con)
    # Check the table row, not the whole output: the footer explains both words.
    row = [ln for ln in capsys.readouterr().out.splitlines() if "KSOXIPOWDER" in ln][0]
    assert row.rstrip().endswith("adjustment")


def test_a_refund_for_something_never_bought_is_not_guessed(capsys):
    from costco_cli import cli
    con, t = _refund_con()
    r = receipt((t - timedelta(days=5)).isoformat() + "T12:00:00",
                [line("999999", "MYSTERY", -1, -9.99)], "REF")
    r["transactionType"] = "Refund"
    db.save_warehouse_receipt(con, r)
    cli.cmd_refunds(argparse.Namespace(days=90), con)
    assert "unknown" in capsys.readouterr().out


def test_no_refunds_says_so(capsys):
    from costco_cli import cli
    con, _ = _refund_con()
    cli.cmd_refunds(argparse.Namespace(days=90), con)
    assert "No refunds in the last 90 days" in capsys.readouterr().out


# ---------- status view ----------

def _status_con():
    con = db.connect(":memory:")
    t = date.today()
    db.save_warehouse_receipt(con, receipt((t - timedelta(days=4)).isoformat() + "T12:00:00",
                                           [line("1707952", "KSOXIPOWDER", 4, 59.96)], "R1"))
    db.save_coupons(con, [{"item_number": "1707952", "valid_from": t.isoformat(),
                           "valid_to": (t + timedelta(days=9)).isoformat(),
                           "discount": 3.0, "price_after": 11.99, "name": "Kirkland Oxi"}])
    db.set_meta(con, "last_sync", t.isoformat())
    return con


def _quiet_checks(monkeypatch, reachable=True, worker=True, installed=True):
    """Keep the status view off the network and off the process table."""
    from costco_cli import cli
    monkeypatch.setattr(cli, "server_reachable", lambda timeout=2.0: (reachable, "ConnectionError"))
    monkeypatch.setattr(cli.ops, "worker_status", lambda: (installed, worker, worker))
    return cli


def test_status_leads_with_the_money(monkeypatch, capsys):
    cli = _quiet_checks(monkeypatch)
    cli.cmd_status(argparse.Namespace(), _status_con())
    out = capsys.readouterr().out
    assert out.startswith("Money waiting")
    assert "$12.00 across 1 adjustment" in out
    assert "Settle one: costco claim" in out


def test_status_says_plainly_when_nothing_is_waiting(monkeypatch, capsys):
    cli = _quiet_checks(monkeypatch)
    cli.cmd_status(argparse.Namespace(), db.connect(":memory:"))
    assert "nothing to claim" in capsys.readouterr().out


def test_status_surfaces_the_suppressed_count(monkeypatch, capsys):
    """26A: the dedup blob is fine, it was only invisible."""
    import json as _json
    cli = _quiet_checks(monkeypatch)
    con = _status_con()
    db.set_meta(con, "notified_drops", _json.dumps(["R1:1707952:11.99", "R2:1819379:9.99"]))
    cli.cmd_status(argparse.Namespace(), con)
    assert "2 drops suppressed" in capsys.readouterr().out


def test_status_warns_when_tracking_is_down(monkeypatch, capsys):
    cli = _quiet_checks(monkeypatch, reachable=False, worker=False)
    cli.cmd_status(argparse.Namespace(), _status_con())
    out = capsys.readouterr().out
    assert "server         DOWN" in out
    assert "worker         NOT running" in out
    assert "Reminders will not fire until both are up." in out


def test_status_does_not_warn_when_both_are_up(monkeypatch, capsys):
    cli = _quiet_checks(monkeypatch)
    cli.cmd_status(argparse.Namespace(), _status_con())
    assert "Reminders will not fire" not in capsys.readouterr().out


def test_plurals_read_correctly():
    from costco_cli import cli
    assert cli.plural(1, "purchase") == "1 purchase"
    assert cli.plural(2, "purchase") == "2 purchases"
    assert cli.plural(0, "drop") == "0 drops"


def test_status_points_at_install_when_the_worker_agent_is_missing(monkeypatch, capsys):
    cli = _quiet_checks(monkeypatch, worker=False, installed=False)
    cli.cmd_status(argparse.Namespace(), _status_con())
    assert "costco worker install" in capsys.readouterr().out


# ---------- the installed copy vs the source ----------

def test_a_changed_source_reads_as_stale(monkeypatch, tmp_path):
    """`git push` does not change what runs at 07:00; this is what notices."""
    from costco_cli import ops
    installed = tmp_path / "installed" / "costco_cli"
    source = tmp_path / "repo" / "src" / "costco_cli"
    for d in (installed, source):
        d.mkdir(parents=True)
        (d / "cli.py").write_text("print('one')\n")
    monkeypatch.setattr(ops, "_pipx_source", lambda: tmp_path / "repo")
    monkeypatch.setattr(ops, "costco_cli", None, raising=False)

    import costco_cli
    monkeypatch.setattr(costco_cli, "__file__", str(installed / "__init__.py"))
    assert ops.staleness()[0] is False

    (source / "cli.py").write_text("print('two')\n")
    assert ops.staleness()[0] is True


def test_an_editable_install_is_never_stale(monkeypatch, tmp_path):
    """One copy, so there is nothing to drift."""
    from costco_cli import ops
    pkg = tmp_path / "repo" / "src" / "costco_cli"
    pkg.mkdir(parents=True)
    (pkg / "cli.py").write_text("x = 1\n")
    monkeypatch.setattr(ops, "_pipx_source", lambda: tmp_path / "repo")
    import costco_cli
    monkeypatch.setattr(costco_cli, "__file__", str(pkg / "__init__.py"))
    assert ops.staleness() is None


def test_version_line_says_how_to_fix_a_stale_copy(monkeypatch):
    from costco_cli import ops
    monkeypatch.setattr(ops, "staleness", lambda: (True, pathlib.Path("/repo"), "aaa", "bbb"))
    line = ops.version_line("0.1.0")
    assert "out of date" in line and "pipx install --force /repo" in line


def test_version_line_is_quiet_when_current(monkeypatch):
    from costco_cli import ops
    monkeypatch.setattr(ops, "staleness", lambda: (False, pathlib.Path("/repo"), "aaa", "aaa"))
    line = ops.version_line("0.1.0")
    assert "out of date" not in line and "build aaa" in line


def test_the_summary_accounts_for_drops_it_never_alerted_on():
    """24A: seasonal drops are deliberately never emailed, so without this line
    that money is invisible to anyone who only reads mail."""
    from costco_cli import cli

    class _Quiet:
        purchase_id, item_number, name = "R1", "16001", "Pinot Noir"
        total, alertable = 8.0, False
        deadline = date.today() + timedelta(days=12)

    class _Loud(_Quiet):
        purchase_id, item_number, name = "R2", "1707952", "Oxi Powder"
        total, alertable = 12.0, True

    con = db.connect(":memory:")
    cli_open = cli.open_claims
    try:
        cli.open_claims = lambda c: [_Loud(), _Quiet()]
        text = cli.summary_text(con, "running", today=date.today(), days=7)
    finally:
        cli.open_claims = cli_open
    assert "Waiting   2 adjustments, $20.00" in text
    assert "1 of these never alerted ($8.00, seasonal department)" in text


def test_the_summary_is_silent_when_everything_was_alerted():
    from costco_cli import cli

    class _Loud:
        purchase_id, item_number, name = "R2", "1707952", "Oxi Powder"
        total, alertable = 12.0, True
        deadline = date.today() + timedelta(days=12)

    con = db.connect(":memory:")
    cli_open = cli.open_claims
    try:
        cli.open_claims = lambda c: [_Loud()]
        text = cli.summary_text(con, "running", today=date.today(), days=7)
    finally:
        cli.open_claims = cli_open
    assert "never alerted" not in text
