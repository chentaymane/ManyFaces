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
        "user_data_dir": str(config.profile_data_dir(profile.id)),
        # Camoufox only accepts desktop OS names; an Android profile runs on the
        # Linux engine with its identity spoofed to a phone via `config`.
        "os": "linux" if fp.is_mobile else fp.os,
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
        opts["window"] = (fp.screen_width, fp.screen_height)
        if fp.fonts:
            opts["fonts"] = fp.fonts
            opts["custom_fonts_only"] = True
    else:
        screen = _resolve_screen(fp.os, fp.screen_width, fp.screen_height)
        if screen is not None:
            opts["screen"] = screen
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
            from camoufox.sync_api import Camoufox
        except ImportError:
            self.error = (
                "Camoufox is not installed. Run: pip install -r requirements.txt "
                "&& python -m camoufox fetch"
            )
            self._ready.set()
            return

        opts = build_launch_options(self.profile)
        try:
            self._launch(Camoufox, opts)
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            self._ready.set()

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
                page.goto(self.profile.start_url or "about:blank", wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001 - navigation failure shouldn't close the window
                pass

            self._ready.set()
            # Block until stop() is called or the user closes the window.
            while not self._stop.is_set():
                if self._stop.wait(timeout=1.0):
                    break
                if not getattr(browser, "pages", [None]):
                    break  # user closed the last tab/window


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
