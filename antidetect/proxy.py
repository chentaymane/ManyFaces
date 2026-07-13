"""Proxy connectivity testing and list parsing.

Tests a proxy by fetching an IP-echo service through it and reporting the exit IP,
country and latency. Used before assigning a proxy so you never launch a profile
on a dead or mislocated proxy. Also parses pasted proxy lists (several common
formats) into `Proxy` objects for the per-profile pool used by random/rotate modes.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .models import Proxy

# IP-echo endpoint that also returns geo info.
_ECHO_URL = "https://ipinfo.io/json"
_TIMEOUT = 15.0


def parse_line(line: str, default_type: str = "http") -> Proxy | None:
    """Parse one proxy line into a Proxy, or None if it isn't a proxy.

    Accepts the common formats (blank lines and `#` comments are ignored):
      host:port
      protocol://host:port
      host:port:user:pass
      protocol://user:pass@host:port
      protocol://host:port:user:pass
    Protocols map to what the launcher supports: http / https / socks5 (any
    `socks*` scheme becomes socks5).
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    ptype = default_type
    rest = line
    if "://" in line:
        scheme, rest = line.split("://", 1)
        scheme = scheme.lower()
        if scheme in ("http", "https"):
            ptype = scheme
        elif scheme.startswith("socks"):
            ptype = "socks5"
        else:
            ptype = "http"

    user = pw = ""
    if "@" in rest:  # credentials before the host
        cred, hostpart = rest.rsplit("@", 1)
        user, _, pw = cred.partition(":")
        host_port = hostpart.split(":")
    else:
        host_port = rest.split(":")
        if len(host_port) >= 4:  # host:port:user:pass
            user, pw = host_port[2], host_port[3]

    if len(host_port) < 2 or not host_port[0]:
        return None
    try:
        port = int(host_port[1])
    except ValueError:
        return None
    if not (0 < port < 65536):
        return None

    return Proxy(type=ptype, host=host_port[0], port=port, username=user, password=pw)


def parse_list(text: str, default_type: str = "http") -> list[Proxy]:
    """Parse a multi-line proxy list into Proxy objects, skipping bad lines."""
    out: list[Proxy] = []
    for raw in (text or "").splitlines():
        px = parse_line(raw, default_type)
        if px is not None:
            out.append(px)
    return out


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


async def test_many(proxies: list[Proxy], concurrency: int = 16) -> list[dict[str, Any]]:
    """Test a whole pool concurrently, preserving input order in the results.

    Each result carries the proxy's server URL and index so the UI can line the
    verdicts up against the pasted list (like chameleon's verification pass).
    """
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(idx: int, px: Proxy) -> dict[str, Any]:
        async with sem:
            res = await test(px)
        res["index"] = idx
        res["server"] = f"{px.type}://{px.host}:{px.port}"
        return res

    return await asyncio.gather(*(_one(i, p) for i, p in enumerate(proxies)))
