"""Price-adjustment finder.

Costco refunds the difference if an item's price drops within 30 days of
purchase. Two sources of a lower price are checked:
  * warehouse purchases vs. the current coupon book (warehouse prices)
  * online purchases vs. today's costco.com price
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

    @property
    def per_unit(self):
        return round(self.paid_unit - self.now_unit, 2)

    @property
    def total(self):
        return round(self.per_unit * self.quantity, 2)

    @property
    def key(self):
        return f"{self.purchase_id}:{self.item_number}:{self.now_unit:.2f}"


RECENT_LINES = """
SELECT p.id, p.source, p.purchased_at, l.item_number, l.quantity, l.unit_price, l.amount, l.description,
       COALESCE(i.name, l.description) AS name,
       (SELECT -SUM(d.amount) FROM lines d WHERE d.purchase_id = p.id AND d.discount_for = l.item_number) AS received
FROM lines l
JOIN purchases p ON p.id = l.purchase_id
LEFT JOIN items i ON i.item_number = l.item_number
WHERE p.source IN ('warehouse', 'online')
  AND l.discount_for IS NULL AND l.item_number != '' AND l.quantity > 0
  AND p.purchased_at >= ?
"""


def find_drops(con, today=None):
    today = today or date.today()
    since = (today - timedelta(days=config.PRICE_ADJUST_DAYS)).isoformat()
    t = today.isoformat()
    coupons = {r["item_number"]: r for r in con.execute(
        "SELECT * FROM coupons WHERE valid_from <= ? AND valid_to >= ?", (t, t))}
    online = {r["item_number"]: r["price"] for r in con.execute(
        "SELECT item_number, price FROM online_prices o WHERE observed_on ="
        " (SELECT MAX(observed_on) FROM online_prices WHERE item_number = o.item_number)")}

    drops = []
    for r in con.execute(RECENT_LINES, (since,)):
        qty = r["quantity"]
        unit = r["amount"] / qty
        received = (r["received"] or 0) / qty
        paid = round(unit - received, 2)
        bought = datetime.fromisoformat(r["purchased_at"]).date()
        deadline = bought + timedelta(days=config.PRICE_ADJUST_DAYS)
        common = dict(purchase_id=r["id"], source=r["source"], purchased_at=r["purchased_at"],
                      item_number=r["item_number"], name=r["name"], quantity=qty, paid_unit=paid, deadline=deadline)

        c = coupons.get(r["item_number"])
        if r["source"] == "warehouse" and c:
            now = c["price_after"] if c["price_after"] is not None else round(unit - c["discount"], 2)
            if paid - now >= 0.01:
                drops.append(Drop(now_unit=now, reason=f"coupon ${c['discount']:g} off until {c['valid_to']}", **common))
        elif r["source"] == "online" and online.get(r["item_number"]) is not None:
            now = online[r["item_number"]]
            if paid - now >= 0.01:
                drops.append(Drop(now_unit=now, reason="costco.com price dropped", **common))
    drops.sort(key=lambda d: d.deadline)
    return drops
