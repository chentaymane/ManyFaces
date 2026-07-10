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
from .browser import build_launch_options
from .models import Profile


def _resolve(profile_or_id) -> Profile:
    if isinstance(profile_or_id, Profile):
        return profile_or_id
    profile = db.get(profile_or_id)
    if not profile:
        raise ValueError(f"No profile with id {profile_or_id!r}")
    return profile


@contextmanager
def launch(profile_or_id, headless: bool | None = None) -> Iterator[object]:
    """Yield a synchronous Camoufox BrowserContext for the given profile."""
    from camoufox.sync_api import Camoufox

    profile = _resolve(profile_or_id)
    opts = build_launch_options(profile, headless=headless)
    with Camoufox(**opts) as ctx:
        staged = cookie_store.load(profile.id)
        if staged:
            try:
                ctx.add_cookies(staged)
            except Exception:  # noqa: BLE001
                pass
        yield ctx


@asynccontextmanager
async def launch_async(profile_or_id, headless: bool | None = None):
    """Yield an async Camoufox BrowserContext for the given profile."""
    from camoufox.async_api import AsyncCamoufox

    profile = _resolve(profile_or_id)
    opts = build_launch_options(profile, headless=headless)
    async with AsyncCamoufox(**opts) as ctx:
        staged = cookie_store.load(profile.id)
        if staged:
            try:
                await ctx.add_cookies(staged)
            except Exception:  # noqa: BLE001
                pass
        yield ctx
