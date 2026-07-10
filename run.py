"""Entry point: start the management server and open the dashboard.

    python run.py            # start server on http://127.0.0.1:8000
    python run.py --no-open  # don't auto-open the browser
"""
from __future__ import annotations

import sys
import threading
import webbrowser

import uvicorn

from antidetect import config


def _open_browser() -> None:
    webbrowser.open(f"http://{config.HOST}:{config.PORT}")


def main() -> None:
    config.ensure_dirs()
    if "--no-open" not in sys.argv:
        threading.Timer(1.5, _open_browser).start()
    print(f"Anti-detect manager running at http://{config.HOST}:{config.PORT}")
    uvicorn.run("antidetect.api:app", host=config.HOST, port=config.PORT, reload=False)


if __name__ == "__main__":
    main()
