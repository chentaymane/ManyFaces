"""Automation API.

Drive any stored profile from your own Python scripts using Playwright's API,
with the profile's full fingerprint, proxy, cookies and persistent storage applied.

Example (synchronous):

    from antidetect.automate import launch

    with launch("ab12cd34ef56", headless=True) as ctx:
        page = ctx.new_page()
        page.goto("https://browserleaks.com/canvas")
        print(page.title())

Example (async):

    from antidetect.automate import launch_async

    async with launch_async("ab12cd34ef56") as ctx:
        page = await ctx.new_page()
        await page.goto("https://abrahamjuliot.github.io/creepjs/")
"""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import Iterator

from . import cookies as cookie_store, db
from .browser import camoufox_launch_options, virtual_display, window_size
from .models import Profile, ProfileUpdate


def _resolve(profile_or_id, headless: bool | str | None = None) -> Profile:
    """Load the profile and resolve its proxy for this launch, like the dashboard does:
    the stored fingerprint is migrated to the installed engine, and random/rotate
    proxy modes pick from the pool (rotation advances and is persisted)."""
    if isinstance(profile_or_id, Profile):
        profile = profile_or_id.model_copy(deep=True)
    else:
        profile = db.get(profile_or_id)
        if not profile:
            raise ValueError(f"No profile with id {profile_or_id!r}")
    original_fingerprint = profile.fingerprint
    db.prepare_for_launch(profile, persist=False)
    window_size(profile, headless)
    proxy, next_index = profile.select_proxy()
    profile.proxy = proxy
    updates = {}
    if profile.fingerprint != original_fingerprint:
        updates["fingerprint"] = profile.fingerprint
    if next_index is not None:
        updates["rotation_index"] = next_index
    if updates:
        db.update(profile.id, ProfileUpdate(**updates))
    return profile


@contextmanager
def launch(profile_or_id, headless: bool | str | None = None) -> Iterator[object]:
    """Yield a synchronous Camoufox BrowserContext for the given profile.

    headless: False/None opens a window on this display (DisplayFitError if the
    profile's window can't fit it), True runs headless, "virtual" runs headed on
    a private Xvfb display (Linux).
    """
    from camoufox.sync_api import Camoufox

    profile = _resolve(profile_or_id, headless)
    with virtual_display(headless) as display, Camoufox(
        from_options=camoufox_launch_options(profile, headless=headless, virtual_display=display),
        persistent_context=True,
    ) as ctx:
        staged = cookie_store.load(profile.id)
        if staged:
            try:
                ctx.add_cookies(staged)
            except Exception:  # noqa: BLE001
                pass
        yield ctx


@asynccontextmanager
async def launch_async(profile_or_id, headless: bool | str | None = None):
    """Yield an async Camoufox BrowserContext for the given profile (see `launch`)."""
    from camoufox.async_api import AsyncCamoufox

    import asyncio

    profile = _resolve(profile_or_id, headless)
    # Starting/killing Xvfb blocks (up to ~30 s); keep it off the event loop.
    cm = virtual_display(headless)
    display = await asyncio.to_thread(cm.__enter__)
    try:
        opts = camoufox_launch_options(profile, headless=headless, virtual_display=display)
        async with AsyncCamoufox(from_options=opts, persistent_context=True) as ctx:
            staged = cookie_store.load(profile.id)
            if staged:
                try:
                    await ctx.add_cookies(staged)
                except Exception:  # noqa: BLE001
                    pass
            yield ctx
    finally:
        await asyncio.to_thread(cm.__exit__, None, None, None)
