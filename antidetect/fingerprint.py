"""Coherent, deeply-randomized fingerprint generation.

Each profile gets its own *coherent* device fingerprint — the OS, GPU, screen,
locale, hardware, and every noise vector (canvas, audio, fonts, battery, WebRTC,
media devices) agree with one another and stay identical across launches. All
values are pinned through Camoufox's validated native `config` keys, so the profile
looks like the same real machine every session.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, asdict
from typing import Any

# Short OS names used by Camoufox's WebGL/font databases.
_OS_SHORT = {"windows": "win", "macos": "mac", "linux": "lin"}

# Fallback WebGL pairs, used only when Camoufox's GPU database isn't importable at
# all (engine package missing). Strings match Camoufox 0.5's database verbatim —
# it matches pairs exactly, trailing ", or similar" included. A stored pair the
# installed database doesn't know is re-resolved at launch (`resolve_webgl_pair`).
_WEBGL_FALLBACK = {
    "windows": [
        ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 980 Direct3D11 vs_5_0 ps_5_0), or similar"),
        ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) HD Graphics Direct3D11 vs_5_0 ps_5_0), or similar"),
        ("Google Inc. (AMD)", "ANGLE (AMD, Radeon R9 200 Series Direct3D11 vs_5_0 ps_5_0), or similar"),
    ],
    "macos": [("Apple", "Apple M1, or similar")],
    "linux": [("Intel", "Intel(R) HD Graphics, or similar")],
}

# Real displays per OS as (css_width, css_height, device_pixel_ratio). screen.width
# is in CSS px, so it and the DPR must be drawn TOGETHER: css * dpr is the physical
# panel, and it has to be a panel that exists. Sampling them independently built
# machines like 1920x1080 @ 1.25 (a 2400x1350 panel nobody sells). Duplicates weight
# the draw. Macs get Mac panels (Retina built-ins, 5K, plain external monitors).
_DISPLAYS = {
    "windows": [
        (1920, 1080, 1.0), (1920, 1080, 1.0), (1920, 1080, 1.0),
        (1536, 864, 1.25), (1536, 864, 1.25),   # 1080p laptop at 125%
        (1280, 720, 1.5),                       # 1080p laptop at 150%
        (1366, 768, 1.0), (1600, 900, 1.0), (1440, 900, 1.0), (1680, 1050, 1.0),
        (1920, 1200, 1.0), (2560, 1440, 1.0),
        (2048, 1152, 1.25),                     # 1440p at 125%
        (2560, 1440, 1.5), (1920, 1080, 2.0),   # 4K at 150% / 200%
    ],
    "macos": [
        (1440, 900, 2.0), (1512, 982, 2.0), (1470, 956, 2.0), (1728, 1117, 2.0),
        (1280, 832, 2.0), (2560, 1440, 2.0),    # Retina built-ins, 5K display
        (1920, 1080, 1.0), (2560, 1440, 1.0),   # plain external monitors
    ],
    "linux": [
        (1920, 1080, 1.0), (1920, 1080, 1.0), (1920, 1080, 1.0),
        (2560, 1440, 1.0), (1366, 768, 1.0), (1600, 900, 1.0), (1680, 1050, 1.0),
        (1920, 1200, 1.0), (1440, 900, 1.0),
        (1536, 864, 1.25), (1280, 720, 1.5),    # 1080p with fractional scaling
        (2560, 1440, 1.5), (1920, 1080, 2.0),   # 4K at 150% / 200%
    ],
}

# Device-pixel ratios each desktop OS offers, derived from the catalogue (mirrors
# Camoufox's coherence rules, so a stored value is never "repaired" into another).
_DPR_BY_OS = {os_name: sorted({d for _, _, d in rows}) for os_name, rows in _DISPLAYS.items()}


def physical_size(width: int, height: int, dpr: float) -> tuple[int, int]:
    """Panel pixels a CSS-px screen at `dpr` occupies."""
    return round(width * dpr), round(height * dpr)


# Smallest window a fitted (restored, not maximised) desktop window may shrink to,
# absolute and as a share of the profile's screen. Below that it reads as a bot.
_MIN_FIT_WINDOW = (1024, 600)
_MIN_FIT_SHARE = 0.6


def min_window(os_name: str, width: int, height: int) -> tuple[int, int]:
    """Smallest believable desktop window (CSS px) for a `width` x `height` screen.

    The browser never shrinks a headful window below this to fit a display.
    """
    full_h = height - _TASKBAR.get(os_name, 0)
    return (min(width, math.ceil(max(_MIN_FIT_WINDOW[0], _MIN_FIT_SHARE * width))),
            min(full_h, math.ceil(max(_MIN_FIT_WINDOW[1], _MIN_FIT_SHARE * height))))


def opens_in(os_name: str, width: int, height: int, dpr: float, area: tuple[int, int]) -> bool:
    """Whether a desktop with this screen can open headful in `area` monitor pixels
    (Linux renders at the profile's DPR), at most shrunk to its `min_window`."""
    mw, mh = min_window(os_name, width, height)
    return math.ceil(mw * dpr) <= area[0] and math.ceil(mh * dpr) <= area[1]


def displays(os_name: str, max_physical_size: tuple[int, int] | None = None) -> list[tuple[int, int, float]]:
    """The OS's display catalogue, optionally only those that can open headful in a
    `max_physical_size` workarea (monitor pixels; see `opens_in`).

    An empty list means this work area cannot fit any preset for the OS.
    """
    rows = _DISPLAYS.get(os_name, _DISPLAYS["windows"])
    if not max_physical_size:
        return rows
    return [r for r in rows if opens_in(os_name, *r, max_physical_size)]


class DisplayPresetError(ValueError):
    """The requested profile cannot be created with the display fit constraint."""

    def __init__(self, area: tuple[int, int]) -> None:
        super().__init__(f"No matching profile fits the {area[0]} × {area[1]} display work area. "
                         "Choose another operating system or turn off Fit this display.")

# Core counts an Apple Silicon Mac ships with.
_APPLE_SILICON_CORES = [8, 8, 10, 10, 12, 16]

# Taskbar / menu-bar height per OS: screen.availHeight = height - this.
_TASKBAR = {"windows": 48, "macos": 25, "linux": 0}

# (language, region, timezone) that hang together geographically.
_LOCALES = [
    ("en-US", "US", "America/New_York"),
    ("en-US", "US", "America/Chicago"),
    ("en-US", "US", "America/Los_Angeles"),
    ("en-GB", "GB", "Europe/London"),
    ("de-DE", "DE", "Europe/Berlin"),
    ("fr-FR", "FR", "Europe/Paris"),
    ("es-ES", "ES", "Europe/Madrid"),
    ("pt-BR", "BR", "America/Sao_Paulo"),
    ("nl-NL", "NL", "Europe/Amsterdam"),
    ("en-CA", "CA", "America/Toronto"),
]

_CORES = [4, 6, 8, 8, 12, 16]
_SAMPLE_RATES = [44100, 48000]
_DNT = ["unspecified", "1", "0"]
_OS_CHOICES = ["windows", "macos", "linux"]

# --- mobile phone profiles -----------------------------------------------------
# Camoufox is a desktop-Gecko build, so a "phone" profile is mobile-browser
# emulation: the whole navigator/screen/touch/UA surface is pinned to a real
# device, which is what mobile fingerprinting keys off. WebGL exposes the phone's
# GPU vendor/renderer strings (the deep GL parameter set stays desktop-derived —
# Camoufox only ships a desktop GPU database).
#
# Android is the coherent choice (Firefox-for-Android really is Gecko). iOS is
# offered too because it's the most-requested device class, but note the caveat:
# a real iPhone runs Safari/WebKit, while Camoufox runs Gecko. We spoof the whole
# iOS Safari surface (UA, platform, Apple GPU, iOS fonts, touch), so it passes
# casual checks, but an engine-level probe can still tell Gecko from WebKit — an
# iOS profile is inherently weaker than an Android one. It's kept OUT of the
# "Random" pool for that reason and only used when explicitly selected.
_MOBILE_OS = {"android", "ios"}

# Real Android device presets: name, css_w, css_h, dpr, android_ver, gpu_vendor,
# gpu_renderer, cores. Screen sizes are CSS pixels (what screen.width reports).
_ANDROID_DEVICES = [
    ("Google Pixel 6",     412, 915, 2.625, "14", "ARM",      "Mali-G78",        8),
    ("Google Pixel 7",     412, 915, 2.625, "14", "ARM",      "Mali-G710",       8),
    ("Google Pixel 8",     412, 915, 2.625, "14", "ARM",      "Mali-G715",       9),
    ("Google Pixel 9",     412, 923, 2.625, "15", "ARM",      "Mali-G715",       8),
    ("Samsung Galaxy S21", 360, 800, 3.0,   "14", "Qualcomm", "Adreno (TM) 660", 8),
    ("Samsung Galaxy S22", 360, 780, 3.0,   "14", "Qualcomm", "Adreno (TM) 730", 8),
    ("Samsung Galaxy S23", 360, 780, 3.0,   "14", "Qualcomm", "Adreno (TM) 740", 8),
    ("Samsung Galaxy S24", 384, 832, 2.8125,"14", "Qualcomm", "Adreno (TM) 750", 8),
    ("OnePlus 11",         412, 919, 3.5,   "14", "Qualcomm", "Adreno (TM) 740", 8),
    ("OnePlus 12",         412, 919, 3.5,   "14", "Qualcomm", "Adreno (TM) 750", 8),
    ("Xiaomi 13",          393, 873, 3.0,   "14", "Qualcomm", "Adreno (TM) 740", 8),
    ("Xiaomi 14",          393, 873, 3.0,   "14", "Qualcomm", "Adreno (TM) 750", 8),
]

# Real iPhone presets: name, css_w, css_h, dpr, ios_ver (UA "Version/" token uses
# the dotted form), cores. All modern iPhones render WebGL as "Apple GPU".
_IOS_DEVICES = [
    ("iPhone 12",         390, 844, 3.0, "16_7", 6),
    ("iPhone 13",         390, 844, 3.0, "17_5", 6),
    ("iPhone 13 Pro",     390, 844, 3.0, "17_5", 6),
    ("iPhone 14",         390, 844, 3.0, "17_5", 6),
    ("iPhone 14 Pro",     393, 852, 3.0, "17_6", 6),
    ("iPhone 15",         393, 852, 3.0, "17_6", 6),
    ("iPhone 15 Pro",     393, 852, 3.0, "18_1", 6),
    ("iPhone 15 Pro Max", 430, 932, 3.0, "18_1", 6),
    ("iPhone SE (2022)",  375, 667, 2.0, "17_5", 6),
]

# The limited font set a stock Firefox-for-Android exposes (no desktop fonts).
_ANDROID_FONTS = [
    "Roboto", "Roboto Condensed", "Roboto Mono", "Noto Sans", "Noto Serif",
    "Noto Sans Mono", "Noto Color Emoji", "Droid Sans", "Droid Sans Mono",
]

# System fonts an iOS device exposes (no desktop-only fonts leak).
_IOS_FONTS = [
    "Helvetica", "Helvetica Neue", "Arial", "Arial Hebrew", "Times New Roman",
    "Georgia", "Courier", "Courier New", "Verdana", "Trebuchet MS", "Palatino",
    "Avenir", "Avenir Next", "Baskerville", "Bodoni 72", "Cochin", "Copperplate",
    "Didot", "Futura", "Gill Sans", "Hoefler Text", "Marker Felt", "Menlo",
    "Optima", "Papyrus", "Snell Roundhand", "Symbol", "Zapfino",
    "Apple Color Emoji", "Apple SD Gothic Neo",
]


def _firefox_major(executable_path: str | None = None) -> str:
    """Major Firefox version of the Camoufox build that will run, for a matching UA.

    With a bundled/explicit binary its own application.ini is authoritative;
    otherwise it's the build `camoufox fetch` installed.
    """
    if executable_path:
        try:
            from camoufox.utils import resolve_verstr

            return resolve_verstr(executable_path).split(".", 1)[0]
        except Exception:  # noqa: BLE001 - older camoufox; fall through
            pass
    try:
        from camoufox.pkgman import installed_verstr

        return installed_verstr().split(".", 1)[0]
    except Exception:  # noqa: BLE001 - engine not installed yet; use a sane default
        return "135"


# navigator.* values a stock desktop Firefox reports per OS:
# (UA platform token, navigator.platform, navigator.oscpu, appVersion).
_FIREFOX_DESKTOP = {
    "windows": ("Windows NT 10.0; Win64; x64", "Win32", "Windows NT 10.0; Win64; x64", "5.0 (Windows)"),
    "macos": ("Macintosh; Intel Mac OS X 10.15", "MacIntel", "Intel Mac OS X 10.15", "5.0 (Macintosh)"),
    "linux": ("X11; Linux x86_64", "Linux x86_64", "Linux x86_64", "5.0 (X11)"),
}


def firefox_desktop_navigator(os_name: str, major: str) -> dict[str, str]:
    """The navigator identity of a stock desktop Firefox `major` on `os_name`."""
    token, platform, oscpu, app_version = _FIREFOX_DESKTOP.get(os_name, _FIREFOX_DESKTOP["windows"])
    return {
        "navigator.userAgent": f"Mozilla/5.0 ({token}; rv:{major}.0) Gecko/20100101 Firefox/{major}.0",
        "navigator.platform": platform,
        "navigator.oscpu": oscpu,
        "navigator.appVersion": app_version,
    }


def with_firefox_major(user_agent: str, major: str) -> str:
    """Rewrite a stored Firefox UA's version tokens to the engine that actually runs.

    A UA claiming another Firefox than the one answering is a feature-detection
    tell, and the engine gets upgraded under existing profiles.
    """
    import re

    if "Firefox/" not in user_agent:
        return user_agent  # e.g. the iOS Safari UA: no Gecko version to align
    ua = re.sub(r"rv:\d+(\.\d+)?", f"rv:{major}.0", user_agent)
    ua = re.sub(r"Gecko/\d+\.\d+", f"Gecko/{major}.0", ua)  # mobile form
    return re.sub(r"Firefox/\d+(\.\d+)?", f"Firefox/{major}.0", ua)


def _gpu_db():
    """Camoufox's real-device GPU database API (0.5+), or None if unavailable."""
    try:
        from camoufox import webgl

        if hasattr(webgl, "firefox_gpus"):
            return webgl
    except Exception:  # noqa: BLE001 - engine package missing/broken
        pass
    return None


def webgl_pairs(os_name: str) -> list[tuple[str, str]]:
    """Every (vendor, renderer) Camoufox will accept for this desktop OS.

    Software rasterisers and the resistFingerprinting mask are excluded — no
    consumer machine reports them, and they're a classic bot tell. Sorted, so a
    seeded pick is stable across runs.
    """
    db = _gpu_db()
    if db is None:
        return []
    short = _OS_SHORT.get(os_name, "win")
    try:
        from camoufox.coherence import gpu_fits_os
        from camoufox.fingerprints import is_software_renderer
    except Exception:  # noqa: BLE001
        gpu_fits_os = lambda r, o: True  # noqa: E731
        is_software_renderer = lambda r: False  # noqa: E731
    try:
        pairs = db.firefox_gpus(short)
    except Exception:  # noqa: BLE001
        return []
    return sorted(
        (v, r) for v, r in pairs
        if r and r != "Mozilla" and not is_software_renderer(r) and gpu_fits_os(r, short)
    )


def resolve_webgl_pair(os_name: str, vendor: str, renderer: str, seed: str | int = 0) -> tuple[str, str]:
    """Return (vendor, renderer) if Camoufox accepts it, else a valid pair for this OS.

    Camoufox 0.5+ rejects (and aborts the launch on) any pair its database doesn't
    hold, by exact match. Profiles created against an older database can hold such
    pairs, so we swap in a valid one — chosen deterministically from `seed` (the
    profile id), so the GPU stays the same on every launch.
    """
    pairs = webgl_pairs(os_name)
    if not pairs or (vendor, renderer) in pairs:
        return vendor, renderer
    import random as _random

    # Prefer a GPU from the same vendor family so a migrated profile changes as
    # little as possible.
    family = [p for p in pairs if vendor and p[0] == vendor] or pairs
    return _random.Random(f"{seed}|{os_name}").choice(family)


def _sample_webgl(os_name: str, rng, width: int | None = None, height: int | None = None,
                  cores: int | None = None) -> tuple[str, str]:
    """Return a valid (vendor, renderer) pair for this OS.

    Uses Camoufox's own real-GPU database, weighted the way real Firefox users
    report it and kept coherent with the screen and core count (no discrete desktop
    GPU behind a netbook panel, no Intel Mac GPU beside Apple Silicon hardware).
    Falls back to a uniform pick from the valid pairs, then to a small static list,
    so profile creation can never crash on this.
    """
    db = _gpu_db()
    short = _OS_SHORT.get(os_name, "win")
    if db is not None and hasattr(db, "sample_webgl_for_screen"):
        try:
            data = db.sample_webgl_for_screen(short, width, height, seed=rng.getrandbits(32), cores=cores)
            vendor, renderer = data.get("webGl:vendor", ""), data.get("webGl:renderer", "")
            if vendor and renderer:
                return vendor, renderer
        except Exception:  # noqa: BLE001
            pass
    pairs = webgl_pairs(os_name)
    if pairs:
        return rng.choice(pairs)
    return rng.choice(_WEBGL_FALLBACK.get(os_name, _WEBGL_FALLBACK["windows"]))


def _sample_fonts(os_name: str, rng) -> list[str]:
    """Return a plausible font set for this OS (Camoufox 0.4.x only).

    Camoufox 0.5+ draws a realistic per-identity font set itself (an OS base plus
    measured-probability add-ons, aware of the host's own fonts), seeded by the
    pinned identity so it's identical every launch — so desktop profiles leave it
    to Camoufox and store no list. On 0.4.x we keep the old behaviour: the large
    common base with a handful randomly dropped.
    """
    if _gpu_db() is not None:
        return []
    try:
        import camoufox
        import json
        import os as _os

        path = _os.path.join(_os.path.dirname(camoufox.__file__), "fonts.json")
        with open(path, encoding="utf-8") as fh:
            all_fonts = json.load(fh).get(_OS_SHORT.get(os_name, "win"), [])
    except Exception:  # noqa: BLE001
        return []
    if not all_fonts:
        return []
    fonts = list(all_fonts)
    drop = rng.randint(0, min(6, len(fonts) // 10))
    for _ in range(drop):
        fonts.pop(rng.randrange(len(fonts)))
    return fonts


def _private_ipv4(rng) -> str:
    """Random RFC1918 local IP for WebRTC local-candidate spoofing."""
    block = rng.choice(["192.168", "10.0", "172.16"])
    return f"{block}.{rng.randint(0, 254)}.{rng.randint(2, 254)}"


@dataclass
class Fingerprint:
    os: str = "windows"
    # Mobile (Android) emulation. is_mobile flips the whole config to a phone
    # identity; the fields below are empty/False for desktop profiles.
    is_mobile: bool = False
    device_name: str = ""
    user_agent: str = ""
    app_version: str = ""
    platform: str = ""
    oscpu: str = ""
    screen_width: int = 1920
    screen_height: int = 1080
    webgl_vendor: str = ""
    webgl_renderer: str = ""
    hardware_concurrency: int = 8
    device_memory: int = 8  # display only (Firefox doesn't expose navigator.deviceMemory)
    language: str = "en-US"
    region: str = "US"
    timezone: str = "America/New_York"
    # --- deep randomization vectors -------------------------------------------
    color_depth: int = 24
    device_pixel_ratio: float = 1.0
    max_touch_points: int = 0
    do_not_track: str = "unspecified"
    audio_sample_rate: int = 48000
    audio_channels: int = 2
    canvas_aa_offset: int = 0       # shifts canvas anti-aliasing -> canvas noise
    canvas_aa_cap_offset: bool = True
    fonts: list[str] = field(default_factory=list)
    fonts_spacing_seed: int = 0     # per-profile font-metric noise
    battery_charging: bool = True
    battery_level: float = 1.0
    battery_charging_time: float = 0.0
    battery_discharging_time: float = 0.0
    webcams: int = 0
    micros: int = 1
    speakers: int = 1
    webrtc_local_ipv4: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Fingerprint":
        known = {f: d[f] for f in cls.__dataclass_fields__ if f in d}
        return cls(**known)

    @property
    def locale(self) -> str:
        return f"{self.language},{self.language.split('-')[0]}"

    def camoufox_config(self, ff_major: str | None = None) -> dict[str, Any]:
        """All native property overrides pinned via Camoufox's `config` argument.

        The whole visible identity is pinned — navigator, screen, hardware, locale
        and the noise vectors — so the profile is the same machine every launch.
        Pinning `navigator.userAgent` matters twice over: Camoufox 0.5+ only treats
        a caller's config as a fixed identity (stable salt for every value it draws
        itself: voices, fonts, GL parameters, media devices, noise seeds) when it
        names the UA; without it all of those are re-rolled on each launch.

        WebGL is NOT here: desktop GPUs go through the validated `webgl_config`
        argument, and phone GPUs (absent from Camoufox's desktop-only database)
        are written into the final config after validation (see browser.py).
        `ff_major` is the Firefox major of the engine that will run; the UA must
        name it, or feature detection gives the spoof away.
        """
        ff = ff_major or _firefox_major()
        cfg: dict[str, Any] = {
            "navigator.hardwareConcurrency": self.hardware_concurrency,
            "navigator.doNotTrack": self.do_not_track,
            "navigator.maxTouchPoints": self.max_touch_points,
            "screen.colorDepth": self.color_depth,
            "screen.pixelDepth": self.color_depth,
            "window.devicePixelRatio": self.device_pixel_ratio,
            "AudioContext:sampleRate": self.audio_sample_rate,
            "AudioContext:maxChannelCount": self.audio_channels,
            "canvas:aaOffset": self.canvas_aa_offset,
            "canvas:aaCapOffset": self.canvas_aa_cap_offset,
            "fonts:spacing_seed": self.fonts_spacing_seed,
            "battery:charging": self.battery_charging,
            "battery:level": self.battery_level,
            "battery:chargingTime": self.battery_charging_time,
            "battery:dischargingTime": self.battery_discharging_time,
            "mediaDevices:webcams": self.webcams,
            "mediaDevices:micros": self.micros,
            "mediaDevices:speakers": self.speakers,
            "timezone": self.timezone,
        }
        if self.fonts:
            cfg["fonts"] = self.fonts
        if self.is_mobile:
            # Pin the phone identity directly. These land in `config` before
            # Camoufox merges its (desktop) generated fingerprint, and its merge
            # never overwrites keys we've already set — so ours win.
            ua = with_firefox_major(self.user_agent, ff)
            cfg.update({
                "navigator.userAgent": ua,
                "navigator.appVersion": _app_version(self.os, ua),
                "navigator.appCodeName": "Mozilla",
                "navigator.platform": self.platform,
                "navigator.oscpu": self.oscpu,
                "screen.width": self.screen_width,
                "screen.height": self.screen_height,
                "screen.availWidth": self.screen_width,
                "screen.availHeight": self.screen_height,
                "window.innerWidth": self.screen_width,
                "window.innerHeight": self.screen_height,
                "window.outerWidth": self.screen_width,
                "window.outerHeight": self.screen_height,
                "window.screenX": 0,
                "window.screenY": 0,
            })
        else:
            cfg.update(firefox_desktop_navigator(self.os, ff))
            # macOS's menu bar sits on top (availTop 25); Windows' taskbar at the
            # bottom. The window is placed maximised at the top-left of the
            # available area, so its position is stable too.
            top = 25 if self.os == "macos" else 0
            cfg.update({
                "screen.width": self.screen_width,
                "screen.height": self.screen_height,
                "screen.availWidth": self.screen_width,
                "screen.availHeight": self.screen_height - _TASKBAR.get(self.os, 0),
                "screen.availTop": top,
                "screen.availLeft": 0,
                "window.screenX": 0,
                "window.screenY": top,
            })
        return cfg

    def mobile_overrides(self, ff_major: str | None = None) -> dict[str, Any]:
        """Phone values to force into the FINAL Camoufox config, after its checks.

        Camoufox's coherence pass assumes a desktop: it would snap a 2.625 DPR to
        2, zero the touch points on an iPhone's macOS base, and shrink the viewport
        to make room for desktop window chrome. Its WebGL database is desktop-only
        too, so the phone GPU can't pass validation. These are written back over
        the validated config (see browser.py), natively — not via page JS.
        """
        if not self.is_mobile:
            return {}
        keys = (
            "navigator.userAgent", "navigator.appVersion", "navigator.platform",
            "navigator.oscpu", "navigator.maxTouchPoints", "navigator.hardwareConcurrency",
            "screen.width", "screen.height", "screen.availWidth", "screen.availHeight",
            "screen.colorDepth", "screen.pixelDepth", "window.devicePixelRatio",
            "window.innerWidth", "window.innerHeight", "window.outerWidth",
            "window.outerHeight", "window.screenX", "window.screenY",
        )
        cfg = self.camoufox_config(ff_major)
        out = {k: cfg[k] for k in keys if k in cfg}
        if self.webgl_vendor and self.webgl_renderer:
            out["webGl:vendor"] = self.webgl_vendor
            out["webGl:renderer"] = self.webgl_renderer
        return out

    def webgl_config(self) -> tuple[str, str] | None:
        """The (vendor, renderer) pair for Camoufox's `webgl_config` argument.

        Mobile profiles return None: Camoufox's GPU database is desktop-only, so
        their GPU strings are written into the final config instead (see
        `mobile_overrides`).
        """
        if self.is_mobile:
            return None
        if self.webgl_vendor and self.webgl_renderer:
            return (self.webgl_vendor, self.webgl_renderer)
        return None


def _app_version(os_name: str, user_agent: str) -> str:
    """navigator.appVersion as the real browser reports it for this UA.

    Firefox reports a short platform form ("5.0 (Android 14)"); Safari reports
    the UA minus its "Mozilla/" prefix.
    """
    if os_name == "android":
        import re

        m = re.search(r"Android (\d+)", user_agent)
        return f"5.0 (Android {m.group(1) if m else '14'})"
    return user_agent.split("Mozilla/", 1)[-1]


def normalize(fp: Fingerprint, seed: str | int = 0) -> Fingerprint:
    """Bring a stored fingerprint in line with what the installed Camoufox accepts.

    Returns a NEW fingerprint (the input is untouched); callers compare with `==`
    to decide whether to persist it. Fixes, deterministically per `seed`:
      - a desktop GPU pair the current database doesn't hold (would abort launch);
      - desktop values Camoufox's coherence pass would silently rewrite anyway
        (a Mac with touch points or a 1.25 DPR, an Apple Silicon GPU with 6 cores
        or 24-bit colour), so what the UI shows is what pages see;
      - a stored desktop font list on 0.5+, where Camoufox draws a realistic,
        host-aware per-identity set itself.
    """
    d = fp.to_dict()
    if not fp.is_mobile:
        if fp.webgl_vendor and fp.webgl_renderer:
            d["webgl_vendor"], d["webgl_renderer"] = resolve_webgl_pair(
                fp.os, fp.webgl_vendor, fp.webgl_renderer, seed=seed
            )
        # The catalogue guides new profiles. Preserve supported DPR values on
        # saved profiles even if that screen/DPR pair is no longer generated.
        allowed_dpr = _DPR_BY_OS.get(fp.os, _DPR_BY_OS["windows"])
        if fp.device_pixel_ratio not in allowed_dpr:
            d["device_pixel_ratio"] = min(allowed_dpr, key=lambda v: (abs(v - fp.device_pixel_ratio), v))
        if d["color_depth"] not in (24, 30):
            d["color_depth"] = 24
        if fp.os == "macos":
            d["max_touch_points"] = 0
            if "Apple M" in d["webgl_renderer"]:
                d["color_depth"] = 30
                if d["hardware_concurrency"] not in _APPLE_SILICON_CORES:
                    d["hardware_concurrency"] = min(
                        (c for c in sorted(set(_APPLE_SILICON_CORES)) if c >= d["hardware_concurrency"]),
                        default=16,
                    )
        if d["fonts"] and _gpu_db() is not None:
            d["fonts"] = []
    elif fp.os == "android":
        d["app_version"] = _app_version("android", fp.user_agent)
    return Fingerprint.from_dict(d)


def list_mobile_devices() -> list[dict[str, Any]]:
    """All selectable phone presets, for the 'New Phone' device picker.

    Android first (the stronger, coherent spoof), then iPhone. `name` is the stable
    id passed back to `generate(device=…)`.
    """
    out: list[dict[str, Any]] = []
    for name, w, h, dpr, andver, gpu_v, gpu_r, cores in _ANDROID_DEVICES:
        out.append({"name": name, "os": "android", "screen": f"{w}×{h}", "dpr": dpr})
    for name, w, h, dpr, iosver, cores in _IOS_DEVICES:
        out.append({"name": name, "os": "ios", "screen": f"{w}×{h}", "dpr": dpr})
    return out


def _find_device(name: str):
    """Return ("android"|"ios", preset tuple) for a device name, or None."""
    for d in _ANDROID_DEVICES:
        if d[0] == name:
            return "android", d
    for d in _IOS_DEVICES:
        if d[0] == name:
            return "ios", d
    return None


def _generate_android(rng, device=None) -> Fingerprint:
    """Generate a coherent Firefox-for-Android phone fingerprint.

    Pass `device` (a preset tuple) to pin a specific phone; otherwise one is picked
    at random.
    """
    name, w, h, dpr, andver, gpu_v, gpu_r, cores = device or rng.choice(_ANDROID_DEVICES)
    lang, region, tz = rng.choice(_LOCALES)
    ff = _firefox_major()
    ua = f"Mozilla/5.0 (Android {andver}; Mobile; rv:{ff}.0) Gecko/{ff}.0 Firefox/{ff}.0"

    # Phones almost always run on battery; sometimes plugged in and charging.
    charging = rng.random() < 0.35
    level = round(rng.uniform(0.2, 0.98), 2)

    return Fingerprint(
        os="android",
        is_mobile=True,
        device_name=name,
        user_agent=ua,
        app_version=_app_version("android", ua),
        platform="Linux armv8l",
        oscpu="Linux armv8l",
        screen_width=w,
        screen_height=h,
        webgl_vendor=gpu_v,
        webgl_renderer=gpu_r,
        hardware_concurrency=cores,
        device_memory=rng.choice([4, 6, 8, 8, 12]),
        language=lang,
        region=region,
        timezone=tz,
        color_depth=24,
        device_pixel_ratio=dpr,
        max_touch_points=5,
        do_not_track=rng.choice(_DNT),
        audio_sample_rate=48000,
        audio_channels=2,
        canvas_aa_offset=rng.randint(-8, 8),
        canvas_aa_cap_offset=rng.random() < 0.5,
        fonts=list(_ANDROID_FONTS),
        fonts_spacing_seed=rng.randint(0, 1_000_000),
        battery_charging=charging,
        battery_level=level,
        battery_charging_time=float(rng.choice([0, 900, 1800, 2700])) if charging else 0.0,
        battery_discharging_time=0.0 if charging else float(rng.choice([3600, 7200, 10800, 14400])),
        webcams=rng.choice([1, 2, 2]),
        micros=1,
        speakers=1,
        webrtc_local_ipv4=_private_ipv4(rng),
    )


def _generate_ios(rng, device=None) -> Fingerprint:
    """Generate a best-effort iOS-Safari iPhone fingerprint.

    NOTE: Camoufox runs Gecko, not WebKit, so this is a spoof of the visible iOS
    Safari surface (UA, platform, Apple GPU, iOS fonts, touch), not a real WebKit
    engine — an engine-level probe can still distinguish it. Use Android for the
    strongest mobile identity; use this when an iPhone specifically is required.

    Pass `device` (a preset tuple) to pin a specific iPhone; otherwise random.
    """
    name, w, h, dpr, iosver, cores = device or rng.choice(_IOS_DEVICES)
    lang, region, tz = rng.choice(_LOCALES)
    dotted = iosver.replace("_", ".")
    ua = (
        f"Mozilla/5.0 (iPhone; CPU iPhone OS {iosver} like Mac OS X) "
        f"AppleWebKit/605.1.15 (KHTML, like Gecko) "
        f"Version/{dotted} Mobile/15E148 Safari/604.1"
    )

    charging = rng.random() < 0.35
    level = round(rng.uniform(0.2, 0.98), 2)

    return Fingerprint(
        os="ios",
        is_mobile=True,
        device_name=name,
        user_agent=ua,
        app_version=ua.split("Mozilla/", 1)[-1],  # navigator.appVersion drops the prefix
        platform="iPhone",
        # Real iOS Safari has no navigator.oscpu; keep it empty rather than leak a
        # desktop value. (Camoufox pins whatever we set here.)
        oscpu="",
        screen_width=w,
        screen_height=h,
        webgl_vendor="Apple Inc.",
        webgl_renderer="Apple GPU",
        hardware_concurrency=cores,
        device_memory=rng.choice([4, 6, 6, 8]),
        language=lang,
        region=region,
        timezone=tz,
        color_depth=24,
        device_pixel_ratio=dpr,
        max_touch_points=5,
        do_not_track=rng.choice(_DNT),
        audio_sample_rate=48000,
        audio_channels=2,
        canvas_aa_offset=rng.randint(-8, 8),
        canvas_aa_cap_offset=rng.random() < 0.5,
        fonts=list(_IOS_FONTS),
        fonts_spacing_seed=rng.randint(0, 1_000_000),
        battery_charging=charging,
        battery_level=level,
        battery_charging_time=float(rng.choice([0, 900, 1800, 2700])) if charging else 0.0,
        battery_discharging_time=0.0 if charging else float(rng.choice([3600, 7200, 10800, 14400])),
        webcams=rng.choice([1, 2, 2]),
        micros=1,
        speakers=1,
        webrtc_local_ipv4=_private_ipv4(rng),
    )


# Weighted pool for "Random": mostly desktop, with a realistic minority of phones.
# iOS is intentionally excluded — it's the weaker spoof (Gecko under an iOS UA), so
# it's only ever produced when a user explicitly asks for it, never by the random mix.
_RANDOM_OS_POOL = ["windows", "windows", "macos", "linux", "android", "android"]


def generate(
    os_name: str | None = None,
    seed: str | None = None,
    device: str | None = None,
    max_physical_size: tuple[int, int] | None = None,
) -> Fingerprint:
    """Generate a fresh, internally-coherent, deeply-randomized fingerprint.

    With no `os_name`, one is drawn from `_RANDOM_OS_POOL`, which now includes
    Android — so bulk/random creation yields a natural mix of desktop and phone
    profiles. Pass `device` (a preset name from `list_mobile_devices()`) to pin a
    specific phone model; it overrides `os_name`.

    `max_physical_size` (a workarea, in monitor pixels) keeps a desktop to real
    displays that can open headful there (`opens_in`); it never invents a
    resolution. A random OS draw under a limit uses desktop OSes with fitting
    presets. Explicit phone presets retain their fixed geometry.
    """
    rng = random.Random(seed) if seed else random
    if device:
        found = _find_device(device)
        if found:
            kind, tup = found
            return _generate_ios(rng, tup) if kind == "ios" else _generate_android(rng, tup)
        # Unknown device name: fall back to os-based generation below.
    if os_name is None:
        pool = _RANDOM_OS_POOL
        if max_physical_size:
            pool = [o for o in pool if o not in _MOBILE_OS and displays(o, max_physical_size)]
            if not pool:
                raise DisplayPresetError(max_physical_size)
        os_name = rng.choice(pool)
    if os_name == "ios":
        return _generate_ios(rng)
    if os_name in _MOBILE_OS:
        return _generate_android(rng)
    os_name = os_name if os_name in _OS_SHORT else rng.choice(_OS_CHOICES)

    available = displays(os_name, max_physical_size)
    if max_physical_size and not available:
        raise DisplayPresetError(max_physical_size)
    w, h, dpr = rng.choice(available)
    lang, region, tz = rng.choice(_LOCALES)
    cores = rng.choice(_CORES)
    vendor, renderer = _sample_webgl(os_name, rng, w, h, cores)
    color_depth = rng.choice([24, 24, 24, 30])
    touch = 0 if rng.random() < 0.85 else rng.choice([1, 5, 10])
    if os_name == "macos":
        touch = 0  # no Mac has a touchscreen
        if "Apple M" in renderer:
            # Apple Silicon: deep colour by default and at least 8 cores.
            color_depth = 30
            cores = rng.choice(_APPLE_SILICON_CORES)

    # Laptops carry a battery that discharges; desktops report charging & full.
    is_laptop = rng.random() < 0.6
    if is_laptop:
        charging = rng.random() < 0.5
        level = round(rng.uniform(0.15, 0.98), 2)
        charging_time = float(rng.choice([0, 600, 1200, 1800, 2400])) if charging else 0.0
        discharging_time = 0.0 if charging else float(rng.choice([3600, 7200, 10800, 14400]))
    else:
        charging, level, charging_time, discharging_time = True, 1.0, 0.0, 0.0

    return Fingerprint(
        os=os_name,
        screen_width=w,
        screen_height=h,
        webgl_vendor=vendor,
        webgl_renderer=renderer,
        hardware_concurrency=cores,
        device_memory=rng.choice([4, 8, 8, 16, 16, 32]),
        language=lang,
        region=region,
        timezone=tz,
        color_depth=color_depth,
        device_pixel_ratio=dpr,
        max_touch_points=touch,
        do_not_track=rng.choice(_DNT),
        audio_sample_rate=rng.choice(_SAMPLE_RATES),
        audio_channels=2,
        canvas_aa_offset=rng.randint(-8, 8),
        canvas_aa_cap_offset=rng.random() < 0.5,
        fonts=_sample_fonts(os_name, rng),
        fonts_spacing_seed=rng.randint(0, 1_000_000),
        battery_charging=charging,
        battery_level=level,
        battery_charging_time=charging_time,
        battery_discharging_time=discharging_time,
        webcams=rng.choice([0, 0, 1, 1, 2]),
        micros=rng.choice([0, 1, 1, 2]),
        speakers=rng.choice([1, 1, 2]),
        webrtc_local_ipv4=_private_ipv4(rng),
    )
