"""Daily background sync via launchd, with macOS notifications for price drops."""
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from . import config

LABEL = "com.github.mdkocher.costco-cli"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def notify(title, message):
    script = f'display notification {_quote(message)} with title {_quote(title)}'
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def _quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _costco_command():
    exe = shutil.which("costco")
    if exe:
        return [exe]
    return [sys.executable, "-m", "costco_cli"]


def install(hour, minute):
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    cmd = _costco_command()
    plist = {
        "Label": LABEL,
        "ProgramArguments": cmd + ["daily"],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(config.LOG_DIR / "daily.log"),
        "StandardErrorPath": str(config.LOG_DIR / "daily.log"),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "unload", str(PLIST)], check=False, capture_output=True)
    PLIST.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "load", str(PLIST)], check=True)
    return PLIST, " ".join(cmd + ["daily"])


def uninstall():
    if not PLIST.exists():
        return False
    subprocess.run(["launchctl", "unload", str(PLIST)], check=False, capture_output=True)
    PLIST.unlink()
    return True


def status():
    if not PLIST.exists():
        return None
    plist = plistlib.loads(PLIST.read_bytes())
    t = plist["StartCalendarInterval"]
    loaded = subprocess.run(["launchctl", "list", LABEL], capture_output=True).returncode == 0
    return f"{t['Hour']:02d}:{t['Minute']:02d}", loaded
