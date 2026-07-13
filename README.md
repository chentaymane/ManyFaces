# Anti-Detect Browser Manager

A multi-profile anti-detect browser manager — the open, self-hosted equivalent of
Dolphin Anty / Multilogin / GoLogin. Each profile is a fully isolated browser
identity: its own persistent storage and cookies, its own **coherent** fingerprint,
and its own proxy.

Built on **[Camoufox](https://camoufox.com/)** — a patched build of Firefox that
spoofs fingerprints (canvas, WebGL, fonts, WebRTC, navigator, screen, audio) at the
**native C++ level**. That is dramatically harder to detect than the JavaScript
injection most DIY tools use, and it passes CreepJS, BrowserScan, Pixelscan and
BrowserLeaks out of the box.

## Legitimate use

Anti-detect browsers are standard tooling for **multi-account management on your own
accounts**: agencies running many client ad/social accounts, e-commerce sellers with
multiple storefronts, QA teams testing geo/device variations, privacy research, and
web scraping of public data. This project is for those uses.

**Do not** use it to violate the terms of service of sites you access, to commit
fraud, to evade bans you were legitimately given, or to create fake identities.
Those uses are illegal in many jurisdictions and are not supported.

## Features

- **Desktop app** — runs in a native window (pywebview / WebView2), not a browser
  tab. `python desktop.py`.
- **Profile management** — create, edit, clone, delete, and **bulk-create** N
  fully-randomized profiles at once. Fully isolated persistent storage per profile
  (`~/.antidetect/profiles/<id>`).
- **Deep fingerprint spoofing** — a coherent device fingerprint where *everything*
  agrees and stays identical across launches. Randomized and pinned per profile:
  - OS, GPU (from Camoufox's validated real-device DB, via `webgl_config`), screen
    size, colour depth, device-pixel-ratio
  - CPU cores, locale, timezone, language, region
  - **Canvas** anti-aliasing offset, **audio** sample rate, **font** set + metric
    spacing seed (the real canvas/audio/font noise vectors)
  - Battery state, media-device counts (cams/mics/speakers), max touch points,
    Do-Not-Track, WebRTC local-IP
  - `navigator.webdriver` hidden. Verified: same profile → identical
    GPU/screen/CPU/DPR/audio/locale every launch.
- **Proxy management** — HTTP / HTTPS / SOCKS5 per profile, one-click live test
  (exit IP, geo, latency), and GeoIP locale/timezone-matching to the proxy's country.
- **Cookie management** — import/export (Playwright JSON format) plus realistic
  random cookie generation (proper GA/Facebook/DoubleClick formats across real
  tracker domains) to warm up a profile's jar and verify isolation.
- **One-click Randomize All** — regenerate a profile's entire fingerprint and reseed
  a fresh cookie jar in one action.
- **Automation API** — drive any profile from your own Python scripts with the full
  fingerprint/proxy/cookies applied (`antidetect.automate.launch`).

## Setup

```bash
# 1. Install Python deps
pip install -r requirements.txt

# 2. Download the Camoufox browser (one-time, ~500 MB)
python -m camoufox fetch

# 3a. Run as a desktop app (native window)
python desktop.py

# 3b. …or run as a local web server instead
python run.py   # dashboard at http://127.0.0.1:8000
```

On Windows the desktop app uses the Edge **WebView2** runtime (preinstalled on
Windows 10/11; if missing, install "Evergreen WebView2 Runtime" from Microsoft).

## Portable package (recommended — no download, works offline)

The best way to ship this: a folder containing the app **and the browser engine
together**, so the end user never downloads anything — they unzip and double-click.

```bash
pip install pyinstaller
pyinstaller build.spec --noconfirm    # build the app exe
python -m camoufox fetch              # ensure the browser is installed locally
python package_portable.py            # bundle exe + browser -> dist/AntiDetectManager-Portable.zip
```

This produces `dist/AntiDetectManager-Portable.zip`. The end user just unzips it and
runs `AntiDetectManager.exe`; the app finds the bundled `browser/` folder next to it
and launches instantly — **no 500 MB download, no GitHub dependency, fully offline.**
This sidesteps slow/flaky GitHub access entirely.

## Build just the standalone .exe

If you prefer a single small exe that downloads the browser on first run (needs a
working connection), build only the executable:

```bash
pip install pyinstaller
pyinstaller build.spec --noconfirm
```

The result is `dist/AntiDetectManager.exe` (~160 MB). Just launch it — it opens the
native window and, on first run, downloads the Camoufox browser (~500 MB) into the
user cache with an in-app progress screen (live %, MB, speed, and ETA).

The downloader is built for slow/flaky links (e.g. throttled GitHub access):
- **8 parallel connections** to beat per-connection throttling (~2× faster).
- **Resumable** — progress is saved to disk, so a dropped connection *or even closing
  and reopening the app* continues from where it stopped, never restarting the 500 MB.
- **Self-healing** — auto-retries through transient network drops; a manual Retry
  button appears if it can't recover on its own.

The build does **not** bundle the browser binary, keeping the exe small; the
Playwright driver that launches it *is* bundled.

> Verified end-to-end from the packaged exe: server boot, engine detection, profile
> create / bulk-create, and a real browser launch. Any server error is also written
> to `%USERPROFILE%\.antidetect\error.log` for troubleshooting (no console needed).

> **Python version note:** verified working on Python 3.14 (Camoufox 135 beta). The
> management server and API also run without Camoufox installed — everything works
> except actually launching a browser window, which returns a clear error telling
> you to run the `camoufox fetch` step.

## Automation example

```python
from antidetect.automate import launch

with launch("<profile-id>", headless=True) as ctx:
    page = ctx.new_page()
    page.goto("https://abrahamjuliot.github.io/creepjs/")
    page.screenshot(path="creep.png")
```

Async version: `antidetect.automate.launch_async`.

## Architecture

```
run.py                 -> starts uvicorn + opens dashboard
antidetect/
  api.py               -> FastAPI: profile CRUD, launch, proxy test, cookies
  browser.py           -> Camoufox session manager (threaded, headful)
  automate.py          -> scripting entry points (sync + async)
  fingerprint.py       -> coherent fingerprint generation
  proxy.py             -> proxy connectivity/geo testing
  cookies.py           -> cookie import/export/random
  models.py            -> Pydantic schemas
  db.py                -> SQLite profile storage
  config.py            -> paths & settings
web/                   -> dashboard (vanilla JS)
```

Profile metadata lives in SQLite (`~/.antidetect/antidetect.db`); persistent browser
data lives on disk per profile.

## Roadmap toward parity with commercial tools

- Full BrowserForge fingerprint serialization (pin every attribute, not just core).
- Chromium engine option (patched build) for sites that block Firefox.
- Team features: multi-user, roles, shared/encrypted proxy pool.
- Profile "warm-up" automation and cookie robot.
- Fingerprint quality scoring against CreepJS/Pixelscan in-app.
- Tag/folder organization, bulk actions, import/export of profiles.
