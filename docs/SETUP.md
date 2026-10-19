# Setting up costco-cli on an always-on Mac

This guide sets up `costco` on a Mac that stays on (a Mac mini, an old MacBook with the lid open, etc.) so it syncs every morning and texts you when there's a price adjustment to claim.

Time needed: about 30 minutes, most of it Twilio signup.

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

The daily job runs inside your user session, and reads the Twilio token from your login Keychain. So your user has to be logged in.

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

## 4. Set up text alerts with Twilio

### 4a. Create a Twilio account

1. Sign up at [twilio.com/try-twilio](https://www.twilio.com/try-twilio) and verify your email and your own mobile number.
2. Open the [Twilio Console](https://console.twilio.com). On the dashboard, under **Account Info**, you'll see:
   - **Account SID**: starts with `AC`, 34 characters
   - **Auth Token**: click to reveal. Treat it like a password.

Keep that page open; you'll paste both in step 4c.

### 4b. Get a number that can send texts

1. In the Console, go to **Phone Numbers > Manage > Buy a number** (a trial account gets credit for one).
2. Pick a number with **SMS** capability. Note it in `+1XXXXXXXXXX` form.

**Trial accounts** can only text numbers listed under **Phone Numbers > Manage > Verified Caller IDs**. Your own mobile is usually there already from signup. Trial messages start with "Sent from your Twilio trial account".

**US delivery.** Carriers block or filter texts from unregistered numbers. To get reliable delivery after the trial:
- **Toll-free number + Toll-Free Verification** is usually the simplest for a personal alert like this. Buy a toll-free number and submit verification in the Console (search the Console for "Toll-Free Verification").
- **A2P 10DLC registration** is the route for a regular local number. Twilio has a "Sole Proprietor" option for individuals.

Twilio's menus move around; if a path above doesn't match, use the Console's search box.

### 4c. Connect costco-cli to Twilio

On the always-on Mac:

```bash
costco notify twilio
```

It asks for four things:

| Prompt | What to enter |
|---|---|
| Account SID | the `AC...` value from the Console |
| Twilio phone number to send from | the number from step 4b, like `+15415550123` |
| Your mobile number | where alerts should go, like `+15415550199` |
| Auth token (hidden) | paste the Auth Token; nothing shows as you paste, that's normal |

It then sends a test text: "costco-cli: SMS alerts are set up."

Where things are stored:
- **Auth Token**: macOS Keychain on that Mac, item `costco-cli-twilio`. Not in any file, not in the repo. You can see it in the Keychain Access app.
- **SID and phone numbers**: the local database at `~/.local/share/costco-cli/costco.db`.

Useful commands:

```bash
costco notify          # show where alerts go
costco notify test     # send a test alert
costco notify twilio   # run setup again (e.g. after rotating the Auth Token)
costco notify macos    # stop texting; remove Twilio settings and the Keychain item
```

If you ever rotate the Auth Token in the Twilio Console, run `costco notify twilio` again with the new one.

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
| `Twilio returned HTTP 401` | Wrong SID or Auth Token. Run `costco notify twilio` again. |
| `Twilio returned HTTP 400` mentioning an unverified number | Trial account: add your mobile under Verified Caller IDs, or upgrade the account. |
| Test says sent but no text arrives | Carrier filtering of an unregistered number. Complete Toll-Free Verification or A2P 10DLC (step 4b). Check **Monitor > Logs > Messaging** in the Twilio Console for the delivery status. |
| `Twilio auth token not found in Keychain` | The job ran while you were logged out, or the item was deleted. Log in to the Mac; if it persists, run `costco notify twilio`. |
| No alerts in the morning | `costco schedule status` should say loaded. Check the log file above, and that the Mac wasn't logged out. |
