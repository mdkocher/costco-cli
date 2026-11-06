"""Running the background pieces: the task worker, the server, and knowing
whether the copy on your PATH is the code you actually wrote.

`schedule` already manages the daily job; this manages the two things that
have to be up for reminders to fire, plus the staleness check that caught me
three times in one afternoon.
"""
import hashlib
import json
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from . import config

WORKER_LABEL = "com.github.mdkocher.costco-cli.worker"
WORKER_PLIST = Path.home() / "Library/LaunchAgents" / f"{WORKER_LABEL}.plist"

CONTAINER = "lh-standalone"
IMAGE = "ghcr.io/littlehorse-enterprises/littlehorse/lh-standalone:1.3.1"
VOLUME = "lh-data"
# The entrypoint sets KAFKA_LOG_DIR but tests KAFKA_DATA_DIR, so without this
# it reformats Kafka with a new cluster id on every boot -- which makes the
# second start of a persistent volume fail outright. Not optional.
DOCKER_RUN = [
    "docker", "run", "--name", CONTAINER, "-d",
    "-v", f"{VOLUME}:/data", "-e", "KAFKA_DATA_DIR=/data/kafka",
    "-p", "2023:2023", "-p", "8080:8080", "-p", "9092:9092", IMAGE,
]


# ---------- the task worker ----------

def _worker_command():
    exe = shutil.which("costco-worker")
    return [exe] if exe else [sys.executable, "-m", "costco_cli.lh"]


def worker_install():
    """A LaunchAgent for the worker, mirroring `costco schedule`.

    KeepAlive is the point: the worker has died silently more than once, and a
    worker you believe is running is worse than one that is plainly off.
    ThrottleInterval stops it hot-looping when the server is down -- without it
    a stopped Docker means a restart every few seconds.
    """
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    cmd = _worker_command()
    plist = {
        "Label": WORKER_LABEL,
        "ProgramArguments": cmd,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 300,
        "StandardOutPath": str(config.LOG_DIR / "worker.log"),
        "StandardErrorPath": str(config.LOG_DIR / "worker.log"),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
    }
    WORKER_PLIST.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "unload", str(WORKER_PLIST)], check=False, capture_output=True)
    WORKER_PLIST.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "load", str(WORKER_PLIST)], check=True)
    return WORKER_PLIST, " ".join(cmd)


def worker_uninstall():
    if not WORKER_PLIST.exists():
        return False
    subprocess.run(["launchctl", "unload", str(WORKER_PLIST)], check=False, capture_output=True)
    WORKER_PLIST.unlink()
    return True


def worker_status():
    """(installed, loaded, running) for the worker."""
    installed = WORKER_PLIST.exists()
    loaded = subprocess.run(["launchctl", "list", WORKER_LABEL],
                            capture_output=True).returncode == 0
    running = subprocess.run(["pgrep", "-f", "costco-worker"],
                             capture_output=True).returncode == 0
    return installed, loaded, running


# ---------- the server ----------

def _docker(*args, **kw):
    return subprocess.run(["docker", *args], capture_output=True, text=True, **kw)


def server_state():
    """'running', 'stopped', 'absent', or 'no-docker'."""
    probe = _docker("ps", "-a", "--filter", f"name={CONTAINER}", "--format", "{{.State}}")
    if probe.returncode != 0:
        return "no-docker"
    state = probe.stdout.strip()
    if not state:
        return "absent"
    return "running" if state == "running" else "stopped"


def server_start():
    """Start the server, creating the container if it is not there.

    Returns what it did, so the caller can say something true rather than
    guessing.
    """
    state = server_state()
    if state == "no-docker":
        raise RuntimeError("Docker is not running. Start Docker Desktop first.")
    if state == "running":
        return "already running"
    if state == "stopped":
        out = _docker("start", CONTAINER)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip() or "docker start failed")
        return "started the existing container"
    out = _docker(*DOCKER_RUN[1:])
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "docker run failed")
    return "created and started the container"


def server_stop():
    if server_state() != "running":
        return False
    out = _docker("stop", CONTAINER)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "docker stop failed")
    return True


# ---------- is the installed copy the code you wrote? ----------

def _fingerprint(package_dir):
    """A hash over a package's .py files, so two copies can be compared."""
    h = hashlib.sha256()
    for f in sorted(Path(package_dir).rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        h.update(f.relative_to(package_dir).as_posix().encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


def _pipx_source():
    """Where pipx installed this from, if it did."""
    meta = (Path.home() / "Library/Application Support/pipx/venvs/costco-cli"
            / "pipx_metadata.json")
    try:
        return Path(json.loads(meta.read_text())["main_package"]["package_or_url"])
    except Exception:
        return None


def staleness():
    """(stale, source_path, installed_fp, source_fp), or None when not applicable.

    `git push` does not change what runs at 07:00 -- the scheduled job calls the
    pipx copy, which is a snapshot taken at install time. Comparing the two
    directly needs no build-time bookkeeping and cannot drift.
    """
    import costco_cli
    installed = Path(costco_cli.__file__).parent
    source_root = _pipx_source()
    if source_root is None:
        return None
    source = source_root / "src" / "costco_cli"
    if not source.is_dir() or source.resolve() == installed.resolve():
        return None            # editable install: there is only one copy
    a, b = _fingerprint(installed), _fingerprint(source)
    return a != b, source_root, a, b


def version_line(version):
    """What `costco --version` prints: the version, and whether it is current."""
    line = f"costco-cli {version}"
    s = staleness()
    if s is None:
        return line + " (running from source)"
    stale, source_root, installed_fp, source_fp = s
    line += f" (build {installed_fp})"
    if stale:
        line += (f"\nThis copy is out of date: {source_root} is at {source_fp}."
                 f"\nThe 07:00 job runs this copy, so update it with:"
                 f"\n  pipx install --force {source_root}")
    return line
