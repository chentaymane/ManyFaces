"""Desktop app entry point.

Runs the anti-detect manager as a native desktop window (via pywebview / the OS
WebView2 runtime on Windows) instead of a browser tab. The FastAPI server runs in a
background thread inside the same process.

    python desktop.py
"""
from __future__ import annotations

import os
import shutil
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


def _choose_port(host: str, preferred: int) -> int:
    """Return the preferred port if free, otherwise an OS-assigned free one.

    A leftover/zombie instance holding the default port used to make the server fail
    to bind while the window silently connected to the STALE server — the root of the
    "stuck on setup screen" bug. Picking a free port makes that impossible.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


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

    # On Windows, force the WebView2 (Edge) engine to use a FRESH cache each launch
    # so it can never serve a stale page. Harmless/irrelevant on Linux/macOS (which
    # use GTK/Qt WebKit backends), so we scope it to Windows.
    if sys.platform == "win32":
        wv2_dir = config.DATA_DIR / "webview2"
        try:
            if wv2_dir.exists():
                shutil.rmtree(wv2_dir, ignore_errors=True)
            wv2_dir.mkdir(parents=True, exist_ok=True)
            os.environ["WEBVIEW2_USER_DATA_FOLDER"] = str(wv2_dir)
        except Exception:  # noqa: BLE001 - non-fatal; app still runs with default cache
            pass

    # Claim a port up front (falling back to a free one if the default is taken) so
    # we never collide with a leftover instance and connect to its stale server.
    config.PORT = _choose_port(config.HOST, config.PORT)

    threading.Thread(target=_server, daemon=True).start()
    if not _wait_for_port(config.HOST, config.PORT):
        print(f"Server failed to start on {config.HOST}:{config.PORT}", file=sys.stderr)
        return
    print(f"Anti-Detect Manager running at http://{config.HOST}:{config.PORT}", flush=True)

    import webview  # imported here so `--help`/import errors are clearer

    # Cache-bust the URL each launch so the WebView2/Edge engine can never serve a
    # stale page (e.g. a first-run "engine not installed" state cached before the
    # browser finished downloading).
    url = f"http://{config.HOST}:{config.PORT}/?v={int(time.time())}"
    webview.create_window(
        "Anti-Detect Browser Manager",
        url,
        width=1280,
        height=860,
        min_size=(960, 640),
        confirm_close=True,
    )
    # private_mode=True (default) keeps no persistent cache between runs; gui=None
    # auto-selects the platform backend (EdgeChromium/WebView2 on Windows).
    webview.start(private_mode=True)


if __name__ == "__main__":
    main()
