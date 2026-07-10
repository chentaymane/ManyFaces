"""Application paths and configuration."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Root data directory. Override with ANTIDETECT_DATA_DIR.
DATA_DIR = Path(os.environ.get("ANTIDETECT_DATA_DIR", Path.home() / ".antidetect")).resolve()

# Where each profile's persistent browser data (cookies, localStorage, cache) lives.
PROFILES_DIR = DATA_DIR / "profiles"

# SQLite database file holding profile metadata, fingerprints and proxies.
DB_PATH = DATA_DIR / "antidetect.db"


def _web_dir() -> Path:
    """Locate the web dashboard files, whether running from source or a frozen .exe.

    PyInstaller unpacks bundled data under sys._MEIPASS; in dev it sits next to the
    package. We check the frozen location first so the packaged app finds its UI.
    """
    if getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "web"
        if bundled.exists():
            return bundled
    return (Path(__file__).parent.parent / "web").resolve()


# Web dashboard static files.
WEB_DIR = _web_dir()

# Server bind.
HOST = os.environ.get("ANTIDETECT_HOST", "127.0.0.1")
PORT = int(os.environ.get("ANTIDETECT_PORT", "8000"))


def ensure_dirs() -> None:
    """Create all data directories if they do not exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)


def profile_data_dir(profile_id: str) -> Path:
    """Return (and create) the persistent user-data directory for a profile."""
    p = PROFILES_DIR / profile_id
    p.mkdir(parents=True, exist_ok=True)
    return p
