"""Desktop app entry point.

Runs the anti-detect manager as a native desktop window (via pywebview / the OS
WebView2 runtime on Windows) instead of a browser tab. The FastAPI server runs in a
background thread inside the same process.

    python desktop.py
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time

# In a windowed (--noconsole) PyInstaller build, stdout/stderr are None. Libraries
# that write progress bars (e.g. the Camoufox downloader's tqdm) would crash on that,
# so redirect to the null device before importing anything that might print.
if sys.stdout is None or sys.stderr is None:
    _null = open(os.devnull, "w")
    sys.stdout = sys.stdout or _null
    sys.stderr = sys.stderr or _null

import uvicorn

from antidetect import config


def _server() -> None:
    # Pass the app OBJECT, not an "antidetect.api:app" import string: uvicorn's
    # string-based import fails inside a frozen PyInstaller build.
    from antidetect.api import app

    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")


def _wait_for_port(host: str, port: int, timeout: float = 20.0) -> bool:
    """Block until the server accepts connections (so the window opens on a live UI)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


def main() -> None:
    config.ensure_dirs()

    threading.Thread(target=_server, daemon=True).start()
    _wait_for_port(config.HOST, config.PORT)

    import webview  # imported here so `--help`/import errors are clearer

    webview.create_window(
        "Anti-Detect Browser Manager",
        f"http://{config.HOST}:{config.PORT}",
        width=1280,
        height=860,
        min_size=(960, 640),
        confirm_close=True,
    )
    # http=True keeps localStorage/session working; gui=None auto-selects the
    # platform backend (EdgeChromium/WebView2 on Windows).
    webview.start()


if __name__ == "__main__":
    main()
