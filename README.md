# costco-cli

Track your Costco purchases and catch price adjustments from the terminal. No phone app, no third-party service: your receipts are pulled from your own Costco account into a local SQLite file.

```
$ costco drops
Claim by    Bought      Type       Item     Name                      Qty   Paid    Now   Back  Why
----------  ----------  ---------  -------  ------------------------  ---  ------  -----  -----  ------------------------------
2026-11-06  2026-10-07  warehouse  1707952  Kirkland Signature Oxi …    4  $13.59  $9.99  $14.40  coupon $3 off until 2026-11-15

$14.40 in possible price adjustments. Bring the receipt to the membership counter, or use costco.com chat for online orders.
```

## What it does

- **Syncs** warehouse receipts, gas receipts and costco.com orders (with line items and prices) into `~/.local/share/costco-cli/costco.db`.
- **`drops`** finds things you bought in the last 30 days that are now cheaper, so you can claim Costco's price adjustment:
  - warehouse purchases vs. the current warehouse coupon book
  - online purchases vs. today's costco.com price
- **`item`** shows every time you bought something and what you paid.
- **`spend`** summarizes spending by month, year, type (warehouse / online / gas), location or department.
- **`returns`** lists items still inside the price-adjustment (30 day) or electronics return (90 day) window.
- **`coupons`** shows the current coupon book, or just the items you've bought before (`--mine`).
- **`schedule`** runs a sync every morning and sends a macOS notification when there is money to claim.

## Install

Requires macOS, Python 3.10+, and Google Chrome.

```bash
pipx install git+https://github.com/mdkocher/costco-cli
```

Or with pip:

```bash
pip install git+https://github.com/mdkocher/costco-cli
```

## Quick start

```bash
costco login              # a Chrome window opens; sign in to Costco yourself
costco receipts           # last 90 days of purchases
costco drops              # anything to claim?
costco schedule install   # daily sync + notifications at 07:00
```

`costco login` also runs the first sync, which pulls up to two years of history.

## Commands

| Command | |
|---|---|
| `costco login` | Sign in once in a Chrome window. Run again when the saved session expires. |
| `costco sync [--days N]` | Pull new purchases, item names, online prices and the coupon book. |
| `costco receipts [ID] [--type warehouse\|gas\|online] [--days N\|--since D\|--year Y]` | List purchases, or show one receipt/order (a suffix of the ID is enough). |
| `costco item <name or item #>` | Purchase history, plus any active coupon and the current online price. |
| `costco spend [--by month\|year\|type\|where\|dept]` | Spending summary. |
| `costco returns [--days 30]` | Items still inside a return/adjustment window. |
| `costco drops` | Price drops you can claim. |
| `costco coupons [search] [--mine]` | Current warehouse coupon book. |
| `costco schedule [install\|uninstall\|status] [--time HH:MM]` | Daily background sync with notifications (launchd). |

## How it works

Costco's sign-in page blocks automated browsers, so `costco` never drives the login. It starts a normal Chrome window with a dedicated profile (`~/.local/share/costco-cli/chrome-profile`) and you sign in yourself. Afterwards it briefly connects to that Chrome over a local DevTools port to read the short-lived session token the costco.com website already keeps, then calls the same JSON endpoints the website's Orders & Purchases page uses. Later syncs reuse the saved profile headlessly.

- Your password is never seen, typed, or stored by this tool.
- The session token is held in memory only for the length of a sync.
- Item names and online prices come from Costco's public product endpoint. The coupon book comes from the public warehouse-savings page.

## Privacy

Everything stays on your machine under `~/.local/share/costco-cli` (override with `COSTCO_CLI_HOME`):

```
costco.db         your purchases, item names, prices and coupons
chrome-profile/   the Chrome profile holding your Costco sign-in
logs/daily.log    output of the scheduled job
```

Delete that folder to remove everything.

## Caveats

- Unofficial. This uses Costco's website endpoints, which can change without notice. It only reads your own account, a few requests per sync.
- In-store prices aren't published, so warehouse drops only cover items in the current coupon book. Gas is tracked for spending only.
- Price adjustments are at Costco's discretion. Check the item and date before heading to the counter.
- macOS only for now (Chrome path, launchd, notifications). Set `COSTCO_CLI_CHROME` to point at a different Chrome binary.

## Development

```bash
git clone https://github.com/mdkocher/costco-cli && cd costco-cli
pip install -e '.[dev]'
pytest
```

## License

MIT
