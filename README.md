# ManyFaces

A multi-profile anti-detect browser manager — the open, self-hosted equivalent of
Dolphin Anty / Multilogin / GoLogin. Each profile is a fully isolated browser
identity: its own persistent storage and cookies, its own **coherent** fingerprint,
and its own proxy.

The default engine is **[Camoufox](https://camoufox.com/)** — a patched build of
Firefox that spoofs fingerprints (canvas, WebGL, fonts, WebRTC, navigator, screen,
audio) at the **native C++ level**. That is dramatically harder to detect than the
JavaScript injection most DIY tools use, and it passes CreepJS, BrowserScan,
Pixelscan and BrowserLeaks out of the box.

Each profile can instead run on **Chromium** (for sites that block Firefox, and for
true mobile emulation) or on a **real Android device** in the official emulator (a
genuine Chrome-for-Android engine — nothing spoofed, because it *is* a phone). All
three are selectable per profile.

## Legitimate use

Anti-detect browsers are standard tooling for **multi-account management on your own
accounts**: agencies running many client ad/social accounts, e-commerce sellers with
multiple storefronts, QA teams testing geo/device variations, privacy research, and
web scraping of public data. This project is for those uses.

**Do not** use it to violate the terms of service of sites you access, to commit
fraud, to evade bans you were legitimately given, or to create fake identities.
Those uses are illegal in many jurisdictions and are not supported.

## Features

- **Three browser engines, per profile** — pick **Camoufox** (patched Firefox, the
  strongest native-level stealth), **Chromium** (Playwright Chromium), or **Android**
  (a real device) on each profile. Chromium is for sites that block Firefox, and it
  renders phone profiles as a **true mobile interface** (real device viewport, high
  DPR, touch, mobile layout) — emulation Firefox/Gecko can't do. Fingerprints
  (navigator, screen, WebGL GPU strings, touch) are pinned on both Camoufox and
  Chromium and stay identical across launches; `navigator.webdriver` is hidden on
  Chromium too. (The Android engine is a real device, so there's nothing to pin.)
- **Real Android engine (AVD)** — for a phone that isn't emulated at all: the
  **Android** engine boots a genuine Android device in the official Android Emulator,
  so the browser inside is real **Chrome-for-Android** (real ARM-ish Blink, real
  touch/GPU) — nothing spoofed, because it *is* a phone. It's free and set up in one
  click from the app: the installer fetches the Android SDK + a system image (a few
  GB), **scrcpy** (the mirror window), and, if needed, a small Java runtime, streaming
  live progress. The device runs **headless** and is shown through the scrcpy mirror
  — a clean window with real touch/keyboard input — which sidesteps the emulator's own
  window (it black-screens on many PCs: a missing `opengl32sw` / layered-window bug in
  the emulator's Qt UI, unrelated to the phone itself). Requires hardware
  virtualization (WHPX / KVM / Hypervisor.framework) and uses **hardware GPU**
  rendering for speed. The first boot of a device takes ~1–2 min; after that it
  **quickboots in seconds** (a snapshot is saved on close and restored next launch).
  Trade-off vs the emulated engines: each profile is one Android VM (real RAM/CPU,
  ~4–5 GB disk), so it's for authenticity, not running dozens at once.
  Proxy support is limited to an **unauthenticated HTTP/HTTPS** proxy (the emulator
  can't take SOCKS or user/password proxies on its command line); such proxies apply
  to the emulated engines but not to Android profiles.
- **Desktop app** — runs in a native window (pywebview / WebView2), not a browser
  tab. `python desktop.py`.
- **Profile management** — create, edit, clone, delete, and **bulk-create** N
  fully-randomized profiles at once. Fully isolated persistent storage per profile
  (`~/.antidetect/profiles/<id>`).
- **One-click phone profiles** — a dedicated **📱 New Phone** button: pick a real
  device (Pixel, Galaxy, OnePlus, Xiaomi, or iPhone presets) and hit **Create &
  launch**. Phone profiles default to the **Chromium** engine, which renders them as
  a true phone — real mobile viewport, high DPR, touch, and mobile page layout — with
  a coherent per-device fingerprint (UA, screen, GPU strings, touch) pinned across
  launches. The window opens like a **phone emulator**: a chromeless, phone-shaped
  portrait window (no tabs or address bar — just the screen) sized to the exact
  device resolution. It lands on a built-in **phone home screen** (clock, a
  search/address box, and quick-launch app icons) instead of a blank page, and every
  page shows floating on-screen **‹ › back/forward buttons** — so there's always
  something to see and a way to navigate (`Alt`+`←`/`→` and the mouse back button
  work too). On the **Camoufox** engine, phones instead run a coherent
  Firefox-for-Android identity (mobile UA, phone screen + DPR, `Linux armv8l`
  platform, mobile GPU strings, Android-only font set). iPhone is offered on both,
  but is a weaker spoof than Android (an iPhone runs WebKit while both engines are
  non-WebKit), so an engine-level probe can still tell.
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
- **Proxy pool & rotation** — paste a list of proxies (any common format:
  `host:port`, `proto://host:port`, `host:port:user:pass`, `proto://user:pass@host:port`)
  and pick a mode per profile: **manual** (one fixed proxy), **random** (a fresh pool
  pick each launch), or **rotate** (round-robin through the pool, advancing every
  launch). Test the whole pool at once — each proxy is verified concurrently for exit
  IP, country and latency. Inspired by
  [chameleon-ip-rotator](https://github.com/chentaymane/chameleon-ip-rotator).
- **Cookie management** — import/export (Playwright JSON format) plus realistic
  random cookie generation (proper GA/Facebook/DoubleClick formats across real
  tracker domains) to warm up a profile's jar and verify isolation.
- **One-click Randomize All** — regenerate a profile's entire fingerprint and reseed
  a fresh cookie jar in one action.
- **Automation API** — drive any profile from your own Python scripts with the full
  fingerprint/proxy/cookies applied (`antidetect.automate.launch`).

## Setup (Windows, Linux & macOS)

Runs on all three. The recommended way is browser mode (`run.py`) — it works
everywhere with no GUI toolkit needed.

```bash
# 1. Install Python deps  (use python3 / pip3 on Linux & macOS)
pip install -r requirements.txt

# 2. Download the Camoufox browser for your OS (one-time, ~500 MB)
python -m camoufox fetch

# 3. Start it — opens the dashboard in your normal browser
python run.py
```

**Or just double-click a launcher** (does step 3 for you):
- **Windows:** `START.bat`
- **Linux / macOS:** `./start.sh`  (first run: `chmod +x start.sh`)

### Optional engines

- **Chromium** — needed only for profiles set to the Chromium engine (including the
  emulated phone profiles). Install the browser once:
  ```bash
  python -m playwright install chromium
  ```
- **Real Android** — no manual setup: pick the Android engine, then use the in-app
  **one-click installer** on the Android setup screen. It fetches the Android SDK + a
  system image (a few GB), the scrcpy mirror window, and a portable Java runtime if
  your system Java is older than 17. Requires hardware virtualization (WHPX on
  Windows, KVM on Linux, Hypervisor.framework on macOS).

### Optional: native desktop window instead of a browser tab

```bash
python desktop.py
```
- **Windows** uses the Edge **WebView2** runtime (preinstalled on Windows 10/11).
- **Linux/macOS** need a WebView backend for pywebview — install one of:
  `pip install "pywebview[qt]"` (Qt) or the GTK stack
  (`sudo apt install python3-gi gir1.2-webkit2-4.1` on Debian/Ubuntu).
- If you'd rather not install those, just use `python run.py` — same app, in your
  browser.

## Portable package (recommended — no download, works offline)

The best way to ship this: a folder containing the app **and the browser engine
together**, so the end user never downloads anything — they unzip and double-click.

```bash
pip install pyinstaller
pyinstaller build.spec --noconfirm    # build the app exe
python -m camoufox fetch              # ensure the browser is installed locally
python package_portable.py            # bundle exe + browser -> dist/ManyFaces-Portable.zip
```

This produces `dist/ManyFaces-Portable.zip`. The end user just unzips it and
runs the app (`ManyFaces.exe` on Windows, `ManyFaces` on Linux); it
finds the bundled `browser/` folder next to it and launches instantly — **no 500 MB
download, no GitHub dependency, fully offline.** This sidesteps slow/flaky GitHub
access entirely.

> Build on the OS you're targeting: the bundled Camoufox binary is platform-specific,
> so build the Windows package on Windows and the Linux package on Linux. The same
> `build.spec` / `package_portable.py` work on both (the spec omits the Windows-only
> icon automatically on Linux).

## Build just the standalone .exe

If you prefer a single small exe that downloads the browser on first run (needs a
working connection), build only the executable:

```bash
pip install pyinstaller
pyinstaller build.spec --noconfirm
```

The result is `dist/ManyFaces.exe` (~160 MB). Just launch it — it opens the
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
  api.py               -> FastAPI: profile CRUD, launch, proxy test, cookies, Android setup
  browser.py           -> Camoufox + Chromium session manager (threaded, headful)
  android.py           -> real Android (AVD) engine: SDK install, emulator boot, scrcpy mirror
  automate.py          -> scripting entry points (sync + async)
  fingerprint.py       -> coherent fingerprint generation (desktop + phone presets)
  proxy.py             -> proxy connectivity/geo testing
  cookies.py           -> cookie import/export/random
  models.py            -> Pydantic schemas
  db.py                -> SQLite profile storage
  config.py            -> paths & settings
web/
  index.html/app.js/style.css  -> dashboard (vanilla JS)
  mobile_start.html            -> phone home screen served to emulated-phone profiles
```

Profile metadata lives in SQLite (`~/.antidetect/antidetect.db`); persistent browser
data lives on disk per profile. The Android SDK, AVDs, and scrcpy live under
`~/.antidetect/android/`.

## Roadmap toward parity with commercial tools

- Full BrowserForge fingerprint serialization (pin every attribute, not just core).
- Authenticated / SOCKS proxy support for the real-Android engine (via a local
  forwarding proxy, since the emulator only takes an unauthenticated HTTP proxy).
- GPU-mode selector for the Android engine (software / host / auto) in the setup UI.
- Team features: multi-user, roles, shared/encrypted proxy pool.
- Profile "warm-up" automation and cookie robot.
- Fingerprint quality scoring against CreepJS/Pixelscan in-app.
- Tag/folder organization, bulk actions, import/export of profiles.
