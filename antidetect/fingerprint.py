"""Coherent, deeply-randomized fingerprint generation.

Each profile gets its own *coherent* device fingerprint — the OS, GPU, screen,
locale, hardware, and every noise vector (canvas, audio, fonts, battery, WebRTC,
media devices) agree with one another and stay identical across launches. All
values are pinned through Camoufox's validated native `config` keys, so the profile
looks like the same real machine every session.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field, asdict
from typing import Any

# Short OS names used by Camoufox's WebGL/font databases.
_OS_SHORT = {"windows": "win", "macos": "mac", "linux": "lin"}

# Fallback WebGL pairs (only used if Camoufox's own GPU database isn't importable).
_WEBGL_FALLBACK = {
    "windows": [
        ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 980 Direct3D11 vs_5_0 ps_5_0)"),
        ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) HD Graphics Direct3D11 vs_5_0 ps_5_0)"),
        ("Google Inc. (AMD)", "ANGLE (AMD, Radeon R9 200 Series Direct3D11 vs_5_0 ps_5_0)"),
    ],
    "macos": [("Apple", "Apple M1")],
    "linux": [("Intel", "Mesa Intel(R) UHD Graphics 620 (KBL GT2)")],
}

# Common desktop resolutions (width, height).
_SCREENS = [
    (1920, 1080), (1536, 864), (1366, 768), (2560, 1440),
    (1440, 900), (1600, 900), (1680, 1050), (1280, 720),
]

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
_DEVICE_PIXEL_RATIOS = [1.0, 1.0, 1.25, 1.5, 2.0]
_SAMPLE_RATES = [44100, 48000]
_DNT = ["unspecified", "1", "0"]
_OS_CHOICES = ["windows", "macos", "linux"]


def _sample_webgl(os_name: str, rng) -> tuple[str, str]:
    """Return a valid (vendor, renderer) pair for this OS.

    Prefers Camoufox's own real-GPU database (guaranteed accepted by `webgl_config`);
    falls back to a small static list if Camoufox isn't installed.
    """
    try:
        from camoufox.webgl import sample_webgl
    except ImportError:
        return rng.choice(_WEBGL_FALLBACK.get(os_name, _WEBGL_FALLBACK["windows"]))
    data = sample_webgl(_OS_SHORT.get(os_name, "win"))
    return data.get("webGl:vendor", ""), data.get("webGl:renderer", "")


def _sample_fonts(os_name: str, rng) -> list[str]:
    """Return a plausible font set for this OS from Camoufox's font database.

    Real machines share most system fonts, so we keep the large common base and
    randomly drop only a handful — enough per-profile variation to individualise
    without producing an implausibly tiny (and thus suspicious) font list.
    """
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

    def camoufox_config(self) -> dict[str, Any]:
        """All native property overrides pinned via Camoufox's `config` argument.

        Screen/window dimensions and WebGL are handled separately (the `screen`
        constraint and `webgl_config` args respectively), so they are not here.
        Every key below is validated against Camoufox's property list.
        """
        return {
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
            "fonts": self.fonts,
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

    def webgl_config(self) -> tuple[str, str] | None:
        """The (vendor, renderer) pair for Camoufox's `webgl_config` argument."""
        if self.webgl_vendor and self.webgl_renderer:
            return (self.webgl_vendor, self.webgl_renderer)
        return None


def generate(os_name: str | None = None, seed: str | None = None) -> Fingerprint:
    """Generate a fresh, internally-coherent, deeply-randomized fingerprint."""
    rng = random.Random(seed) if seed else random
    os_name = os_name if os_name in _OS_SHORT else rng.choice(_OS_CHOICES)

    vendor, renderer = _sample_webgl(os_name, rng)
    w, h = rng.choice(_SCREENS)
    lang, region, tz = rng.choice(_LOCALES)

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
        hardware_concurrency=rng.choice(_CORES),
        device_memory=rng.choice([4, 8, 8, 16, 16, 32]),
        language=lang,
        region=region,
        timezone=tz,
        color_depth=rng.choice([24, 24, 24, 30]),
        device_pixel_ratio=rng.choice(_DEVICE_PIXEL_RATIOS),
        max_touch_points=0 if rng.random() < 0.85 else rng.choice([1, 5, 10]),
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
