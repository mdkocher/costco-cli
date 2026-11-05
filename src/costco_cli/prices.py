"""Price-adjustment finder.

Costco refunds the difference if an item's price drops within 30 days of
purchase. Three sources of a lower price are checked:
  * warehouse purchases vs. the current coupon book (warehouse prices)
  * warehouse purchases vs. a later receipt of your own for the same item at
    the same warehouse, which is the only published record of what the shelf
    actually charges
  * online purchases vs. today's costco.com price

Costco does not publish in-warehouse prices anywhere, and the costco.com price
is a different price list -- it carries shipping and handling and normally runs
above the warehouse price -- so it is never used to judge a warehouse purchase.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import config


@dataclass
class Drop:
    purchase_id: str
    source: str
    purchased_at: str
    item_number: str
    name: str
    quantity: float
    paid_unit: float      # what you paid per unit after any instant savings
    now_unit: float       # current price per unit
    reason: str
    deadline: date
    department: int = None
    from_history: bool = False   # found by comparing your own receipts

    @property
    def per_unit(self):
        return round(self.paid_unit - self.now_unit, 2)

    @property
    def total(self):
        return round(self.per_unit * self.quantity, 2)

    @property
    def key(self):
        return f"{self.purchase_id}:{self.item_number}:{self.now_unit:.2f}"

    @property
    def alertable(self):
        """Whether this drop is worth a text.

        Receipt-history drops in seasonal departments are recorded and listed
        but never texted: see config.NO_ALERT_DEPARTMENTS. A coupon in those
        departments still alerts, because a coupon is a published markdown.
        """
        return not (self.from_history and self.department in config.NO_ALERT_DEPARTMENTS)


RECENT_LINES = """
SELECT p.id, p.source, p.purchased_at, p.warehouse_number, l.item_number, l.quantity, l.unit_price,
       l.amount, l.description, l.department,
       COALESCE(i.name, l.description) AS name,
       (SELECT -SUM(d.amount) FROM lines d WHERE d.purchase_id = p.id AND d.discount_for = l.item_number) AS received
FROM lines l
JOIN purchases p ON p.id = l.purchase_id
LEFT JOIN items i ON i.item_number = l.item_number
WHERE p.source IN ('warehouse', 'online')
  AND l.discount_for IS NULL AND l.item_number != '' AND l.quantity > 0
  AND p.purchased_at >= ? AND p.purchased_at < ?
"""


def paid_unit(row):
    """Per-unit price the register charged, after any instant savings on the line."""
    qty = row["quantity"]
    return round(row["amount"] / qty - (row["received"] or 0) / qty, 2)


def _warehouse_observations(rows):
    """Prices your own warehouse receipts record, keyed by item and warehouse.

    {(item_number, warehouse_number): [(date, unit_paid), ...]}. Keyed by
    warehouse because Costco prices regionally -- a Bend adjustment cannot be
    argued from a Culver City receipt.
    """
    seen = {}
    for r in rows:
        if r["source"] != "warehouse":
            continue
        key = (r["item_number"], r["warehouse_number"])
        when = datetime.fromisoformat(r["purchased_at"]).date()
        seen.setdefault(key, []).append((when, paid_unit(r)))
    return seen


def _cheapest_later(observations, bought, deadline):
    """Lowest price seen for the item after `bought` and still inside the window.

    Returns (date, unit_price) or None. Same-day receipts are not a price
    change, so the comparison is strictly later than the purchase date.
    """
    later = [(d, u) for d, u in (observations or []) if bought < d <= deadline]
    return min(later, key=lambda du: du[1]) if later else None


def find_drops(con, today=None):
    today = today or date.today()
    since = (today - timedelta(days=config.PRICE_ADJUST_DAYS)).isoformat()
    t = today.isoformat()
    coupons = {r["item_number"]: r for r in con.execute(
        "SELECT * FROM coupons WHERE valid_from <= ? AND valid_to >= ?", (t, t))}
    online = {r["item_number"]: r["price"] for r in con.execute(
        "SELECT item_number, price FROM online_prices o WHERE observed_on ="
        " (SELECT MAX(observed_on) FROM online_prices WHERE item_number = o.item_number)")}

    # Upper bound so that `today` means what it says: passing a past date
    # replays that day rather than quietly consulting later receipts.
    rows = con.execute(RECENT_LINES, (since, (today + timedelta(days=1)).isoformat())).fetchall()
    observations = _warehouse_observations(rows)

    drops = []
    for r in rows:
        paid = paid_unit(r)
        bought = datetime.fromisoformat(r["purchased_at"]).date()
        deadline = bought + timedelta(days=config.PRICE_ADJUST_DAYS)
        common = dict(purchase_id=r["id"], source=r["source"], purchased_at=r["purchased_at"],
                      item_number=r["item_number"], name=r["name"], quantity=r["quantity"],
                      paid_unit=paid, deadline=deadline, department=r["department"])

        if r["source"] == "warehouse":
            # Both warehouse sources can fire on one line; claim the lower price.
            best = None   # (now_unit, reason, from_history)
            c = coupons.get(r["item_number"])
            if c:
                # Against the gross unit price: subtracting the coupon from an
                # already-discounted price would report the same saving twice.
                gross = r["amount"] / r["quantity"]
                now = c["price_after"] if c["price_after"] is not None else round(gross - c["discount"], 2)
                best = (now, f"coupon ${c['discount']:g} off until {c['valid_to']}", False)
            seen = _cheapest_later(observations.get((r["item_number"], r["warehouse_number"])), bought, deadline)
            if seen and (best is None or seen[1] < best[0]):
                when, unit = seen
                best = (unit, f"you paid ${unit:.2f} for it on {when.isoformat()}", True)
            if best and paid - best[0] >= 0.01:
                drops.append(Drop(now_unit=best[0], reason=best[1], from_history=best[2], **common))
        elif r["source"] == "online" and online.get(r["item_number"]) is not None:
            now = online[r["item_number"]]
            if paid - now >= 0.01:
                drops.append(Drop(now_unit=now, reason="costco.com price dropped", **common))
    drops.sort(key=lambda d: d.deadline)
    return drops
