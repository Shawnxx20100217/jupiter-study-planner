import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from init_instance import initialize
from package_share import package, package_plugin


IDENTITY = {"expected_student": "Example Student", "expected_school_year": "Example School 2026-27",
            "expected_term": "1st Semester", "expected_courses": ["Mathematics", "English"],
            "school_timezone": "Asia/Shanghai"}


class InstanceTests(unittest.TestCase):
    def test_new_instance_is_private_unsynced_and_collector_compatible(self):
        from jupiter_collector import config_at
        from jupiter_runtime import instance_config
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            result = initialize(base / "personal", IDENTITY, base / "plugin")
            config = config_at(result["instance"])
            _, state, report, _ = instance_config(result["instance"])
            self.assertEqual(config["expected_courses"], IDENTITY["expected_courses"])
            self.assertEqual(state, (base / "personal/state").resolve())
            self.assertEqual(report, (base / "personal/reports/study-plan.md").resolve())
            self.assertEqual(stat.S_IMODE(Path(result["instance"]).stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
            self.assertFalse((state / "state.json").exists())
            self.assertFalse((state / "run-status.json").exists())
            self.assertFalse(result["schedule_enabled"])
            self.assertEqual(config["schedule_interval_minutes"], 60)
            self.assertEqual(config["ticktick_sync_mode"], "source_mirror")
            self.assertEqual(config["ticktick_source_project_name"], "原始任务")
            paths_file = Path(result["paths"])
            paths = json.loads(paths_file.read_text())
            self.assertEqual(paths["instance"], result["instance"])
            self.assertEqual(paths["state"], str(state))
            self.assertEqual(paths["scripts"], str((base / "plugin/scripts").resolve()))
            self.assertEqual(paths["reports"], result["report_directory"])
            self.assertEqual(stat.S_IMODE(paths_file.stat().st_mode), 0o600)

    def test_paths_preserve_virtual_environment_python_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            venv_python = base / "venv/bin/python"
            venv_python.parent.mkdir(parents=True)
            venv_python.symlink_to(sys.executable)
            with patch("init_instance.sys.executable", str(venv_python)):
                result = initialize(base / "personal", IDENTITY, base / "plugin")
            paths = json.loads(Path(result["paths"]).read_text())
            self.assertEqual(paths["python"], str(venv_python.absolute()))
            self.assertNotEqual(paths["python"], str(venv_python.resolve()))

    def test_existing_data_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "personal"
            initialize(directory, IDENTITY, Path(temp) / "plugin")
            instance = directory / "state/local-instance.json"
            before = instance.read_bytes()
            with self.assertRaises(ValueError):
                initialize(directory, {**IDENTITY, "expected_student": "Someone Else"}, Path(temp) / "plugin")
            self.assertEqual(before, instance.read_bytes())

    def test_existing_report_directory_not_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "personal"
            (directory / "reports").mkdir(parents=True)
            (directory / "reports/study-plan.md").write_text("Private report")
            with self.assertRaises(ValueError):
                initialize(directory, IDENTITY, Path(temp) / "plugin")
            self.assertFalse((directory / "state").exists())

    def test_plugin_data_and_bad_identity_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin = Path(temp) / "plugin"
            for data, identity in ((plugin / "state", IDENTITY),
                                   (Path(temp) / "personal", {**IDENTITY, "school_timezone": "not/a-zone"}),
                                   (Path(temp) / "personal", {**IDENTITY, "expected_courses": ["Math", "Math"]})):
                with self.assertRaises(ValueError):
                    initialize(data, identity, plugin)
                self.assertFalse(data.exists())


class PackageTests(unittest.TestCase):
    def test_archive_uses_allowlist_and_omits_private_files_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "plugin"
            allowed = {".codex-plugin/plugin.json": "{}", "README.md": "Instructions",
                       "scripts/main.py": "pass", "tests/test_main.py": "pass",
                       "examples/local-instance.example.json": "{}", "assets/icon.svg": "<svg/>"}
            blocked = {"state/state.json": "private", "scripts/state.json": "private",
                       "scripts/__pycache__/main.pyc": "compiled", "reports/study-plan.html": "private",
                       "assets/study-plan.html": "private", "examples/collector-session.json": "secret",
                       "examples/.hidden.json": "secret", "test.zip": "zip", ".codex-plugin/secret.json": "secret"}
            for name, data in {**allowed, **blocked}.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(data)
            (root / "scripts/link.py").symlink_to(root / "scripts/main.py")
            archive = Path(temp) / "share.zip"
            self.assertEqual(package(root, archive), len(allowed))
            with zipfile.ZipFile(archive) as handle:
                self.assertEqual(set(handle.namelist()), {"jupiter-study-planner/" + name for name in allowed})

    def test_source_tree_required(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                package(temp, Path(temp) / "share.zip")

    def test_plugin_archive_uses_canonical_root_scripts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            (root / "scripts").mkdir(parents=True)
            (root / "scripts/runtime.py").write_text("canonical runtime")
            (root / "requirements-collector.txt").write_text("scrapling==0.4.15")
            (root / "assets/dashboard.html").parent.mkdir(parents=True)
            (root / "assets/dashboard.html").write_text("dashboard")
            plugin = root / "plugins/jupiter-reader"
            (plugin / ".codex-plugin").mkdir(parents=True)
            (plugin / ".codex-plugin/plugin.json").write_text('{"name":"jupiter-reader"}')
            (plugin / "assets").mkdir()
            (plugin / "assets/icon.svg").write_text("<svg/>")
            (plugin / "skills/jupiter-reader").mkdir(parents=True)
            (plugin / "skills/jupiter-reader/SKILL.md").write_text("skill")
            # A stale copied file must never win over the canonical root file.
            (plugin / "scripts").mkdir()
            (plugin / "scripts/runtime.py").write_text("stale duplicate")

            archive = Path(temp) / "jupiter-reader.zip"
            count = package_plugin(root, "jupiter-reader", archive)
            self.assertEqual(count, 6)
            with zipfile.ZipFile(archive) as handle:
                names = set(handle.namelist())
                self.assertEqual(names, {
                    "jupiter-reader/.codex-plugin/plugin.json",
                    "jupiter-reader/assets/icon.svg",
                    "jupiter-reader/assets/dashboard.html",
                    "jupiter-reader/skills/jupiter-reader/SKILL.md",
                    "jupiter-reader/scripts/runtime.py",
                    "jupiter-reader/requirements-collector.txt",
                })
                self.assertEqual(handle.read("jupiter-reader/scripts/runtime.py"), b"canonical runtime")


if __name__ == "__main__":
    unittest.main()
