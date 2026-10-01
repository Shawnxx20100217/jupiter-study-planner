"""Dedicated login/session coordination tests; no browsers or network."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import jupiter_browser_session as session
import jupiter_collector as collector
import jupiter_login as login


class VisibleLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jupiter-login-test-")
        self.root = Path(self.temp.name).resolve()
        self.instance = self.root / "instance.json"
        self.state = self.root / "state"
        self.state.mkdir()
        self.saved = self.state / "collector-session.json"
        self.saved.write_text('{"cookies": []}')
        self.instance.write_text(json.dumps({
            "state_dir": "state", "profile_dir": "state/dedicated profile",
            "expected_student": "Fictional Student", "expected_school_year": "School 2026-27",
            "expected_courses": ["Course A"]}))
        self.patches = [patch.object(login.platform, "system", return_value="Darwin"),
                        patch.object(login.shutil, "which", return_value="/usr/bin/open"),
                        patch.object(login, "ensure_profile_idle")]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_successful_visible_login_retires_old_snapshot(self):
        runner = Mock()
        result = login.launch_visible_login(self.instance, runner=runner)
        self.assertTrue(result["ok"])
        self.assertFalse(self.saved.exists())
        command = runner.call_args.args[0]
        self.assertIn("--user-data-dir=" + str(self.state / "dedicated profile"), command)
        self.assertNotIn("--use-mock-keychain", command)
        self.assertFalse(result["cookies_imported"])

    def test_failed_launch_keeps_previous_snapshot(self):
        runner = Mock(side_effect=subprocess.CalledProcessError(1, ["open"]))
        with self.assertRaises(collector.CollectorError) as raised:
            login.launch_visible_login(self.instance, runner=runner)
        self.assertEqual(raised.exception.code, "chrome_launch_failed")
        self.assertTrue(self.saved.exists())

    def test_collector_lock_blocks_launch_and_preserves_snapshot(self):
        runner = Mock()
        with collector.browser_lock(collector.config_at(self.instance)):
            with self.assertRaises(collector.CollectorError) as raised:
                login.launch_visible_login(self.instance, runner=runner)
        self.assertEqual(raised.exception.code, "browser_busy")
        runner.assert_not_called()
        self.assertTrue(self.saved.exists())

    def test_existing_visible_browser_blocks_second_launch(self):
        login.ensure_profile_idle.side_effect = session.BrowserSessionError("browser_busy", "Already open")
        runner = Mock()
        with self.assertRaises(session.BrowserSessionError):
            login.launch_visible_login(self.instance, runner=runner)
        runner.assert_not_called()
        self.assertTrue(self.saved.exists())

    def test_launch_holds_collection_lock_until_snapshot_is_retired(self):
        def runner(*args, **kwargs):
            with self.assertRaises(collector.CollectorError):
                with collector.browser_lock(collector.config_at(self.instance)):
                    self.fail("Collector entered during login launch")
            self.assertTrue(self.saved.exists())
        login.launch_visible_login(self.instance, runner=runner)
        with collector.browser_lock(collector.config_at(self.instance)):
            self.assertFalse(self.saved.exists())


class ProfileBusyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jupiter-profile-test-")
        self.profile = Path(self.temp.name).resolve() / "dedicated profile"
        self.profile.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, command):
        return Mock(return_value=SimpleNamespace(stdout=command))

    def test_active_chrome_profile_is_busy_even_without_singleton(self):
        command = f"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir={self.profile} --new-window https://login.jupitered.com/"
        with self.assertRaises(session.BrowserSessionError) as raised:
            session.ensure_profile_idle(self.profile, runner=self.runner(command))
        self.assertEqual(raised.exception.code, "browser_busy")

    def test_regular_chrome_and_other_dedicated_profiles_are_ignored(self):
        for suffix in ("", f" --user-data-dir={self.profile}-other --new-window",
                       f" --user-data-dir={self.profile} extra --new-window"):
            command = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" + suffix
            session.ensure_profile_idle(self.profile, runner=self.runner(command))

    def test_live_singleton_is_busy_without_process_listing(self):
        (self.profile / "SingletonLock").symlink_to("host-12345")
        runner = self.runner("")
        with patch.object(session.os, "kill"):
            with self.assertRaises(session.BrowserSessionError):
                session.ensure_profile_idle(self.profile, runner=runner)
        runner.assert_not_called()

    def test_stale_singleton_does_not_block_and_is_not_deleted(self):
        lock = self.profile / "SingletonLock"
        lock.symlink_to("host-12345")
        with patch.object(session.os, "kill", side_effect=ProcessLookupError):
            session.ensure_profile_idle(self.profile, runner=self.runner(""))
        self.assertTrue(lock.is_symlink())

    def test_unavailable_process_listing_does_not_claim_profile_idle(self):
        with self.assertRaises(session.BrowserSessionError) as raised:
            session.ensure_profile_idle(self.profile, runner=Mock(side_effect=OSError))
        self.assertEqual(raised.exception.code, "browser_state_unavailable")

    def test_native_launch_options_preserve_native_keychain_and_locale(self):
        defaults = ["--no-first-run", "--password-store=basic"]
        ignored = ["--enable-automation"]
        result = session.native_profile_launch_options(defaults, ignored, "en-US")
        self.assertNotIn("--password-store=basic", result["args"])
        self.assertIn("--use-mock-keychain", result["ignore_default_args"])
        self.assertIn("--password-store=basic", result["ignore_default_args"])
        self.assertIn("--enable-automation", result["ignore_default_args"])
        self.assertIn("--accept-lang=en-US,en", result["args"])
        self.assertEqual(defaults, ["--no-first-run", "--password-store=basic"])
        self.assertEqual(ignored, ["--enable-automation"])


if __name__ == "__main__":
    unittest.main()
