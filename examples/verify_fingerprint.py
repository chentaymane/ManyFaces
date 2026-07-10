"""Launch a profile headless and dump what the page actually sees.

Proves the fingerprint is applied: reads navigator/screen/WebGL from inside the
spoofed browser and prints them. Run after creating at least one profile.

    python examples/verify_fingerprint.py [profile_id]

With no id, the most recently created profile is used.
"""
from __future__ import annotations

import sys

from antidetect import db
from antidetect.automate import launch


def main() -> None:
    profiles = db.list_all()
    if not profiles:
        print("No profiles yet. Create one in the dashboard first.")
        return

    if len(sys.argv) > 1:
        target = db.get(sys.argv[1])
        if not target:
            print(f"No profile with id {sys.argv[1]!r}")
            return
    else:
        target = profiles[0]

    print(f"Launching profile {target.name!r} ({target.id}) headless…")
    with launch(target.id, headless=True) as ctx:
        page = ctx.new_page()
        page.goto("about:blank")
        seen = page.evaluate(
            """() => {
                const gl = document.createElement('canvas').getContext('webgl');
                const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
                return {
                    userAgent: navigator.userAgent,
                    platform: navigator.platform,
                    languages: navigator.languages,
                    hardwareConcurrency: navigator.hardwareConcurrency,
                    deviceMemory: navigator.deviceMemory,
                    screen: [screen.width, screen.height],
                    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                    webdriver: navigator.webdriver,
                    webglVendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
                    webglRenderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
                };
            }"""
        )

    print("\nWhat the page sees (should match the profile's fingerprint):")
    for key, value in seen.items():
        print(f"  {key:20s} {value}")

    expected = target.fingerprint
    print("\nProfile expects:")
    print(f"  os                   {expected.os}")
    print(f"  screen               [{expected.screen_width}, {expected.screen_height}]")
    print(f"  hardwareConcurrency  {expected.hardware_concurrency}")
    print(f"  timezone             {expected.timezone}")
    print(f"  webglRenderer        {expected.webgl_renderer}")
    print("\nnavigator.webdriver should be False/undefined (not True).")


if __name__ == "__main__":
    main()
