"""FastAPI application: profile CRUD, launch control, proxy test, cookies,
fingerprint tools, and a local automation API.
"""
from __future__ import annotations

import threading
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
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
)

app = FastAPI(title="Anti-Detect Browser Manager", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    config.ensure_dirs()
    db.init()


# ------------------------------------------------------------------ engine ----
# The Camoufox browser (~150 MB) is downloaded to the user cache on first run, so
# the app itself stays small. These endpoints let the UI show setup progress.

_engine: dict[str, Any] = {"installed": None, "downloading": False, "version": None, "error": None}


def _detect_engine() -> None:
    try:
        from camoufox.pkgman import installed_verstr

        _engine["version"] = installed_verstr()
        _engine["installed"] = True
    except Exception:  # noqa: BLE001 - any failure means "not installed yet"
        _engine["installed"] = False


@app.get("/api/engine/status")
def engine_status() -> dict[str, Any]:
    if _engine["installed"] is None:
        _detect_engine()
    return _engine


@app.post("/api/engine/ensure")
def engine_ensure() -> dict[str, Any]:
    """Kick off the one-time browser download in the background (idempotent)."""
    if _engine["installed"] is None:
        _detect_engine()
    if _engine["installed"] or _engine["downloading"]:
        return _engine

    _engine["downloading"] = True
    _engine["error"] = None

    def _run() -> None:
        try:
            from camoufox.pkgman import camoufox_path

            camoufox_path(download_if_missing=True)
            _detect_engine()
        except Exception as exc:  # noqa: BLE001
            _engine["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            _engine["downloading"] = False

    threading.Thread(target=_run, daemon=True).start()
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
    try:
        manager.start(profile)
    except BrowserError as exc:
        raise HTTPException(502, str(exc))
    return {"running": manager.is_running(profile_id)}


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


# -------------------------------------------------------------- fingerprint ---

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
    seed_cookies: int = 15         # random cookies per profile (0 = none)


@app.post("/api/profiles/bulk", status_code=201)
def bulk_create(req: BulkCreateRequest) -> dict[str, Any]:
    """Create N profiles, each with a fully-randomized fingerprint and cookie jar."""
    count = max(1, min(req.count, 200))
    created: list[dict[str, Any]] = []
    for i in range(count):
        payload = ProfileCreate(name=f"{req.name_prefix} {i + 1}", os=req.os)
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

if config.WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(config.WEB_DIR / "index.html"))
