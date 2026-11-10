"""Optional real-browser UI test: MANYFACES_BROWSER_TESTS=1 python -m unittest discover -s tests -v."""
import os
import socket
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_regressions import IsolatedData


@unittest.skipUnless(os.environ.get("MANYFACES_BROWSER_TESTS") == "1", "Set MANYFACES_BROWSER_TESTS=1 to run Playwright")
class DashboardTests(IsolatedData):
    def test_sort_search_filter_persistence_selection_and_refresh(self):
        import uvicorn
        from playwright.sync_api import sync_playwright
        from antidetect import api, db
        from antidetect.models import Profile

        db.create(Profile(name="Profile 10", notes="Client A", tags=["QA"]))
        db.create(Profile(name="Profile 2", engine="chromium"))
        db.create(Profile(name="Alpha", engine="android"))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(server.started)
            with patch.object(api.manager, "status", return_value="stopped"), sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.route("**/api/engine/status", lambda route: route.fulfill(json={"installed": True}))
                page.goto(f"http://127.0.0.1:{port}/")
                page.wait_for_function("document.querySelectorAll('.row-check').length === 3")
                page.select_option("#sort-order", "name")
                self.assertEqual(page.locator("#profiles-body .name").all_text_contents(), ["Alpha", "Profile 2", "Profile 10"])
                page.locator(".row-check").first.check()
                self.assertTrue(page.locator("#head-check").evaluate("e => e.indeterminate"))
                # Equal polling responses leave existing rows intact.
                page.locator("#profiles-body tr").first.evaluate("e => e.dataset.marker = 'kept'")
                page.evaluate("loadProfiles()")
                self.assertEqual(page.locator("#profiles-body tr").first.get_attribute("data-marker"), "kept")
                page.fill("#search", "client a")
                self.assertEqual(page.locator("#profiles-body .name").all_text_contents(), ["Profile 10"])
                page.fill("#search", "")
                page.click('[data-filter="stopped"]')
                page.reload()
                page.wait_for_function("document.querySelectorAll('.row-check').length === 3")
                self.assertEqual(page.locator("#sort-order").input_value(), "name")
                self.assertIn("active", page.locator('[data-filter="stopped"]').get_attribute("class"))
                page.click('[data-filter="error"]')
                self.assertIn("No profiles match", page.locator("#profiles-body").inner_text())
                page.click("#clear-filter")
                self.assertEqual(page.locator(".row-check").count(), 3)
                # A rejected launch must leave its button usable for retry even
                # when the following profile poll returns identical data.
                page.route("**/api/profiles/*/start", lambda route: route.fulfill(status=502, json={"detail": "Test launch failed"}))
                launch = page.locator('button[data-act="start"]').first
                launch.click()
                page.wait_for_function("document.querySelector('#toast').textContent === 'Test launch failed'")
                self.assertTrue(launch.is_enabled())
                self.assertEqual(launch.inner_text(), "Launch")
                encoded = page.evaluate("proxyToLine({type:'https', username:'u@x', password:'p:a#ss', host:'::1', port:443})")
                self.assertEqual(encoded, "https://u%40x:p%3Aa%23ss@[::1]:443")
                output = Path(".testdata")
                output.mkdir(exist_ok=True)
                page.screenshot(path=str(output / "dashboard-desktop.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                new_button = page.locator("#new-btn").bounding_box()
                self.assertGreaterEqual(new_button["x"], 0)
                self.assertLessEqual(new_button["x"] + new_button["width"], 390)
                page.screenshot(path=str(output / "dashboard-mobile.png"), full_page=True)
                self.assertEqual(errors, [])
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
