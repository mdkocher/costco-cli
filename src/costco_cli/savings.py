"""Costco's monthly warehouse savings (the coupon book), scraped from the
public warehouse-savings page. Prices there are warehouse prices."""
import html
import re
from datetime import date

import requests

from . import config

VALID_RE = re.compile(r"Valid (\d{1,2})/(\d{1,2})/(\d{2,4})\s*-\s*(\d{1,2})/(\d{1,2})/(\d{2,4})")
ITEM_RE = re.compile(r"\| Item ([\d,\s]+?(?:(?:AND/OR|and|or|,)\s*[\d,\s]+?)*) \|")
PRICE_RE = re.compile(r"\$ \| (\d[\d,]*) \| \. \| (\d\d)")
DISCOUNT_RE = re.compile(r"\$(\d+(?:\.\d\d)?) OFF", re.I)


def _date(m, d, y):
    y = int(y)
    return date(y + 2000 if y < 100 else y, int(m), int(d)).isoformat()


def flatten(page_html):
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", page_html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " | ", text)
    text = html.unescape(re.sub(r"\s+", " ", text))
    return re.sub(r"(\s*\|\s*)+", " | ", text)


def _tile_name(prefix):
    """A tile reads "<name> | Warehouse | & | Online | <name> | <size> | Item ...".
    Take everything after the channel marker, so a size line stays with the name."""
    tail = prefix[-300:]
    cut = max((tail.rfind(mk) + len(mk), mk) for mk in (" | Online", " | Warehouse"))[0]
    if cut < len(" | Online"):
        return tail.rsplit(" | ", 1)[-1].strip()
    name = " ".join(s.strip() for s in tail[cut:].split(" | ") if s.strip())
    return re.sub(r"^Only\s+", "", name)  # "Warehouse | Only" tiles


def parse(page_html):
    """Return a list of coupon dicts: item_number, valid_from, valid_to, discount, price_after, name."""
    text = flatten(page_html)
    ranges = [(m.start(), _date(*m.groups()[:3]), _date(*m.groups()[3:])) for m in VALID_RE.finditer(text)]
    matches = list(ITEM_RE.finditer(text))
    coupons = []
    for i, m in enumerate(matches):
        valid = [r for r in ranges if r[0] < m.start()]
        if not valid:
            continue
        _, valid_from, valid_to = valid[-1]
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end]
        name = _tile_name(text[:m.start()])
        price = PRICE_RE.search(body)
        discount = DISCOUNT_RE.search(body)
        if not discount:
            continue
        for num in re.findall(r"\d{4,8}", m.group(1)):
            coupons.append({
                "item_number": num,
                "valid_from": valid_from,
                "valid_to": valid_to,
                "discount": float(discount.group(1)),
                "price_after": float(f"{price.group(1).replace(',', '')}.{price.group(2)}") if price else None,
                "name": name,
            })
    return coupons


def fetch():
    r = requests.get(config.SAVINGS_PAGE, headers={"User-Agent": config.USER_AGENT, "Accept-Language": "en-US"},
                     timeout=30)
    r.raise_for_status()
    return parse(r.text)
