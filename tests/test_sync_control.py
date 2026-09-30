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


if __name__ == "__main__":
    unittest.main()
