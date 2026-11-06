"""costco: track your Costco purchases and catch price adjustments."""
import argparse
import getpass
import traceback
import json
import sys
from datetime import date, datetime, timedelta

from . import __version__, auth, config, db, lh, notify, prices, schedule, sync
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
    # Drops you have already dealt with are history, not a to-do item.
    decided = db.claim_outcomes(con)
    hidden = 0
    if not args.all:
        kept = [d for d in drops if (d.purchase_id, d.item_number) not in decided]
        hidden = len(drops) - len(kept)
        drops = kept
    if not drops:
        print("No price drops on anything bought in the last 30 days."
              + (f" ({hidden} already settled: costco claims)" if hidden else ""))
        return drops
    table([[d.deadline.isoformat(), day(d.purchased_at), d.source, d.item_number, (d.name or "")[:40],
            qty(d.quantity), money(d.paid_unit), money(d.now_unit), money(d.total),
            d.reason + ("" if d.alertable else " (seasonal, no alert sent)")] for d in drops],
          ["Claim by", "Bought", "Type", "Item", "Name", "Qty", "Paid", "Now", "Back", "Why"], right={5, 6, 7, 8})
    total = sum(d.total for d in drops)
    print(f"\n{money(total)} in possible price adjustments. Bring the receipt to the membership counter, "
          "or use costco.com chat for online orders."
          + (f"\n{hidden} already settled; see costco claims." if hidden else ""))
    return drops


def cmd_claims(args, con):
    rows = db.claims(con, limit=args.limit)
    if not rows:
        print("No claims settled yet. Price drops waiting on you: costco drops")
        return
    table([[r["decided_at"][:10], r["outcome"], r["item_number"], (r["name"] or "")[:40],
            money(r["amount"]), r["purchase_id"]] for r in rows],
          ["Settled", "Outcome", "Item", "Name", "Back", "Purchase"], right={4})
    back = sum(r["amount"] or 0 for r in rows if r["outcome"] == "claimed")
    lost = sum(r["amount"] or 0 for r in rows if r["outcome"] == "expired")
    line = f"\n{money(back)} claimed"
    if lost:
        line += f", {money(lost)} expired unclaimed"
    print(line + ".")


def notify_new_drops(con, drops):
    seen = set(json.loads(db.get_meta(con, "notified_drops", "[]")))
    fresh = [d for d in drops if d.key not in seen]
    worth_texting = [d for d in fresh if d.alertable]
    if worth_texting:
        total = sum(d.total for d in worth_texting)
        items = "; ".join(f"{(d.name or d.item_number)[:30]} {money(d.total)} by {d.deadline:%b %-d}"
                          for d in worth_texting[:3])
        more = f" (+{len(worth_texting) - 3} more)" if len(worth_texting) > 3 else ""
        try:
            settle = (f"costco claim {worth_texting[0].item_number}"
                      if len(worth_texting) == 1 else "costco claim")
            notify.send(con, "Costco price drop",
                        f"{money(total)} back. {items}{more}. Settle it: {settle}")
        except Exception as e:
            print(f"alert failed: {e}")
            return []  # leave them un-notified so tomorrow's run retries
    # Everything current is now notified; drops whose window closed fall off.
    db.set_meta(con, "notified_drops", json.dumps(sorted(d.key for d in drops)))
    con.commit()
    return fresh


def start_claim_runs(con, drops):
    """Make sure every unsettled drop has a claim workflow.

    This reconciles rather than merely creates: the run id is derived from the
    purchase line, so a drop whose run the server lost is started again, and
    anything already running is left alone. That is what makes losing the
    server survivable -- SQLite stays the record either way.

    Returns (started, status): status is None when runs were actually started,
    UNCHECKED when there was nothing to reconcile, or a "DOWN - ..." reason.
    """
    decided = db.claim_outcomes(con)
    todo = [d for d in drops if (d.purchase_id, d.item_number) not in decided]
    if not todo:
        # Nothing to reconcile, so the server was never contacted. Saying
        # "running" here would assert a health check that never happened.
        return 0, UNCHECKED
    started = 0
    for d in todo:
        try:
            if lh.ensure_run(d):
                started += 1
        except Exception as e:
            # The server being unreachable is a fault worth reporting, but it
            # must not cost you the receipt sync -- those are unrelated concerns.
            return started, f"DOWN - {type(e).__name__}: {e}"
    return started, None


def open_claims(con):
    """Drops you have not settled yet, soonest deadline first."""
    settled = db.claim_outcomes(con)
    return [d for d in prices.find_drops(con)
            if (d.purchase_id, d.item_number) not in settled]


def pick_claim(drops, ask=input):
    """Ask which adjustment to settle, and how. Returns (drop, outcome) or (None, None).

    This is the answer to "what is waiting on me?", which otherwise needs the
    item number copied out of an alert. Unlike `lhctl execute userTaskRun`,
    y/n is accepted, because the parsing is ours.
    """
    if not drops:
        print("Nothing waiting on you. Price drops: costco drops")
        return None, None
    today = date.today()
    print("Price adjustments waiting on you:\n")
    for i, d in enumerate(drops, 1):
        left = (d.deadline - today).days
        print(f"  {i}. {money(d.total):>8}  {(d.name or d.item_number)[:34]:34} "
              f"claim by {d.deadline} ({left}d left)")
    choice = ask(f"\nWhich one? [1-{len(drops)}, Enter to quit] ").strip()
    if not choice:
        return None, None
    if not choice.isdigit() or not 1 <= int(choice) <= len(drops):
        sys.exit(f"Not one of the choices: {choice!r}")
    d = drops[int(choice) - 1]
    answer = ask(f"Did you get the {money(d.total)} back? [y/n, Enter to quit] ").strip().lower()
    if answer in ("y", "yes"):
        return d, "claimed"
    if answer in ("n", "no"):
        return d, "skipped"
    return None, None


def cmd_claim(args, con):
    """Settle a price adjustment: record it, and stop the nudges."""
    if not args.item:
        d, outcome = pick_claim(open_claims(con))
        if d is None:
            return 0
        return _settle(con, d, outcome, args.note)

    matches = [d for d in open_claims(con)
               if d.item_number == args.item
               and (args.purchase is None or d.purchase_id == args.purchase)]
    if not matches:
        sys.exit(f"No open price drop for item {args.item}. See: costco drops")
    if len(matches) > 1:
        print(f"Item {args.item} is on {len(matches)} receipts. Choose one:")
        for d in matches:
            print(f"  --purchase {d.purchase_id}   bought {day(d.purchased_at)}, {money(d.total)} back")
        return 1
    return _settle(con, matches[0], "skipped" if args.skip else "claimed", args.note)


def _settle(con, d, outcome, note=None):
    db.record_claim(con, d.purchase_id, d.item_number, d.total, outcome)
    con.commit()
    print(f"{outcome.title()}: {money(d.total)} on {d.name or d.item_number}.")

    try:
        if lh.complete_claim(d.purchase_id, d.item_number,
                             claimed=(outcome == "claimed"), note=note):
            print("  Claim workflow closed; no more reminders.")
        else:
            print("  Nothing open on the server for it, so there was nothing to close.")
    except Exception as e:
        # Recorded either way: SQLite is the record, the workflow only nudges.
        print(f"  Recorded, but could not reach LittleHorse ({e}); "
              f"reminders may continue until {d.deadline}.")
    return 0


UNCHECKED = "not checked (nothing waiting)"
DEFAULT_SUMMARY_DAYS = 7


def summary_interval(con):
    """How often the summary goes out, in days. 0 means never."""
    try:
        return int(db.get_meta(con, "summary_days", str(DEFAULT_SUMMARY_DAYS)))
    except ValueError:
        return DEFAULT_SUMMARY_DAYS


def summary_text(con, tracking, today=None, days=None):
    """What has happened lately, in one readable block.

    Everything here is already in the database; the point is that none of it
    was visible without writing SQL by hand.
    """
    today = today or date.today()
    days = days or DEFAULT_SUMMARY_DAYS
    since = (today - timedelta(days=days)).isoformat()

    # Refunds are reported separately below; counting them here would both
    # inflate the receipt count and net their negative total into what you spent.
    bought = con.execute("SELECT source, COUNT(*) n, COALESCE(SUM(total), 0) t FROM purchases"
                         " WHERE purchased_at >= ?"
                         "   AND (transaction_type IS NULL OR transaction_type != 'Refund')"
                         " GROUP BY source ORDER BY n DESC", (since,)).fetchall()
    settled = con.execute("SELECT outcome, COUNT(*) n, COALESCE(SUM(amount), 0) t FROM claims"
                          " WHERE decided_at >= ? GROUP BY outcome", (since,)).fetchall()
    refunds = con.execute(
        "SELECT p.purchased_at, l.amount, COALESCE(i.name, l.description) nm"
        " FROM lines l JOIN purchases p ON p.id = l.purchase_id"
        " LEFT JOIN items i ON i.item_number = l.item_number"
        " WHERE p.transaction_type = 'Refund' AND p.purchased_at >= ?"
        " ORDER BY p.purchased_at DESC", (since,)).fetchall()
    waiting = open_claims(con)
    tracked = con.execute("SELECT COUNT(*) FROM purchases").fetchone()[0]

    out = [f"Costco summary, {days} days to {today.isoformat()}", ""]
    out.append(f"Sync      last run {db.get_meta(con, 'last_sync', 'never')}, {tracked} purchases tracked")
    if bought:
        kinds = ", ".join(f"{r['n']} {r['source']}" for r in bought)
        out.append(f"Bought    {sum(r['n'] for r in bought)} receipts, "
                   f"{money(sum(r['t'] or 0 for r in bought))} ({kinds})")
    else:
        out.append("Bought    nothing")
    if waiting:
        out.append(f"Waiting   {len(waiting)} adjustment(s), {money(sum(d.total for d in waiting))}")
        for d in waiting:
            out.append(f"            {money(d.total):>8}  {(d.name or d.item_number)[:30]:30}"
                       f"  by {d.deadline} ({(d.deadline - today).days}d)")
    else:
        out.append("Waiting   nothing to claim")
    out.append("Settled   " + (", ".join(f"{r['n']} {r['outcome']} {money(r['t'])}" for r in settled)
                               or "nothing this period"))
    if refunds:
        out.append(f"Refunds   {len(refunds)}")
        for r in refunds:
            out.append(f"            {money(r['amount']):>9}  {str(r['nm'])[:30]:30}  {r['purchased_at'][:10]}")
    else:
        out.append("Refunds   none")
    out.append(f"Tracking  {tracking}")
    out += ["", "Settle an adjustment: costco claim"]
    return "\n".join(out)


def alert_failure(con, what, exc):
    """Mail a failure. Without this a crash is silent: you would only notice
    weeks later that the alerts had stopped."""
    print(f"{what}: {type(exc).__name__}: {exc}")
    traceback.print_exc()
    try:
        notify.send(con, "costco-cli failed", f"{what}: {type(exc).__name__}: {exc}")
    except Exception as e:
        print(f"could not send the failure alert: {e}")


def maybe_summary(con, tracking, today=None):
    """Send the periodic summary if one is due.

    On elapsed days rather than a weekday, so a Mac asleep on Monday does not
    skip the period. A send that fails deliberately does not move the clock --
    one flaky SMTP moment would otherwise cost a whole period of coverage.
    """
    today = today or date.today()
    days = summary_interval(con)
    if days <= 0:
        return False
    last = db.get_meta(con, "last_summary")
    if last is None:                      # first run: start the clock, say nothing
        db.set_meta(con, "last_summary", today.isoformat())
        con.commit()
        return False
    if (today - date.fromisoformat(last)).days < days:
        return False
    try:
        notify.send(con, "Costco summary", summary_text(con, tracking, today, days))
    except Exception as e:
        print(f"summary failed: {e}")
        return False
    db.set_meta(con, "last_summary", today.isoformat())
    con.commit()
    return True


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
    try:
        drops = prices.find_drops(con)
        fresh = notify_new_drops(con, drops)
        quiet = [d for d in fresh if not d.alertable]
        print(f"{len(fresh)} new price drops"
              + (f" ({len(quiet)} seasonal, no alert sent)" if quiet else ""))
        started, status = start_claim_runs(con, drops)
        if status is UNCHECKED:
            print("no claims waiting; claim tracking not contacted")
        elif status:
            # Reported, not raised: a reminder layer that is down must not cost
            # you the receipt sync that already succeeded above.
            print(f"claim tracking {status}")
            try:
                notify.send(con, "costco-cli: claim tracking down",
                            f"{len(drops)} drop(s) found but no claim workflow could be "
                            f"started. {status}")
            except Exception as e:
                print(f"could not send the tracking alert: {e}")
        else:
            print(f"{started} claim workflow(s) started")
        if maybe_summary(con, status or "running"):
            print("summary sent")
    except Exception as e:
        alert_failure(con, "daily run failed", e)
        return 1
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
    if args.action == "email":
        print("Email alerts. For Gmail, create an app password at myaccount.google.com/apppasswords.")
        from_ = input("Send from (your email address): ").strip()
        to = input(f"Send to [{from_ or 'the same address'}]: ").strip() or from_
        host = input(f"SMTP host [{notify.DEFAULT_SMTP_HOST}]: ").strip() or notify.DEFAULT_SMTP_HOST
        port = input(f"SMTP port [{notify.DEFAULT_SMTP_PORT}]: ").strip() or str(notify.DEFAULT_SMTP_PORT)
        password = getpass.getpass("App password (hidden): ").strip()
        if not (from_ and to and password):
            sys.exit("An address to send from, an address to send to, and an app password are all needed.")
        if not port.isdigit():
            sys.exit(f"SMTP port should be a number, not {port!r}.")
        notify.save_password(from_, password)
        for k, v in (("email_from", from_), ("email_to", to), ("smtp_host", host), ("smtp_port", port),
                     ("notify_channel", "email")):
            db.set_meta(con, k, v)
        con.commit()
        print(f"Saved. App password is in your macOS Keychain (service {notify.KEYCHAIN_SERVICE}). "
              "Sending a test message...")
        notify.send(con, "costco-cli", "Email alerts are set up.")
        print("Sent.")
    elif args.action == "summary":
        if args.off:
            db.set_meta(con, "summary_days", "0")
            con.commit()
            print("Summary emails off.")
        elif args.days is not None:
            if args.days < 1:
                sys.exit("--days must be 1 or more; use --off to stop them.")
            db.set_meta(con, "summary_days", str(args.days))
            con.commit()
            print(f"Summary every {args.days} day(s).")
        elif args.send:
            every = summary_interval(con) or DEFAULT_SUMMARY_DAYS
            notify.send(con, "Costco summary", summary_text(con, UNCHECKED, days=every))
            db.set_meta(con, "last_summary", date.today().isoformat())
            con.commit()
            print(f"Sent, and the clock reset: next one in {every} day(s).")
        else:
            every = summary_interval(con)
            print(f"Emailed every {every} day(s).\n" if every else "Summary emails are off.\n")
            print(summary_text(con, UNCHECKED, days=every or DEFAULT_SUMMARY_DAYS))
    elif args.action == "test":
        used = notify.send(con, "costco-cli", "Test alert.")
        print(f"Sent via {used}.")
    elif args.action == "macos":
        s = notify.email_settings(con)
        if s:
            notify.delete_password(s["email_from"])
        for k in notify.EMAIL_KEYS:
            con.execute("DELETE FROM meta WHERE key = ?", (k,))
        db.set_meta(con, "notify_channel", "macos")
        con.commit()
        print("Alerts will use macOS notifications. Email settings and Keychain password removed.")
    else:
        s = notify.email_settings(con)
        if notify.channel(con) == "email" and s:
            print(f"Email from {s['email_from']} to {s['email_to']} via {s['smtp_host']}:{s['smtp_port']}")
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

    p = sub.add_parser("claim", help="settle a price adjustment and stop the reminders")
    p.add_argument("item", nargs="?", help="item number; omit to pick from a list")
    p.add_argument("--purchase", help="receipt or order id, when the item is on more than one")
    p.add_argument("--skip", action="store_true", help="record that you passed on it")
    p.add_argument("--note", help="anything worth remembering")
    p.set_defaults(fn=cmd_claim)

    p = sub.add_parser("claims", help="price adjustments you have already settled")
    p.add_argument("--limit", type=int, default=None, help="show only the most recent N")
    p.set_defaults(fn=cmd_claims)

    p = sub.add_parser("drops", help="price drops you can claim on recent purchases")
    p.add_argument("--all", action="store_true", help="include drops already settled")
    p.add_argument("--no-refresh", dest="refresh", action="store_false", help="use stored prices, skip lookups")
    p.set_defaults(fn=cmd_drops)

    p = sub.add_parser("coupons", help="current warehouse coupon book")
    p.add_argument("search", nargs="?")
    p.add_argument("--mine", action="store_true", help="only items you've bought before")
    p.set_defaults(fn=cmd_coupons)

    p = sub.add_parser("notify", help="where alerts go: macOS notifications or email")
    p.add_argument("action", choices=["status", "email", "summary", "test", "macos"],
                   nargs="?", default="status")
    p.add_argument("--days", type=int, help="summary: send one every N days")
    p.add_argument("--off", action="store_true", help="summary: stop sending them")
    p.add_argument("--send", action="store_true", help="summary: email it now, whenever the last one went")
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
    if notify.migrate_legacy_channel(con):
        print("Note: SMS alerts have been removed. Alerts now use macOS notifications; "
              "run `costco notify email` to get them by email instead.", file=sys.stderr)
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
