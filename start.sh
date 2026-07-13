#!/usr/bin/env bash
# Anti-Detect Manager launcher for Linux / macOS.
# Starts the server and opens the dashboard in your default browser.
#
#   chmod +x start.sh   # once
#   ./start.sh

set -e
cd "$(dirname "$0")"

# Pick a Python: prefer python3, fall back to python.
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "Python 3 is not installed. Install it (e.g. 'sudo apt install python3 python3-pip') and retry."
  exit 1
fi

echo "============================================================"
echo "  Anti-Detect Manager"
echo "  Starting... your browser will open automatically."
echo "  Keep this terminal open while using the app (Ctrl+C stops)."
echo "============================================================"

exec "$PY" run.py
