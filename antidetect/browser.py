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

import json
import threading
import traceback
import warnings
from typing import Any, Optional

from . import config, cookies as cookie_store, fingerprint as fp_mod
from .models import Profile


# Phone profiles deliberately expose only the phone's fonts; Camoufox warns about
# that on every launch (it assumes a desktop identity), which is just noise here.
warnings.filterwarnings("ignore", message="Disabling OS-specific fonts")


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

    The binary stays in the bundle folder and is passed as `executable_path`.
    Camoufox 0.5+ reads the version from the binary's own application.ini, so
    nothing else is needed. Camoufox 0.4.x read INSTALL_DIR/version.json even with
    an explicit path, so for it we copy that tiny file over (never on 0.5+, whose
    versioned cache would treat a stray root version.json as a stale install and
    wipe the directory). Returns True if a bundled browser is present.
    """
    bundle = config.bundled_browser_dir()
    if not bundle:
        return False
    try:
        import camoufox.multiversion  # noqa: F401 - 0.5+ layout: nothing to copy
        return True
    except ImportError:
        pass
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


def _engine_exe() -> str | None:
    """The Camoufox binary that will run: the bundled one, else Camoufox's choice."""
    return config.bundled_browser_exe()


def engine_installed() -> bool:
    """Whether a Camoufox browser is available to launch without a download."""
    if _engine_exe():
        return True
    try:
        from camoufox.pkgman import installed_verstr

        installed_verstr()
        return True
    except Exception:  # noqa: BLE001 - not installed / package missing
        return False


def _supported_properties(exe: str | None) -> dict[str, dict] | None:
    """The engine's declared config properties (properties.json), or None if unknown."""
    try:
        from pathlib import Path

        from camoufox.utils import _load_properties

        return _load_properties(Path(exe) if exe else None)
    except Exception:  # noqa: BLE001 - older/newer camoufox layout; skip filtering
        return None


def _drop_unsupported(cfg: dict[str, Any], exe: str | None) -> None:
    """Remove config keys the installed engine no longer reads.

    Newer Camoufox builds retired several properties (canvas:aaOffset, battery:*,
    fonts:spacing_seed, webrtc:localipv4, …) — they now handle those surfaces
    natively. Passing them is a no-op that only spams "Skipping …" warnings, so we
    keep them in the stored fingerprint (older engines still use them) but drop
    them from the launch.
    """
    props = _supported_properties(exe)
    if not props:
        return
    for key in list(cfg):
        entry = props.get(key)
        if entry is None or "removed" in entry:
            cfg.pop(key, None)


def _host_screen() -> tuple[int, int] | None:
    """Usable size of the primary display, for sizing desktop windows to fit."""
    try:
        from screeninfo import get_monitors

        mons = get_monitors()
        primary = next((m for m in mons if getattr(m, "is_primary", False)), mons[0] if mons else None)
        if primary:
            return int(primary.width), int(primary.height)
    except Exception:  # noqa: BLE001 - headless box / no display / screeninfo missing
        pass
    return None


def _desktop_window(fp) -> tuple[int, int]:
    """Outer window size for a desktop profile: maximised on its spoofed screen,
    but never larger than the real display (else the window hangs off-screen)."""
    w = fp.screen_width
    h = fp.screen_height - fp_mod._TASKBAR.get(fp.os, 0)
    host = _host_screen()
    if host:
        w = min(w, host[0])
        h = min(h, max(480, host[1] - 48))
    return w, h


def _mobile_base_os(fp) -> str:
    """The desktop OS Camoufox will treat a phone profile as.

    Camoufox derives its target OS from the user agent, not the `os` argument, and
    both phone UAs (Android, iOS) resolve to Linux — so WebGL validation, fonts and
    the coherence rules all run against Linux.
    """
    try:
        from camoufox.utils import determine_ua_os

        short = determine_ua_os(fp.user_agent)
        return {"win": "windows", "mac": "macos", "lin": "linux"}.get(short, "linux")
    except Exception:  # noqa: BLE001
        return "linux"


def _mobile_base_gpu(fp, seed: str) -> tuple[str, str]:
    """A database-valid GPU for the desktop OS a phone is emulated on.

    It only has to pass validation and supply the deep GL parameter set; the
    phone's own vendor/renderer strings are written over it afterwards.
    """
    return fp_mod.resolve_webgl_pair(_mobile_base_os(fp), "", "", seed=seed)


def build_launch_options(profile: Profile, headless: bool | None = None) -> dict[str, Any]:
    """Build the Camoufox launch arguments for a profile (`Camoufox(**opts)` form).

    Prefer `camoufox_launch_options()`, which runs these through Camoufox and then
    applies the phone pins Camoufox's desktop-only validation can't accept.
    """
    fp = profile.fingerprint.to_fingerprint()
    geoip_active = profile.geoip and profile.proxy.is_set
    exe = _engine_exe()
    ff_major = fp_mod._firefox_major(exe)

    cfg = fp.camoufox_config(ff_major)
    if geoip_active:
        # Let GeoIP derive the timezone from the proxy's exit IP so it can't
        # contradict the real network location; keep our pinned value otherwise.
        cfg.pop("timezone", None)
    if not profile.block_webrtc and fp.webrtc_local_ipv4:
        # Older engines only; newer ones retired this key (dropped below).
        cfg["webrtc:localipv4"] = fp.webrtc_local_ipv4
    _drop_unsupported(cfg, exe)

    opts: dict[str, Any] = {
        "headless": False if headless is None else headless,
        "persistent_context": True,
        # Don't let Playwright enforce its own viewport: Juggler rejects its
        # `isMobile` field and a spoofed window size deadlocks it. We pin screen and
        # window dimensions ourselves (config + the `window` option).
        "no_viewport": True,
        "user_data_dir": str(config.profile_data_dir(profile.id)),
        # Camoufox only accepts desktop OS names, so a phone profile runs on the
        # desktop base its UA maps to (Linux) with its identity pinned via `config`.
        "os": _mobile_base_os(fp) if fp.is_mobile else fp.os,
        "locale": fp.locale,
        "humanize": profile.humanize,
        "block_webrtc": profile.block_webrtc,
        "geoip": geoip_active,
        "config": cfg,
        # Every manual pin above is deliberate (that's what keeps a profile the same
        # device); don't print a LeakWarning for each one on every launch.
        "i_know_what_im_doing": True,
    }
    if exe:
        opts["executable_path"] = exe  # use the shipped browser, never download
    if fp.is_mobile:
        # Validation-only base GPU; the phone's strings replace it after launch
        # options are built (mobile_overrides).
        opts["webgl_config"] = _mobile_base_gpu(fp, profile.id)
        # Size the real window to the phone and expose ONLY the phone's fonts
        # (custom_fonts_only stops Camoufox merging the desktop font set, which
        # would otherwise leak a desktop identity).
        opts["window"] = (fp.screen_width, fp.screen_height)
        if fp.fonts:
            opts["fonts"] = fp.fonts
            opts["custom_fonts_only"] = True
        # Make it behave like a real phone, not just a narrow desktop window:
        # native touch events (`ontouchstart`, `TouchEvent`, `pointer: coarse`,
        # `hover: none`) and the meta-viewport tag, so pages render their true
        # mobile layout. Without these a phone UA still gets a desktop layout.
        opts["firefox_user_prefs"] = {
            "dom.w3c_touch_events.enabled": 1,
            "dom.w3c_touch_events.legacy_apis.enabled": True,
            "dom.meta-viewport.enabled": True,
            "apz.allow_zooming": True,
            # Report a touchscreen's pointer to CSS: primary/all pointer = coarse
            # (bit 0x01), no hover.
            "ui.primaryPointerCapabilities": 1,
            "ui.allPointerCapabilities": 1,
        }
    else:
        webgl = fp.webgl_config()
        if webgl is not None:
            # Camoufox 0.5+ aborts the launch on a pair its database doesn't hold;
            # normalize() has already migrated stored profiles, this is a backstop.
            opts["webgl_config"] = fp_mod.resolve_webgl_pair(fp.os, *webgl, seed=profile.id)
        opts["window"] = _desktop_window(fp)
    if profile.proxy.is_set:
        opts["proxy"] = profile.proxy.playwright_dict()
    return opts


def _read_camou_config(env: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    keys = sorted((k for k in env if k.startswith("CAMOU_CONFIG_")), key=lambda k: int(k.rsplit("_", 1)[1]))
    blob = "".join(str(env[k]) for k in keys)
    return (json.loads(blob) if blob else {}), keys


def _write_camou_config(env: dict[str, Any], cfg: dict[str, Any], old_keys: list[str]) -> None:
    for k in old_keys:
        env.pop(k, None)
    blob = json.dumps(cfg, ensure_ascii=False, separators=(",", ":"))
    # 2047 is Windows' per-variable limit Camoufox chunks to; safe everywhere.
    size = 2047
    for i in range(0, len(blob), size):
        env[f"CAMOU_CONFIG_{i // size + 1}"] = blob[i:i + size]


def _apply_overrides(from_options: dict[str, Any], overrides: dict[str, Any]) -> None:
    """Write values into the final, already-validated Camoufox config (natively —
    the browser reads them from its environment at startup, no page JS)."""
    if not overrides:
        return
    env = dict(from_options.get("env") or {})
    cfg, keys = _read_camou_config(env)
    cfg.update(overrides)
    renderer = overrides.get("webGl:renderer")
    if renderer:
        vendor = overrides.get("webGl:vendor", "")
        for pkey in ("webGl:parameters", "webGl2:parameters"):
            params = cfg.get(pkey)
            if isinstance(params, dict):
                params["7937"] = renderer  # RENDERER (Firefox reports the real one)
                if "37445" in params:
                    params["37445"] = vendor  # UNMASKED_VENDOR_WEBGL
                if "37446" in params:
                    params["37446"] = renderer  # UNMASKED_RENDERER_WEBGL
    _write_camou_config(env, cfg, keys)
    from_options["env"] = env


def camoufox_launch_options(profile: Profile, headless: bool | None = None) -> dict[str, Any]:
    """Final Playwright launch options for a Camoufox profile.

    Runs our arguments through Camoufox's own `launch_options()` (fingerprint
    generation, validation, fonts, GeoIP, …), then writes the phone pins that its
    desktop-only checks would reject or "repair" back into the result. Pass the
    result to `Camoufox(from_options=…, persistent_context=True)`.
    """
    import inspect

    from camoufox.utils import launch_options

    opts = build_launch_options(profile, headless=headless)
    opts.pop("persistent_context", None)
    if "pin_cpu_cores" in inspect.signature(launch_options).parameters:
        opts["pin_cpu_cores"] = False  # what Camoufox's own launcher defaults to
    from_options = launch_options(**opts)
    fp = profile.fingerprint.to_fingerprint()
    _apply_overrides(from_options, fp.mobile_overrides(fp_mod._firefox_major(_engine_exe())))
    return from_options


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


def log_error(where: str, detail: str) -> None:
    """Append a traceback to ~/.antidetect/error.log (the windowed app has no console)."""
    try:
        config.ensure_dirs()
        with (config.DATA_DIR / "error.log").open("a", encoding="utf-8") as fh:
            fh.write(f"--- {where} ---\n{detail}\n")
    except Exception:  # noqa: BLE001
        pass


def friendly_error(exc: Exception) -> str:
    """A one-glance explanation of a launch failure (full traceback goes to error.log)."""
    name = type(exc).__name__
    msg = str(exc).strip()
    low = msg.lower()
    if name == "CamoufoxNotInstalled" or "camoufox fetch" in low:
        return "The Camoufox browser isn't installed. Click \"Download engine\" in the app, or run: python -m camoufox fetch"
    if "executable doesn't exist" in low:
        return "Chromium is not installed for Playwright. Run: python -m playwright install chromium"
    if name == "InvalidProxy" or ("proxy" in low and ("refused" in low or "connect" in low)):
        return f"The proxy didn't work: {msg.splitlines()[0] if msg else name}. Test it in the profile editor."
    if "user data directory is already in use" in low or "profile appears to be in use" in low:
        return "This profile's browser data is locked by another running browser. Close it and retry."
    first = msg.splitlines()[0] if msg else ""
    if len(first) > 400:
        first = first[:400] + "…"
    return f"{name}: {first}" if first else name


class BrowserSession:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.error: Optional[str] = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def status(self) -> str:
        """launching | running | error | stopped."""
        if self.alive:
            return "error" if self.error else ("running" if self._ready.is_set() else "launching")
        return "error" if self.error else "stopped"

    @property
    def running(self) -> bool:
        return self.status == "running"

    def start(self) -> None:
        """Non-blocking: kick off the browser on its own thread and return at once.
        Callers poll `status` for progress instead of waiting here (a launch can take
        seconds, or minutes for the first Android boot — blocking froze the UI)."""
        if self.alive:
            return
        self._stop.clear()
        self._ready.clear()
        self.error = None
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"browser-{self.profile.id}")
        self._thread.start()

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
            self.error = friendly_error(exc)
            log_error(f"launch {self.profile.id} ({self.profile.engine})", traceback.format_exc())
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
        if not engine_installed():
            # Without this, Camoufox silently starts a ~1.3 GB download inside the
            # launch with no progress shown — it looks like a hang.
            self.error = (
                "The Camoufox browser isn't downloaded yet. Click \"Download engine\" "
                "in the banner at the top of the app (or run: python -m camoufox fetch)."
            )
            self._ready.set()
            return
        from_options = camoufox_launch_options(self.profile)
        self._launch(Camoufox, from_options)

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

    def _launch(self, Camoufox, from_options: dict[str, Any]) -> None:
        with Camoufox(from_options=from_options, persistent_context=True) as browser:
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
            if session and session.status in ("launching", "running"):
                return  # already up or coming up — don't launch a second window
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

    def status(self, profile_id: str) -> str:
        session = self._sessions.get(profile_id)
        return session.status if session else "stopped"

    def error(self, profile_id: str) -> Optional[str]:
        session = self._sessions.get(profile_id)
        return session.error if session else None

    def running_ids(self) -> list[str]:
        with self._lock:
            return [pid for pid, s in self._sessions.items() if s.status in ("running", "launching")]

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._sessions)
        for pid in ids:
            self.stop(pid)


manager = SessionManager()
