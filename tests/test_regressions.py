"""Offline regression tests. Run with python -m unittest discover -s tests -v."""
import json
import sqlite3
import tempfile
import sys
import time
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from antidetect import api, browser, config, cookies, db, proxy
from antidetect.models import Profile, ProfileUpdate, Proxy


class IsolatedData(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, value in {"DATA_DIR": root, "PROFILES_DIR": root / "profiles", "DB_PATH": root / "data.db"}.items():
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)


class DatabaseTests(IsolatedData):
    def test_connections_closed_after_success_and_failure(self):
        with db._connect() as connection:
            connection.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with self.assertRaises(RuntimeError):
            with db._connect() as failed:
                raise RuntimeError("abort")
        with self.assertRaises(sqlite3.ProgrammingError):
            failed.execute("SELECT 1")

    def test_concurrent_patches_preserve_both_fields(self):
        profile = db.create(Profile(name="Original"))
        barrier = threading.Barrier(2)

        def update(patch_data):
            barrier.wait()
            db.update(profile.id, ProfileUpdate(**patch_data))

        with ThreadPoolExecutor(2) as executor:
            list(executor.map(update, [{"name": "Renamed"}, {"notes": "Saved notes"}]))
        result = db.get(profile.id)
        self.assertEqual((result.name, result.notes), ("Renamed", "Saved notes"))

    def test_missing_update_does_not_create_profile(self):
        self.assertIsNone(db.update("missing", ProfileUpdate(notes="x")))
        self.assertEqual(db.list_all(), [])


class ApiTests(IsolatedData):
    def setUp(self):
        super().setUp()
        self.client = TestClient(api.app, base_url="http://127.0.0.1")
        self.addCleanup(self.client.close)

    def test_invalid_input_is_422_without_partial_profiles(self):
        for payload in [{"name": "  "}, {"name": "Test", "os": "invalid"}, {"name": "Test", "proxy": {"port": 65536}}]:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/api/profiles", json=payload).status_code, 422)
        for payload in [{"engine": "invalid"}, {"count": 0}, {"seed_cookies": 1001}, {"name_prefix": "  "}]:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/api/profiles/bulk", json=payload).status_code, 422)
        self.assertEqual(db.list_all(), [])

    def test_null_patch_is_rejected_and_omitted_fields_survive(self):
        profile = db.create(Profile(name="Original", notes="Keep"))
        url = f"/api/profiles/{profile.id}"
        self.assertEqual(self.client.patch(url, json={"name": None}).status_code, 422)
        result = self.client.patch(url, json={"name": "  New  "})
        self.assertEqual(result.status_code, 200)
        self.assertEqual((result.json()["name"], result.json()["notes"]), ("New", "Keep"))

    def test_clone_accepts_maximum_length_name(self):
        profile = db.create(Profile(name="x" * 200))
        result = self.client.post(f"/api/profiles/{profile.id}/clone")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json()["name"]), 200)
        self.assertTrue(result.json()["name"].endswith(" (copy)"))

    def test_cannot_change_engine_or_identity_while_launching(self):
        profile = db.create(Profile(name="Test"))
        with patch.object(api.manager, "status", return_value="launching"):
            self.assertEqual(self.client.patch(f"/api/profiles/{profile.id}", json={"engine": "android"}).status_code, 409)
            self.assertEqual(self.client.post(f"/api/profiles/{profile.id}/randomize-all", json={}).status_code, 409)

    def test_stop_and_delete_missing_profile_have_no_side_effects(self):
        with patch.object(api.manager, "stop") as stop, patch.object(api.android_engine, "delete_avd") as delete:
            self.assertEqual(self.client.post("/api/profiles/missing/stop").status_code, 404)
            self.assertEqual(self.client.delete("/api/profiles/missing").status_code, 404)
            stop.assert_not_called()
            delete.assert_not_called()

    def test_import_reports_bad_cookies_and_keeps_valid_entries(self):
        response = self.client.post("/api/profiles-import", json={"profiles": [
            {"name": "Bad", "cookies": "wrong"},
            {"name": "Good", "id": "../../escape", "cookies": [{"name": "a", "value": "b", "domain": "example.com"}]},
        ]})
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["created"], 1)
        self.assertEqual(len(data["errors"]), 1)
        profile = data["profiles"][0]
        self.assertNotEqual(profile["id"], "../../escape")
        self.assertEqual(cookies.load(profile["id"])[0]["value"], "b")
        self.assertEqual(len(db.list_all()), 1)

    def test_import_cookie_write_failure_rolls_back_profile(self):
        with patch.object(cookies, "save", side_effect=OSError("disk full")):
            result = self.client.post("/api/profiles-import", json={"profiles": [{"name": "Test", "cookies": [{"name": "a", "value": "b", "domain": "example.com"}]}]}).json()
        self.assertEqual(result["created"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(db.list_all(), [])

    def test_import_version_is_checked(self):
        self.assertEqual(self.client.post("/api/profiles-import", json={"manyfaces_export": 2, "profiles": []}).status_code, 422)

    def test_export_import_roundtrip_preserves_settings(self):
        profile = db.create(Profile(name="Test", tags=["QA"], engine="chromium", proxy=Proxy(host="localhost", port=8080)))
        cookies.save(profile.id, [{"name": "sid", "value": "test", "domain": "example.com"}])
        bundle = self.client.get("/api/profiles-export").json()
        result = self.client.post("/api/profiles-import", json=bundle).json()
        clone = result["profiles"][0]
        self.assertNotEqual(clone["id"], profile.id)
        self.assertEqual(clone["proxy"], profile.proxy.model_dump())
        self.assertEqual(clone["tags"], ["QA"])
        self.assertEqual(cookies.load(clone["id"]), cookies.load(profile.id))

    def test_failed_start_does_not_advance_proxy_rotation(self):
        profile = db.create(Profile(name="Phone", engine="android", proxy_mode="rotate", proxy_pool=[Proxy(host="a", port=80), Proxy(host="b", port=80)]))
        with patch.object(api.android_engine.manager, "start", side_effect=RuntimeError("not installed")):
            response = self.client.post(f"/api/profiles/{profile.id}/start")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(db.get(profile.id).rotation_index, 0)

    def test_lifespan_stops_both_managers(self):
        with patch.object(api.manager, "stop_all") as desktop, patch.object(api.android_engine.manager, "stop_all") as android:
            with TestClient(api.app, base_url="http://127.0.0.1"):
                pass
        desktop.assert_called_once()
        android.assert_called_once()


class CookieTests(IsolatedData):
    def test_invalid_shape_expiry_and_same_site_are_normalized(self):
        result = cookies.normalise([None, {}, {"name": "a", "value": "b"},
            {"name": "ok", "value": "yes", "domain": "example.com", "expires": "NaN", "sameSite": "invalid"}])
        self.assertEqual(len(result), 1)
        self.assertNotIn("expires", result[0])
        self.assertEqual(result[0]["sameSite"], "Lax")

    def test_failed_atomic_write_preserves_previous_jar(self):
        original = cookies.save("test", [{"name": "a", "value": "b", "domain": "example.com"}])
        with patch.object(cookies.os, "replace", side_effect=OSError("busy")):
            with self.assertRaises(OSError):
                cookies.save("test", [])
        self.assertEqual(cookies.load("test"), original)
        self.assertEqual([p.name for p in config.profile_data_dir("test").iterdir()], ["cookies.json"])

    def test_wrong_json_shape_loads_empty(self):
        (config.profile_data_dir("test") / "cookies.json").write_text('{"not": "cookies"}', encoding="utf-8")
        self.assertEqual(cookies.load("test"), [])

    def test_profile_path_traversal_rejected(self):
        for value in ["../escape", "..", "C:\\escape", "", "a/b"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                config.profile_data_dir(value)


class ProxyTests(unittest.TestCase):
    def test_formats_and_ipv6(self):
        for line, expected in [
            ("host:8080:user:pass:word", ("http", "host", 8080, "user", "pass:word")),
            ("https://u%40x:p%3Aa%23ss@host:443", ("https", "host", 443, "u@x", "p:a#ss")),
            ("socks5://[::1]:1080", ("socks5", "::1", 1080, "", "")),
        ]:
            with self.subTest(line=line):
                p = proxy.parse_line(line)
                self.assertEqual((p.type, p.host, p.port, p.username, p.password), expected)
        self.assertEqual(Proxy(type="https", host="::1", port=443).server_url(), "https://[::1]:443")

    def test_invalid_proxy_lines_are_skipped(self):
        for line in ["ftp://host:80", "host:0", "host:65536", "host:no", "host:80/path", "# comment", "http://bad host:80"]:
            with self.subTest(line=line):
                self.assertIsNone(proxy.parse_line(line))


class SessionTests(unittest.TestCase):
    def test_concurrent_start_registers_one_live_session(self):
        manager = browser.SessionManager()
        profile = Profile(name="Test")
        entered = threading.Event()
        release = threading.Event()
        instances = []

        class FakeSession:
            status = "stopped"

            def __init__(self, profile, headless=False):
                instances.append(self)

            def start(self):
                entered.set()
                release.wait(2)
                self.status = "launching"

        with patch.object(browser, "BrowserSession", FakeSession), ThreadPoolExecutor(2) as executor:
            first = executor.submit(manager.start, profile)
            self.assertTrue(entered.wait(2))
            second = executor.submit(manager.start, profile)
            release.set()
            first.result()
            second.result()
        self.assertEqual(manager.running_ids(), [profile.id])
        self.assertEqual(len(instances), 1)


class DesktopTests(IsolatedData):
    def test_window_exit_and_creation_failure_stop_server(self):
        import desktop

        for fail in (False, True):
            with self.subTest(fail=fail):
                finished = threading.Event()

                class FakeServer:
                    should_exit = False

                    def run(self):
                        while not self.should_exit:
                            time.sleep(0.001)
                        finished.set()

                server = FakeServer()
                window = Mock()
                if fail:
                    window.create_window.side_effect = RuntimeError("window failed")
                with patch.object(desktop, "_make_server", return_value=server), patch.object(desktop, "_choose_port", return_value=8000), patch.object(desktop, "_wait_for_port", return_value=True), patch.dict(sys.modules, {"webview": window}):
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, "window failed"):
                            desktop.main()
                    else:
                        desktop.main()
                self.assertTrue(server.should_exit)
                self.assertTrue(finished.is_set())


class DownloadTests(IsolatedData):
    def download(self, body, status=206, header="bytes 0-3/4", meta=None):
        dest = config.DATA_DIR / "engine.part"
        metadata = config.DATA_DIR / "meta.json"
        if meta is not None:
            dest.write_bytes(b"abcd")
            metadata.write_text(json.dumps({"done": meta}), encoding="utf-8")
        response = Mock(status_code=status, headers={"content-range": header})
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = body
        with patch("requests.get", return_value=response), patch.object(api.time, "sleep"):
            api._download_resumable("https://example.com", str(dest), str(metadata), 4, lambda n: None, segments=1, retries=1)
        return dest.read_bytes()

    def test_correct_range_succeeds(self):
        self.assertEqual(self.download([b"abcd"]), b"abcd")

    def test_ignored_range_fails(self):
        with self.assertRaisesRegex(OSError, "honor"):
            self.download([b"abcd"], status=200)

    def test_truncated_range_fails(self):
        with self.assertRaisesRegex(OSError, "early"):
            self.download([b"ab"])

    def test_oversized_range_fails(self):
        with self.assertRaisesRegex(OSError, "beyond"):
            self.download([b"abcde"])

    def test_invalid_saved_offset_restarts_segment(self):
        self.assertEqual(self.download([b"abcd"], meta=[100]), b"abcd")


if __name__ == "__main__":
    unittest.main()
