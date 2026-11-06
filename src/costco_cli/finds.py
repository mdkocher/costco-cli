"""New and discounted items other shoppers have spotted, from Costco97's feed.

Deliberately browse-only. This is a national feed with no item numbers, it does
not cover Bend, and its warehouse tags mean "stocked here", not "on clearance
here at this price" -- so it can never feed a claim, and it must never appear in
the alerts or the summary. Those channels only carry money waiting and things
that are broken, which is what makes them worth reading.

What it is good for: noticing that something in a category you actually buy from
has shown up or gone on offer somewhere.
"""
import html
import re
import xml.etree.ElementTree as ET

import requests

from . import config

FEED_URL = "https://costco97.com/feed/"

# Packaging and units carry no signal about what a thing is.
NOISE = {
    "pack", "count", "ct", "oz", "lb", "lbs", "fl", "ml", "l", "g", "kg", "pk",
    "variety", "value", "size", "new", "product", "costco", "item", "items",
    "piece", "pieces", "box", "bag", "case", "bottle", "bottles", "can", "cans",
    "with", "and", "the", "for", "inch", "in", "of", "or", "a", "an", "set",
}
WORD = re.compile(r"[a-z][a-z0-9'-]{2,}")


def tokens(text):
    """Distinctive lowercase words from a product name."""
    return {w for w in WORD.findall((text or "").lower()) if w not in NOISE}


def parse(xml_text):
    """Feed items as dicts. Separated from fetching so it is testable offline."""
    root = ET.fromstring(xml_text)
    out = []
    for item in root.iterfind("./channel/item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        link = (item.findtext("link") or "").strip()
        cats = [c.text.strip() for c in item.iterfind("category") if c.text]
        # Warehouse tags are "stocked here" lists, hundreds long. The useful
        # categories are the short ones: Food, Home, Personal Care.
        kinds = [c for c in cats if "," not in c and c.lower() != "deals"]
        # No price. A deal post contains the sale price, the regular price and
        # the saving, with nothing in the markup to tell them apart -- picking
        # one gave "$0.15 Nutella". A confident wrong price is worse than none
        # in a tool whose whole job is getting prices right. The link has it.
        out.append({
            "title": html.unescape(re.sub(r"^New Product:\s*", "", title)),
            "link": link,
            "kinds": kinds,
            "deal": "Deals" in cats,
        })
    return out


def _content(item):
    for tag in ("{http://purl.org/rss/1.0/modules/content/}encoded", "content:encoded"):
        text = item.findtext(tag)
        if text:
            return text
    return ""


def fetch(timeout=15):
    r = requests.get(FEED_URL, headers={"User-Agent": config.USER_AGENT}, timeout=timeout)
    r.raise_for_status()
    return parse(r.text)


def vocabulary(con):
    """Distinctive words from everything you have actually bought."""
    vocab = set()
    for (name,) in con.execute(
        "SELECT DISTINCT COALESCE(i.name, l.description) FROM lines l"
        " LEFT JOIN items i ON i.item_number = l.item_number"
        " WHERE l.discount_for IS NULL AND l.quantity > 0"
    ):
        vocab |= tokens(name)
    return vocab


MIN_HITS = 2


def rank(items, vocab, min_hits=MIN_HITS):
    """Feed items that share words with your purchases, best match first.

    Deliberately crude: no item numbers to join on, so this is word overlap and
    nothing more. Good enough to notice a brand you buy; not good enough to put
    in an email, which is why this is browse-only.
    """
    scored = []
    for it in items:
        hits = tokens(it["title"]) & vocab
        # One shared word is noise: "plus" matched a Keurig, "degrees" a jacket.
        if len(hits) >= min_hits:
            scored.append((len(hits), sorted(hits), it))
    scored.sort(key=lambda s: (-s[0], s[2]["title"]))
    return [(hits, it) for _, hits, it in scored]
