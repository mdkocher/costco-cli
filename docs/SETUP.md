# Setting up costco-cli on an always-on Mac

This guide sets up `costco` on a Mac that stays on (a Mac mini, an old MacBook with the lid open, etc.) so it syncs every morning and alerts you when there's a price adjustment to claim.

Time needed: about 15 minutes.

What you'll end up with:

- `costco` installed on the always-on Mac
- that Mac signed in to your Costco account (in its own Chrome profile)
- a daily job at 7:00 that syncs and texts you about price drops
- a text if the Costco sign-in ever expires, so you know to sign in again

---

## 1. Prepare the Mac

### Turn on Screen Sharing (so you can manage it from your laptop)

On the always-on Mac: **System Settings > General > Sharing > Screen Sharing** → on.

From your laptop you can then connect with Finder (sidebar > Network > the Mac > Share Screen) or:

```bash
open vnc://mac-mini.local
```

(Use that Mac's name, shown in **System Settings > General > Sharing** under "Local hostname".)

Also turn on **Remote Login** in the same Sharing pane if you want to SSH in (`ssh you@mac-mini.local`). Everything below works over SSH except `costco login`, which needs the screen.

### Keep it awake

- **System Settings > Energy** (or **Battery > Options** on a laptop): turn on **Prevent automatic sleeping when the display is off**, and **Start up automatically after a power failure** if offered.
- The display can sleep; the Mac must not.

If the Mac is asleep at 7:00, launchd runs the job when it wakes, so a missed morning isn't lost.

### Stay logged in

The daily job runs inside your user session, and reads the email app password from your login Keychain. So your user has to be logged in.

- After a restart, log in once (Screen Sharing works for this).
- Optional: **System Settings > Users & Groups > Automatically log in as** your user. This isn't available with FileVault on, and it means anyone with physical access gets in. Only do it if the Mac is somewhere you trust.

---

## 2. Install the software

Open Terminal on the always-on Mac (or SSH in).

### Homebrew

Skip if `brew --version` already works.

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Follow the "Next steps" it prints (it tells you to add Homebrew to your PATH), then open a new Terminal window.

### Google Chrome

Install from [google.com/chrome](https://www.google.com/chrome/) or:

```bash
brew install --cask google-chrome
```

It must end up at `/Applications/Google Chrome.app`. (If it's somewhere else, set `COSTCO_CLI_CHROME` to the binary path.)

### Python and pipx

```bash
brew install python pipx
```

```bash
pipx ensurepath
```

Open a new Terminal window so the PATH change takes effect.

### costco-cli

```bash
pipx install git+https://github.com/mdkocher/costco-cli
```

Check it:

```bash
costco --version
```

---

## 3. Sign in to Costco

This step needs the Mac's screen, so do it at the Mac or over Screen Sharing (not SSH).

```bash
costco login
```

A Chrome window opens on Costco's Orders & Purchases page. Sign in normally, including any verification code. Once you land on your account page, the window closes by itself and the first sync starts. It pulls up to two years of warehouse receipts, gas receipts and online orders; expect under a minute.

Check it worked:

```bash
costco receipts
```

The sign-in is saved in `~/.local/share/costco-cli/chrome-profile`. Later syncs reuse it without opening a window.

---

## 4. Set up email alerts

Alerts are macOS notifications by default, which only reach you at that Mac. Email also reaches your phone, which matters because a price adjustment has a deadline.

### 4a. Get an app password

For Gmail, you need an **app password** rather than your account password:

1. Two-step verification must be on: [myaccount.google.com/security](https://myaccount.google.com/security).
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Create one named `costco-cli`. Google shows 16 characters in four groups — you can paste it with or without the spaces.

Any SMTP server that accepts implicit TLS works just as well; you'll need its host and port instead. Common ones:

| Provider | Host | Port |
|---|---|---|
| Gmail | `smtp.gmail.com` | 465 |
| iCloud Mail | `smtp.mail.me.com` | 587 |
| Fastmail | `smtp.fastmail.com` | 465 |

### 4b. Connect costco-cli to email

On the always-on Mac:

```bash
costco notify email
```

It asks for five things:

| Prompt | What to enter |
|---|---|
| Send from | the mailbox doing the sending, like `you@gmail.com` |
| Send to | where alerts should go; press Return to use the same address |
| SMTP host | press Return for `smtp.gmail.com` |
| SMTP port | press Return for `465` |
| App password (hidden) | paste the app password; nothing shows as you paste, that's normal |

It then sends a test message: "costco-cli: Email alerts are set up."

Where things are stored:
- **App password**: macOS Keychain on that Mac, item `costco-cli-email`. Not in any file, not in the repo. You can see it in the Keychain Access app.
- **Addresses and SMTP settings**: the local database at `~/.local/share/costco-cli/costco.db`.

Useful commands:

```bash
costco notify          # show where alerts go
costco notify test     # send a test alert
costco notify email    # run setup again (e.g. after rotating the app password)
costco notify macos    # stop emailing; remove the settings and the Keychain item
```

If you revoke the app password, run `costco notify email` again with a new one.

If sending fails for any reason, the alert still appears as a macOS notification on that Mac, so nothing is silently lost.

---

## 5. Turn on the daily job

```bash
costco schedule install --time 07:00
```

```bash
costco schedule status
```

Each morning it runs `costco daily`, which:
1. syncs new purchases, item names, online prices and the coupon book
2. checks for price drops on anything bought in the last 30 days
3. texts you about new drops (each drop is only texted once)
4. texts you if the Costco sign-in has expired

Run the same thing by hand any time to check it end to end:

```bash
costco daily
```

The log is at `~/.local/share/costco-cli/logs/daily.log`:

```bash
tail -n 20 ~/.local/share/costco-cli/logs/daily.log
```

---

## Day to day

From your laptop you can SSH in and use any command:

```bash
ssh you@mac-mini.local costco drops
```

**When you get "Costco sign-in expired":** Screen Share into the Mac, open Terminal, run `costco login`, sign in. Done.

**Claiming a price drop:** warehouse purchases at the membership counter with the receipt or your membership card (they can look the purchase up); online orders through costco.com customer service chat. Costco's price adjustment window is 30 days from purchase.

---

## Updating

```bash
pipx reinstall costco-cli
```

This pulls the latest version from GitHub. Your data, sign-in and schedule are kept.

## Removing it

```bash
costco schedule uninstall
```

```bash
costco notify macos
```

```bash
pipx uninstall costco-cli
```

```bash
rm -rf ~/.local/share/costco-cli
```

The last command deletes your purchase database and the saved Costco sign-in.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `costco: command not found` | Run `pipx ensurepath` and open a new Terminal window. |
| `Chrome not found at ...` | Install Chrome in /Applications, or set `COSTCO_CLI_CHROME` to its binary path. |
| `Something is already listening on port 9333` | Another program uses that port. Set `COSTCO_CLI_CDP_PORT` to another number, e.g. `export COSTCO_CLI_CDP_PORT=9444` in `~/.zprofile`. |
| Costco sign-in page won't accept your login | Make sure you're signing in inside the window `costco login` opened. If it keeps failing, quit all Chrome windows and run `costco login` again. |
| `Saved Costco sign-in has expired` | Run `costco login` at the Mac's screen. |
| `The mail server rejected the app password` | The app password is wrong or was revoked. Create a new one and run `costco notify email` again. |
| `Could not send mail: ...` | Check the SMTP host and port with `costco notify`. Gmail wants `smtp.gmail.com:465`; some providers want 587. |
| Test says sent but no mail arrives | Check the spam folder, and that **Send to** is the address you expect (`costco notify` shows it). |
| `Email app password not found in Keychain` | The job ran while you were logged out, or the item was deleted. Log in to the Mac; if it persists, run `costco notify email`. |
| No alerts in the morning | `costco schedule status` should say loaded. Check the log file above, and that the Mac wasn't logged out. |
