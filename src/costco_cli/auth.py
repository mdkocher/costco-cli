"""Costco session handling.

Costco's sign-in page rejects browsers driven by automation, so we launch a
plain Chrome with its own profile and only attach (over the local DevTools
port) after the user is signed in, to read the short-lived idToken the
website keeps in localStorage. Nothing is typed for the user and no password
is ever seen or stored by this tool.
"""
import base64
import json
import os
import subprocess
import time
from dataclasses import dataclass

import requests
from playwright.sync_api import sync_playwright

from . import config

CDP = f"http://127.0.0.1:{config.CDP_PORT}"


class AuthError(Exception):
    pass


@dataclass
class Session:
    token: str
    client_id: str

    def __repr__(self):  # keep the token out of tracebacks and logs
        return "Session(<redacted>)"


def _token_exp(token):
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))["exp"]


def _launch_chrome(headless):
    if not os.path.exists(config.CHROME):
        raise AuthError(f"Chrome not found at {config.CHROME}. Set COSTCO_CLI_CHROME.")
    try:
        requests.get(f"{CDP}/json/version", timeout=1)
        raise AuthError(f"Something is already listening on port {config.CDP_PORT}. Set COSTCO_CLI_CDP_PORT.")
    except requests.RequestException:
        pass
    config.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    args = [
        config.CHROME,
        f"--user-data-dir={config.PROFILE_DIR}",
        f"--remote-debugging-port={config.CDP_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        args.append("--headless=new")
    proc = subprocess.Popen(args + [config.ACCOUNT_PAGE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        try:
            requests.get(f"{CDP}/json/version", timeout=1)
            return proc
        except requests.RequestException:
            time.sleep(0.25)
    proc.terminate()
    raise AuthError("Chrome did not open its debugging port")


def _tab_urls():
    """Tab URLs from the DevTools HTTP endpoint. Listing does not attach to pages."""
    try:
        tabs = requests.get(f"{CDP}/json/list", timeout=2).json()
    except requests.RequestException:
        return None
    return [t["url"] for t in tabs if t.get("type") == "page"]


def _read_session():
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        try:
            for ctx in browser.contexts:
                for pg in ctx.pages:
                    if not pg.url.startswith("https://www.costco.com"):
                        continue
                    try:
                        ls = pg.evaluate("() => ({t: localStorage.getItem('idToken'), c: localStorage.getItem('clientID')})")
                    except Exception:
                        continue
                    if ls["t"] and ls["c"] and _token_exp(ls["t"]) - time.time() > 120:
                        return Session(ls["t"], ls["c"])
        finally:
            browser.close()  # disconnects only; we terminate Chrome ourselves
    return None


def get_session(interactive=False, timeout_s=None):
    """Return a fresh Session.

    interactive=True shows a Chrome window and waits for the user to sign in.
    Otherwise Chrome runs headless on the saved profile and fails fast if the
    saved sign-in has expired.
    """
    timeout_s = timeout_s or (15 * 60 if interactive else 45)
    proc = _launch_chrome(headless=not interactive)
    deadline = time.time() + timeout_s
    try:
        while time.time() < deadline:
            urls = _tab_urls()
            if urls is None:
                raise AuthError("Chrome was closed before sign-in finished.")
            on_signin = any("signin.costco.com" in u for u in urls)
            if on_signin and not interactive:
                raise AuthError("Saved Costco sign-in has expired. Run: costco login")
            # Attach only once no sign-in page is open, so the login itself is untouched.
            if not on_signin and any(u.startswith("https://www.costco.com/myaccount") for u in urls):
                time.sleep(2 if interactive else 1)
                session = _read_session()
                if session:
                    return session
            time.sleep(1)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    raise AuthError("Timed out waiting for a Costco session." + ("" if interactive else " Run: costco login"))
