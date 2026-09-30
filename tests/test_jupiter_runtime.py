"""Runner tests use fabricated data and never launch browsers or contact services."""
import copy
import json
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import jupiter_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="jupiter-runtime-test-")
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / "private"
        self.report = self.root / "reports" / "study.md"
        self.instance = self.root / "instance.json"
        self.instance.write_text(json.dumps({"state_dir": str(self.state), "report_path": str(self.report)}))
        self.raw = {"observed_at": "2026-09-25T08:00:00+00:00", "timezone": "Asia/Shanghai",
                    "coverage": {"complete": True, "courses": ["Test Course"]},
                    "school_year": "2026-27",
                    "courses": [{"name": "Test Course", "rows": [{"id": "1", "title": "Practice", "score": "87"}]}],
                    "notices": [], "html": "<html>PRIVATE PAGE</html>", "cookies": "PRIVATE COOKIE"}
        self.snapshot = {"observed_at": self.raw["observed_at"], "timezone": "Asia/Shanghai",
                         "coverage": copy.deepcopy(self.raw["coverage"]),
                         "tasks": [{"id": "1", "course": "Test Course", "title": "Practice", "kind": "assignment",
                                    "status": "open", "due_precision": "date", "due_date": "2026-09-28"}],
                         "import_metadata": {"notices": {}}, "import_review": []}

    def tearDown(self):
        self.temporary.cleanup()

    def collect(self, path, login=False):
        self.assertEqual(Path(path), self.instance)
        self.assertFalse(login)
        return copy.deepcopy(self.raw)

    def normalize(self, raw, state):
        self.assertIn("tasks", state)
        return copy.deepcopy(self.snapshot)

    def execute(self, **kwargs):
        return runtime.run(self.instance, collector=self.collect, normalize=self.normalize,
                           now=kwargs.pop("now", "2026-09-25T08:00:00+00:00"), **kwargs)

    def read(self, name):
        return json.loads((self.state / name).read_text())

    def test_success_writes_state_reports_and_minimal_raw(self):
        result = self.execute()
        self.assertTrue(result["ok"])
        self.assertEqual(result["observed"], 1)
        self.assertEqual(result["new_notifications"], 1)
        self.assertEqual(self.read("run-status.json")["last_success"], "2026-09-25T08:00:00+00:00")
        self.assertTrue(self.report.is_file())
        self.assertTrue(self.report.with_suffix(".json").is_file())
        self.assertTrue(self.report.with_name(self.report.stem + "-calendar.md").is_file())
        self.assertTrue(self.report.with_suffix(".ics").is_file())
        raw = (self.state / "raw-latest.json").read_text()
        self.assertNotIn("PRIVATE", raw)
        self.assertNotIn("87", raw)
        self.assertNotIn("import_metadata", self.read("state.json"))
        self.assertEqual((self.state / "state.json").stat().st_mode & 0o777, 0o600)

    def test_unchanged_scan_does_not_repeat_notification(self):
        self.execute()
        self.snapshot["observed_at"] = "2026-09-25T09:00:00+00:00"
        result = self.execute(now=self.snapshot["observed_at"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["new_notifications"], 0)
        self.assertEqual(len(self.read("notifications.json")), 1)

    def test_deadline_reminders_are_configurable_and_deduplicated(self):
        task = {"id": "deadline-1", "course": "Test Course", "title": "Quiz",
                "due_precision": "time", "due_at": "2026-09-26T12:00:00+08:00",
                "planning_disposition": "active", "source_status": "open"}
        plan = {"timezone": "Asia/Shanghai", "queue": [task]}
        config = {"deadline_reminders_enabled": True, "deadline_reminder_minutes": [1440, 120, 30]}
        first, state = runtime.deadline_reminders(plan, config,
                                                   runtime.planner.instant("2026-09-25T12:00:00+08:00"))
        self.assertEqual([item["minutes_before"] for item in first], [1440])
        second, state = runtime.deadline_reminders(plan, config,
                                                    runtime.planner.instant("2026-09-26T10:01:00+08:00"), state)
        self.assertEqual([item["minutes_before"] for item in second], [120])
        third, state = runtime.deadline_reminders(plan, config,
                                                   runtime.planner.instant("2026-09-26T11:31:00+08:00"), state)
        self.assertEqual([item["minutes_before"] for item in third], [30])
        repeat, _ = runtime.deadline_reminders(plan, config,
                                                runtime.planner.instant("2026-09-26T11:40:00+08:00"), state)
        self.assertEqual(repeat, [])

    def test_ticktick_owns_reminders_when_enabled(self):
        task = {"id": "deadline-1", "course": "Test Course", "title": "Quiz",
                "due_precision": "time", "due_at": "2026-09-26T12:00:00+08:00",
                "planning_disposition": "active", "source_status": "open"}
        plan = {"timezone": "Asia/Shanghai", "queue": [task]}
        items, _ = runtime.deadline_reminders(plan, {"ticktick_enabled": True},
                                               runtime.planner.instant("2026-09-25T12:00:00+08:00"))
        self.assertEqual(items, [])

    def _add_refresh_fixture(self):
        """Create one local-completed task plus one remaining date-only task."""
        self.execute()
        state = self.read("state.json")
        state["tasks"]["2"] = {
            "first_seen": "2026-09-25T08:00:00+00:00",
            "last_seen": "2026-09-25T08:00:00+00:00", "personal": {},
            "source": {"id": "2", "course": "Test Course", "title": "Reading",
                        "kind": "assignment", "status": "open", "due_precision": "date",
                        "due_date": "2026-09-29"},
            "unverified": False, "verification_reason": None,
        }
        (self.state / "state.json").write_text(json.dumps(state), encoding="utf-8")
        runtime.planner.update(self.state, "1", status="completed")
        reviews = [
            {"key": "notice:new", "fingerprint": "new", "resolved": False,
             "item": {"message": "请核对新通知", "notice": {"title": "新通知"}}},
            {"key": "notice:old", "fingerprint": "old", "resolved": True,
             "item": {"message": "已解决通知", "notice": {"title": "已解决"}}},
        ]
        (self.state / "pending-review.json").write_text(json.dumps(reviews), encoding="utf-8")
        (self.state / "config.json").write_text(json.dumps({"include_date_only_events": True}), encoding="utf-8")

    def test_ticktick_completion_refreshes_report_ics_and_pending_reviews(self):
        self._add_refresh_fixture()
        import ticktick_client

        def fake_sync(instance_path, now=None, dry_run=False):
            runtime.planner.update(self.state, "1", status="completed")
            return {"ok": True, "local_updated": 1, "remote_completed": 1}

        with patch.object(ticktick_client, "sync_instance", side_effect=fake_sync):
            result = runtime.sync_ticktick(self.instance, now="2026-09-25T09:00:00+00:00")
        self.assertTrue(result["presentation_refreshed"])
        refreshed = json.loads(self.report.with_suffix(".json").read_text())
        self.assertEqual([item["id"] for item in refreshed["queue"]], ["2"])
        self.assertEqual(refreshed["pending_review_count"], 1)
        report_text = self.report.read_text(encoding="utf-8")
        self.assertIn("请核对新通知", report_text)
        self.assertNotIn("已解决通知", report_text)
        ics = self.report.with_suffix(".ics").read_text(encoding="utf-8")
        self.assertIn("Reading", ics)
        self.assertNotIn("Practice", ics)

    def test_refresh_honors_explicit_date_only_setting(self):
        self._add_refresh_fixture()
        self.instance.write_text(json.dumps({"state_dir": str(self.state),
                                             "report_path": str(self.report),
                                             "include_date_only_events": False}))
        runtime.refresh_plan_from_state(self.instance, now="2026-09-25T09:00:00+00:00")
        ics = self.report.with_suffix(".ics").read_text(encoding="utf-8")
        self.assertNotIn("Reading", ics)

    def test_run_honors_explicit_date_only_setting(self):
        self.instance.write_text(json.dumps({"state_dir": str(self.state),
                                             "report_path": str(self.report),
                                             "include_date_only_events": False}))
        self.execute()
        ics = self.report.with_suffix(".ics").read_text(encoding="utf-8")
        self.assertNotIn("Practice", ics)

    def test_refresh_failure_is_reported_after_remote_success(self):
        import ticktick_client
        with patch.object(ticktick_client, "sync_instance",
                          return_value={"ok": True, "local_updated": 1}), \
             patch.object(runtime, "refresh_plan_from_state",
                          side_effect=RuntimeError("export failed")):
            result = runtime.sync_ticktick(self.instance)
        self.assertTrue(result["ok"])
        self.assertFalse(result["presentation_refreshed"])
        self.assertEqual(result["warnings"][0]["code"], "local_refresh_failed")

    def test_date_only_deadline_reminder_uses_school_day_end(self):
        plan = {"timezone": "Asia/Shanghai", "queue": [{
            "id": "date-1", "course": "Test Course", "title": "Worksheet",
            "due_precision": "date", "due_date": "2026-09-26",
            "planning_disposition": "active", "source_status": "open"}]}
        items, _ = runtime.deadline_reminders(plan, {"deadline_reminder_minutes": [30]},
                                               runtime.planner.instant("2026-09-26T23:30:00+08:00"))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["due"], "2026-09-26（日期型截止）")

    def test_title_and_deadline_changes_detected(self):
        self.execute()
        self.snapshot["observed_at"] = "2026-09-25T09:00:00+00:00"
        self.snapshot["tasks"][0]["title"] = "Revised practice"
        self.snapshot["tasks"][0]["due_date"] = "2026-09-29"
        result = self.execute(now=self.snapshot["observed_at"])
        self.assertEqual(result["changes"]["updated"], 1)
        self.assertEqual(len(self.read("notifications.json")), 2)

    def test_failure_preserves_success_state_reports_and_raw(self):
        self.execute()
        before = {path: path.read_bytes() for path in (self.state / "state.json", self.report,
                  self.report.with_suffix(".json"), self.state / "raw-latest.json")}
        dashboard = self.report.with_suffix(".html")
        self.assertTrue(dashboard.is_file())

        def broken(path, login=False):
            raise RuntimeError("SECRET TOKEN https://example.invalid/?password=SECRET")

        result = runtime.run(self.instance, collector=broken, normalize=self.normalize, now="2026-09-25T09:00:00+00:00")
        self.assertFalse(result["ok"])
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        status = self.read("run-status.json")
        self.assertEqual(status["last_success"], "2026-09-25T08:00:00+00:00")
        self.assertEqual(status["last_attempt"], "2026-09-25T09:00:00+00:00")
        self.assertNotIn("SECRET", json.dumps(status))
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertIn('"status": "failed"', dashboard.read_text(encoding="utf-8"))

    def test_repeated_failure_quiet_and_recovery_notifies_once(self):
        self.execute()

        def broken(path, login=False):
            raise TimeoutError("anything")

        for moment in ("2026-09-25T09:00:00+00:00", "2026-09-25T10:00:00+00:00"):
            result = runtime.run(self.instance, collector=broken, normalize=self.normalize, now=moment)
            self.assertFalse(result["ok"])
        self.assertEqual(len(self.read("notifications.json")), 2)
        self.snapshot["observed_at"] = "2026-09-25T11:00:00+00:00"
        result = self.execute(now=self.snapshot["observed_at"])
        self.assertTrue(result["changes"]["recovered"])
        self.assertEqual(len(self.read("notifications.json")), 3)
        self.assertIsNone(self.read("notification-state.json")["failure"])
        self.execute(now="2026-09-25T12:00:00+00:00")
        self.assertEqual(len(self.read("notifications.json")), 3)

    def test_pending_notice_survives_future_no_new_reviews(self):
        notice = {"id": "n1", "author": "Teacher", "date_text": "Today", "title": "Reading", "text": "Please read chapter 2."}
        self.snapshot["import_review"] = [{"id": "n1", "type": "teacher_notice", "message": "新通知需要核对", "notice": notice}]
        self.snapshot["import_metadata"] = {"notices": {"n1": notice}}
        self.execute()
        self.snapshot["import_review"] = []
        self.snapshot["observed_at"] = "2026-09-25T09:00:00+00:00"

        def normalize(raw, state):
            self.assertEqual(state["import_metadata"]["notices"]["n1"]["title"], "Reading")
            return copy.deepcopy(self.snapshot)

        result = runtime.run(self.instance, collector=self.collect, normalize=normalize, now=self.snapshot["observed_at"])
        self.assertEqual(result["pending_review"], 1)
        self.assertEqual(result["new_notifications"], 0)
        self.assertIn("Please read chapter 2", self.report.read_text())

    def test_changed_notice_reopens_resolved_review(self):
        self.snapshot["import_review"] = [{"id": "n1", "type": "teacher_notice", "message": "Notice",
                                          "notice": {"id": "n1", "text": "First version"}}]
        self.execute()
        records = self.read("pending-review.json")
        records[0]["resolved"] = True
        (self.state / "pending-review.json").write_text(json.dumps(records))
        self.snapshot["import_review"][0]["notice"]["text"] = "Changed version"
        self.snapshot["observed_at"] = "2026-09-25T09:00:00+00:00"
        result = self.execute(now=self.snapshot["observed_at"])
        self.assertEqual(result["changes"]["review_changed"], 1)
        self.assertFalse(self.read("pending-review.json")[0]["resolved"])

    def test_dry_run_writes_no_state_or_report(self):
        result = self.execute(dry_run=True)
        self.assertTrue(result["ok"])
        self.assertFalse(self.state.exists())
        self.assertFalse(self.report.exists())

    def test_dry_run_leaves_existing_files_byte_identical(self):
        self.execute()
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.snapshot["tasks"][0]["due_date"] = "2026-09-30"
        self.execute(dry_run=True)
        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_remind_reads_last_plan_without_collecting(self):
        self.execute()
        config = json.loads(self.instance.read_text())
        config.update({"system_notifications": False, "deadline_reminders_enabled": True,
                       "deadline_reminder_minutes": [120, 30]})
        self.instance.write_text(json.dumps(config))
        plan_path = self.report.with_suffix(".json")
        plan = json.loads(plan_path.read_text())
        plan["timezone"] = "Asia/Shanghai"
        plan["queue"][0].update({"due_precision": "time", "due_date": "2026-09-26",
                                  "due_at": "2026-09-26T12:00:00+08:00",
                                  "planning_disposition": "active", "source_status": "open",
                                  "personal_status": "open"})
        plan_path.write_text(json.dumps(plan))
        result = runtime.remind(self.instance, now="2026-09-26T10:01:00+08:00")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["deadline_reminders"], 1)
        self.assertEqual(self.read("notifications.json")[-1]["type"], "deadline")
        self.assertIn("deadlines", self.read("notification-state.json"))

    def test_remind_dry_run_does_not_write_files(self):
        self.execute()
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        result = runtime.remind(self.instance, now="2026-09-25T08:00:00+00:00", dry_run=True)
        self.assertTrue(result["ok"])
        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_failed_sync_still_checks_saved_deadline_reminder(self):
        self.execute()
        config = json.loads(self.instance.read_text())
        config.update({"system_notifications": False, "deadline_reminders_enabled": True,
                       "deadline_reminder_minutes": [120]})
        self.instance.write_text(json.dumps(config))
        plan_path = self.report.with_suffix(".json")
        plan = json.loads(plan_path.read_text())
        plan["timezone"] = "Asia/Shanghai"
        plan["queue"][0].update({"due_precision": "time", "due_date": "2026-09-26",
                                  "due_at": "2026-09-26T12:00:00+08:00",
                                  "planning_disposition": "active", "source_status": "open",
                                  "personal_status": "open"})
        plan_path.write_text(json.dumps(plan))

        def broken(path, login=False):
            raise TimeoutError("network")

        result = runtime.run(self.instance, collector=broken, normalize=self.normalize,
                             now="2026-09-26T10:01:00+08:00")
        self.assertFalse(result["ok"])
        self.assertEqual(result["deadline_reminders"], 1)
        self.assertEqual(self.read("notifications.json")[-1]["type"], "deadline")

    def test_concurrent_run_skipped_without_collecting(self):
        def should_not_run(path, login=False):
            self.fail("collector called while a run was active")

        with runtime.run_lock(self.state):
            result = runtime.run(self.instance, collector=should_not_run, normalize=self.normalize)
        self.assertTrue(result["skipped"])
        self.assertFalse((self.state / "run-status.json").exists())

    def test_invalid_snapshot_does_not_mutate_old_data(self):
        self.execute()
        before = (self.state / "state.json").read_bytes()
        self.snapshot["tasks"][0]["due_precision"] = "invented"
        result = self.execute()
        self.assertEqual(result["error_code"], "invalid_source_data")
        self.assertEqual((self.state / "state.json").read_bytes(), before)

    def test_personal_progress_survives_runner_sync(self):
        self.execute()
        runtime.planner.update(self.state, "1", remaining=17, status="completed")
        self.snapshot["observed_at"] = "2026-09-25T09:00:00+00:00"
        self.execute(now=self.snapshot["observed_at"])
        personal = self.read("state.json")["tasks"]["1"]["personal"]
        self.assertEqual(personal["remaining_minutes"], 17)
        self.assertEqual(personal["status"], "completed")

    def test_commit_failure_rolls_back_prior_replacements(self):
        first, second = self.root / "first", self.root / "second"
        first.write_text("old first")
        second.write_text("old second")
        actual = runtime.os.replace
        count = 0

        def fail_second(source, destination):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("simulated write failure")
            return actual(source, destination)

        with patch.object(runtime.os, "replace", side_effect=fail_second):
            with self.assertRaises(OSError):
                runtime.commit_files({first: "new first", second: "new second"})
        self.assertEqual(first.read_text(), "old first")
        self.assertEqual(second.read_text(), "old second")
        self.assertEqual({p.name for p in self.root.iterdir()}, {"first", "second", "instance.json"})

    def test_private_raw_removes_markup_and_query_secrets(self):
        self.raw["notices"] = [{"id": "1", "text": "<p>Read this</p><script>SECRET</script> https://user:pass@example.org/read?token=SECRET#SECRET",
                                "cookie": "SECRET", "score": 87}]
        encoded = json.dumps(runtime.private_raw(self.raw))
        self.assertNotIn("SECRET", encoded)
        self.assertNotIn("pass", encoded)
        self.assertNotIn("87", encoded)
        self.assertIn("https://example.org/read", encoded)

    def test_generate_plist_uses_literal_arguments_and_never_enables(self):
        output = self.root / "generated with spaces.plist"
        result = runtime.generate_launch_agent(self.instance, sys.executable, output)
        self.assertFalse(result["enabled"])
        value = plistlib.loads(output.read_bytes())
        self.assertEqual(value["ProgramArguments"][2:], ["run", "--instance", str(self.instance)])
        self.assertEqual(value["StartCalendarInterval"], [{"Hour": 7, "Minute": 0}, {"Hour": 17, "Minute": 0}])
        self.assertFalse(value["RunAtLoad"])
        self.assertFalse(value["KeepAlive"])
        self.assertFalse(self.state.exists())

    def test_generate_reminder_plist_uses_saved_plan_and_thirty_minutes(self):
        output = self.root / "reminder.plist"
        result = runtime.generate_launch_agent(self.instance, sys.executable, output, mode="remind")
        self.assertFalse(result["enabled"])
        self.assertEqual(result["mode"], "remind")
        value = plistlib.loads(output.read_bytes())
        self.assertEqual(value["ProgramArguments"][2:], ["remind", "--instance", str(self.instance)])
        self.assertEqual(value["StartInterval"], 1800)
        self.assertTrue(value["RunAtLoad"])
        self.assertNotIn("StartCalendarInterval", value)
        self.assertFalse(self.state.exists())

    def test_generate_watch_plist_polls_jupiter_every_fifteen_minutes(self):
        output = self.root / "watch.plist"
        result = runtime.generate_launch_agent(self.instance, sys.executable, output, mode="watch")
        self.assertFalse(result["enabled"])
        self.assertEqual(result["mode"], "watch")
        value = plistlib.loads(output.read_bytes())
        self.assertEqual(value["ProgramArguments"][2:], ["run", "--instance", str(self.instance)])
        self.assertEqual(value["StartInterval"], 900)
        self.assertTrue(value["RunAtLoad"])
        self.assertNotIn("StartCalendarInterval", value)

    def test_user_can_pause_and_resume_background_automation(self):
        self.instance.write_text(json.dumps({"state_dir": str(self.state),
                                             "report_path": str(self.report),
                                             "ticktick_enabled": True}))
        paused = runtime.set_automation_paused(self.instance, True)
        self.assertTrue(paused["ok"])
        config = json.loads(self.instance.read_text())
        self.assertTrue(config["automation_paused"])
        self.assertTrue(config["collection_paused"])
        result = runtime.run(self.instance, collector=self.collect, normalize=self.normalize)
        self.assertEqual(result["error_code"], "automation_paused")
        self.assertTrue(result["skipped"])
        resumed = runtime.set_automation_paused(self.instance, False)
        self.assertFalse(resumed["automation_paused"])
        config = json.loads(self.instance.read_text())
        self.assertFalse(config["automation_paused"])
        self.assertFalse(config["collection_paused"])
        self.assertEqual(self.instance.stat().st_mode & 0o777, 0o600)

    def test_pause_blocks_standalone_ticktick_and_reminder_agents(self):
        self.instance.write_text(json.dumps({"state_dir": str(self.state),
                                             "report_path": str(self.report),
                                             "ticktick_enabled": True,
                                             "automation_paused": True}))
        tick = runtime.sync_ticktick(self.instance)
        self.assertTrue(tick["skipped"])
        self.assertEqual(tick["error_code"], "automation_paused")
        reminder = runtime.remind(self.instance, now="2026-09-25T08:00:00+00:00")
        self.assertTrue(reminder["skipped"])
        self.assertEqual(reminder["error_code"], "automation_paused")

    def test_launch_at_login_preference_is_persistent_and_has_private_plist_path(self):
        enabled = runtime.set_launch_at_login(self.instance, True)
        self.assertTrue(enabled["launch_at_login"])
        self.assertTrue(enabled["plist"].endswith(".plist"))
        self.assertTrue(json.loads(self.instance.read_text())["launch_at_login"])
        disabled = runtime.set_launch_at_login(self.instance, False)
        self.assertFalse(disabled["launch_at_login"])
        self.assertFalse(json.loads(self.instance.read_text())["launch_at_login"])

    def test_launch_agent_preserves_virtual_environment_symlink(self):
        interpreter = self.root / "venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(sys.executable)
        output = self.root / "agent.plist"
        runtime.generate_launch_agent(self.instance, interpreter, output)
        self.assertEqual(plistlib.loads(output.read_bytes())["ProgramArguments"][0], str(interpreter))
        self.assertNotEqual(str(interpreter), str(interpreter.resolve()))

    def test_real_importer_with_fake_collector_end_to_end(self):
        self.raw["courses"][0]["rows"][0].update(date="2026-09-28", source_display_status="ungraded", category="Homework")
        self.raw["notices"] = [{"id": "n1", "author": "Teacher", "date_text": "Sep 25", "title": "Reminder", "text": "Read chapter 2"}]
        first = runtime.run(self.instance, collector=self.collect, now="2026-09-25T08:00:00+00:00")
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["pending_review"], 1)
        task_id = next(iter(self.read("state.json")["tasks"]))
        runtime.planner.update(self.state, task_id, remaining=12)
        self.raw["observed_at"] = "2026-09-25T09:00:00+00:00"
        second = runtime.run(self.instance, collector=self.collect, now=self.raw["observed_at"])
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["new_notifications"], 0)
        self.assertEqual(self.read("state.json")["tasks"][task_id]["personal"]["remaining_minutes"], 12)
        self.assertEqual(self.read("raw-latest.json")["school_year"], "2026-27")

    def test_changed_notice_replaces_added_notice_in_pending_list(self):
        earlier = runtime.merge_reviews([], [{"type": "notice_added", "id": "n1", "message": "New",
                                             "notice": {"id": "n1", "text": "First"}}], "time1")
        changed = runtime.merge_reviews(earlier, [{"type": "notice_changed", "id": "n1", "message": "Updated",
                                                 "notice": {"id": "n1", "text": "Second"}}], "time2")
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["item"]["notice"]["text"], "Second")

    def test_changed_ambiguous_dates_remain_reviewable_and_change_fingerprint(self):
        earlier = runtime.merge_reviews([], [{"type": "date_ambiguous", "id": "1", "message": "Check",
                                             "raw_due": "5/10", "candidates": ["2026-05-10", "2027-05-10"]}], "time1")
        changed = runtime.merge_reviews(earlier, [{"type": "date_ambiguous", "id": "1", "message": "Check",
                                                 "raw_due": "5/11", "candidates": ["2026-05-11", "2027-05-11"]}], "time2")
        self.assertNotEqual(earlier[0]["fingerprint"], changed[0]["fingerprint"])
        self.assertEqual(changed[0]["item"]["raw_due"], "5/11")

    def test_output_cannot_overwrite_state_or_instance(self):
        self.instance.write_text(json.dumps({"state_dir": str(self.state), "report_path": str(self.state / "state.json")}))
        self.assertEqual(self.execute()["error_code"], "invalid_instance")
        self.assertFalse(self.state.exists())

    def test_first_failed_run_has_no_fabricated_last_success(self):
        def failed(path, login=False):
            error = RuntimeError("PRIVATE")
            error.code = "login_required"
            raise error

        result = runtime.run(self.instance, collector=failed, normalize=self.normalize, now="2026-09-25T08:00:00+00:00")
        self.assertEqual(result["error_code"], "login_required")
        self.assertNotIn("last_success", self.read("run-status.json"))
        self.assertFalse((self.state / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
