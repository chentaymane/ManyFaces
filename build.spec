# PyInstaller spec — builds a single-file ManyFaces.exe.
#
# The Camoufox *browser binary* (~1.3 GB archive) is NOT bundled; it downloads to the user
# cache on first run (see the engine-setup banner). We DO bundle the Camoufox and
# fpgen Python packages and their data files (fonts, voices, GPU/fingerprint
# models, GeoIP db), the web UI, and uvicorn/pywebview runtime pieces.
#
#   pyinstaller build.spec --noconfirm

import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

# Icon is Windows-only (.ico); Linux/macOS builds use no embedded icon here.
_icon = "assets/icon.ico" if sys.platform == "win32" else None

datas = [("web", "web")]
binaries = []
hiddenimports = []

# Packages that ship data files and/or dynamic submodules. Camoufox 0.5+ generates
# fingerprints with fpgen (BrowserForge is gone) and parses UAs with ua_parser.
for pkg in (
    "camoufox",
    "fpgen",
    "ua_parser",
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

# Belt-and-suspenders: Camoufox's runtime data (fonts.json, font/voice manifests,
# fpgen-model.json, media-devices.json, launchServer.js, …) MUST be present, or
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
