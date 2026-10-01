"""sync_control tests use a fake launchctl and never touch the user's agents."""
import json
from pathlib import Path
import tempfile
import unittest
import sys

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import sync_control


class Result:
    def __init__(self, code=0, out=""):
        self.returncode, self.stdout, self.stderr = code, out, ""


class FakeLaunchctl:
    def __init__(self):
        self.loaded = set()
        self.running = set()
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        argv = argv[1:] if argv and argv[0] == "/bin/launchctl" else argv
        action, target = argv[0], argv[1]
        label = target.rsplit("/", 1)[-1]
        if action == "print":
            if label not in self.loaded:
                return Result(1)
            pid = "\n\tpid = 42" if label in self.running else ""
            return Result(0, pid)
        if action == "bootstrap":
            self.loaded.add(Path(argv[2]).stem)
            return Result()
        if action == "enable":
            return Result()
        if action == "disable":
            return Result()
        if action == "bootout":
            self.loaded.discard(label)
            self.running.discard(label)
            return Result()
        return Result(1)


class SyncControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sync-control-")
        root = Path(self.temp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.state = root / "state"
        self.report = root / "reports" / "study.md"
        self.instance = root / "instance.json"
        self.instance.write_text(json.dumps({"state_dir": str(self.state), "report_path": str(self.report)}))
        self.fake = FakeLaunchctl()
        self.python = Path(sys.executable)

    def tearDown(self):
        self.temp.cleanup()

    def test_four_switch_combinations_are_idempotent(self):
        for enabled, login in ((True, True), (True, False), (False, True), (False, False)):
            first = sync_control.configure(self.instance, self.python, enabled, login,
                                           home=self.home, runner=self.fake)
            second = sync_control.configure(self.instance, self.python, enabled, login,
                                            home=self.home, runner=self.fake)
            self.assertTrue(first["ok"])
            self.assertEqual(first["enabled"], enabled)
            self.assertEqual(first["launch_at_login"], login)
            self.assertEqual(second["enabled"], enabled)
            config = json.loads(self.instance.read_text())
            self.assertEqual(config["automation_paused"], not enabled)
            self.assertEqual(config["launch_at_login"], login)

    def test_login_off_moves_owned_agents_out_of_launchagents_without_touching_other_instance(self):
        sync_control.configure(self.instance, self.python, True, True,
                               home=self.home, runner=self.fake)
        other = self.home / "Library/LaunchAgents" / "other.plist"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_bytes(b"keep")
        result = sync_control.configure(self.instance, self.python, True, False,
                                        home=self.home, runner=self.fake)
        self.assertTrue(result["ok"])
        self.assertFalse(list((self.home / "Library/LaunchAgents").glob("local.jupiter*.plist")))
        self.assertTrue(other.exists())
        self.assertTrue(list((self.state / "agents").glob("local.jupiter*.plist")))

    def test_running_job_is_disabled_then_allowed_to_finish(self):
        result = sync_control.configure(self.instance, self.python, True, True,
                                        home=self.home, runner=self.fake)
        labels = sync_control._labels(self.instance.resolve())
        self.fake.running.add(labels["watch"])
        result = sync_control.configure(self.instance, self.python, False, True,
                                        home=self.home, runner=self.fake)
        self.assertFalse(result["enabled"])
        self.assertTrue(result["stopping"])
        self.assertIn("disable", [call[1] for call in self.fake.calls])

    def test_configure_reloads_idle_agents_when_interval_changes(self):
        sync_control.configure(self.instance, self.python, True, True,
                               home=self.home, runner=self.fake)
        self.instance.write_text(json.dumps({"state_dir": str(self.state),
                                             "report_path": str(self.report),
                                             "schedule_interval_minutes": 60}))
        result = sync_control.configure(self.instance, self.python, True, True,
                                        home=self.home, runner=self.fake)
        self.assertTrue(result["ok"])
        self.assertFalse(result["reload_deferred"])
        watch = self.home / "Library/LaunchAgents" / (sync_control._labels(self.instance.resolve())["watch"] + ".plist")
        ticktick = self.home / "Library/LaunchAgents" / (sync_control._labels(self.instance.resolve())["ticktick"] + ".plist")
        self.assertEqual(__import__("plistlib").loads(watch.read_bytes())["StartInterval"], 3600)
        self.assertEqual(__import__("plistlib").loads(ticktick.read_bytes())["StartInterval"], 3600)
        self.assertIn("bootout", [call[1] for call in self.fake.calls])

    def test_running_agent_defers_reload_without_booting_it_out(self):
        sync_control.configure(self.instance, self.python, True, True,
                               home=self.home, runner=self.fake)
        labels = sync_control._labels(self.instance.resolve())
        self.fake.running.add(labels["watch"])
        self.instance.write_text(json.dumps({"state_dir": str(self.state),
                                             "report_path": str(self.report),
                                             "schedule_interval_minutes": 60}))
        before = len(self.fake.calls)
        result = sync_control.configure(self.instance, self.python, True, True,
                                        home=self.home, runner=self.fake)
        self.assertTrue(result["reload_deferred"])
        self.assertIn(labels["watch"], result["reload_pending_labels"])
        watch_calls = self.fake.calls[before:]
        self.assertNotIn("bootout", [call[1] for call in watch_calls
                                      if len(call) > 2 and labels["watch"] in call[-1]])

    def test_source_mirror_status_uses_source_file_and_keeps_plan_timestamp_separate(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "run-status.json").write_text(json.dumps({
            "status": "ok", "last_success": "2026-10-01T00:10:00+00:00"
        }))
        (self.state / "ticktick-status.json").write_text(json.dumps({
            "ok": True, "last_success": "2026-09-30T00:01:00+00:00"
        }))
        (self.state / "ticktick-source-status.json").write_text(json.dumps({
            "ok": True, "last_success": "2026-10-01T00:11:00+00:00"
        }))
        config = json.loads(self.instance.read_text())
        config.update({"ticktick_sync_mode": "source_mirror", "automation_paused": True})
        self.instance.write_text(json.dumps(config))
        result = sync_control.status(self.instance, home=self.home, runner=self.fake)
        self.assertEqual(result["mode"], "source_mirror")
        self.assertEqual(result["ticktick_last_success"], "2026-10-01T00:11:00+00:00")
        self.assertTrue(result["jupiter_ok"])
        self.assertTrue(result["ticktick_ok"])

    def test_failed_nested_ticktick_result_is_safe_and_not_green(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "run-status.json").write_text(json.dumps({
            "status": "ok", "last_success": "2026-10-01T00:10:00+00:00"
        }))
        (self.state / "ticktick-source-status.json").write_text(json.dumps({
            "ok": False, "result": {"ticktick": {"ok": False, "error_code": "api_error",
                                                     "message": "raw token-like response body"}}
        }))
        config = json.loads(self.instance.read_text())
        config.update({"ticktick_sync_mode": "source_mirror", "automation_paused": True})
        self.instance.write_text(json.dumps(config))
        result = sync_control.status(self.instance, home=self.home, runner=self.fake)
        self.assertEqual(result["ticktick_last_success"], None)
        self.assertIn("未接受", result["last_error"])
        self.assertNotIn("raw token", result["last_error"])


if __name__ == "__main__":
    unittest.main()
