# PyInstaller spec — builds a single-file AntiDetectManager.exe.
#
# The Camoufox *browser binary* (~150 MB) is NOT bundled; it downloads to the user
# cache on first run (see the engine-setup overlay). We DO bundle the Camoufox and
# BrowserForge Python packages and their data files (fonts, GeoIP db, fingerprint
# models), the web UI, and uvicorn/pywebview runtime pieces.
#
#   pyinstaller build.spec --noconfirm

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [("web", "web")]
binaries = []
hiddenimports = []

# Packages that ship data files and/or dynamic submodules.
for pkg in ("camoufox", "browserforge", "webview", "language_tags", "screeninfo"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass  # optional packages (e.g. screeninfo) may be absent; skip cleanly

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
    name="AntiDetectManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX can trip antivirus false-positives; keep it off
    runtime_tmpdir=None,
    console=False,           # windowed app, no terminal
    disable_windowed_traceback=False,
    icon=None,
)
