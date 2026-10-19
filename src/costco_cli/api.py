"""Calls to the same GraphQL endpoints costco.com's own pages use."""
import requests

from . import config

RECEIPTS_QUERY = """query receiptsWithCounts($startDate: String!, $endDate: String!, $documentType: String!, $documentSubType: String!) {
  receiptsWithCounts(startDate: $startDate, endDate: $endDate, documentType: $documentType, documentSubType: $documentSubType) {
    receipts { warehouseName warehouseNumber receiptType documentType transactionDateTime transactionBarcode transactionType
      total subTotal taxes totalItemCount instantSavings
      itemArray { itemNumber itemDescription01 itemDescription02 unit amount itemUnitPriceAmount itemDepartmentNumber } }
  }
}"""

ONLINE_ORDERS_QUERY = """query getOnlineOrders($startDate:String!, $endDate:String!, $pageNumber:Int, $pageSize:Int, $warehouseNumber:String!) {
  getOnlineOrders(startDate:$startDate, endDate:$endDate, pageNumber:$pageNumber, pageSize:$pageSize, warehouseNumber:$warehouseNumber) {
    totalNumberOfRecords
    bcOrders { orderNumber: sourceOrderNumber orderPlacedDate: orderedDate orderTotal status }
  }
}"""

ORDER_DETAILS_QUERY = """query getOrderDetails($orderNumbers: [String]) {
  getOrderDetails(orderNumbers: $orderNumbers) {
    orderNumber: sourceOrderNumber orderPlacedDate: orderedDate status warehouseNumber
    merchandiseTotal shippingAndHandling uSTaxTotal1 discountAmount orderTotal
    orderShipTos { orderLineItems { lineNumber itemNumber itemDescription: sourceItemDescription price: unitPrice
      quantity: orderedTotalQuantity merchandiseTotalAmount isFeeItem orderStatus } }
  }
}"""

PRODUCTS_QUERY = """query products($itemNumbers: [String], $clientId: String!, $locale: [String], $warehouseNumber: String!) {
  products(itemNumbers: $itemNumbers, clientId: $clientId, locale: $locale, warehouseNumber: $warehouseNumber) {
    catalogData { itemNumber published description { shortDescription } }
    fulfillmentData { itemNumber price channel }
  }
}"""

ONLINE_PAGE_SIZE = 50
PRODUCT_BATCH = 40


class ApiError(Exception):
    pass


def _post(url, service, query, variables, session=None):
    headers = {
        "Content-Type": "application/json-patch+json",
        "Costco.Env": "ecom",
        "Costco.Service": service,
        "Client-Identifier": config.CLIENT_IDENTIFIER,
        "User-Agent": config.USER_AGENT,
    }
    if session:
        headers["Costco-X-Wcs-Clientid"] = session.client_id
        headers["Costco-X-Authorization"] = f"Bearer {session.token}"
    r = requests.post(url, headers=headers, json={"query": query, "variables": variables}, timeout=30)
    if r.status_code == 401:
        raise ApiError("Costco rejected the session token. Run: costco login")
    if r.status_code != 200:
        raise ApiError(f"{url} returned HTTP {r.status_code}")
    body = r.json()
    if body.get("errors"):
        raise ApiError(f"GraphQL error: {body['errors']}")
    return body["data"]


def _orders(session, query, variables):
    return _post(config.ORDERS_API, "restOrders", query, variables, session)


def _one(value):
    """Some fields come back as a one-element list, others as an object."""
    return value[0] if isinstance(value, list) else value


def warehouse_receipts(session, start, end):
    data = _orders(session, RECEIPTS_QUERY, {
        "startDate": f"{start.month}/{start.day:02d}/{start.year}",
        "endDate": f"{end.month}/{end.day:02d}/{end.year}",
        "documentType": "all",
        "documentSubType": "all",
    })
    return _one(data["receiptsWithCounts"])["receipts"] or []


def online_orders(session, start, end, warehouse):
    orders, page = [], 1
    while True:
        res = _one(_orders(session, ONLINE_ORDERS_QUERY, {
            "startDate": f"{start.year}-{start.month}-{start.day:02d}",
            "endDate": f"{end.year}-{end.month}-{end.day:02d}",
            "pageNumber": page,
            "pageSize": ONLINE_PAGE_SIZE,
            "warehouseNumber": warehouse,
        })["getOnlineOrders"])
        batch = res["bcOrders"] or []
        orders += batch
        if not batch or len(orders) >= res["totalNumberOfRecords"]:
            return orders
        page += 1


def order_details(session, order_number):
    return _one(_orders(session, ORDER_DETAILS_QUERY, {"orderNumbers": [order_number]})["getOrderDetails"])


def products(item_numbers, warehouse):
    """Names and current online prices. No sign-in needed.

    Returns {item_number: {"name": str|None, "online_price": float|None}}.
    Warehouse-only items report an online price of 0, which we treat as None.
    """
    out = {}
    items = list(dict.fromkeys(str(i) for i in item_numbers))
    for i in range(0, len(items), PRODUCT_BATCH):
        chunk = items[i:i + PRODUCT_BATCH]
        data = _post(config.PRODUCTS_API, "restProduct", PRODUCTS_QUERY, {
            "itemNumbers": chunk,
            "clientId": config.ACCOUNT_APP_ID,
            "locale": ["en-US"],
            "warehouseNumber": warehouse,
        })["products"]
        for c in data.get("catalogData") or []:
            desc = (c.get("description") or {}).get("shortDescription")
            out.setdefault(c["itemNumber"], {"name": None, "online_price": None})["name"] = desc
        for f in data.get("fulfillmentData") or []:
            price = f.get("price")
            entry = out.setdefault(f["itemNumber"], {"name": None, "online_price": None})
            entry["online_price"] = price if price and price > 0 else None
    return out
