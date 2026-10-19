"""Where alerts go: macOS notifications (default) or SMS via Twilio.

Twilio's auth token is kept in the macOS Keychain, never in the database or
any file. The account SID and phone numbers are stored in the local database.
"""
import subprocess

import requests

from . import db

KEYCHAIN_SERVICE = "costco-cli-twilio"
TWILIO_API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


class NotifyError(Exception):
    pass


def _macos(title, message):
    def q(s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    subprocess.run(["osascript", "-e", f"display notification {q(message)} with title {q(title)}"],
                   check=False, capture_output=True)


def save_token(sid, token):
    # Twilio SIDs and tokens are alphanumeric; checking keeps the stdin command below unambiguous.
    if not (sid.isalnum() and token.isalnum()):
        raise NotifyError("Account SID and auth token should be letters and digits only.")
    # Sent over stdin via `security -i` so the token never appears in the process list.
    r = subprocess.run(["security", "-i"], input=f"add-generic-password -U -s {KEYCHAIN_SERVICE} -a {sid} -w {token}\n",
                       capture_output=True, text=True)
    if r.returncode != 0 or "error" in (r.stderr + r.stdout).lower():
        raise NotifyError("Could not save the token to Keychain.")


def _token(sid):
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", sid, "-w"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise NotifyError("Twilio auth token not found in Keychain. Run: costco notify twilio")
    return r.stdout.strip()


def delete_token(sid):
    subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", sid],
                   check=False, capture_output=True)


def twilio_settings(con):
    keys = ("twilio_sid", "twilio_from", "twilio_to")
    vals = [db.get_meta(con, k) for k in keys]
    return dict(zip(("sid", "from", "to"), vals)) if all(vals) else None


def _twilio(con, title, message):
    s = twilio_settings(con)
    if not s:
        raise NotifyError("Twilio is not set up. Run: costco notify twilio")
    r = requests.post(TWILIO_API.format(sid=s["sid"]), auth=(s["sid"], _token(s["sid"])),
                      data={"From": s["from"], "To": s["to"], "Body": f"{title}: {message}"}, timeout=20)
    if r.status_code >= 300:
        try:
            detail = r.json().get("message")
        except ValueError:
            detail = r.text[:200]
        raise NotifyError(f"Twilio returned HTTP {r.status_code}: {detail}")


def channel(con):
    return db.get_meta(con, "notify_channel", "macos")


def send(con, title, message):
    """Send an alert on the configured channel. Falls back to a macOS
    notification if SMS fails, so an alert is never silently lost."""
    if channel(con) == "twilio":
        try:
            _twilio(con, title, message)
            return "twilio"
        except (NotifyError, requests.RequestException) as e:
            _macos(title, f"{message} (SMS failed: {e})")
            raise
    _macos(title, message)
    return "macos"
