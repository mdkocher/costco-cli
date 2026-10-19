"""SQLite storage. One file at ~/.local/share/costco-cli/costco.db."""
import json
import re
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS purchases (
    id               TEXT PRIMARY KEY,   -- receipt barcode or online order number
    source           TEXT NOT NULL,      -- warehouse | gas | online
    purchased_at     TEXT NOT NULL,      -- ISO local time
    location         TEXT,
    warehouse_number TEXT,
    status           TEXT,
    transaction_type TEXT,               -- Sales | Refund | ... (warehouse)
    subtotal         REAL,
    tax              REAL,
    savings          REAL,
    total            REAL,
    raw              TEXT
);
CREATE TABLE IF NOT EXISTS lines (
    purchase_id  TEXT NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
    line_no      INTEGER NOT NULL,
    item_number  TEXT,
    description  TEXT,
    quantity     REAL,
    unit_price   REAL,
    amount       REAL,
    department   INTEGER,
    discount_for TEXT,                   -- item number an instant-savings line applies to
    PRIMARY KEY (purchase_id, line_no)
);
CREATE INDEX IF NOT EXISTS lines_item ON lines(item_number);
CREATE INDEX IF NOT EXISTS lines_discount ON lines(discount_for);
CREATE TABLE IF NOT EXISTS items (
    item_number  TEXT PRIMARY KEY,
    name         TEXT,
    updated_at   TEXT
);
CREATE TABLE IF NOT EXISTS online_prices (
    item_number  TEXT NOT NULL,
    observed_on  TEXT NOT NULL,          -- YYYY-MM-DD
    price        REAL NOT NULL,
    PRIMARY KEY (item_number, observed_on)
);
CREATE TABLE IF NOT EXISTS coupons (
    item_number  TEXT NOT NULL,
    valid_from   TEXT NOT NULL,          -- YYYY-MM-DD
    valid_to     TEXT NOT NULL,
    discount     REAL,
    price_after  REAL,
    name         TEXT,
    PRIMARY KEY (item_number, valid_from)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

DISCOUNT_RE = re.compile(r"^/\s*(\d+)")


def connect(path=None):
    path = path or config.DB_PATH
    if str(path) != ":memory:":
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def get_meta(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(con, key, value):
    con.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)))


def _replace_purchase(con, purchase, lines):
    con.execute("DELETE FROM purchases WHERE id = ?", (purchase["id"],))
    cols = ", ".join(purchase)
    con.execute(f"INSERT INTO purchases ({cols}) VALUES ({', '.join('?' * len(purchase))})", list(purchase.values()))
    con.executemany(
        "INSERT INTO lines (purchase_id, line_no, item_number, description, quantity, unit_price, amount, department, discount_for)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(purchase["id"], n, *line) for n, line in enumerate(lines, 1)],
    )


def save_warehouse_receipt(con, r):
    source = "gas" if r.get("documentType") == "FuelReceipts" or r.get("receiptType") == "Gas Station" else "warehouse"
    lines = []
    for l in r.get("itemArray") or []:
        desc = " ".join(filter(None, [l.get("itemDescription01"), l.get("itemDescription02")])).strip()
        m = DISCOUNT_RE.match(desc)
        units, amount = l.get("unit"), l.get("amount")
        # itemUnitPriceAmount is often stale; amount / units is what was charged.
        unit_price = round(amount / units, 2) if units and amount is not None else l.get("itemUnitPriceAmount")
        lines.append((
            str(l.get("itemNumber") or ""), desc, units, unit_price,
            amount, l.get("itemDepartmentNumber"), m.group(1) if m else None,
        ))
    _replace_purchase(con, {
        "id": r["transactionBarcode"],
        "source": source,
        "purchased_at": r["transactionDateTime"],
        "location": (r.get("warehouseName") or "").title(),
        "warehouse_number": str(r.get("warehouseNumber") or ""),
        "status": None,
        "transaction_type": r.get("transactionType"),
        "subtotal": r.get("subTotal"),
        "tax": r.get("taxes"),
        "savings": r.get("instantSavings"),
        "total": r.get("total"),
        "raw": json.dumps(r),
    }, lines)
    return source


def save_online_order(con, o):
    lines = []
    for ship in o.get("orderShipTos") or []:
        for l in ship.get("orderLineItems") or []:
            if l.get("isFeeItem"):
                continue
            lines.append((
                str(l.get("itemNumber") or ""), l.get("itemDescription"), l.get("quantity"),
                l.get("price"), l.get("merchandiseTotalAmount"), None, None,
            ))
    _replace_purchase(con, {
        "id": o["orderNumber"],
        "source": "online",
        "purchased_at": (o.get("orderPlacedDate") or "")[:19],
        "location": "Costco.com",
        "warehouse_number": str(o.get("warehouseNumber") or ""),
        "status": o.get("status"),
        "transaction_type": None,
        "subtotal": o.get("merchandiseTotal"),
        "tax": o.get("uSTaxTotal1"),
        "savings": o.get("discountAmount"),
        "total": o.get("orderTotal"),
        "raw": json.dumps(o),
    }, lines)


def upsert_item(con, item_number, name, today):
    con.execute(
        "INSERT INTO items(item_number, name, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(item_number) DO UPDATE SET name = COALESCE(excluded.name, items.name), updated_at = excluded.updated_at",
        (item_number, name, today),
    )


def record_online_price(con, item_number, price, today):
    con.execute("INSERT OR REPLACE INTO online_prices(item_number, observed_on, price) VALUES (?, ?, ?)",
                (item_number, today, price))


def save_coupons(con, coupons):
    con.executemany(
        "INSERT OR REPLACE INTO coupons(item_number, valid_from, valid_to, discount, price_after, name)"
        " VALUES (:item_number, :valid_from, :valid_to, :discount, :price_after, :name)",
        coupons,
    )
