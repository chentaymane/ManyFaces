"""FastAPI application: profile CRUD, launch control, proxy test, cookies,
fingerprint tools, and a local automation API.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Literal, Optional, Union

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, StrictBool

from . import __version__, config, cookies as cookie_store, db, fingerprint as fp_gen, proxy as proxy_mod
from . import android as android_engine
from . import browser as browser_mod
from .browser import manager, BrowserError, DisplayFitError
from .models import (
    FingerprintModel,
    Profile,
    ProfileCreate,
    ProfileUpdate,
    Proxy,
    ProxyType,
    Engine,
    OperatingSystem,
)


@asynccontextmanager
async def lifespan(app):
    config.ensure_dirs()
    db.init()
    try:
        yield
    finally:
        # Both browser engines own processes that need graceful cleanup.
        try:
            manager.stop_all()
        finally:
            android_engine.manager.stop_all()


app = FastAPI(title="ManyFaces", version=__version__, lifespan=lifespan)
# Refuse other Host headers: a page open in a managed browser could otherwise
# DNS-rebind a name onto 127.0.0.1 and read this API (e.g. the host's display
# geometry). Bound to every interface on purpose, the operator chose exposure.
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["*"] if config.HOST in ("0.0.0.0", "::", "")
    else sorted({"127.0.0.1", "localhost", config.HOST}),
)


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
    # A portable build ships the browser alongside the app and launches it via
    # executable_path, so it counts as installed without any download.
    from .browser import activate_bundled_engine

    if activate_bundled_engine():
        exe = config.bundled_browser_exe()
        try:
            from camoufox.utils import resolve_verstr

            _engine["version"] = resolve_verstr(exe)
        except Exception:  # noqa: BLE001 - camoufox 0.4.x: read the bundled version.json
            _engine["version"] = "bundled"
        _engine["installed"] = True
        _engine["detail"] = None
        return
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
        r.raise_for_status()
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
    re-downloading the whole ~1.3 GB file. Each segment also retries with backoff to
    ride out transient blips (the common failure on throttled/flaky GitHub links).
    """
    import concurrent.futures as cf
    import math
    import threading

    import requests

    if total <= 0 or segments <= 0 or retries <= 0:
        raise ValueError("Download size, segments and retries must be positive")
    segments = min(segments, total)
    seg_size = math.ceil(total / segments)

    # Reuse an existing partial file only if it's the right size; else start fresh.
    if os.path.exists(dest) and os.path.getsize(dest) == total:
        done = _load_meta(meta_path, segments)
    else:
        with open(dest, "wb") as fh:
            fh.truncate(total)
        done = [0] * segments
        _save_meta(meta_path, done)

    for idx, offset in enumerate(done):
        length = max(0, min(seg_size, total - idx * seg_size))
        if offset < 0 or offset > length:
            done[idx] = 0

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
                    expected_range = f"bytes {pos}-{end}/{total}"
                    if r.status_code != 206 or r.headers.get("content-range") != expected_range:
                        raise IOError("Server did not honor the requested download range")
                    with open(dest, "r+b") as fh:
                        fh.seek(pos)
                        for chunk in r.iter_content(262144):
                            if not chunk:
                                continue
                            if len(chunk) > end - (start + done[idx]) + 1:
                                raise IOError("Server sent data beyond the requested range")
                            fh.write(chunk)
                            with lock:
                                done[idx] += len(chunk)
                                progress()
                if start + done[idx] <= end:
                    raise IOError("Download segment ended early")
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


def _install_downloaded(fetcher, archive: str) -> None:
    """Install an already-downloaded Camoufox zip the way `camoufox fetch` would.

    Camoufox 0.5+ keeps versioned installs (browsers/<repo>/<version>/ plus an
    active-version pointer and a compatibility flag), so we hand the archive to
    its own installer — it verifies the release checksum, extracts, writes
    version.json and marks the build active — instead of unzipping by hand. Only
    the network step is ours (resumable, multi-connection, with progress).
    Camoufox 0.4.x used a flat INSTALL_DIR + `set_version()`, handled as before.
    """
    if hasattr(fetcher, "set_version"):  # camoufox 0.4.x
        import zipfile

        from camoufox.pkgman import INSTALL_DIR

        if INSTALL_DIR.exists():
            shutil.rmtree(INSTALL_DIR, ignore_errors=True)
        INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as zf:
            members = zf.infolist()
            count = len(members)
            _engine["total"] = count
            for i, member in enumerate(members, 1):
                zf.extract(member, str(INSTALL_DIR))
                if i % 40 == 0 or i == count:
                    _engine["downloaded"] = i
                    _engine["percent"] = int(i * 100 / count) if count else 100
        fetcher.set_version()
        return

    from camoufox.multiversion import install_versioned

    def _from_disk(buffer, url):  # noqa: ARG001 - replaces the network download
        with open(archive, "rb") as src:
            shutil.copyfileobj(src, buffer, 1 << 20)
        buffer.seek(0)
        return buffer

    fetcher.download_file = _from_disk  # instance attribute shadows the staticmethod
    install_versioned(fetcher, replace=True)
    try:
        from camoufox.pkgman import ensure_browser_profile_dir

        ensure_browser_profile_dir()
    except Exception:  # noqa: BLE001 - optional housekeeping in newer releases
        pass
    _engine["percent"] = 100


def _download_engine() -> None:
    """Download + install the Camoufox browser, updating _engine progress fields."""
    part = str(config.DATA_DIR / "camoufox-download.zip.part")
    meta = str(config.DATA_DIR / "camoufox-download.zip.meta")

    try:
        import requests
        from camoufox.pkgman import CamoufoxFetcher

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

        _engine.update(phase="extracting", percent=0, downloaded=0, total=0, speed=0)
        _install_downloaded(fetcher, part)

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

def _engine_manager(profile: Profile):
    """Pick the session manager that owns this profile's engine."""
    return android_engine.manager if profile.engine == "android" else manager


def _is_running(profile_id: str) -> bool:
    return any(m.status(profile_id) in ("running", "launching")
               for m in (manager, android_engine.manager))


def _require_stopped(profile_id: str) -> None:
    if _is_running(profile_id):
        raise HTTPException(409, "Stop the profile before changing its settings or fingerprint")


def _live_status(profile: Profile) -> tuple[str, Optional[str]]:
    """(status, error) from whichever manager owns this profile's engine.
    status ∈ launching | running | error | stopped."""
    mgr = _engine_manager(profile)
    return mgr.status(profile.id), mgr.error(profile.id)


def _with_status(profile: Profile) -> dict[str, Any]:
    d = profile.model_dump()
    st, err = _live_status(profile)
    d["status"] = st
    d["running"] = st in ("running", "launching")
    d["launch_error"] = err
    return d


@app.get("/api/profiles")
def list_profiles() -> list[dict[str, Any]]:
    return [_with_status(p) for p in db.list_all()]


def _fit_limit(fit_display: bool, engine: Engine) -> Optional[tuple[int, int]]:
    if not fit_display:
        return None
    if engine != "camoufox":
        raise HTTPException(422, "Fit this display is available for Camoufox profiles.")
    area = browser_mod.host_display_limit()
    if area is None:
        raise HTTPException(422, "Could not measure a Linux display. Turn off Fit this display to continue.")
    return area


def _build_profile(payload: ProfileCreate, area: Optional[tuple[int, int]]) -> Profile:
    try:
        profile = payload.build(max_physical_size=area)
        if area and payload.fingerprint:
            db.prepare_for_launch(profile, persist=False)
        fp = profile.fingerprint
        if area and (fp.is_mobile or not fp_gen.opens_in(
            fp.os, fp.screen_width, fp.screen_height, fp.device_pixel_ratio, area
        )):
            raise fp_gen.DisplayPresetError(area)
        return profile
    except fp_gen.DisplayPresetError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/display")
def display_info() -> dict[str, Any]:
    """What the dashboard needs to offer display-fit options for headful launches."""
    limit = browser_mod.host_display_limit()
    return {
        "fit_limit": list(limit) if limit else None,
        "virtual_available": browser_mod.virtual_display_available(),
    }


@app.post("/api/profiles", status_code=201)
def create_profile(payload: ProfileCreate) -> dict[str, Any]:
    profile = db.create(_build_profile(payload, _fit_limit(payload.fit_display, payload.engine)))
    return _with_status(profile)


@app.get("/api/profiles/{profile_id}")
def get_profile(profile_id: str) -> dict[str, Any]:
    profile = db.get(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    return _with_status(profile)


@app.patch("/api/profiles/{profile_id}")
def update_profile(profile_id: str, patch: ProfileUpdate) -> dict[str, Any]:
    _require_stopped(profile_id)
    profile = db.update(profile_id, patch)
    if not profile:
        raise HTTPException(404, "Profile not found")
    return _with_status(profile)


@app.delete("/api/profiles/{profile_id}", status_code=204)
def delete_profile(profile_id: str) -> None:
    if not db.get(profile_id):
        raise HTTPException(404, "Profile not found")
    manager.stop(profile_id)
    android_engine.manager.stop(profile_id)
    # Reclaim the ~3–4 GB AVD too, so deleting a profile doesn't orphan its device.
    android_engine.delete_avd(profile_id)
    if not db.delete(profile_id):
        raise HTTPException(404, "Profile not found")
    # And its browser data (cookies, storage, cache) — otherwise every deleted
    # profile leaves its whole Firefox/Chromium profile behind on disk.
    data_dir = config.PROFILES_DIR / profile_id
    if data_dir.is_dir() and data_dir.resolve().parent == config.PROFILES_DIR.resolve():
        shutil.rmtree(data_dir, ignore_errors=True)


@app.post("/api/profiles/{profile_id}/clone")
def clone_profile(profile_id: str) -> dict[str, Any]:
    src = db.get(profile_id)
    if not src:
        raise HTTPException(404, "Profile not found")
    data = src.model_dump()
    for key in ("id", "created_at", "updated_at"):
        data.pop(key, None)
    data["name"] = f"{src.name[:193]} (copy)"
    clone = db.create(Profile.model_validate(data))
    return _with_status(clone)


# ------------------------------------------------------------ launch control --

_launch_lock = threading.Lock()


class StartRequest(BaseModel):
    # False: a window on this display. "virtual": headed on a private Xvfb display
    # (Linux, Camoufox). True: no window.
    headless: Union[StrictBool, Literal["virtual"]] = False


@app.post("/api/profiles/{profile_id}/start")
def start_profile(profile_id: str, req: Optional[StartRequest] = None) -> dict[str, Any]:
    # Serialize the status check, proxy selection and session registration.
    with _launch_lock:
        return _start_profile(profile_id, (req or StartRequest()).headless)


def _start_profile(profile_id: str, headless: Union[bool, str] = False) -> dict[str, Any]:
    profile = db.get(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    st, _ = _live_status(profile)
    if st in ("launching", "running"):
        # Already up: don't advance the proxy rotation for a launch that won't happen.
        return {"status": st, "running": True, "proxy": None}
    if headless is not False:
        if profile.engine != "camoufox":
            raise HTTPException(400, "Headless and virtual-display launches are Camoufox-only")
        if headless == "virtual" and not browser_mod.virtual_display_available():
            raise HTTPException(400, "A virtual display needs Linux with Xvfb installed")
    original_fingerprint = profile.fingerprint
    if profile.engine != "android":
        db.prepare_for_launch(profile, persist=False)
    if profile.engine == "camoufox":
        # Refuse a headful window that can't fit this display now, with the numbers
        # the dashboard needs to offer alternatives, instead of failing in the
        # background launch.
        try:
            browser_mod.window_size(profile, headless)
        except DisplayFitError as exc:
            raise HTTPException(409, exc.to_dict())
    # Resolve the effective proxy for this launch (random draw / rotate advance).
    eff_proxy, next_index = profile.select_proxy()
    profile.proxy = eff_proxy  # in-memory: what this session launches with
    try:
        # Non-blocking: returns immediately; the browser/device comes up in the
        # background. Only fast, definite failures (e.g. Android not installed) raise.
        if profile.engine == "camoufox":
            manager.start(profile, headless=headless)
        else:
            _engine_manager(profile).start(profile)
    except (BrowserError, RuntimeError) as exc:
        raise HTTPException(502, str(exc))
    updates = {}
    if profile.fingerprint != original_fingerprint:
        updates["fingerprint"] = profile.fingerprint
    if next_index is not None:
        updates["rotation_index"] = next_index
    if updates:
        db.update(profile_id, ProfileUpdate(**updates))
    st, _ = _live_status(profile)
    return {
        "status": st,
        "running": st in ("running", "launching"),
        "proxy": f"{eff_proxy.type}://{eff_proxy.host}:{eff_proxy.port}" if eff_proxy.is_set else None,
    }


@app.get("/api/profiles/{profile_id}/status")
def profile_status(profile_id: str) -> dict[str, Any]:
    """Poll target for the UI: how a launch is progressing (launching→running/error)."""
    profile = db.get(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    st, err = _live_status(profile)
    return {"status": st, "running": st in ("running", "launching"), "error": err}


@app.post("/api/profiles/{profile_id}/stop")
def stop_profile(profile_id: str) -> dict[str, Any]:
    if not db.get(profile_id):
        raise HTTPException(404, "Profile not found")
    manager.stop(profile_id)
    android_engine.manager.stop(profile_id)
    return {"running": False, "status": "stopped"}


@app.get("/api/running")
def running() -> dict[str, list[str]]:
    return {"profiles": manager.running_ids() + android_engine.manager.running_ids()}


# ------------------------------------------------------------- android engine ----
@app.get("/api/android/status")
def android_status() -> dict[str, Any]:
    return android_engine.status()


@app.post("/api/android/install")
def android_install() -> dict[str, Any]:
    started = android_engine.start_install()
    return {"started": started, **android_engine.install_state.snapshot()}


@app.get("/api/android/install/status")
def android_install_status() -> dict[str, Any]:
    return android_engine.install_state.snapshot()


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
def generate_fp(os: OperatingSystem | None = None) -> dict[str, Any]:
    return fp_gen.generate(os_name=os).to_dict()


def _regenerate(profile: Profile, os_name: str | None) -> FingerprintModel:
    """A fresh fingerprint of the same kind of device unless an OS is requested.

    🎲 on a Windows profile used to be able to hand back an Android phone (the
    random pool mixes OSes), silently changing what the profile *is*.
    """
    old = profile.fingerprint
    if os_name is None:
        os_name = old.os
    # A profile whose panel fits this display keeps fitting after a re-roll, so a
    # 'Fit this display' profile doesn't silently stop opening headful.
    limit = browser_mod.host_display_limit() if profile.engine == "camoufox" else None
    if limit and (old.is_mobile or not fp_gen.opens_in(old.os, old.screen_width, old.screen_height,
                                                       old.device_pixel_ratio, limit)
                  or not fp_gen.displays(os_name, limit)):
        limit = None
    return FingerprintModel.from_fingerprint(fp_gen.generate(os_name=os_name, max_physical_size=limit))


@app.post("/api/profiles/{profile_id}/fingerprint/randomize")
def randomize_fp(profile_id: str, os: OperatingSystem | None = None) -> dict[str, Any]:
    _require_stopped(profile_id)
    profile = db.get(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    new_fp = _regenerate(profile, os)
    updated = db.update(profile_id, ProfileUpdate(fingerprint=new_fp))
    return _with_status(updated)  # type: ignore[arg-type]


class RandomizeAllRequest(BaseModel):
    os: OperatingSystem | None = None
    seed_cookies: int = Field(default=15, ge=0, le=1000)


@app.post("/api/profiles/{profile_id}/randomize-all")
def randomize_all(profile_id: str, req: RandomizeAllRequest) -> dict[str, Any]:
    """Regenerate the entire fingerprint AND reseed a fresh random cookie jar."""
    _require_stopped(profile_id)
    profile = db.get(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    new_fp = _regenerate(profile, req.os)
    updated = db.update(profile_id, ProfileUpdate(fingerprint=new_fp))
    cookie_store.clear(profile_id)
    if req.seed_cookies > 0:
        cookie_store.save(profile_id, cookie_store.generate_random(count=req.seed_cookies))
    return _with_status(updated)  # type: ignore[arg-type]


class BulkCreateRequest(BaseModel):
    count: int = Field(default=5, ge=1, le=200)
    name_prefix: str = Field(default="Profile", min_length=1, max_length=190, pattern=r"\S")
    os: OperatingSystem | None = None
    engine: Engine = "camoufox"
    seed_cookies: int = Field(default=15, ge=0, le=1000)
    fit_display: bool = False


@app.post("/api/profiles/bulk", status_code=201)
def bulk_create(req: BulkCreateRequest) -> dict[str, Any]:
    """Create N profiles, each with a fully-randomized fingerprint and cookie jar."""
    count = max(1, min(req.count, 200))
    created: list[dict[str, Any]] = []
    limit = _fit_limit(req.fit_display, req.engine)
    profiles = [
        _build_profile(ProfileCreate(name=f"{req.name_prefix} {i + 1}", os=req.os, engine=req.engine), limit)
        for i in range(count)
    ]
    for profile in profiles:
        db.create(profile)
        if req.seed_cookies > 0:
            cookie_store.save(profile.id, cookie_store.generate_random(count=req.seed_cookies))
        created.append(_with_status(profile))
    return {"created": len(created), "profiles": created}


# ------------------------------------------------------- profile import/export --
# A portable JSON bundle of profiles (settings, pinned fingerprint, proxies and
# staged cookies) for backups or moving profiles to another machine. Browser
# storage (history, logins, cache) stays on disk and is not included.

_EXPORT_VERSION = 1


@app.get("/api/profiles-export")
def export_profiles(ids: str = "") -> dict[str, Any]:
    wanted = [i for i in ids.split(",") if i] if ids else None
    out = []
    for profile in db.list_all():
        if wanted is not None and profile.id not in wanted:
            continue
        data = profile.model_dump()
        data["cookies"] = cookie_store.load(profile.id)
        out.append(data)
    return {"manyfaces_export": _EXPORT_VERSION, "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "profiles": out}


class ImportPayload(BaseModel):
    manyfaces_export: int = Field(default=1, ge=1, le=1)
    profiles: list[dict[str, Any]] = Field(max_length=200)


@app.post("/api/profiles-import", status_code=201)
def import_profiles(payload: ImportPayload) -> dict[str, Any]:
    """Create profiles from an export bundle. Each gets a NEW id (so importing twice,
    or into the machine it came from, never overwrites an existing profile)."""
    created, errors = [], []
    for i, raw in enumerate(payload.profiles):
        data = {k: v for k, v in raw.items() if k not in ("id", "created_at", "updated_at", "status", "running", "launch_error", "cookies")}
        try:
            profile = Profile.model_validate(data)
            staged = raw.get("cookies", [])
            if not isinstance(staged, list) or any(not isinstance(c, dict) for c in staged):
                raise ValueError("cookies must be a list of cookie objects")
            staged = cookie_store.normalise(staged)
            profile = db.create(profile)
            try:
                if staged:
                    cookie_store.save(profile.id, staged)
            except OSError:
                db.delete(profile.id)
                raise
        except Exception as exc:  # noqa: BLE001 - report bad entries, keep importing the rest
            errors.append(f"#{i + 1}: {type(exc).__name__}: {str(exc).splitlines()[0]}")
            continue
        created.append(_with_status(profile))
    return {"created": len(created), "errors": errors, "profiles": created}


# ------------------------------------------------------------------ cookies ---

class CookiePayload(BaseModel):
    cookies: list[dict[str, Any]]


class RandomCookieRequest(BaseModel):
    count: int = Field(default=10, ge=0, le=1000)
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
