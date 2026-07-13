"""Entry point: start the management server and open the dashboard in your browser.

    python run.py            # start server and open the dashboard
    python run.py --no-open  # start server only, don't open the browser

This opens the app in your normal web browser (Chrome/Edge/etc.), which avoids the
embedded-window caching issues some setups hit. It auto-picks a free port so it can
never collide with a leftover instance.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from antidetect import config


def _choose_port(host: str, preferred: int) -> int:
    """Return the preferred port if free, otherwise an OS-assigned free one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _open_browser(url: str) -> None:
    webbrowser.open(url)


def main() -> None:
    config.ensure_dirs()
    config.PORT = _choose_port(config.HOST, config.PORT)
    # Cache-bust the URL so the browser can never show a stale first-run page.
    url = f"http://{config.HOST}:{config.PORT}/?v={int(time.time())}"

    if "--no-open" not in sys.argv:
        threading.Timer(1.5, _open_browser, args=(url,)).start()

    print("=" * 56)
    print("  ManyFaces is running.")
    print(f"  Open this in your browser:  http://{config.HOST}:{config.PORT}")
    print("  Keep this window open while using the app. Ctrl+C to stop.")
    print("=" * 56)

    from antidetect.api import app

    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
