"""Where alerts go: macOS notifications (default) or email.

The sending account's app password is kept in the macOS Keychain, never in the
database or any file. The addresses and SMTP settings are stored in the local
database.
"""
import re
import smtplib
import subprocess
from email.message import EmailMessage

from . import db

KEYCHAIN_SERVICE = "costco-cli-email"
DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 465
EMAIL_KEYS = ("email_from", "email_to", "smtp_host", "smtp_port")

# Settings left behind by the Twilio SMS alerts that costco-cli used to support.
LEGACY_KEYCHAIN_SERVICE = "costco-cli-twilio"
LEGACY_KEYS = ("twilio_sid", "twilio_from", "twilio_to")

ADDRESS_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


class NotifyError(Exception):
    pass


def _macos(title, message):
    def q(s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    subprocess.run(["osascript", "-e", f"display notification {q(message)} with title {q(title)}"],
                   check=False, capture_output=True)


def save_password(address, password):
    """Store the app password in the login Keychain, keyed by sending address."""
    # Gmail displays app passwords in groups of four, so accept the spaced form.
    password = password.replace(" ", "")
    if not ADDRESS_RE.match(address):
        raise NotifyError(f"{address!r} does not look like an email address.")
    # App passwords are alphanumeric; checking keeps the stdin command below unambiguous.
    if not password.isalnum():
        raise NotifyError("An app password should be letters and digits only.")
    # Sent over stdin via `security -i` so the password never appears in the process list.
    r = subprocess.run(["security", "-i"],
                       input=f"add-generic-password -U -s {KEYCHAIN_SERVICE} -a {address} -w {password}\n",
                       capture_output=True, text=True)
    if r.returncode != 0 or "error" in (r.stderr + r.stdout).lower():
        raise NotifyError("Could not save the app password to Keychain.")


def _password(address):
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", address, "-w"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise NotifyError("Email app password not found in Keychain. Run: costco notify email")
    return r.stdout.strip()


def delete_password(address):
    subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", address],
                   check=False, capture_output=True)


def email_settings(con):
    """The configured email settings, or None if email is not set up."""
    vals = {k: db.get_meta(con, k) for k in EMAIL_KEYS}
    if not (vals["email_from"] and vals["email_to"]):
        return None
    vals["smtp_host"] = vals["smtp_host"] or DEFAULT_SMTP_HOST
    try:
        vals["smtp_port"] = int(vals["smtp_port"] or DEFAULT_SMTP_PORT)
    except ValueError:
        raise NotifyError(f"smtp_port is not a number: {vals['smtp_port']!r}. Run: costco notify email")
    return vals


def _email(con, title, message):
    s = email_settings(con)
    if not s:
        raise NotifyError("Email is not set up. Run: costco notify email")
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = s["email_from"]
    msg["To"] = s["email_to"]
    msg.set_content(message)
    password = _password(s["email_from"])
    try:
        with smtplib.SMTP_SSL(s["smtp_host"], s["smtp_port"], timeout=20) as server:
            server.login(s["email_from"], password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise NotifyError("The mail server rejected the app password. Run: costco notify email") from e
    except (smtplib.SMTPException, OSError) as e:
        raise NotifyError(f"Could not send mail: {e}") from e


def channel(con):
    return db.get_meta(con, "notify_channel", "macos")


def send(con, title, message):
    """Send an alert on the configured channel. Falls back to a macOS
    notification if email fails, so an alert is never silently lost."""
    if channel(con) == "email":
        try:
            _email(con, title, message)
            return "email"
        except NotifyError as e:
            _macos(title, f"{message} (email failed: {e})")
            raise
    _macos(title, message)
    return "macos"


def migrate_legacy_channel(con):
    """Clear settings left over from the Twilio SMS alerts that were removed.

    Switches the channel back to macOS notifications and deletes the orphaned
    Keychain item, so no live credential is left behind. Returns True if
    anything was cleaned up, so the caller can mention it once.
    """
    sid = db.get_meta(con, "twilio_sid")
    stale = [k for k in LEGACY_KEYS if db.get_meta(con, k) is not None]
    was_sms = channel(con) == "twilio"
    if not stale and not was_sms:
        return False
    if sid:
        subprocess.run(["security", "delete-generic-password", "-s", LEGACY_KEYCHAIN_SERVICE, "-a", sid],
                       check=False, capture_output=True)
    for key in stale:
        con.execute("DELETE FROM meta WHERE key = ?", (key,))
    if was_sms:
        db.set_meta(con, "notify_channel", "macos")
    con.commit()
    return True
