"""Assemble the portable, no-download distribution.

Produces `dist/ManyFaces-Portable/` (the app exe + the Camoufox browser side by
side) and zips it to `dist/ManyFaces-Portable.zip`. The app finds the `browser/`
folder next to the exe and runs it directly — so the end user never downloads
anything; they just unzip and double-click.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent
DIST = PROJ / "dist"
EXE = DIST / "ManyFaces.exe"
OUT = DIST / "ManyFaces-Portable"
ZIP = DIST / "ManyFaces-Portable.zip"

START_TXT = """ManyFaces - Portable
====================

HOW TO USE
  1. Double-click  ManyFaces.exe
  2. That's it. The browser engine is already included in the "browser" folder,
     so there is NO download and it works fully offline.

NOTES
  * Keep ManyFaces.exe and the "browser" folder together in this folder.
  * Windows SmartScreen may warn on first launch (unsigned app):
    click "More info" -> "Run anyway".
  * Your profiles and data are stored in:  %USERPROFILE%\\.antidetect

For legitimate multi-account management, testing, and privacy use only.
"""


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> None:
    from camoufox.pkgman import INSTALL_DIR

    browser_src = Path(str(INSTALL_DIR))
    if not (browser_src / "camoufox.exe").exists():
        log(f"ERROR: no installed browser at {browser_src}; run the app once to fetch it.")
        sys.exit(1)
    if not EXE.exists():
        log(f"ERROR: {EXE} not found; build it first (pyinstaller build.spec).")
        sys.exit(1)

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    log("copying app exe...")
    shutil.copy2(str(EXE), str(OUT / "ManyFaces.exe"))

    log("copying browser engine (~940 MB, one-time)...")
    t0 = time.time()
    shutil.copytree(str(browser_src), str(OUT / "browser"))
    log(f"browser copied in {time.time() - t0:.0f}s")

    (OUT / "START HERE.txt").write_text(START_TXT, encoding="utf-8")

    # Zip the folder for easy sharing.
    log("zipping portable package...")
    t0 = time.time()
    if ZIP.exists():
        ZIP.unlink()
    with zipfile.ZipFile(str(ZIP), "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, _, files in os.walk(str(OUT)):
            for f in files:
                full = Path(root) / f
                zf.write(str(full), str(Path("ManyFaces-Portable") / full.relative_to(OUT)))
    size_mb = ZIP.stat().st_size / 1048576
    log(f"zip done in {time.time() - t0:.0f}s -> {ZIP.name} ({size_mb:.0f} MB)")
    log("PORTABLE PACKAGE READY")


if __name__ == "__main__":
    main()
