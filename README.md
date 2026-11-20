# costco-cli

Track your Costco purchases and catch price adjustments from the terminal. No phone app, no third-party service: receipts are pulled from your own Costco account into a local SQLite file.

```
$ costco drops
Claim by    Bought      Type       Item     Name                      Qty   Paid    Now   Back  Why
----------  ----------  ---------  -------  ------------------------  ---  ------  -----  -----  ------------------------------
2026-11-06  2026-10-07  warehouse  1707952  Kirkland Signature Oxi …    4  $13.59  $9.99  $14.40  coupon $3 off until 2026-11-15

$14.40 in possible price adjustments. Bring the receipt to the membership counter, or use costco.com chat for online orders.
```

## What it does

- **Syncs** warehouse receipts, gas receipts and costco.com orders, with line items and prices.
- **Finds price drops** on anything bought in the last 30 days, so you can claim Costco's price adjustment. Warehouse purchases are checked against the coupon book and your own later receipts; online purchases against today's costco.com price.
- **Tracks claims** you make or skip, and what Costco actually refunded.
- **Reports** purchase history per item, spending by month/type/department, and open return windows.
- **Runs daily** in the background and alerts you by macOS notification or email, with a weekly summary so silence means something.

## Install

Requires macOS, Python 3.10–3.13, and Google Chrome.

```bash
pipx install git+https://github.com/mdkocher/costco-cli
```

## Quick start

```bash
costco login              # a Chrome window opens; sign in to Costco yourself
costco drops              # anything to claim?
costco schedule install   # daily sync + alerts at 07:00
costco                    # where things stand: money waiting, last sync, alerts
```

`costco login` also runs the first sync, which pulls up to two years of history.

## Commands

| Command | |
|---|---|
| `costco` | Status: money waiting and deadlines, last sync, alert setup, background services. |
| `costco login` | Sign in once in a Chrome window. Run again when the session expires. |
| `costco sync [--days N]` | Pull new purchases, item names, online prices and the coupon book. |
| `costco drops [--all]` | Price drops you can claim. |
| `costco claim [item] [--skip] [--purchase ID] [--note ...]` | Record a price adjustment as claimed or skipped. Omit the item to pick from a list. |
| `costco refunds [--days 90] [--limit N]` | What you got back: adjustments you settled, and what Costco actually refunded. |
| `costco receipts [ID] [--type warehouse\|gas\|online] [--days N\|--since D\|--year Y]` | List purchases, or show one receipt/order (a suffix of the ID is enough). |
| `costco receipts --open [--days 30]` | Items still inside the price-adjustment window (`--days 90` for electronics returns). |
| `costco item <name or item #>` | Purchase history, plus any active coupon and current online price. |
| `costco spend [--by month\|year\|type\|where\|dept]` | Spending summary. |
| `costco coupons [search] [--mine]` | Current warehouse coupon book. |
| `costco finds [--all] [--limit N]` | New and discounted items other shoppers spotted, matched to what you buy. |
| `costco schedule [install\|uninstall\|status] [--time HH:MM]` | Daily background sync with alerts (launchd). |
| `costco notify [status\|email\|test\|macos]` | Where alerts go. |
| `costco summary [--send\|--days N\|--off]` | The periodic digest: print it, send it, or change how often. |
| `costco reminders [status\|start\|stop]` | The server and worker behind claim reminders. |

## Alerts

Alerts are macOS notifications by default. To get them by email too:

```bash
costco notify email   # prompts for addresses, SMTP host/port, and an app password
costco notify test
```

The password is kept in the macOS Keychain (`costco-cli-email`), never in a file. Gmail needs an [app password](https://myaccount.google.com/apppasswords); any SMTP server over implicit TLS works (default `smtp.gmail.com:465`). If sending fails, the alert still shows as a macOS notification.

You get an alert when new price drops appear, when the Costco sign-in expires, and when a daily run fails. A summary arrives weekly (`costco summary --days N` to change it), so no mail at all means something is wrong.

## Claims and reminders

`costco claim` records what you did about a drop. With no arguments it lists what's waiting and asks; with an item number it settles that one:

```bash
costco claim 1707952         # you got it back
costco claim 1707952 --skip  # not worth the trip
```

Settled items drop off `costco drops`; `costco refunds` shows them next to what Costco actually refunded. A skip is not forever: if the price falls further and the drop grows past what you skipped, the item comes back with a fresh alert and claim reminders.

To be reminded a week and a day before a claim deadline, run a [LittleHorse](https://littlehorse.io) server (needs Docker) and its worker. One command sets up both:

```bash
costco reminders start   # Docker container + a LaunchAgent worker that restarts itself
```

SQLite stays the record: claims work without the server, and the daily sync rebuilds the server's state if it is lost. Details in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Running on an always-on Mac

The full walkthrough, including email setup and troubleshooting, is in **[docs/SETUP.md](docs/SETUP.md)**. In short: install, `costco login` (needs the screen; Screen Sharing works), `costco notify email`, `costco schedule install`, and keep the Mac awake and logged in.

## How it works and privacy

Costco's sign-in blocks automated browsers, so you sign in yourself in a normal Chrome window with a dedicated profile. `costco` then reads the short-lived session token the website already keeps and calls the same endpoints as costco.com's Orders & Purchases page. Your password is never seen or stored, and the token is held only in memory during a sync.

Everything stays under `~/.local/share/costco-cli` (override with `COSTCO_CLI_HOME`): the database, the Chrome profile, and logs. To remove it all, run `costco notify macos` (deletes the Keychain entry) and delete that folder.

## Caveats

- Unofficial: Costco's website endpoints can change without notice.
- In-store prices aren't published, so warehouse drops only show up via the coupon book or a cheaper later receipt of your own. Online prices are never used to judge warehouse purchases. Gas is tracked for spending only.
- Price adjustments are at Costco's discretion. Check the item and date before heading to the counter.
- `costco finds` reads a public national feed with no prices or item numbers, so it's browse-only and never triggers alerts.
- macOS only for now. Set `COSTCO_CLI_CHROME` to use a different Chrome binary.

## Development

```bash
git clone https://github.com/mdkocher/costco-cli && cd costco-cli
python3.12 -m venv .venv && ./.venv/bin/pip install -e '.[dev]'
./.venv/bin/pytest
```

## License

MIT
