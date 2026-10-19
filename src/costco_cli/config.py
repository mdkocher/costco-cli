"""Paths and constants. All personal data lives under DATA_DIR, never in the repo."""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("COSTCO_CLI_HOME", Path.home() / ".local/share/costco-cli"))
DB_PATH = DATA_DIR / "costco.db"
PROFILE_DIR = DATA_DIR / "chrome-profile"
LOG_DIR = DATA_DIR / "logs"

CHROME = os.environ.get(
    "COSTCO_CLI_CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)
CDP_PORT = int(os.environ.get("COSTCO_CLI_CDP_PORT", "9333"))

# Public identifiers from costco.com's own web app (visible in its page source),
# not credentials. Your login session never leaves the local Chrome profile.
ACCOUNT_APP_ID = "4900eb1f-0c10-4bd9-99c3-c59e6c1ecebf"
CLIENT_IDENTIFIER = "481b1aec-aa3b-454b-b81b-48187e28f205"
# costco.com's own "online warehouse" (the site's default 847_NA), not your store.
ONLINE_WAREHOUSE = "847"

ACCOUNT_PAGE = f"https://www.costco.com/myaccount/#/app/{ACCOUNT_APP_ID}/ordersandpurchases"
ORDERS_API = "https://ecom-api.costco.com/ebusiness/order/v1/orders/graphql"
PRODUCTS_API = "https://ecom-api.costco.com/ebusiness/product/v1/products/graphql"
SAVINGS_PAGE = "https://www.costco.com/warehouse-savings.html"

PRICE_ADJUST_DAYS = 30
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0 Safari/537.36"
)
