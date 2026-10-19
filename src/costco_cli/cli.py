"""costco: track your Costco purchases and catch price adjustments."""
import argparse
import getpass
import json
import sys
from datetime import date, datetime, timedelta

from . import __version__, auth, config, db, notify, prices, schedule, sync
from .api import ApiError


# ---------- output helpers ----------

def money(x):
    if x is None:
        return ""
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def qty(x):
    if x is None:
        return ""
    return f"{x:g}"


def table(rows, headers, right=()):
    rows = [["" if c is None else str(c) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    def fmt(cells):
        return "  ".join(c.rjust(w) if i in right else c.ljust(w) for i, (c, w) in enumerate(zip(cells, widths))).rstrip()
    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(fmt(r))


def day(ts):
    return ts[:10]


def since_arg(args):
    if getattr(args, "since", None):
        return args.since
    if getattr(args, "year", None):
        return f"{args.year}-01-01"
    return (date.today() - timedelta(days=args.days)).isoformat()


def until_arg(args):
    if getattr(args, "year", None):
        return f"{args.year + 1}-01-01"
    return "9999"


# ---------- commands ----------

def cmd_login(args, con):
    print("A Chrome window will open. Sign in to Costco there; it closes on its own when done.")
    session = auth.get_session(interactive=True)
    print("Signed in. Saved session lives in", config.PROFILE_DIR)
    if args.sync:
        sync.run(con, session=session)


def cmd_sync(args, con):
    log = (lambda *_: None) if args.quiet else print
    sync.run(con, days=args.days, log=log)


def cmd_receipts(args, con):
    if args.id:
        p = con.execute("SELECT * FROM purchases WHERE id = ? OR id LIKE ?", (args.id, f"%{args.id}")).fetchone()
        if not p:
            sys.exit(f"No receipt or order matching {args.id}")
        print(f"{p['source'].title()}  {p['purchased_at'].replace('T', ' ')[:16]}  {p['location']}  #{p['id']}")
        if p["status"]:
            print("Status:", p["status"])
        rows = con.execute(
            "SELECT l.*, COALESCE(i.name, l.description) AS name FROM lines l"
            " LEFT JOIN items i ON i.item_number = l.item_number AND l.discount_for IS NULL"
            " WHERE purchase_id = ? ORDER BY line_no", (p["id"],))
        table([[r["item_number"] if not r["discount_for"] else "",
                (r["name"] or "")[:60] if not r["discount_for"] else f"  instant savings ({r['discount_for']})",
                qty(r["quantity"]), money(r["unit_price"]) if not r["discount_for"] else "", money(r["amount"])]
               for r in rows], ["Item", "Description", "Qty", "Each", "Amount"], right={2, 3, 4})
        print()
        for label, key in (("Subtotal", "subtotal"), ("Savings", "savings"), ("Tax", "tax"), ("Total", "total")):
            if p[key]:
                print(f"{label:>10}: {money(p[key])}")
        return

    where, params = ["purchased_at >= ?", "purchased_at < ?"], [since_arg(args), until_arg(args)]
    if args.type:
        where.append("source = ?")
        params.append(args.type)
    rows = con.execute(
        f"SELECT p.*, (SELECT COUNT(*) FROM lines l WHERE l.purchase_id = p.id AND l.discount_for IS NULL) AS n"
        f" FROM purchases p WHERE {' AND '.join(where)} ORDER BY purchased_at DESC", params).fetchall()
    table([[day(r["purchased_at"]), r["source"], r["location"], r["n"], money(r["total"]), r["id"]] for r in rows],
          ["Date", "Type", "Where", "Items", "Total", "ID"], right={3, 4})
    print(f"\n{len(rows)} purchases, {money(sum(r['total'] or 0 for r in rows))}")


def cmd_item(args, con):
    q = args.query
    nums = [r[0] for r in con.execute(
        "SELECT item_number FROM items WHERE item_number = ? OR name LIKE ?"
        " UNION SELECT item_number FROM lines WHERE discount_for IS NULL AND (item_number = ? OR description LIKE ?)",
        (q, f"%{q}%", q, f"%{q}%"))]
    if not nums:
        sys.exit(f"Nothing matching '{q}' in your purchases. Try a shorter word or the item number.")
    marks = ",".join("?" * len(nums))
    rows = con.execute(f"""
        SELECT p.purchased_at, p.source, p.location, p.id, l.item_number, l.quantity, l.unit_price, l.amount,
               COALESCE(i.name, l.description) AS name,
               (SELECT -SUM(d.amount) FROM lines d WHERE d.purchase_id = p.id AND d.discount_for = l.item_number) AS saved
        FROM lines l JOIN purchases p ON p.id = l.purchase_id LEFT JOIN items i ON i.item_number = l.item_number
        WHERE l.discount_for IS NULL AND l.item_number IN ({marks})
        ORDER BY l.item_number, p.purchased_at DESC""", nums).fetchall()
    table([[day(r["purchased_at"]), r["item_number"], (r["name"] or "")[:48], r["location"], qty(r["quantity"]),
            money(r["unit_price"]), money(-r["saved"]) if r["saved"] else "", money(r["amount"] - (r["saved"] or 0))]
           for r in rows], ["Date", "Item", "Name", "Where", "Qty", "Each", "Saved", "Paid"], right={4, 5, 6, 7})
    t = date.today().isoformat()
    for n in nums:
        c = con.execute("SELECT * FROM coupons WHERE item_number = ? AND valid_from <= ? AND valid_to >= ?",
                        (n, t, t)).fetchone()
        o = con.execute("SELECT * FROM online_prices WHERE item_number = ? ORDER BY observed_on DESC LIMIT 1",
                        (n,)).fetchone()
        notes = []
        if c:
            notes.append(f"coupon ${c['discount']:g} off until {c['valid_to']}"
                         + (f" ({money(c['price_after'])})" if c["price_after"] else ""))
        if o:
            notes.append(f"costco.com {money(o['price'])} on {o['observed_on']}")
        if notes:
            print(f"{n}: " + "; ".join(notes))
    total = sum(r["amount"] - (r["saved"] or 0) for r in rows)
    print(f"\n{len(rows)} purchases, {money(total)} total")


SPEND_GROUPS = {
    "month": "substr(purchased_at, 1, 7)",
    "type": "source",
    "where": "location",
    "year": "substr(purchased_at, 1, 4)",
}


def cmd_spend(args, con):
    since, until = since_arg(args), until_arg(args)
    if args.by == "dept":
        rows = con.execute("""
            SELECT l.department AS k, COUNT(*) AS n, SUM(l.amount) AS total FROM lines l JOIN purchases p ON p.id = l.purchase_id
            WHERE p.source = 'warehouse' AND p.purchased_at >= ? AND p.purchased_at < ?
            GROUP BY k ORDER BY total DESC""", (since, until)).fetchall()
        table([[r["k"], r["n"], money(r["total"])] for r in rows], ["Dept", "Lines", "Spent"], right={1, 2})
        print("\nWarehouse lines only, before tax. Department numbers are Costco's own codes.")
        return
    key = SPEND_GROUPS[args.by]
    rows = con.execute(f"""
        SELECT {key} AS k, COUNT(*) AS n, SUM(total) AS total, SUM(COALESCE(savings, 0)) AS saved
        FROM purchases WHERE purchased_at >= ? AND purchased_at < ?
        GROUP BY k ORDER BY {'k' if args.by in ('month', 'year') else 'total DESC'}""", (since, until)).fetchall()
    table([[r["k"], r["n"], money(r["saved"]) if r["saved"] else "", money(r["total"])] for r in rows],
          [args.by.title(), "Trips", "Saved", "Spent"], right={1, 2, 3})
    print(f"\n{money(sum(r['total'] or 0 for r in rows))} since {since}")


def cmd_returns(args, con):
    since = (date.today() - timedelta(days=args.days)).isoformat()
    rows = con.execute("""
        SELECT p.purchased_at, p.source, p.id, l.item_number, l.quantity, l.amount, COALESCE(i.name, l.description) AS name
        FROM lines l JOIN purchases p ON p.id = l.purchase_id LEFT JOIN items i ON i.item_number = l.item_number
        WHERE p.source IN ('warehouse', 'online') AND l.discount_for IS NULL AND l.quantity > 0 AND p.purchased_at >= ?
        ORDER BY p.purchased_at""", (since,)).fetchall()
    today = date.today()
    out = []
    for r in rows:
        until = datetime.fromisoformat(r["purchased_at"]).date() + timedelta(days=args.days)
        out.append([until.isoformat(), (until - today).days, day(r["purchased_at"]), r["source"], r["item_number"],
                    (r["name"] or "")[:48], money(r["amount"])])
    table(out, ["Until", "Days", "Bought", "Type", "Item", "Name", "Paid"], right={1, 6})
    print(f"\n{len(out)} items inside a {args.days}-day window. Price adjustments: 30 days. "
          "Electronics returns: 90 days (use --days 90).")


def cmd_drops(args, con):
    if args.refresh:
        today = date.today()
        sync.refresh_items(con, today, print)
        sync.refresh_coupons(con, today, print)
        con.commit()
    drops = prices.find_drops(con)
    if not drops:
        print("No price drops on anything bought in the last 30 days.")
        return drops
    table([[d.deadline.isoformat(), day(d.purchased_at), d.source, d.item_number, (d.name or "")[:40],
            qty(d.quantity), money(d.paid_unit), money(d.now_unit), money(d.total), d.reason] for d in drops],
          ["Claim by", "Bought", "Type", "Item", "Name", "Qty", "Paid", "Now", "Back", "Why"], right={5, 6, 7, 8})
    total = sum(d.total for d in drops)
    print(f"\n{money(total)} in possible price adjustments. Bring the receipt to the membership counter, "
          "or use costco.com chat for online orders.")
    return drops


def notify_new_drops(con, drops):
    seen = set(json.loads(db.get_meta(con, "notified_drops", "[]")))
    fresh = [d for d in drops if d.key not in seen]
    if fresh:
        total = sum(d.total for d in fresh)
        items = "; ".join(f"{(d.name or d.item_number)[:30]} {money(d.total)} by {d.deadline:%b %-d}" for d in fresh[:3])
        more = f" (+{len(fresh) - 3} more)" if len(fresh) > 3 else ""
        try:
            notify.send(con, "Costco price drop", f"{money(total)} back. {items}{more}. Run: costco drops")
        except Exception as e:
            print(f"alert failed: {e}")
            return []  # leave them un-notified so tomorrow's run retries
    # Everything current is now notified; drops whose window closed fall off.
    db.set_meta(con, "notified_drops", json.dumps(sorted(d.key for d in drops)))
    con.commit()
    return fresh


def cmd_daily(args, con):
    """What the scheduled job runs: sync, then notify about new drops."""
    print(f"--- {datetime.now():%Y-%m-%d %H:%M}")
    try:
        sync.run(con)
    except auth.AuthError as e:
        print(e)
        try:
            notify.send(con, "costco-cli", "Costco sign-in expired. Run: costco login on the sync Mac")
        except Exception as ne:
            print(f"alert failed: {ne}")
        return 1
    fresh = notify_new_drops(con, prices.find_drops(con))
    print(f"{len(fresh)} new price drops")
    return 0


def cmd_coupons(args, con):
    t = date.today().isoformat()
    sql = "SELECT * FROM coupons WHERE valid_from <= ? AND valid_to >= ?"
    params = [t, t]
    if args.search:
        sql += " AND (name LIKE ? OR item_number = ?)"
        params += [f"%{args.search}%", args.search]
    if args.mine:
        sql += " AND item_number IN (SELECT item_number FROM lines WHERE discount_for IS NULL)"
    rows = con.execute(sql + " ORDER BY discount DESC", params).fetchall()
    if not rows and not db.get_meta(con, "coupons_fetched"):
        sys.exit("No coupon book yet. Run: costco sync")
    table([[r["item_number"], (r["name"] or "")[:56], money(r["discount"]), money(r["price_after"]), r["valid_to"]]
           for r in rows], ["Item", "Name", "Off", "Price", "Until"], right={2, 3})
    print(f"\n{len(rows)} coupons" + (" on things you've bought before" if args.mine else ""))


def cmd_notify(args, con):
    if args.action == "twilio":
        print("Twilio SMS setup. Find these at console.twilio.com.")
        sid = input("Account SID: ").strip()
        from_ = input("Twilio phone number to send from (+15551234567): ").strip()
        to = input("Your mobile number (+15551234567): ").strip()
        token = getpass.getpass("Auth token (hidden): ").strip()
        if not (sid and from_ and to and token):
            sys.exit("All four values are needed.")
        notify.save_token(sid, token)
        for k, v in (("twilio_sid", sid), ("twilio_from", from_), ("twilio_to", to), ("notify_channel", "twilio")):
            db.set_meta(con, k, v)
        con.commit()
        print("Saved. Token is in your macOS Keychain (service costco-cli-twilio). Sending a test message...")
        notify.send(con, "costco-cli", "SMS alerts are set up.")
        print("Sent.")
    elif args.action == "test":
        used = notify.send(con, "costco-cli", "Test alert.")
        print(f"Sent via {used}.")
    elif args.action == "macos":
        s = notify.twilio_settings(con)
        if s:
            notify.delete_token(s["sid"])
        for k in ("twilio_sid", "twilio_from", "twilio_to"):
            con.execute("DELETE FROM meta WHERE key = ?", (k,))
        db.set_meta(con, "notify_channel", "macos")
        con.commit()
        print("Alerts will use macOS notifications. Twilio settings and Keychain token removed.")
    else:
        s = notify.twilio_settings(con)
        if notify.channel(con) == "twilio" and s:
            print(f"SMS via Twilio from {s['from']} to {s['to']}")
        else:
            print("macOS notifications on this computer")


def cmd_schedule(args, con):
    if args.action == "install":
        hh, mm = (int(x) for x in args.time.split(":"))
        path, cmd = schedule.install(hh, mm)
        print(f"Daily at {args.time}: {cmd}\nInstalled {path}\nLog: {config.LOG_DIR / 'daily.log'}")
    elif args.action == "uninstall":
        print("Removed." if schedule.uninstall() else "No schedule installed.")
    else:
        s = schedule.status()
        print("No schedule installed." if not s else f"Daily at {s[0]} ({'loaded' if s[1] else 'not loaded'})")


# ---------- parser ----------

def add_range(p, default_days):
    p.add_argument("--days", type=int, default=default_days, help=f"look back this many days (default {default_days})")
    p.add_argument("--since", help="start date YYYY-MM-DD")
    p.add_argument("--year", type=int, help="a calendar year, e.g. 2026")


def build_parser():
    ap = argparse.ArgumentParser(prog="costco", description=__doc__)
    ap.add_argument("--version", action="version", version=f"costco-cli {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")

    p = sub.add_parser("login", help="sign in to Costco in a Chrome window (once, or when the session expires)")
    p.add_argument("--no-sync", dest="sync", action="store_false", help="don't sync right after signing in")
    p.set_defaults(fn=cmd_login)

    p = sub.add_parser("sync", help="pull new receipts, online orders, prices and coupons")
    p.add_argument("--days", type=int, help="re-pull this many days back instead of since the last sync")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("receipts", help="list purchases, or show one receipt/order by ID")
    p.add_argument("id", nargs="?", help="receipt barcode or order number (a suffix is enough)")
    p.add_argument("--type", choices=["warehouse", "gas", "online"])
    add_range(p, 90)
    p.set_defaults(fn=cmd_receipts)

    p = sub.add_parser("item", help="purchase history for an item (name search or item number)")
    p.add_argument("query")
    p.set_defaults(fn=cmd_item)

    p = sub.add_parser("spend", help="spending summary")
    p.add_argument("--by", choices=["month", "year", "type", "where", "dept"], default="month")
    add_range(p, 365)
    p.set_defaults(fn=cmd_spend)

    p = sub.add_parser("returns", help="items still inside the price-adjustment or return window")
    p.add_argument("--days", type=int, default=config.PRICE_ADJUST_DAYS)
    p.set_defaults(fn=cmd_returns)

    p = sub.add_parser("drops", help="price drops you can claim on recent purchases")
    p.add_argument("--no-refresh", dest="refresh", action="store_false", help="use stored prices, skip lookups")
    p.set_defaults(fn=cmd_drops)

    p = sub.add_parser("coupons", help="current warehouse coupon book")
    p.add_argument("search", nargs="?")
    p.add_argument("--mine", action="store_true", help="only items you've bought before")
    p.set_defaults(fn=cmd_coupons)

    p = sub.add_parser("notify", help="where alerts go: macOS notifications or SMS via Twilio")
    p.add_argument("action", choices=["status", "twilio", "test", "macos"], nargs="?", default="status")
    p.set_defaults(fn=cmd_notify)

    p = sub.add_parser("schedule", help="run sync + drop alerts daily in the background")
    p.add_argument("action", choices=["install", "uninstall", "status"], nargs="?", default="status")
    p.add_argument("--time", default="07:00", help="HH:MM, default 07:00")
    p.set_defaults(fn=cmd_schedule)

    p = sub.add_parser("daily")  # what the scheduled job runs; no help, so unlisted
    p.set_defaults(fn=cmd_daily)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    con = db.connect()
    try:
        rc = args.fn(args, con)
        return rc if isinstance(rc, int) else 0
    except (auth.AuthError, ApiError, notify.NotifyError) as e:
        print(f"costco: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        con.close()
