"""Camoufox browser session management.

Each running profile gets a `BrowserSession` on its own thread. Camoufox's sync API
is a blocking context manager, so the thread enters the context, opens the start
page, injects staged cookies, then blocks on a stop event until asked to close.

Camoufox spoofs the fingerprint at the native Firefox level (canvas, WebGL, fonts,
WebRTC, navigator, screen, audio), which is far harder to detect than JS injection.
We hand it: the profile's persistent user-data dir, its proxy, its OS/locale, and a
`config` dict pinning the stored fingerprint so the device stays consistent.
"""
from __future__ import annotations

import threading
import traceback
from typing import Any, Optional

from . import config, cookies as cookie_store
from .models import Profile


class BrowserError(RuntimeError):
    pass


def normalize_start_url(url: str | None) -> str:
    """Turn whatever the user typed into a URL a browser will actually load.

    The #1 "it doesn't work" bug was a start URL like `google.com` or
    `www.example.com` with no scheme: `page.goto()` treats that as an invalid URL
    and the tab lands on an error page. We fix it up here:
      - blank / None                       -> about:blank
      - already has a scheme (http, https,
        about, file, data, chrome, …)      -> left untouched
      - a bare host/path (`google.com/x`)  -> gets an `https://` prefix
    """
    url = (url or "").strip()
    if not url:
        return "about:blank"
    # A scheme looks like `word:` at the very start (http:, https:, about:, file:,
    # data:, chrome:, view-source:, …). If one is present, trust the user — UNLESS
    # what follows the colon is just a port number, in which case it's really a
    # `host:port` (e.g. `localhost:8000`) and needs an https:// prefix.
    import re

    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):(.*)$", url, re.DOTALL)
    if m and not m.group(2).split("/")[0].isdigit():
        return url
    # Protocol-relative URL (`//host/…`) -> assume https.
    if url.startswith("//"):
        return "https:" + url
    # Otherwise it's a bare domain/path; default to https.
    return "https://" + url


def mobile_start_url() -> str:
    """file:// URL of the built-in phone start page (search box + quick links).

    A phone opened on about:blank looks like a black/blank screen — and in the
    chromeless emulator window there's no address bar to escape it. We land phones
    on this page instead, so there's always something to see and a way to navigate.
    """
    page = config.WEB_DIR / "mobile_start.html"
    return page.as_uri() if page.exists() else "about:blank"


def effective_start_url(profile: Profile, fp) -> str:
    """Resolve the URL a session should open, upgrading a blank phone to the start page."""
    url = normalize_start_url(profile.start_url)
    if url == "about:blank" and getattr(fp, "is_mobile", False):
        return mobile_start_url()
    return url


def activate_bundled_engine() -> bool:
    """If the app ships a bundled browser, make Camoufox use it without downloading.

    Camoufox reads its version from INSTALL_DIR/version.json even when we pass an
    explicit executable_path, so we copy that tiny file over from the bundle. The
    browser binary itself stays in the bundle folder (pointed at via executable_path
    in build_launch_options) — no multi-hundred-MB copy, no network.
    Returns True if a bundled browser is present and now usable.
    """
    bundle = config.bundled_browser_dir()
    if not bundle:
        return False
    version_src = bundle / "version.json"
    if not version_src.exists():
        return False
    try:
        from camoufox.pkgman import INSTALL_DIR
        import shutil

        INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        dst = INSTALL_DIR / "version.json"
        if not dst.exists():
            shutil.copy(str(version_src), str(dst))
        return True
    except Exception:  # noqa: BLE001
        return False


def _resolve_screen(os_name: str, width: int, height: int):
    """Return a validated Screen constraint near the target size, or None.

    BrowserForge only knows *real* device resolutions, so an arbitrary band may
    contain none and raise "No headers" at launch. We validate candidate bands here
    with Camoufox's pure-Python fingerprint generator (no browser started), from
    tightest-near-target to widest, and return the first that generates cleanly.
    This means the actual launch never has to retry — which matters because a failed
    Camoufox sync launch leaves an asyncio loop that would poison a retry.
    """
    try:
        from browserforge.fingerprints import Screen
        from camoufox.fingerprints import generate_fingerprint
    except ImportError:
        return None

    bands = [
        (max(1024, width - 160), width + 64, max(720, height - 120), height + 64),
        (1280, 1920, 720, 1200),
        (1024, 2560, 720, 1440),
    ]
    for mnw, mxw, mnh, mxh in bands:
        screen = Screen(min_width=mnw, max_width=mxw, min_height=mnh, max_height=mxh)
        try:
            generate_fingerprint(screen=screen, os=os_name)
            return screen
        except Exception:  # noqa: BLE001 - this band has no real match, try wider
            continue
    return None


def _apply_mobile_webgl(cfg: dict[str, Any], vendor: str, renderer: str, base_os: str = "lin") -> None:
    """Make a mobile profile's WebGL report the phone GPU, masked AND unmasked.

    Camoufox ships only a desktop GPU database, so we take a base bundle and rewrite
    every place the vendor/renderer surface: the top-level strings and the UNMASKED
    vendor/renderer parameters (GL enums 37445/37446) inside both the WebGL1 and
    WebGL2 parameter maps — which is what fingerprinters actually read via
    WEBGL_debug_renderer_info. The remaining GL params/extensions stay desktop-derived
    (no mobile GPU data exists to source them from). We inject the whole bundle into
    `cfg`, so Camoufox's own (desktop) sample never overwrites it.

    `base_os` picks which desktop GPU family the untouched params come from — "mac"
    for iOS (Apple GL params are the closest match to an iPhone), "lin" for Android.
    """
    try:
        from camoufox.webgl import sample_webgl
    except Exception:  # noqa: BLE001 - engine missing; skip, launch will report it
        return
    try:
        bundle = sample_webgl(base_os)
    except Exception:  # noqa: BLE001
        return
    bundle.pop("webGl2Enabled", None)  # a control flag, not a config property
    for pkey in ("webGl:parameters", "webGl2:parameters"):
        params = bundle.get(pkey)
        if isinstance(params, dict):
            if "37445" in params:
                params["37445"] = vendor    # UNMASKED_VENDOR_WEBGL
            if "37446" in params:
                params["37446"] = renderer  # UNMASKED_RENDERER_WEBGL
    bundle["webGl:vendor"] = vendor
    bundle["webGl:renderer"] = renderer
    cfg.update(bundle)


def build_launch_options(profile: Profile, headless: bool | None = None) -> dict[str, Any]:
    """Build a single, pre-validated set of Camoufox launch options for a profile."""
    fp = profile.fingerprint.to_fingerprint()
    geoip_active = profile.geoip and profile.proxy.is_set

    cfg = fp.camoufox_config()
    if geoip_active:
        # Let GeoIP derive the timezone from the proxy's exit IP so it can't
        # contradict the real network location; keep our pinned value otherwise.
        cfg.pop("timezone", None)
    if profile.block_webrtc:
        # WebRTC is fully blocked, so a spoofed local IP is moot and would only add
        # a config value with nothing to apply to.
        pass
    elif fp.webrtc_local_ipv4:
        cfg["webrtc:localipv4"] = fp.webrtc_local_ipv4

    opts: dict[str, Any] = {
        "headless": False if headless is None else headless,
        "persistent_context": True,
        # Don't let Playwright enforce its own viewport. Newer Playwright sends a
        # `setDefaultViewport` with an `isMobile` field that the pinned Camoufox
        # Firefox build's Juggler protocol rejects ("property isMobile ... not in
        # this scheme"), which otherwise breaks EVERY launch. We pin screen/window
        # dimensions ourselves (via config + the `window` option), so Playwright's
        # viewport is redundant anyway.
        "no_viewport": True,
        "user_data_dir": str(config.profile_data_dir(profile.id)),
        # Camoufox only accepts desktop OS names, so a phone profile runs on the
        # nearest desktop engine with its identity spoofed via `config`: Android on
        # Linux (both Gecko/ARM-ish), iOS on macOS (Apple GPU + Apple GL params).
        "os": ("macos" if fp.os == "ios" else "linux") if fp.is_mobile else fp.os,
        "locale": fp.locale,
        "humanize": profile.humanize,
        "block_webrtc": profile.block_webrtc,
        "geoip": geoip_active,
        "config": cfg,
    }
    bundled_exe = config.bundled_browser_exe()
    if bundled_exe:
        opts["executable_path"] = bundled_exe  # use the shipped browser, never download
    webgl = fp.webgl_config()
    if webgl is not None:
        opts["webgl_config"] = webgl  # pins GPU consistently across launches
    if fp.is_mobile:
        # Size the real window to the phone and expose ONLY Android fonts
        # (custom_fonts_only stops Camoufox merging the desktop font set, which
        # would otherwise leak a desktop identity). Screen dims are pinned in
        # `cfg`, so no BrowserForge Screen constraint is used here.
        _apply_mobile_webgl(
            cfg, fp.webgl_vendor, fp.webgl_renderer,
            base_os="mac" if fp.os == "ios" else "lin",
        )
        opts["window"] = (fp.screen_width, fp.screen_height)
        if fp.fonts:
            opts["fonts"] = fp.fonts
            opts["custom_fonts_only"] = True
        # Make it behave like a real phone, not just a narrow desktop window:
        # enable native touch events (so `ontouchstart`, `TouchEvent`,
        # `pointer: coarse` and `hover: none` all report like a touchscreen) and
        # honour the meta-viewport tag so pages render their true mobile layout.
        # Without these a phone UA still gets served/rendered as desktop.
        opts["firefox_user_prefs"] = {
            "dom.w3c_touch_events.enabled": 1,
            "dom.w3c_touch_events.legacy_apis.enabled": True,
            "dom.meta-viewport.enabled": True,
            "apz.allow_zooming": True,
            # Report a touchscreen's pointer to CSS: primary/all pointer = coarse
            # (bit 0x01), no hover. This flips `pointer: coarse` / `hover: none` /
            # `any-hover: none` true — the media queries mobile sites switch on.
            "ui.primaryPointerCapabilities": 1,
            "ui.allPointerCapabilities": 1,
        }
    else:
        screen = _resolve_screen(fp.os, fp.screen_width, fp.screen_height)
        if screen is not None:
            opts["screen"] = screen
    if profile.proxy.is_set:
        opts["proxy"] = profile.proxy.playwright_dict()
    return opts


# --------------------------------------------------------------- chromium ----
# A second engine option. Camoufox (Firefox) is the strongest stealth, but some
# sites block Firefox outright, and Firefox/Juggler rejects the true mobile-emulation
# knobs (isMobile, touch, deviceScaleFactor). Chromium via Playwright fills both
# gaps: broad site compatibility, and a *real* phone interface for mobile profiles
# (DevTools-style device emulation — proper viewport, DPR, touch, mobile layout).
# It is less stealthy than Camoufox, so it's an explicit per-profile choice.

_CHROME_MAJOR: Optional[str] = None


def _chrome_major(pw) -> str:
    """Major version of Playwright's bundled Chromium, for a coherent Chrome UA.

    Probed once (cheap headless launch) and cached for the process; falls back to a
    recent stable major if the probe fails.
    """
    global _CHROME_MAJOR
    if _CHROME_MAJOR is None:
        try:
            b = pw.chromium.launch()
            _CHROME_MAJOR = b.version.split(".", 1)[0]
            b.close()
        except Exception:  # noqa: BLE001
            _CHROME_MAJOR = "148"
    return _CHROME_MAJOR


# UA platform token + navigator.platform value per desktop OS.
_CHROME_DESKTOP = {
    "windows": ("Windows NT 10.0; Win64; x64", "Win32"),
    "macos": ("Macintosh; Intel Mac OS X 10_15_7", "MacIntel"),
    "linux": ("X11; Linux x86_64", "Linux x86_64"),
}


def _chrome_ua(fp, major: str) -> str:
    """Build a coherent Chrome user-agent for this profile's device.

    The stored `user_agent` is a *Firefox* string (built for Camoufox); on Chromium
    that would be incoherent, so we synthesise a matching Chrome UA instead.
    """
    if fp.os == "ios":
        # Chrome-on-iOS is still WebKit; the stored Safari UA is the honest choice
        # and is exactly what DevTools uses for an emulated iPhone.
        return fp.user_agent
    if fp.os == "android" or fp.is_mobile:
        import re

        m = re.search(r"Android (\d+)", fp.user_agent or "")
        andver = m.group(1) if m else "14"
        model = (fp.device_name or "Pixel 7").replace("Google ", "").strip()
        return (
            f"Mozilla/5.0 (Linux; Android {andver}; {model}) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Mobile Safari/537.36"
        )
    ua_plat, _ = _CHROME_DESKTOP.get(fp.os, _CHROME_DESKTOP["windows"])
    return (
        f"Mozilla/5.0 ({ua_plat}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


def _nav_platform(fp) -> str:
    if fp.is_mobile:
        return "iPhone" if fp.os == "ios" else "Linux armv8l"
    return _CHROME_DESKTOP.get(fp.os, _CHROME_DESKTOP["windows"])[1]


def chromium_init_script(fp) -> str:
    """JS injected into every page to pin the fingerprint on the Chromium engine.

    Chromium has no native spoofing layer like Camoufox, so we override the
    JS-visible surface: hide the webdriver flag, and pin navigator/screen/WebGL to
    the profile's stored values so the device looks the same every launch.
    """
    import json

    vendor = json.dumps(fp.webgl_vendor or "Google Inc.")
    renderer = json.dumps(fp.webgl_renderer or "ANGLE (Unknown)")
    platform = json.dumps(_nav_platform(fp))
    languages = json.dumps([fp.language, fp.language.split("-")[0]])
    return f"""
(() => {{
  const def = (obj, prop, val) => {{
    try {{ Object.defineProperty(obj, prop, {{ get: () => val, configurable: true }}); }} catch (e) {{}}
  }};
  // Hide the automation flag Playwright sets.
  def(navigator, 'webdriver', undefined);
  def(navigator, 'hardwareConcurrency', {fp.hardware_concurrency});
  def(navigator, 'deviceMemory', {fp.device_memory});
  def(navigator, 'platform', {platform});
  def(navigator, 'maxTouchPoints', {fp.max_touch_points});
  def(navigator, 'languages', {languages});
  // Pin the screen geometry.
  def(screen, 'width', {fp.screen_width});
  def(screen, 'height', {fp.screen_height});
  def(screen, 'availWidth', {fp.screen_width});
  def(screen, 'availHeight', {fp.screen_height});
  def(screen, 'colorDepth', {fp.color_depth});
  def(screen, 'pixelDepth', {fp.color_depth});
  // Pin the GPU strings reported through WEBGL_debug_renderer_info.
  const VENDOR = {vendor}, RENDERER = {renderer};
  for (const proto of [self.WebGLRenderingContext, self.WebGL2RenderingContext]) {{
    if (!proto) continue;
    const gp = proto.prototype.getParameter;
    proto.prototype.getParameter = function (p) {{
      if (p === 37445) return VENDOR;    // UNMASKED_VENDOR_WEBGL
      if (p === 37446) return RENDERER;  // UNMASKED_RENDERER_WEBGL
      return gp.call(this, p);
    }};
  }}
}})();
{_mobile_nav_script() if getattr(fp, "is_mobile", False) else ""}
"""


def _mobile_nav_script() -> str:
    """Floating back/forward buttons for the chromeless phone (app-mode) window.

    The emulator window has no toolbar, so without this a user can't go back — the
    recurring "i cant go back" bug. We inject a tiny control in a shadow root (so page
    CSS can't touch it) and re-add it on every navigation / DOM replacement.
    """
    return """
(() => {
  if (window.top !== window) return;               // top frame only
  const ID = '__mf_nav';
  const add = () => {
    if (document.getElementById(ID) || !document.documentElement) return;
    const host = document.createElement('div');
    host.id = ID;
    host.style.cssText = 'position:fixed;left:9px;bottom:11px;z-index:2147483647;';
    const r = host.attachShadow({ mode: 'open' });
    r.innerHTML =
      '<style>.row{display:flex;gap:8px}button{width:42px;height:42px;border-radius:50%;' +
      'border:none;background:rgba(20,22,28,.6);color:#fff;font-size:24px;line-height:40px;' +
      'padding:0;box-shadow:0 2px 10px rgba(0,0,0,.45);cursor:pointer}' +
      'button:active{transform:scale(.9)}</style>' +
      '<div class="row"><button id="b">\\u2039</button><button id="f">\\u203a</button></div>';
    r.getElementById('b').onclick = () => history.back();
    r.getElementById('f').onclick = () => history.forward();
    document.documentElement.appendChild(host);
  };
  const boot = () => { add(); new MutationObserver(add).observe(document.documentElement, { childList: true }); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
"""


def build_chromium_options(profile: Profile, pw) -> dict[str, Any]:
    """Build launch_persistent_context() options for a Chromium profile."""
    fp = profile.fingerprint.to_fingerprint()
    base = config.profile_data_dir(profile.id)
    # Keep Chromium's user-data separate from Camoufox's (different on-disk formats),
    # so a profile can even be launched on either engine without corruption.
    cdir = base / "chromium"
    cdir.mkdir(parents=True, exist_ok=True)

    major = _chrome_major(pw)
    ua = _chrome_ua(fp, major)
    w, h = fp.screen_width, fp.screen_height

    args = ["--disable-blink-features=AutomationControlled"]
    opts: dict[str, Any] = {
        "user_data_dir": str(cdir),
        "headless": False,
        "user_agent": ua,
        "locale": fp.locale.split(",")[0],
        "timezone_id": fp.timezone,
        "color_scheme": "light",
        "args": args,
        # Drop the "Chrome is being controlled by automated software" banner.
        "ignore_default_args": ["--enable-automation"],
    }
    if fp.is_mobile:
        # A genuine phone interface: fixed device viewport, high DPR, touch, and a
        # mobile layout (isMobile). This is what Camoufox/Firefox can't do.
        opts.update({
            "viewport": {"width": w, "height": h},
            "screen": {"width": w, "height": h},
            "device_scale_factor": fp.device_pixel_ratio,
            "is_mobile": True,
            "has_touch": True,
        })
        # Emulator look: Chromium "app mode" opens a chromeless window — no tabs, no
        # address bar, just the page — sized to the exact phone screen. That turns the
        # launched window into a phone-shaped screen rather than a narrow desktop
        # browser. We open it on about:blank so our fingerprint init-script and cookies
        # are injected *before* the real navigation (see _run_chromium). Back/forward
        # still work via Alt+←/→ and the mouse back button.
        args.append("--app=about:blank")
        args.append(f"--window-size={w},{h}")
    else:
        # Desktop: let the real window drive the viewport; pin its size.
        opts["no_viewport"] = True
        if fp.max_touch_points > 0:
            opts["has_touch"] = True
        args.append(f"--window-size={w},{h}")
    if profile.proxy.is_set:
        opts["proxy"] = profile.proxy.playwright_dict()
    return opts


class BrowserSession:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.error: Optional[str] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = 90.0) -> None:
        if self.running:
            return
        self._stop.clear()
        self._ready.clear()
        self.error = None
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"browser-{self.profile.id}")
        self._thread.start()
        # Wait until the browser is up (or failed) so the API can report status.
        self._ready.wait(timeout=timeout)
        if self.error:
            raise BrowserError(self.error)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)

    def _run(self) -> None:
        try:
            if self.profile.engine == "chromium":
                self._run_chromium()
            else:
                self._run_camoufox()
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            self._ready.set()

    def _run_camoufox(self) -> None:
        try:
            from camoufox.sync_api import Camoufox
        except ImportError:
            self.error = (
                "Camoufox is not installed. Run: pip install -r requirements.txt "
                "&& python -m camoufox fetch"
            )
            self._ready.set()
            return
        opts = build_launch_options(self.profile)
        self._launch(Camoufox, opts)

    def _run_chromium(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.error = (
                "Playwright is not installed. Run: pip install playwright "
                "&& python -m playwright install chromium"
            )
            self._ready.set()
            return

        fp = self.profile.fingerprint.to_fingerprint()
        with sync_playwright() as pw:
            try:
                opts = build_chromium_options(self.profile, pw)
                context = pw.chromium.launch_persistent_context(**opts)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "executable doesn't exist" in msg or "Executable doesn't exist" in msg:
                    self.error = (
                        "Chromium is not installed for Playwright. Run: "
                        "python -m playwright install chromium"
                    )
                else:
                    self.error = f"{type(exc).__name__}: {exc}"
                self._ready.set()
                return

            # Pin the fingerprint on every document, then stage cookies.
            try:
                context.add_init_script(chromium_init_script(fp))
            except Exception:  # noqa: BLE001
                pass
            staged = cookie_store.load(self.profile.id)
            if staged:
                try:
                    context.add_cookies(staged)
                except Exception:  # noqa: BLE001 - bad cookies shouldn't kill the session
                    pass

            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(effective_start_url(self.profile, fp), wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001 - navigation failure shouldn't close the window
                pass

            self._ready.set()
            while not self._stop.is_set():
                if self._stop.wait(timeout=1.0):
                    break
                try:
                    if not context.pages:
                        break  # user closed the last tab/window
                except Exception:  # noqa: BLE001 - context already gone
                    break
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass

    def _launch(self, Camoufox, opts: dict[str, Any]) -> None:
        with Camoufox(**opts) as browser:
            # With persistent_context=True the object *is* a BrowserContext.
            staged = cookie_store.load(self.profile.id)
            if staged:
                try:
                    browser.add_cookies(staged)
                except Exception:  # noqa: BLE001 - bad cookies shouldn't kill the session
                    pass

            page = browser.pages[0] if getattr(browser, "pages", None) else browser.new_page()
            try:
                fp = self.profile.fingerprint.to_fingerprint()
                page.goto(effective_start_url(self.profile, fp), wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001 - navigation failure shouldn't close the window
                pass

            self._ready.set()
            # Block until stop() is called or the user closes the window.
            while not self._stop.is_set():
                if self._stop.wait(timeout=1.0):
                    break
                # Once the user closes the last window, the context tears down:
                # `pages` may return [] or, once the process is gone, raise. Either
                # way it means "closed" — treat both as a clean exit, not an error.
                try:
                    if not browser.pages:
                        break  # user closed the last tab/window
                except Exception:  # noqa: BLE001 - context already gone
                    break


class SessionManager:
    """Registry of running browser sessions keyed by profile id."""

    def __init__(self) -> None:
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = threading.Lock()

    def start(self, profile: Profile) -> None:
        with self._lock:
            session = self._sessions.get(profile.id)
            if session and session.running:
                return
            session = BrowserSession(profile)
            self._sessions[profile.id] = session
        session.start()

    def stop(self, profile_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(profile_id, None)
        if session:
            session.stop()
            return True
        return False

    def is_running(self, profile_id: str) -> bool:
        session = self._sessions.get(profile_id)
        return bool(session and session.running)

    def running_ids(self) -> list[str]:
        return [pid for pid, s in self._sessions.items() if s.running]

    def stop_all(self) -> None:
        for pid in list(self._sessions.keys()):
            self.stop(pid)


manager = SessionManager()
