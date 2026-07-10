"""Automation API example: drive a stored profile like normal Playwright.

Every profile keeps its own cookies/storage, fingerprint and proxy, so running
this repeatedly against the same profile behaves like the same returning user.

    python examples/scrape_example.py <profile_id>
"""
from __future__ import annotations

import sys

from antidetect.automate import launch


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python examples/scrape_example.py <profile_id>")
        return
    profile_id = sys.argv[1]

    with launch(profile_id, headless=True) as ctx:
        page = ctx.new_page()
        page.goto("https://example.com", wait_until="domcontentloaded")
        print("Title:", page.title())
        print("Heading:", page.locator("h1").first.inner_text())
        # Cookies set during the session persist to the profile's storage.
        print("Cookies now in context:", len(ctx.cookies()))


if __name__ == "__main__":
    main()
