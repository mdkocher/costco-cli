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
  - warehouse purchases vs. a later receipt of your own for the same item at the same warehouse
  - online purchases vs. today's costco.com price
- **`item`** shows every time you bought something and what you paid.
- **`spend`** summarizes spending by month, year, type (warehouse / online / gas), location or department.
- **`returns`** lists items still inside the price-adjustment (30 day) or electronics return (90 day) window.
- **`coupons`** shows the current coupon book, or just the items you've bought before (`--mine`).
- **`claim`** / **`claims`** record what you did about an adjustment, so you can see what you actually got back.
- **`schedule`** runs a sync every morning and alerts you when there is money to claim, by macOS notification or email.

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
| `costco schedule [install\|uninstall\|status] [--time HH:MM]` | Daily background sync with alerts (launchd). |
| `costco claim [item] [--skip] [--purchase ID] [--note ...]` | Settle a price adjustment; omit the item to pick from a list. |
| `costco claims [--limit N]` | Adjustments you have already settled. |
| `costco notify [status\|email\|test\|macos]` | Choose where alerts go. |

## Alerts

By default alerts are macOS notifications on the machine running the schedule. That only reaches you at that Mac, so to get them by email as well:

```bash
costco notify email   # prompts for the addresses, SMTP host/port, and an app password (hidden)
# step-by-step walkthrough: docs/SETUP.md#4-set-up-email-alerts
costco notify test
```

The app password is stored in the macOS Keychain (service `costco-cli-email`), never in a file. The addresses and SMTP settings go in the local database. `costco notify macos` switches back and deletes the Keychain entry.

Gmail needs an [app password](https://myaccount.google.com/apppasswords) rather than your account password, and that requires two-step verification. Any SMTP server over implicit TLS works; the defaults are `smtp.gmail.com:465`.

If sending mail fails, the alert still appears as a macOS notification, so it is never silently lost.

You get an alert when new price drops show up, carrying the command to settle
them, and when the saved Costco sign-in has expired.

Two more, so that silence from the daily job means something rather than
nothing:

- **If a run fails** — a crash, or an unreachable LittleHorse server — it mails
  you rather than dying quietly in the log.
- **Once a week** it reports in: how far the sync has got, how many adjustments
  are waiting, how many you settled, and whether claim tracking is running. It
  goes on elapsed days rather than a fixed weekday, so a Mac asleep on Monday
  does not skip the week.

So no mail between one weekly note and the next means something is wrong,
rather than just a quiet month at Costco.

## Settling claims

A price adjustment has a deadline — 30 days from purchase — and `costco drops`
only tells you it exists. `costco claim` records what you did about it:

Run it with no arguments and it asks:

```
$ costco claim
Price adjustments waiting on you:

  1.   $12.00  Kirkland Oxi Powder    claim by 2026-11-25 (20d left)
  2.    $4.00  Head Gloves            claim by 2026-12-02 (27d left)

Which one? [1-2, Enter to quit] 1
Did you get the $12.00 back? [y/n, Enter to quit] y
Claimed: $12.00 on Kirkland Oxi Powder.
```

Or name the item directly, which is what the alerts tell you to do:

```bash
costco claim 1707952         # you got it back
costco claim 1707952 --skip  # not worth the trip
costco claims                # what you have settled, and what it came to
```

Settled items drop off `costco drops` (use `--all` to see them anyway). If the
same item is on more than one receipt, `claim` asks which with `--purchase`.

### Optional: reminders before the deadline

That much is just bookkeeping. To be *nudged* a week and a day before a window
closes, there is an optional [LittleHorse](https://littlehorse.io) workflow: one
run per claimable purchase line, holding a User Task open until you answer it.

It is optional on purpose. SQLite stays the record — `costco claims` never needs
a server — and the daily sync reconciles, so a server that loses everything is
rebuilt from your own database the next morning.

The SDK supports Python 3.10–3.13, so the worker needs its own interpreter:

```bash
python3.12 -m venv .venv-lh
./.venv-lh/bin/pip install -e ".[littlehorse]"
```

Start a server (the `KAFKA_DATA_DIR` setting is what makes its storage survive a
restart — without it the image reformats Kafka on every boot):

```bash
docker run --name lh-standalone -d -v lh-data:/data -e KAFKA_DATA_DIR=/data/kafka \
  -p 2023:2023 -p 8080:8080 -p 9092:9092 \
  ghcr.io/littlehorse-enterprises/littlehorse/lh-standalone:1.3.1
```

Then run the worker. It waits for the server to finish starting (Kafka Streams
needs about half a minute after the container answers), registers the workflow,
and handles the reminders until you stop it:

```bash
./.venv-lh/bin/costco-worker
waiting for the LittleHorse server...
registered costco-price-adjustment; serving costco-notify-drop and costco-record-claim
```

With the extra absent, `costco claim` still records the outcome and says plainly
that reminders will continue. Nothing else changes.

## Running on an always-on Mac

Full walkthrough, including email setup and troubleshooting: **[docs/SETUP.md](docs/SETUP.md)**.

Short version:

1. Install Google Chrome and Python 3 (`brew install python pipx`).
2. `pipx install git+https://github.com/mdkocher/costco-cli`
3. `costco login`. This needs the Mac's screen; Screen Sharing from another Mac works.
4. `costco notify email`
5. `costco schedule install --time 07:00`
6. Keep it from sleeping (System Settings > Energy > Prevent automatic sleeping). launchd also runs a missed job when the Mac wakes.

The job runs as a LaunchAgent, so your user needs to be logged in (after a restart, log in once). When the Costco sign-in eventually expires you'll get an alert; connect with Screen Sharing and run `costco login`.

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

Plus, if you use email alerts, one Keychain item named `costco-cli-email`.

Delete that folder (and run `costco notify macos` first if you set up email) to remove everything.

## Caveats

- Unofficial. This uses Costco's website endpoints, which can change without notice. It only reads your own account, a few requests per sync.
- In-store prices aren't published anywhere, so a warehouse drop is only visible two ways: the item is in the current coupon book, or you bought it again at the same warehouse for less. The costco.com price is a different price list (it carries shipping and handling, and normally runs above the warehouse price), so it is never used to judge a warehouse purchase. Gas is tracked for spending only.
- Price drops found by comparing your own receipts are reported in `costco drops` but only texted about outside seasonal departments, where the price tracks the market rather than a markdown. See `NO_ALERT_DEPARTMENTS` in `config.py`.
- Price adjustments are at Costco's discretion. Check the item and date before heading to the counter.
- macOS only for now (Chrome path, launchd, Keychain, notifications). Set `COSTCO_CLI_CHROME` to point at a different Chrome binary.

## Development

```bash
git clone https://github.com/mdkocher/costco-cli && cd costco-cli
python3 -m venv .venv && ./.venv/bin/pip install -e '.[dev]'
./.venv/bin/pytest
```

The tests for the optional LittleHorse layer skip themselves unless the extra is
installed. To run them too, use an interpreter the SDK supports (3.10–3.13):

```bash
python3.12 -m venv .venv-lh && ./.venv-lh/bin/pip install -e '.[dev,littlehorse]'
./.venv-lh/bin/pytest
```

## License

MIT
