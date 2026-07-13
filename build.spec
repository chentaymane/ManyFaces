# PyInstaller spec — builds a single-file ManyFaces.exe.
#
# The Camoufox *browser binary* (~150 MB) is NOT bundled; it downloads to the user
# cache on first run (see the engine-setup overlay). We DO bundle the Camoufox and
# BrowserForge Python packages and their data files (fonts, GeoIP db, fingerprint
# models), the web UI, and uvicorn/pywebview runtime pieces.
#
#   pyinstaller build.spec --noconfirm

import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

# Icon is Windows-only (.ico); Linux/macOS builds use no embedded icon here.
_icon = "assets/icon.ico" if sys.platform == "win32" else None

datas = [("web", "web")]
binaries = []
hiddenimports = []

# Packages that ship data files and/or dynamic submodules.
# apify_fingerprint_datapoints holds BrowserForge's network-definition zips, loaded
# at import time — without them, importing camoufox/browserforge and launching a
# browser both fail with a missing input-network-definition.zip.
for pkg in (
    "camoufox",
    "browserforge",
    "apify_fingerprint_datapoints",
    "playwright",   # ships driver/node.exe (~92 MB) that actually drives the browser
    "webview",
    "language_tags",
    "screeninfo",
):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass  # optional packages (e.g. screeninfo) may be absent; skip cleanly

# Belt-and-suspenders: Camoufox's runtime data (webgl_data.db, fonts.json,
# GeoLite2-City.mmdb, browserforge.yml, launchServer.js) MUST be present, or
# fingerprint generation and launch fail. Collect them explicitly too.
datas += collect_data_files("camoufox", include_py_files=False)

# uvicorn/anyio resolve their workers dynamically, so name them explicitly.
hiddenimports += collect_submodules("uvicorn")
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "anyio._backends._asyncio",
]


a = Analysis(
    ["desktop.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ManyFaces",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX can trip antivirus false-positives; keep it off
    runtime_tmpdir=None,
    console=False,           # windowed app, no terminal
    disable_windowed_traceback=False,
    icon=_icon,
)
