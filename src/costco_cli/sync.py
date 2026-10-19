"""Pull new purchases, item names, online prices and the coupon book into SQLite."""
from collections import Counter
from datetime import date, timedelta

from . import api, auth, config, db, savings

FIRST_SYNC_DAYS = 730
WINDOW_DAYS = 180   # query receipts in chunks so long ranges stay small
OVERLAP_DAYS = 7    # re-pull the last week to catch late-posting receipts


def _windows(start, end):
    while start <= end:
        stop = min(start + timedelta(days=WINDOW_DAYS - 1), end)
        yield start, stop
        start = stop + timedelta(days=1)


def refresh_items(con, today, log):
    """Fill missing item names and record today's online price for recent purchases."""
    missing = [r[0] for r in con.execute(
        "SELECT DISTINCT l.item_number FROM lines l LEFT JOIN items i ON i.item_number = l.item_number"
        " WHERE l.discount_for IS NULL AND l.item_number != '' AND i.item_number IS NULL"
    )]
    since = (today - timedelta(days=config.PRICE_ADJUST_DAYS)).isoformat()
    recent = [r[0] for r in con.execute(
        "SELECT DISTINCT l.item_number FROM lines l JOIN purchases p ON p.id = l.purchase_id"
        " WHERE l.discount_for IS NULL AND l.item_number != '' AND p.purchased_at >= ?", (since,)
    )]
    wanted = list(dict.fromkeys(missing + recent))
    if not wanted:
        return 0
    found = api.products(wanted, config.ONLINE_WAREHOUSE)
    for num in wanted:
        info = found.get(num, {})
        db.upsert_item(con, num, info.get("name"), today.isoformat())
        if info.get("online_price"):
            db.record_online_price(con, num, info["online_price"], today.isoformat())
    log(f"items: {len(missing)} new names, {len(recent)} prices checked")
    return len(wanted)


def refresh_coupons(con, today, log, force=False):
    if not force and db.get_meta(con, "coupons_fetched") == today.isoformat():
        return 0
    coupons = savings.fetch()
    db.save_coupons(con, coupons)
    db.set_meta(con, "coupons_fetched", today.isoformat())
    log(f"coupon book: {len(coupons)} items")
    return len(coupons)


def run(con, days=None, log=print, session=None):
    today = date.today()
    last = db.get_meta(con, "last_sync")
    if days:
        start = today - timedelta(days=days)
    elif last:
        start = date.fromisoformat(last) - timedelta(days=OVERLAP_DAYS)
    else:
        start = today - timedelta(days=FIRST_SYNC_DAYS)

    session = session or auth.get_session(interactive=False)

    counts = Counter()
    for a, b in _windows(start, today):
        for r in api.warehouse_receipts(session, a, b):
            counts[db.save_warehouse_receipt(con, r)] += 1
    con.commit()

    known = {r["id"]: r["status"] for r in con.execute("SELECT id, status FROM purchases WHERE source = 'online'")}
    for a, b in _windows(start, today):
        for o in api.online_orders(session, a, b, config.ONLINE_WAREHOUSE):
            counts["online"] += 1
            if known.get(o["orderNumber"]) == o["status"] and o["status"] in ("Delivered", "Cancelled"):
                continue
            db.save_online_order(con, api.order_details(session, o["orderNumber"]))
    con.commit()

    log(f"purchases since {start}: {counts['warehouse']} warehouse, {counts['gas']} gas, {counts['online']} online")
    refresh_items(con, today, log)
    refresh_coupons(con, today, log)
    db.set_meta(con, "last_sync", today.isoformat())
    con.commit()
    return counts
