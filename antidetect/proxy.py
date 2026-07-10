"""Proxy connectivity testing.

Tests a proxy by fetching an IP-echo service through it and reporting the exit IP,
country and latency. Used before assigning a proxy so you never launch a profile
on a dead or mislocated proxy.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from .models import Proxy

# IP-echo endpoint that also returns geo info.
_ECHO_URL = "https://ipinfo.io/json"
_TIMEOUT = 15.0


async def test(proxy: Proxy) -> dict[str, Any]:
    if not proxy.is_set:
        return {"ok": False, "error": "Proxy host/port not set"}

    # Build an authenticated proxy URL for httpx.
    scheme = "socks5" if proxy.type == "socks5" else "http"
    auth = f"{proxy.username}:{proxy.password}@" if proxy.username else ""
    proxy_url = f"{scheme}://{auth}{proxy.host}:{proxy.port}"

    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=_TIMEOUT) as client:
            resp = await client.get(_ECHO_URL)
            resp.raise_for_status()
            info = resp.json()
        latency_ms = round((time.perf_counter() - start) * 1000)
        return {
            "ok": True,
            "ip": info.get("ip"),
            "country": info.get("country"),
            "region": info.get("region"),
            "city": info.get("city"),
            "org": info.get("org"),
            "latency_ms": latency_ms,
        }
    except Exception as exc:  # noqa: BLE001 - report any failure to the UI
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
