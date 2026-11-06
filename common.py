"""Shared helpers: config loading, session dirs, CSV writing, stop flag."""
import configparser
import csv
import datetime as dt
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
SESSIONS = ROOT / "sessions"
ACTIVE_FILE = SESSIONS / "ACTIVE"

DEFAULTS = {
    "modem": {
        "url": "http://192.168.100.1/",
        "username": "admin",
        "password": "",
        "interval": "1.0",
        "timeout": "5",
        "status_every": "1",
    },
    "ping": {
        # "auto" = default gateway from the routing table (falls back to the modem IP)
        "targets": "auto, 192.168.100.1, 1.1.1.1",
        "interval": "0.5",
        "timeout_ms": "1000",
    },
    "cs2": {
        # empty = auto-detect via Steam libraryfolders.vdf
        "console_log": "",
    },
    "report": {
        "window_s": "5",
        "ul_smoothed_bad_pct": "5",
        "ul_missed_bad_pct": "1",
        "ul_jitter_p99_bad_ms": "15",
        "ping_jitter_bad_ms": "10",
        "ping_loss_bad_pct": "2",
        # which ping target to use for the fallback classification ("" = last public one)
        "ping_target": "",
    },
    "netmode": {
        "reference_request": "samples/netmode_request.txt",
        "lte": "03",
        "auto": "00",
        "nr": "08",
    },
}


def load_config():
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_dict(DEFAULTS)
    cp.read([ROOT / "config.ini", ROOT / "config.local.ini"], encoding="utf-8")
    env_pw = os.environ.get("HUAWEI_PASSWORD")
    if env_pw:
        cp["modem"]["password"] = env_pw
    return cp


def now():
    t = time.time()
    return t, dt.datetime.fromtimestamp(t).isoformat(timespec="milliseconds")


def new_session_dir(tag=""):
    name = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + (f"-{tag}" if tag else "")
    d = SESSIONS / name
    d.mkdir(parents=True, exist_ok=True)
    ACTIVE_FILE.write_text(str(d), encoding="utf-8")
    return d


def latest_session():
    if not SESSIONS.exists():
        return None
    dirs = sorted(p for p in SESSIONS.iterdir() if p.is_dir())
    return dirs[-1] if dirs else None


class CsvLog:
    """Thread-safe append-only CSV with a fixed header; flushes every row."""

    def __init__(self, path, fields):
        self.fields = fields
        new = not Path(path).exists()
        self.f = open(path, "a", newline="", encoding="utf-8")
        self.w = csv.DictWriter(self.f, fieldnames=fields, extrasaction="ignore")
        if new:
            self.w.writeheader()
        self.lock = threading.Lock()

    def write(self, row):
        with self.lock:
            self.w.writerow(row)
            self.f.flush()

    def close(self):
        self.f.close()


class Stopper:
    """Stops on Ctrl+C (event), on `diag.py stop` (STOP file), or on a deadline."""

    def __init__(self, session_dir, duration=None):
        self.ev = threading.Event()
        self.flag = Path(session_dir) / "STOP"
        self.deadline = time.time() + duration if duration else None

    def stopped(self):
        if self.ev.is_set():
            return True
        if self.flag.exists() or (self.deadline and time.time() >= self.deadline):
            self.ev.set()
        return self.ev.is_set()

    def wait(self, seconds):
        """Sleep up to `seconds`; returns True if we should stop."""
        end = time.time() + seconds
        while time.time() < end:
            if self.stopped():
                return True
            self.ev.wait(min(0.2, max(0, end - time.time())))
        return self.stopped()


def read_csv(path):
    if not Path(path).exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
