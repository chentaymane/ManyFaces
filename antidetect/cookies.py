"""Cookie management for profiles.

Supports importing/exporting cookies (JSON in Playwright's cookie format) and
generating random benign cookies for testing a profile's isolation. Cookies are
injected into the profile's persistent Firefox storage on next launch via the
browser layer; here we only validate/normalise and persist a staging file.
"""
from __future__ import annotations

import json
import random
import string
import time
from pathlib import Path
from typing import Any

from . import config

_COOKIE_STAGING = "cookies.json"


def _staging_path(profile_id: str) -> Path:
    return config.profile_data_dir(profile_id) / _COOKIE_STAGING


def _rand(n: int) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=n))


def normalise(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Coerce a list of cookie dicts into Playwright's expected shape."""
    out: list[dict[str, Any]] = []
    for c in cookies:
        if "name" not in c or "value" not in c:
            continue
        cookie: dict[str, Any] = {
            "name": str(c["name"]),
            "value": str(c["value"]),
            "domain": c.get("domain", ""),
            "path": c.get("path", "/"),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", True)),
            "sameSite": c.get("sameSite", "Lax"),
        }
        if "expires" in c and c["expires"] not in (None, "", -1):
            try:
                cookie["expires"] = float(c["expires"])
            except (TypeError, ValueError):
                pass
        out.append(cookie)
    return out


def load(profile_id: str) -> list[dict[str, Any]]:
    path = _staging_path(profile_id)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save(profile_id: str, cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cookies = normalise(cookies)
    _staging_path(profile_id).write_text(
        json.dumps(cookies, indent=2), encoding="utf-8"
    )
    return cookies


def clear(profile_id: str) -> None:
    path = _staging_path(profile_id)
    if path.exists():
        path.unlink()


# Realistic analytics/marketing cookie templates. Each returns a plausibly-formatted
# value so the cookie jar looks like a genuinely-used browser, not obvious filler.
_TRACKER_DOMAINS = [
    ".google.com", ".doubleclick.net", ".facebook.com", ".youtube.com",
    ".bing.com", ".linkedin.com", ".twitter.com", ".tiktok.com",
    ".amazon.com", ".cloudflare.com",
]


def _ga_value() -> str:
    now = int(time.time())
    return f"GA1.2.{random.randint(10**8, 10**9)}.{now - random.randint(0, 5_000_000)}"


def _fbp_value() -> str:
    return f"fb.1.{int(time.time()*1000)}.{random.randint(10**9, 10**10)}"


_COOKIE_TEMPLATES = [
    ("_ga", _ga_value, ".google.com"),
    ("_gid", _ga_value, ".google.com"),
    ("_gat", lambda: "1", ".google.com"),
    ("_fbp", _fbp_value, ".facebook.com"),
    ("_fbc", _fbp_value, ".facebook.com"),
    ("IDE", lambda: _rand(44), ".doubleclick.net"),
    ("VISITOR_INFO1_LIVE", lambda: _rand(22), ".youtube.com"),
    ("MUID", lambda: _rand(32).upper(), ".bing.com"),
    ("bcookie", lambda: f'"v=2&{_rand(36)}"', ".linkedin.com"),
    ("guest_id", lambda: f"v1%3A{random.randint(10**17, 10**18)}", ".twitter.com"),
    ("tt_webid", lambda: str(random.randint(10**18, 10**19)), ".tiktok.com"),
    ("session-id", lambda: f"{random.randint(100,999)}-{_rand(7)}-{_rand(7)}", ".amazon.com"),
    ("__cf_bm", lambda: _rand(43), ".cloudflare.com"),
]


def generate_random(count: int = 10, domain: str = "") -> list[dict[str, Any]]:
    """Generate `count` realistic, varied cookies to warm up a profile's jar.

    Values follow real analytics/marketing cookie formats and are spread across
    common third-party domains, so a fresh profile presents a lived-in cookie store
    instead of an obviously-synthetic one. If `domain` is given, first-party cookies
    for that site are mixed in too. These are randomised test values, not stolen
    sessions — they exist to make new profiles look established and to verify jar
    isolation between profiles.
    """
    now = time.time()
    cookies: list[dict[str, Any]] = []
    templates = list(_COOKIE_TEMPLATES)
    random.shuffle(templates)

    for i in range(count):
        if domain and random.random() < 0.35:
            d = domain if domain.startswith(".") else f".{domain}"
            name = f"{random.choice(['sid', 'sessionid', 'csrftoken', 'uid', 'pref', 'cart'])}"
            value = _rand(random.randint(16, 40))
        else:
            name, maker, d = templates[i % len(templates)]
            value = maker()
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": d,
                "path": "/",
                "httpOnly": random.random() < 0.3,
                "secure": True,
                "sameSite": random.choice(["Lax", "Lax", "None", "Strict"]),
                "expires": now + random.randint(3600, 60 * 60 * 24 * 400),
            }
        )
    return normalise(cookies)
