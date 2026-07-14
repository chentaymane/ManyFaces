"""FastAPI application: profile CRUD, launch control, proxy test, cookies,
fingerprint tools, and a local automation API.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, cookies as cookie_store, db, fingerprint as fp_gen, proxy as proxy_mod
from .browser import manager, BrowserError
from .models import (
    FingerprintModel,
    Profile,
    ProfileCreate,
    ProfileUpdate,
    Proxy,
    ProxyType,
)

app = FastAPI(title="ManyFaces", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    config.ensure_dirs()
    db.init()


@app.middleware("http")
async def _no_cache(request, call_next):
    """Stop the embedded WebView2/Edge browser from caching responses.

    Without this, the desktop shell caches GET responses (notably the engine status
    and the JS/HTML) and can keep serving a stale "not installed" from the very first
    run — leaving the setup screen stuck forever across restarts.
    """
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.exception_handler(Exception)
async def _unhandled(request, exc):  # noqa: ANN001
    """Return the error detail and append a full traceback to ~/.antidetect/error.log.

    In a windowed .exe there's no console, so persisting tracebacks to a file is the
    only way to diagnose a failure after the fact.
    """
    import traceback

    from fastapi.responses import JSONResponse

    try:
        config.ensure_dirs()
        with (config.DATA_DIR / "error.log").open("a", encoding="utf-8") as fh:
            fh.write(f"--- {request.method} {request.url.path} ---\n")
            fh.write(traceback.format_exc() + "\n")
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})


# ------------------------------------------------------------------ engine ----
# The Camoufox browser (~150 MB) is downloaded to the user cache on first run, so
# the app itself stays small. We drive the download ourselves (rather than calling
# Camoufox's built-in fetch) to report real progress to the UI and to enforce a
# timeout — Camoufox's downloader has none, so a stalled connection hangs forever.

_engine: dict[str, Any] = {
    "installed": None,   # True/False/None(unknown)
    "downloading": False,
    "version": None,
    "phase": "idle",     # idle | downloading | extracting | done | error
    "downloaded": 0,     # bytes fetched so far
    "total": 0,          # total bytes (0 if unknown)
    "percent": 0,        # 0-100
    "speed": 0,          # bytes/sec (recent)
    "error": None,       # user-facing error message
    "detail": None,      # diagnostic detail (why detection failed)
}
_engine_lock = threading.Lock()


def _detect_engine() -> None:
    # A portable build ships the browser alongside the app; activating it writes the
    # version shim so the check below passes without any download.
    from .browser import activate_bundled_engine

    activate_bundled_engine()
    try:
        from camoufox.pkgman import installed_verstr

        _engine["version"] = installed_verstr()
        _engine["installed"] = True
        _engine["detail"] = None
    except Exception as exc:  # noqa: BLE001 - any failure means "not installed yet"
        _engine["installed"] = False
        _engine["detail"] = f"{type(exc).__name__}: {exc}"  # why detection failed


def _probe_download(url: str):
    """Return (total_bytes, supports_range). A range probe is more reliable than HEAD."""
    import requests

    r = requests.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=(15, 60))
    try:
        if r.status_code == 206 and "content-range" in r.headers:
            total = int(r.headers["content-range"].split("/")[-1])
            return total, True
        total = int(r.headers.get("content-length", 0))
        return total, False
    finally:
        r.close()


def _load_meta(meta_path: str, segments: int) -> list[int]:
    import json

    try:
        with open(meta_path, encoding="utf-8") as fh:
            data = json.load(fh)
        done = data.get("done", [])
        if isinstance(done, list) and len(done) == segments:
            return [int(x) for x in done]
    except Exception:  # noqa: BLE001
        pass
    return [0] * segments


def _save_meta(meta_path: str, done: list[int]) -> None:
    import json

    try:
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump({"done": done}, fh)
    except OSError:
        pass


def _download_resumable(
    url: str, dest: str, meta_path: str, total: int, on_bytes, segments: int = 8, retries: int = 6
) -> None:
    """Download `url` to `dest` with `segments` parallel range requests, RESUMABLY.

    Per-segment byte offsets are persisted to `meta_path`, so a network drop or even
    closing and reopening the app resumes from where it left off instead of
    re-downloading the whole ~500 MB file. Each segment also retries with backoff to
    ride out transient blips (the common failure on throttled/flaky GitHub links).
    """
    import concurrent.futures as cf
    import math
    import threading

    import requests

    seg_size = math.ceil(total / segments)

    # Reuse an existing partial file only if it's the right size; else start fresh.
    if os.path.exists(dest) and os.path.getsize(dest) == total:
        done = _load_meta(meta_path, segments)
    else:
        with open(dest, "wb") as fh:
            fh.truncate(total)
        done = [0] * segments
        _save_meta(meta_path, done)

    lock = threading.Lock()
    save_state = {"t": time.time()}

    def progress() -> None:
        got = sum(done)
        on_bytes(got)
        now = time.time()
        if now - save_state["t"] >= 1.0:  # persist offsets at most once a second
            _save_meta(meta_path, done)
            save_state["t"] = now

    def worker(idx: int) -> None:
        start = idx * seg_size
        end = min(start + seg_size, total) - 1
        if start > end:
            return
        last_exc = None
        for attempt in range(retries):
            pos = start + done[idx]
            if pos > end:
                return
            try:
                headers = {"Range": f"bytes={pos}-{end}"}
                with requests.get(url, headers=headers, stream=True, timeout=(15, 60)) as r:
                    r.raise_for_status()
                    with open(dest, "r+b") as fh:
                        fh.seek(pos)
                        for chunk in r.iter_content(262144):
                            if not chunk:
                                continue
                            fh.write(chunk)
                            with lock:
                                done[idx] += len(chunk)
                                progress()
                return
            except Exception as exc:  # noqa: BLE001 - retry this segment from where it stalled
                last_exc = exc
                with lock:
                    _save_meta(meta_path, done)
                time.sleep(min(2 * (attempt + 1), 12))
        raise last_exc  # type: ignore[misc]

    try:
        with cf.ThreadPoolExecutor(max_workers=segments) as ex:
            for fut in cf.as_completed([ex.submit(worker, i) for i in range(segments)]):
                fut.result()  # propagate the first segment that exhausted its retries
    finally:
        _save_meta(meta_path, done)


def _friendly_error(exc: Exception) -> str:
    """Turn low-level network errors into an actionable message for the setup screen."""
    import requests

    name = type(exc).__name__
    if isinstance(exc, requests.exceptions.ConnectionError) or "NameResolution" in name or "getaddrinfo" in str(exc):
        return "Network connection to GitHub was lost. Click Retry — the download resumes where it stopped."
    if isinstance(exc, requests.exceptions.Timeout) or "Timeout" in name:
        return "The connection stalled. Click Retry — the download resumes where it stopped."
    return f"{name}: {exc}"


def _download_engine() -> None:
    """Download + install the Camoufox browser, updating _engine progress fields."""
    import shutil
    import zipfile

    part = str(config.DATA_DIR / "camoufox-download.zip.part")
    meta = str(config.DATA_DIR / "camoufox-download.zip.meta")

    try:
        import requests
        from camoufox.pkgman import CamoufoxFetcher, INSTALL_DIR

        config.ensure_dirs()
        _engine.update(phase="downloading", error=None, downloaded=0, total=0, percent=0, speed=0)

        # Resolve the latest supported release (hits GitHub API).
        fetcher = CamoufoxFetcher()
        url = fetcher.url
        total, supports_range = _probe_download(url)
        _engine["total"] = total

        # Progress callback shared by both download strategies (tracks recent speed).
        speed_state = {"t": time.time(), "b": 0}

        def on_bytes(got: int) -> None:
            _engine["downloaded"] = got
            if total:
                _engine["percent"] = int(got * 100 / total)
            now = time.time()
            if now - speed_state["t"] >= 0.5:
                _engine["speed"] = int((got - speed_state["b"]) / (now - speed_state["t"]))
                speed_state["t"] = now
                speed_state["b"] = got

        if total and supports_range:
            # Resumable, multi-connection download (survives drops and app restarts).
            _download_resumable(url, part, meta, total, on_bytes)
        else:
            # Fallback: single stream with a timeout so a stall errors out.
            with requests.get(url, stream=True, timeout=(15, 60)) as resp:
                resp.raise_for_status()
                got = 0
                with open(part, "wb") as fh:
                    for chunk in resp.iter_content(262144):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        got += len(chunk)
                        on_bytes(got)

        # Guard against a truncated download before we try to unzip it.
        actual = os.path.getsize(part)
        if total and actual < total:
            raise IOError(
                f"Download incomplete ({actual // 1048576} of {total // 1048576} MB). Please retry."
            )

        # Extract with real per-file progress. Start from a clean install dir so a
        # previous partial attempt can't leave stale/half files behind.
        _engine.update(phase="extracting", percent=0, downloaded=0, total=0, speed=0)
        if INSTALL_DIR.exists():
            shutil.rmtree(INSTALL_DIR, ignore_errors=True)
        INSTALL_DIR.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(part) as zf:
            members = zf.infolist()
            count = len(members)
            _engine["total"] = count
            for i, member in enumerate(members, 1):
                zf.extract(member, str(INSTALL_DIR))
                if i % 40 == 0 or i == count:
                    _engine["downloaded"] = i
                    _engine["percent"] = int(i * 100 / count) if count else 100
        fetcher.set_version()

        # Success — remove the now-consumed download artifacts.
        for f in (part, meta):
            try:
                os.unlink(f)
            except OSError:
                pass

        _detect_engine()
        _engine["phase"] = "done" if _engine["installed"] else "error"
        if not _engine["installed"]:
            _engine["error"] = "Install finished but the engine was not detected."
    except Exception as exc:  # noqa: BLE001 - keep the .part file so Retry can resume
        _engine.update(phase="error", error=_friendly_error(exc))
    finally:
        _engine["downloading"] = False


@app.get("/api/engine/status")
def engine_status() -> dict[str, Any]:
    # Re-detect whenever we don't yet believe the engine is installed (and aren't
    # mid-download). This self-heals a stale "not installed" from a startup race or
    # an engine that got installed out-of-band (e.g. `camoufox fetch`), so the UI
    # never stays stuck on the setup screen once the browser is actually present.
    if not _engine["installed"] and not _engine["downloading"]:
        _detect_engine()
    return _engine


@app.post("/api/engine/ensure")
def engine_ensure() -> dict[str, Any]:
    """Kick off the one-time browser download in the background (idempotent)."""
    with _engine_lock:
        # Always re-check first so a startup-race click never re-downloads a browser
        # that is actually already installed.
        _detect_engine()
        if _engine["installed"] or _engine["downloading"]:
            return _engine
        _engine["downloading"] = True
        _engine["error"] = None
        threading.Thread(target=_download_engine, daemon=True).start()
    return _engine


# ---------------------------------------------------------------- profiles ----

def _with_status(profile: Profile) -> dict[str, Any]:
    d = profile.model_dump()
    d["running"] = manager.is_running(profile.id)
    return d


@app.get("/api/profiles")
def list_profiles() -> list[dict[str, Any]]:
    return [_with_status(p) for p in db.list_all()]


@app.post("/api/profiles", status_code=201)
def create_profile(payload: ProfileCreate) -> dict[str, Any]:
    profile = db.create(payload.build())
    return _with_status(profile)


@app.get("/api/profiles/{profile_id}")
def get_profile(profile_id: str) -> dict[str, Any]:
    profile = db.get(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    return _with_status(profile)


@app.patch("/api/profiles/{profile_id}")
def update_profile(profile_id: str, patch: ProfileUpdate) -> dict[str, Any]:
    profile = db.update(profile_id, patch)
    if not profile:
        raise HTTPException(404, "Profile not found")
    return _with_status(profile)


@app.delete("/api/profiles/{profile_id}", status_code=204)
def delete_profile(profile_id: str) -> None:
    if manager.is_running(profile_id):
        manager.stop(profile_id)
    if not db.delete(profile_id):
        raise HTTPException(404, "Profile not found")


@app.post("/api/profiles/{profile_id}/clone")
def clone_profile(profile_id: str) -> dict[str, Any]:
    src = db.get(profile_id)
    if not src:
        raise HTTPException(404, "Profile not found")
    data = src.model_dump()
    for key in ("id", "created_at", "updated_at"):
        data.pop(key, None)
    data["name"] = f"{src.name} (copy)"
    clone = db.create(Profile.model_validate(data))
    return _with_status(clone)


# ------------------------------------------------------------ launch control --

@app.post("/api/profiles/{profile_id}/start")
def start_profile(profile_id: str) -> dict[str, Any]:
    profile = db.get(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    # Resolve the effective proxy for this launch (random draw / rotate advance).
    eff_proxy, next_index = profile.select_proxy()
    profile.proxy = eff_proxy  # in-memory: what this session launches with
    if next_index is not None:  # persist rotation so the next launch advances
        db.update(profile_id, ProfileUpdate(rotation_index=next_index))
    try:
        manager.start(profile)
    except BrowserError as exc:
        raise HTTPException(502, str(exc))
    return {
        "running": manager.is_running(profile_id),
        "proxy": f"{eff_proxy.type}://{eff_proxy.host}:{eff_proxy.port}" if eff_proxy.is_set else None,
    }


@app.post("/api/profiles/{profile_id}/stop")
def stop_profile(profile_id: str) -> dict[str, Any]:
    manager.stop(profile_id)
    return {"running": False}


@app.get("/api/running")
def running() -> dict[str, list[str]]:
    return {"profiles": manager.running_ids()}


# -------------------------------------------------------------------- proxy ---

@app.post("/api/proxy/test")
async def test_proxy(proxy: Proxy) -> dict[str, Any]:
    return await proxy_mod.test(proxy)


class ProxyPoolPayload(BaseModel):
    text: str = ""                 # pasted list, one proxy per line
    default_type: ProxyType = "http"


@app.post("/api/proxy/parse")
def parse_proxy_pool(payload: ProxyPoolPayload) -> dict[str, Any]:
    """Parse pasted proxy text into structured entries (no network calls)."""
    proxies = proxy_mod.parse_list(payload.text, payload.default_type)
    return {"count": len(proxies), "proxies": [p.model_dump() for p in proxies]}


@app.post("/api/proxy/test-pool")
async def test_proxy_pool(payload: ProxyPoolPayload) -> dict[str, Any]:
    """Parse a pasted list and test every proxy concurrently (like chameleon)."""
    proxies = proxy_mod.parse_list(payload.text, payload.default_type)
    if not proxies:
        return {"count": 0, "alive": 0, "results": []}
    results = await proxy_mod.test_many(proxies)
    alive = sum(1 for r in results if r.get("ok"))
    return {"count": len(proxies), "alive": alive, "results": results}


class FreeProxyRequest(BaseModel):
    protocol: str = "http"   # "http" (also https) or "socks5"
    limit: int = 100
    verify: bool = False     # test each and return only the working ones


@app.post("/api/proxy/fetch-free")
async def fetch_free_proxies(req: FreeProxyRequest) -> dict[str, Any]:
    """Pull a pool of free proxies from public sources, optionally keeping only live ones.

    Free proxies are unreliable by nature — enable `verify` (the UI's default) to
    return only the ones that currently pass a live exit-IP check.
    """
    ptype = "socks5" if req.protocol.startswith("socks") else "http"
    # When verifying, cast a wider net (most free proxies are dead) but test them
    # fast — short per-proxy timeout, high concurrency — so the request returns in
    # ~10-15s instead of minutes, which is what was showing up as "Failed to fetch".
    want = max(1, min(req.limit, 500))
    fetch_n = min(120, want * 3) if req.verify else want
    hostports = await proxy_mod.fetch_free(ptype, fetch_n)
    lines = [f"{ptype}://{hp}" for hp in hostports]

    if not req.verify or not lines:
        return {"count": len(lines), "alive": None, "proxies": lines[:want], "protocol": ptype}

    proxies = proxy_mod.parse_list("\n".join(lines), ptype)
    results = await proxy_mod.test_many(proxies, concurrency=120, timeout=4.0)
    live = [f"{ptype}://{proxies[r['index']].host}:{proxies[r['index']].port}"
            for r in results if r.get("ok")][:want]
    return {"count": len(proxies), "alive": len(live), "proxies": live, "protocol": ptype}


# -------------------------------------------------------------- fingerprint ---

@app.get("/api/devices")
def list_devices() -> dict[str, Any]:
    """Selectable phone presets for the 'New Phone' picker (Android first, then iPhone)."""
    return {"devices": fp_gen.list_mobile_devices()}


@app.get("/api/fingerprint/generate")
def generate_fp(os: str | None = None) -> dict[str, Any]:
    return fp_gen.generate(os_name=os).to_dict()


@app.post("/api/profiles/{profile_id}/fingerprint/randomize")
def randomize_fp(profile_id: str, os: str | None = None) -> dict[str, Any]:
    profile = db.get(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    new_fp = FingerprintModel.from_fingerprint(fp_gen.generate(os_name=os))
    updated = db.update(profile_id, ProfileUpdate(fingerprint=new_fp))
    return _with_status(updated)  # type: ignore[arg-type]


class RandomizeAllRequest(BaseModel):
    os: str | None = None
    seed_cookies: int = 15  # random cookies to warm up the jar (0 = none)


@app.post("/api/profiles/{profile_id}/randomize-all")
def randomize_all(profile_id: str, req: RandomizeAllRequest) -> dict[str, Any]:
    """Regenerate the entire fingerprint AND reseed a fresh random cookie jar."""
    profile = db.get(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    new_fp = FingerprintModel.from_fingerprint(fp_gen.generate(os_name=req.os))
    updated = db.update(profile_id, ProfileUpdate(fingerprint=new_fp))
    cookie_store.clear(profile_id)
    if req.seed_cookies > 0:
        cookie_store.save(profile_id, cookie_store.generate_random(count=req.seed_cookies))
    return _with_status(updated)  # type: ignore[arg-type]


class BulkCreateRequest(BaseModel):
    count: int = 5
    name_prefix: str = "Profile"
    os: str | None = None          # None => random OS per profile
    engine: str = "camoufox"       # browser engine for every created profile
    seed_cookies: int = 15         # random cookies per profile (0 = none)


@app.post("/api/profiles/bulk", status_code=201)
def bulk_create(req: BulkCreateRequest) -> dict[str, Any]:
    """Create N profiles, each with a fully-randomized fingerprint and cookie jar."""
    count = max(1, min(req.count, 200))
    created: list[dict[str, Any]] = []
    for i in range(count):
        payload = ProfileCreate(name=f"{req.name_prefix} {i + 1}", os=req.os, engine=req.engine)  # type: ignore[arg-type]
        profile = db.create(payload.build())
        if req.seed_cookies > 0:
            cookie_store.save(profile.id, cookie_store.generate_random(count=req.seed_cookies))
        created.append(_with_status(profile))
    return {"created": len(created), "profiles": created}


# ------------------------------------------------------------------ cookies ---

class CookiePayload(BaseModel):
    cookies: list[dict[str, Any]]


class RandomCookieRequest(BaseModel):
    count: int = 10
    domain: str = "example.com"


@app.get("/api/profiles/{profile_id}/cookies")
def get_cookies(profile_id: str) -> dict[str, Any]:
    if not db.get(profile_id):
        raise HTTPException(404, "Profile not found")
    return {"cookies": cookie_store.load(profile_id)}


@app.put("/api/profiles/{profile_id}/cookies")
def set_cookies(profile_id: str, payload: CookiePayload) -> dict[str, Any]:
    if not db.get(profile_id):
        raise HTTPException(404, "Profile not found")
    saved = cookie_store.save(profile_id, payload.cookies)
    return {"cookies": saved, "count": len(saved)}


@app.post("/api/profiles/{profile_id}/cookies/random")
def add_random_cookies(profile_id: str, req: RandomCookieRequest) -> dict[str, Any]:
    if not db.get(profile_id):
        raise HTTPException(404, "Profile not found")
    existing = cookie_store.load(profile_id)
    new = cookie_store.generate_random(count=req.count, domain=req.domain)
    saved = cookie_store.save(profile_id, existing + new)
    return {"cookies": saved, "count": len(saved), "added": len(new)}


@app.delete("/api/profiles/{profile_id}/cookies", status_code=204)
def clear_cookies(profile_id: str) -> None:
    if not db.get(profile_id):
        raise HTTPException(404, "Profile not found")
    cookie_store.clear(profile_id)


# --------------------------------------------------------------- static UI ----

# Unique per server start: appended to app.js/style.css URLs so the browser (esp.
# the Edge/WebView2 engine, which caches aggressively) can NEVER reuse a stale copy
# of the frontend across app restarts. This was the root of the "stuck on setup
# screen" bug — an old cached app.js kept running after the code was fixed.
import time as _time_mod

_ASSET_VERSION = str(int(_time_mod.time()))

if config.WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index():
        html = (config.WEB_DIR / "index.html").read_text(encoding="utf-8")
        # Version the asset URLs so each server start busts any cached JS/CSS.
        html = html.replace("/static/app.js", f"/static/app.js?v={_ASSET_VERSION}")
        html = html.replace("/static/style.css", f"/static/style.css?v={_ASSET_VERSION}")
        return HTMLResponse(html)
