"""Issue #2: headful launches on HiDPI profiles, display fit, and (screen, DPR) realism."""
import dataclasses
import random
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from fastapi.testclient import TestClient

from antidetect import api, browser, db, fingerprint as F
from antidetect.models import FingerprintModel, Profile

from test_regressions import IsolatedData

# Real panels (physical px) the desktop catalogue may claim to be.
REAL_PANELS = {
    (1920, 1080), (1366, 768), (1600, 900), (1440, 900), (1680, 1050), (1920, 1200),
    (2560, 1440), (3840, 2160), (2880, 1800), (3024, 1964), (2940, 1912), (3456, 2234),
    (2560, 1664), (5120, 2880),
}
WORKAREA = (1864, 1040)  # 1920x1080 monitor, 56 px side dock + 40 px top bar


def profile(os_name, pid="fit000000001", **fields):
    fp = F.generate(os_name, seed=pid)
    if fields:
        fp = dataclasses.replace(fp, **fields)
    return Profile(id=pid, name="p", fingerprint=FingerprintModel.from_fingerprint(fp))


def linux_host(workarea=WORKAREA):
    """Pretend to be a Linux desktop with the given workarea (on any CI OS)."""
    stack = ExitStack()
    stack.enter_context(patch.object(browser, "_HOST_LINUX", True))
    stack.enter_context(patch.object(browser, "_host_workarea", return_value=workarea))
    return stack


class FingerprintDisplayTests(unittest.TestCase):
    def test_every_generated_desktop_is_a_real_panel(self):
        for os_name in ("windows", "macos", "linux"):
            for i in range(300):
                fp = F.generate(os_name, seed=f"{os_name}{i}")
                row = (fp.screen_width, fp.screen_height, fp.device_pixel_ratio)
                self.assertIn(row, F._DISPLAYS[os_name])
                self.assertIn(F.physical_size(*row), REAL_PANELS, row)

    def test_catalogue_is_all_real_panels(self):
        for os_name, rows in F._DISPLAYS.items():
            for row in rows:
                self.assertIn(F.physical_size(*row), REAL_PANELS, (os_name, row))

    def test_max_physical_size_keeps_hidpi_variety_within_the_panel(self):
        seen = set()
        for os_name in ("windows", "linux"):
            for i in range(400):
                fp = F.generate(os_name, seed=f"m{i}", max_physical_size=(1920, 1080))
                pw, ph = F.physical_size(fp.screen_width, fp.screen_height, fp.device_pixel_ratio)
                self.assertLessEqual((pw, ph)[0], 1920)
                self.assertLessEqual(ph, 1080)
                self.assertFalse(fp.screen_width >= 1920 and fp.device_pixel_ratio > 1)
                seen.add((fp.screen_width, fp.screen_height, fp.device_pixel_ratio))
        self.assertIn((1536, 864, 1.25), seen)
        self.assertIn((1280, 720, 1.5), seen)

    def test_unsatisfiable_limit_falls_back_to_real_displays(self):
        fp = F.generate("macos", seed="x", max_physical_size=(800, 600))
        self.assertIn((fp.screen_width, fp.screen_height, fp.device_pixel_ratio), F._DISPLAYS["macos"])

    def test_phones_ignore_the_limit(self):
        fp = F.generate("android", seed="p", max_physical_size=(800, 600))
        self.assertTrue(fp.is_mobile)

    def test_normalize_repairs_impossible_screen_dpr_pairs(self):
        fp = dataclasses.replace(F.generate("windows", seed="n"), screen_width=1920,
                                 screen_height=1080, device_pixel_ratio=1.25)
        self.assertEqual(F.normalize(fp).device_pixel_ratio, 1.0)
        ok = dataclasses.replace(fp, screen_width=1536, screen_height=864, device_pixel_ratio=1.25)
        self.assertEqual(F.normalize(ok).device_pixel_ratio, 1.25)


class WindowFitTests(unittest.TestCase):
    def test_headless_and_virtual_ignore_the_host(self):
        p = profile("windows", screen_width=2560, screen_height=1440, device_pixel_ratio=1.0)
        with linux_host((800, 600)) as _, patch.object(browser, "_host_workarea", side_effect=AssertionError("host probed")):
            self.assertEqual(browser.window_size(p, True), (2560, 1392))
            self.assertEqual(browser.window_size(p, "virtual"), (2560, 1392))

    def test_compatible_hidpi_desktops_fit_without_watermarking_the_host(self):
        for i, (w, h, dpr) in enumerate([(1536, 864, 1.25), (1280, 720, 1.5), (1920, 1080, 1.0), (2560, 1440, 1.0)]):
            for os_name in ("windows", "linux"):
                p = profile(os_name, pid=f"fit{os_name[:3]}{i:05d}", screen_width=w, screen_height=h, device_pixel_ratio=dpr)
                with linux_host():
                    fw, fh = browser.window_size(p, False)
                self.assertLessEqual(fw * dpr, WORKAREA[0])
                self.assertLessEqual(fh * dpr, WORKAREA[1])
                for host in (1920, WORKAREA[0]):
                    self.assertNotEqual(round(fw * dpr), host)
                self.assertGreaterEqual(fw, max(1024, 0.6 * w))
                self.assertGreaterEqual(fh, max(600, 0.6 * h))

    def test_window_that_fits_is_left_exactly_as_headless(self):
        p = profile("windows", screen_width=1536, screen_height=864, device_pixel_ratio=1.25)
        with linux_host((1920, 1040)):
            self.assertEqual(browser.window_size(p, False), browser.window_size(p, True))

    def test_fit_inset_is_stable_per_profile_and_varies_between_profiles(self):
        sizes = set()
        for i in range(12):
            p = profile("windows", pid=f"inset{i:07d}", screen_width=1920, screen_height=1080, device_pixel_ratio=1.0)
            with linux_host():
                first = browser.window_size(p, False)
                self.assertEqual(first, browser.window_size(p, False))
            sizes.add(first)
        self.assertGreater(len(sizes), 6)

    def test_oversized_retina_and_phones_raise_instead_of_shrinking(self):
        cases = [
            profile("macos", screen_width=1440, screen_height=900, device_pixel_ratio=2.0),
            profile("macos", screen_width=2560, screen_height=1440, device_pixel_ratio=2.0),
            profile("android", screen_width=360, screen_height=780, device_pixel_ratio=3.0),
        ]
        for p in cases:
            with linux_host(), self.assertRaises(browser.DisplayFitError) as ctx:
                browser.window_size(p, False)
            err = ctx.exception
            self.assertIsInstance(err, ValueError)
            self.assertEqual(err.available_px, WORKAREA)
            self.assertEqual(err.profile_id, p.id)
            self.assertGreater(max(err.required_px[0] - WORKAREA[0], err.required_px[1] - WORKAREA[1]), 0)
            with linux_host():
                browser.window_size(p, True)
                browser.window_size(p, "virtual")

    def test_phone_window_is_always_its_screen(self):
        p = profile("android", screen_width=412, screen_height=915, device_pixel_ratio=2.625)
        for workarea in ((2160, 3800), None):
            with linux_host(workarea):
                for mode in (False, True, "virtual"):
                    self.assertEqual(browser.window_size(p, mode), (412, 915))

    def test_scale_is_one_off_linux(self):
        p = profile("macos", screen_width=1440, screen_height=900, device_pixel_ratio=2.0)
        with patch.object(browser, "_HOST_LINUX", False), patch.object(browser, "_host_workarea", return_value=(1920, 1040)):
            self.assertEqual(browser.window_size(p, False), (1440, 875))

    def test_no_display_means_no_constraint(self):
        p = profile("macos", screen_width=2560, screen_height=1440, device_pixel_ratio=2.0)
        with linux_host(None):
            self.assertEqual(browser.window_size(p, False), (2560, 1415))


def _camoufox_ready():
    try:
        import camoufox.utils  # noqa: F401
    except ImportError:
        return False
    return browser.engine_installed()


@unittest.skipUnless(_camoufox_ready(), "Camoufox engine not installed")
class CamoufoxLaunchOptionsTests(IsolatedData):
    """Runs the real camoufox.utils.launch_options against a simulated 1920x1080 Linux host."""

    def setUp(self):
        super().setUp()
        import camoufox.utils as cu
        from camoufox.display import DisplaySize

        stack = linux_host()
        for target, value in (("OS_NAME", "lin"), ("largest_display", lambda: DisplaySize(1920, 1080)),
                              ("has_display", lambda env: True)):
            if hasattr(cu, target):
                stack.enter_context(patch.object(cu, target, value))
        stack.__enter__()
        self.addCleanup(stack.close)

    def final_config(self, p, headless):
        opts = browser.camoufox_launch_options(p, headless=headless,
                                               virtual_display=":99" if headless == "virtual" else None)
        return opts, browser._read_camou_config(opts["env"])[0]

    def test_compatible_hidpi_profiles_launch_headful(self):
        for w, h, dpr in [(1536, 864, 1.25), (1280, 720, 1.5)]:
            p = profile("windows", screen_width=w, screen_height=h, device_pixel_ratio=dpr)
            opts, cfg = self.final_config(p, False)
            self.assertLessEqual(cfg["window.outerWidth"] * dpr, WORKAREA[0])
            self.assertLessEqual(cfg["window.outerHeight"] * dpr, WORKAREA[1])
            self.assertEqual(opts["firefox_user_prefs"]["layout.css.devPixelsPerPx"], f"{dpr:g}")
            self.assertEqual((cfg["screen.width"], cfg["screen.height"]), (w, h))

    def test_desktop_inner_size_is_left_to_the_browser(self):
        p = profile("windows", screen_width=1920, screen_height=1080, device_pixel_ratio=1.0)
        self.assertFalse({"window.innerWidth", "window.innerHeight", "window.outerWidth", "window.outerHeight"}
                         & set(browser.build_launch_options(p, False)["config"]))
        _, cfg = self.final_config(p, False)
        self.assertNotIn("window.innerWidth", cfg)
        self.assertNotIn("window.innerHeight", cfg)
        self.assertEqual((cfg["window.outerWidth"], cfg["window.outerHeight"]), browser.window_size(p, False))

    def test_oversized_profiles_refuse_headful_and_run_virtual_or_headless(self):
        for p in (profile("macos", screen_width=1440, screen_height=900, device_pixel_ratio=2.0),
                  profile("android", screen_width=360, screen_height=780, device_pixel_ratio=3.0)):
            with self.assertRaises(browser.DisplayFitError):
                self.final_config(p, False)
            for mode in (True, "virtual"):
                _, cfg = self.final_config(p, mode)
                self.assertEqual(cfg["screen.width"], p.fingerprint.screen_width)

    def test_virtual_launch_targets_the_virtual_display(self):
        p = profile("windows", screen_width=2560, screen_height=1440, device_pixel_ratio=1.0)
        opts, cfg = self.final_config(p, "virtual")
        self.assertEqual(opts["env"]["DISPLAY"], ":99")
        self.assertFalse(opts["headless"])
        self.assertEqual((cfg["window.outerWidth"], cfg["window.outerHeight"]), (2560, 1392))

    def test_phone_renders_at_its_own_dpr(self):
        p = profile("android", screen_width=412, screen_height=915, device_pixel_ratio=2.625)
        opts, cfg = self.final_config(p, True)
        self.assertEqual(opts["firefox_user_prefs"]["layout.css.devPixelsPerPx"], "2.625")
        self.assertEqual(cfg["window.devicePixelRatio"], 2.625)


class DisplayFitApiTests(IsolatedData):
    def setUp(self):
        super().setUp()
        self.client = TestClient(api.app)

    def test_unfit_headful_launch_is_409_with_numbers_and_no_side_effects(self):
        p = db.create(profile("macos", screen_width=1440, screen_height=900, device_pixel_ratio=2.0))
        with linux_host(), patch.object(api.manager, "start") as start, \
                patch.object(browser, "virtual_display_available", return_value=True):
            r = self.client.post(f"/api/profiles/{p.id}/start")
        self.assertEqual(r.status_code, 409)
        d = r.json()["detail"]
        self.assertEqual(d["code"], "display_fit")
        self.assertEqual(d["required_px"], [2880, 1750])
        self.assertEqual(d["available_px"], list(WORKAREA))
        self.assertTrue(d["virtual_available"])
        start.assert_not_called()

    def test_virtual_launch_skips_the_host_and_reaches_the_manager(self):
        p = db.create(profile("macos", screen_width=1440, screen_height=900, device_pixel_ratio=2.0))
        with linux_host(), patch.object(api.manager, "start") as start, \
                patch.object(browser, "virtual_display_available", return_value=True):
            r = self.client.post(f"/api/profiles/{p.id}/start", json={"headless": "virtual"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(start.call_args.kwargs["headless"], "virtual")

    def test_virtual_needs_xvfb_and_camoufox(self):
        p = db.create(profile("windows"))
        with patch.object(browser, "virtual_display_available", return_value=False):
            self.assertEqual(self.client.post(f"/api/profiles/{p.id}/start", json={"headless": "virtual"}).status_code, 400)
        c = db.create(Profile(name="c", engine="chromium"))
        with patch.object(browser, "virtual_display_available", return_value=True):
            self.assertEqual(self.client.post(f"/api/profiles/{c.id}/start", json={"headless": "virtual"}).status_code, 400)

    def test_fit_display_creation_uses_the_host_limit(self):
        with patch.object(browser, "host_display_limit", return_value=(1920, 1080)):
            for _ in range(15):
                r = self.client.post("/api/profiles", json={"name": "f", "os": "macos", "fit_display": True})
                fp = r.json()["fingerprint"]
                pw, ph = F.physical_size(fp["screen_width"], fp["screen_height"], fp["device_pixel_ratio"])
                self.assertLessEqual(pw, 1920)
                self.assertLessEqual(ph, 1080)
            r = self.client.post("/api/profiles/bulk", json={"count": 5, "os": "linux", "fit_display": True, "seed_cookies": 0})
            for prof in r.json()["profiles"]:
                fp = prof["fingerprint"]
                self.assertLessEqual(F.physical_size(fp["screen_width"], fp["screen_height"], fp["device_pixel_ratio"])[0], 1920)

    def test_display_info(self):
        with patch.object(browser, "host_display_limit", return_value=(1920, 1080)), \
                patch.object(browser, "virtual_display_available", return_value=False):
            self.assertEqual(self.client.get("/api/display").json(), {"fit_limit": [1920, 1080], "virtual_available": False})


class X11WorkareaTests(unittest.TestCase):
    def test_no_display_is_none(self):
        with patch.dict("os.environ", {"DISPLAY": ""}):
            self.assertIsNone(browser._x11_workarea())

    def test_workarea_narrows_monitor(self):
        import types

        fake = types.SimpleNamespace(has_display=lambda env: True,
                                     largest_display=lambda: types.SimpleNamespace(width=3840, height=1080))
        with patch.dict("sys.modules", {"camoufox.display": fake}), patch.object(browser, "_HOST_LINUX", True), \
                patch.object(browser, "_x11_workarea", return_value=(3840 + 1920, 1024)):
            self.assertEqual(browser._host_workarea(), (3840, 1024))


if __name__ == "__main__":
    unittest.main()
