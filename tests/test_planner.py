from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "planner.py"
SPEC = importlib.util.spec_from_file_location("planner", SCRIPT)
planner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(planner)
NOW = "2026-09-25T16:00:00+08:00"


def task(task_id="math-1", **changes):
    result = {"id": task_id, "course": "Mathematics", "title": "Problem set",
              "due_date": None, "due_at": "2026-09-26T09:00:00+08:00",
              "due_precision": "time", "status": "open", "kind": "assignment",
              "raw_due": "Sep 26, 9:00 am", "source_url": "https://login.jupitered.com/",
              "estimated_minutes": None, "estimate_source": "provisional"}
    result.update(changes)
    return result


def snapshot(*tasks, complete=True, observed_at=NOW):
    return {"observed_at": observed_at, "timezone": "Asia/Shanghai",
            "coverage": {"complete": complete, "courses": ["Mathematics"]}, "tasks": list(tasks)}


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name) / "private-state"

    def sync(self, *tasks, **kwargs):
        return planner.sync(self.state_dir, snapshot(*tasks, **kwargs))

    def plan(self, **kwargs):
        return planner.make_plan(self.state_dir, now=NOW, **kwargs)

    def state(self):
        return planner.load_state(self.state_dir)

    def test_repeated_scan_has_no_duplicate_change_events(self):
        first = self.sync(task())
        second = self.sync(task())
        self.assertEqual([event["type"] for event in first["events"]], ["added"])
        self.assertEqual(second["events"], [])
        self.assertEqual(len(self.state()["events"]), 1)

    def test_changed_deadline_status_and_teacher_missing_are_separate(self):
        self.sync(task(is_missing=False))
        changed = self.sync(task(due_at="2026-09-27T09:00:00+08:00", status="unknown", is_missing=True))
        self.assertEqual({event["type"] for event in changed["events"]},
                         {"deadline_changed", "status_changed", "missing_changed"})
        self.assertEqual(self.plan()["queue"][0]["source_status"], "unknown")
        self.assertIn("Jupiter 明确标记为缺交", self.plan()["queue"][0]["notes"])

    def test_equivalent_timezone_deadline_does_not_emit_change(self):
        self.sync(task())
        self.assertEqual(self.sync(task(due_at="2026-09-26T01:00:00Z"))["events"], [])

    def test_partial_scan_retains_missing_tasks_and_evidence(self):
        self.sync(task(), task("history-2", course="History"))
        result = self.sync(task(), complete=False, observed_at="2026-09-25T17:00:00+08:00")
        record = self.state()["tasks"]["history-2"]
        self.assertEqual(result["retained"], 2)
        self.assertTrue(record["unverified"])
        self.assertEqual(record["last_seen"], NOW)
        self.assertIn("partial", record["verification_reason"])
        self.assertFalse(self.state()["latest_sync"]["coverage"]["complete"])
        self.assertIn("扫描尚未覆盖本次指定的可见课程列表", planner.markdown(self.plan()))

    def test_complete_scan_absence_does_not_delete_or_complete(self):
        self.sync(task())
        self.sync()
        record = self.state()["tasks"]["math-1"]
        self.assertTrue(record["unverified"])
        self.assertEqual(record["source"]["status"], "open")
        self.assertEqual(len(self.plan()["queue"]), 1)
        self.sync(task())
        self.assertFalse(self.state()["tasks"]["math-1"]["unverified"])

    def test_personal_estimate_progress_and_completion_survive_source_sync(self):
        self.sync(task())
        planner.update(self.state_dir, "math-1", estimate=90, remaining=35, status="completed")
        self.sync(task(estimated_minutes=20, estimate_source="observed"))
        item = self.plan()["excluded"][0]
        self.assertEqual((item["effective_estimate_minutes"], item["remaining_minutes"]), (90, 35))
        self.assertEqual(item["source_status"], "open")
        self.assertIn("提交待确认", planner.markdown(self.plan()))
        planner.update(self.state_dir, "math-1", status="open")
        self.assertEqual(self.plan()["queue"][0]["remaining_minutes"], 35)

    def test_submitted_source_excluded_and_unknown_kept(self):
        self.sync(task("submitted", status="submitted"), task("completed", status="completed"),
                  task("unknown", status="unknown"))
        result = self.plan()
        self.assertEqual([item["id"] for item in result["queue"]], ["unknown"])
        self.assertEqual(len(result["excluded"]), 2)

    def test_no_availability_means_queue_without_invented_windows(self):
        self.sync(task(), task("quiz", kind="assessment"))
        result = self.plan()
        self.assertEqual(result["mode"], "ranked_queue")
        self.assertEqual(result["blocks"], [])
        self.assertEqual(result["free_windows"], [])
        self.assertEqual(sorted(item["remaining_minutes"] for item in result["queue"]), [30, 45])
        self.assertTrue(all(item["effective_estimate_source"] == "provisional" for item in result["queue"]))
        self.assertIn("暂估", planner.markdown(result))

    def test_unknown_deadline_not_invented_and_ranked_after_known(self):
        self.sync(task("unknown", due_at=None, due_precision="unknown", raw_due=""), task("known"))
        result = self.plan(minutes=60)
        self.assertEqual([item["id"] for item in result["queue"]], ["known", "unknown"])
        unknown = result["queue"][1]
        self.assertIsNone(unknown["planning_cutoff"])
        self.assertIsNone(unknown["due_at"])
        self.assertIn("未知", unknown["due_label"])

    def test_priority_explains_deadline_and_uncertainty_without_model(self):
        self.sync(task("soon", due_at="2026-09-26T08:00:00+08:00", estimated_minutes=40),
                  task("unknown", due_at=None, due_precision="unknown", raw_due="", status="unknown",
                       is_missing=True))
        result = self.plan()
        soon = next(item for item in result["queue"] if item["id"] == "soon")
        unknown = next(item for item in result["queue"] if item["id"] == "unknown")
        self.assertEqual(soon["priority_band"], "high")
        self.assertIn("24 小时内到期", soon["priority_reasons"])
        self.assertEqual(unknown["priority_band"], "high")
        self.assertIn("deadline_unknown", unknown["risk_flags"])
        self.assertIn("marked_missing", unknown["risk_flags"])
        self.assertIn("先核对截止日期", unknown["next_action"])
        self.assertEqual(result["risk_summary"]["high"], 2)

    def test_current_minutes_keep_explanations_and_capacity_explicit(self):
        self.sync(task(estimated_minutes=60))
        result = self.plan(minutes=20)
        item = result["queue"][0]
        self.assertEqual(result["mode"], "current_session")
        self.assertEqual(result["session_minutes"], 20)
        self.assertEqual(item["scheduled_minutes"], 20)
        self.assertEqual(item["unscheduled_minutes"], 40)
        self.assertTrue(item["priority_reason"])
        self.assertEqual(item["next_action"], "可按剩余用时拆成下一段学习")

    def test_pending_review_count_is_part_of_plan_summary(self):
        self.sync(task())
        result = self.plan(pending_review=[{"resolved": False}, {"resolved": True}])
        self.assertEqual(result["pending_review_count"], 1)
        self.assertEqual(result["risk_summary"]["review"], 1)

    def test_twenty_minute_session_does_not_create_future_windows(self):
        self.sync(task(estimated_minutes=60), task("later", title="Later homework", due_at="2026-09-27T09:00:00+08:00"))
        result = self.plan(minutes=20)
        self.assertEqual(result["mode"], "current_session")
        self.assertEqual(len(result["blocks"]), 1)
        self.assertEqual(result["blocks"][0]["minutes"], 20)
        self.assertEqual(result["queue"][0]["unscheduled_minutes"], 40)
        self.assertNotIn("Later homework", planner.markdown(result))
        self.assertNotIn("尚未安排", planner.markdown(result))
        self.assertEqual(self.plan()["mode"], "ranked_queue")

    def test_subminute_window_does_not_create_or_round_up_work(self):
        self.sync(task())
        result = self.plan(availability={"windows": [{"start": NOW, "end": "2026-09-25T16:00:59+08:00"}]})
        self.assertEqual(result["blocks"], [])
        self.assertEqual(result["unscheduled_minutes"], 30)

    def test_busy_intervals_overlapping_windows_breaks_and_capacity(self):
        self.sync(task(estimated_minutes=160))
        result = self.plan(availability={"windows": [
            {"start": "2026-09-25T15:00:00+08:00", "end": "2026-09-25T17:00:00+08:00"},
            {"start": "2026-09-25T16:30:00+08:00", "end": "2026-09-25T18:00:00+08:00"}],
            "busy": [{"start": "2026-09-25T16:25:00+08:00", "end": "2026-09-25T16:45:00+08:00"}]})
        intervals = [(planner.instant(block["start"]), planner.instant(block["end"])) for block in result["blocks"]]
        busy_start, busy_end = planner.instant("2026-09-25T16:25:00+08:00"), planner.instant("2026-09-25T16:45:00+08:00")
        for index, (start, end) in enumerate(intervals):
            self.assertGreaterEqual(start, planner.instant(NOW))
            self.assertLessEqual(end, planner.instant("2026-09-25T18:00:00+08:00"))
            self.assertTrue(end <= busy_start or start >= busy_end)
            self.assertLessEqual((end - start).total_seconds(), 40 * 60)
            if index:
                self.assertGreaterEqual(start - intervals[index-1][1], timedelta(minutes=5))
        scheduled = sum(block["minutes"] for block in result["blocks"])
        self.assertEqual(scheduled, 95)
        self.assertEqual(result["unscheduled_minutes"], 65)

    def test_mixed_timezone_order_and_exact_deadline_no_late_blocks(self):
        self.sync(task("later", due_at="2026-09-25T10:00:00+00:00", estimated_minutes=50),
                  task("earlier", due_at="2026-09-25T04:20:00-04:00", estimated_minutes=60))
        result = self.plan(minutes=120)
        self.assertEqual([item["id"] for item in result["queue"]], ["earlier", "later"])
        by_id = {item["id"]: item for item in result["queue"]}
        for block in result["blocks"]:
            self.assertLessEqual(planner.instant(block["end"]), planner.instant(by_id[block["id"]]["due_at"]))
        self.assertEqual(by_id["earlier"]["scheduled_minutes"], 20)
        self.assertEqual(by_id["earlier"]["unscheduled_minutes"], 40)
        self.assertEqual(by_id["later"]["scheduled_minutes"], 50)

    def test_date_only_preserves_date_and_uses_previous_day_target(self):
        self.sync(task(due_at=None, due_date="2026-09-26", due_precision="date"))
        item = self.plan()["queue"][0]
        self.assertIsNone(item["due_at"])
        self.assertEqual(item["due_date"], "2026-09-26")
        self.assertEqual(planner.instant(item["planning_cutoff"]), planner.instant("2026-09-26T00:00:00+08:00"))
        self.assertIn("2026-09-25 当日结束", item["due_label"])
        self.assertTrue(item["cutoff_is_conservative"])

    def test_date_only_on_due_day_warns_without_false_exact_overdue_claim(self):
        self.sync(task(due_at=None, due_date="2026-09-25", due_precision="date"))
        result = self.plan(minutes=20)
        self.assertEqual(result["blocks"], [])
        notes = result["queue"][0]["notes"]
        self.assertIn("保守完成目标已过，实际截止时间待核实", notes)
        self.assertNotIn("已超过已知截止时间", notes)

    def test_explicit_date_target_never_spills_into_due_day(self):
        self.sync(task(due_at=None, due_date="2026-09-26", due_precision="date", estimated_minutes=120))
        result = self.plan(availability={"windows": [{"start": "2026-09-25T23:40:00+08:00", "end": "2026-09-26T01:00:00+08:00"}]})
        self.assertEqual(result["blocks"][0]["minutes"], 20)
        self.assertEqual(result["unscheduled_minutes"], 100)

    def test_zero_remaining_does_not_silently_mark_complete(self):
        self.sync(task())
        planner.update(self.state_dir, "math-1", remaining=0)
        result = self.plan(minutes=20)
        self.assertEqual(len(result["queue"]), 1)
        self.assertEqual(result["blocks"], [])
        self.assertEqual(result["queue"][0]["source_status"], "open")

    def test_disposition_change_preserves_source_status_and_personal_progress(self):
        self.sync(task())
        planner.update(self.state_dir, "math-1", estimate=90, remaining=25)
        changed = self.sync(task(planning_disposition="in_class", planning_note="课堂互评，由教师在课堂安排"))
        self.assertEqual([event["type"] for event in changed["events"]], ["planning_disposition_changed"])
        self.assertEqual(changed["events"][0]["before"], "active")
        self.assertEqual(changed["events"][0]["after"], "in_class")
        plan = self.plan(minutes=60)
        self.assertEqual(plan["queue"], [])
        self.assertEqual(plan["blocks"], [])
        self.assertEqual(plan["unscheduled_minutes"], 0)
        item = plan["excluded"][0]
        self.assertEqual(item["source_status"], "open")
        self.assertEqual(item["exclusion_reason"], "课堂互评，由教师在课堂安排")
        self.assertEqual((item["effective_estimate_minutes"], item["remaining_minutes"]), (90, 25))
        self.assertIn("课堂互评，由教师在课堂安排", planner.markdown(self.plan()))
        repeat = self.sync(task(planning_disposition="in_class", planning_note="课堂互评，由教师在课堂安排"))
        self.assertEqual(repeat["events"], [])
        self.sync(task(planning_disposition="active"))
        self.assertEqual(self.plan()["queue"][0]["remaining_minutes"], 25)

    def test_reference_and_superseded_items_never_double_count_work(self):
        self.sync(task("real"), task("reference", planning_disposition="reference", planning_note="Participation 记分项"),
                  task("notice", planning_disposition="superseded", planning_note="已拆成 real，原通知仅保留核对"))
        result = self.plan(minutes=60)
        self.assertEqual([item["id"] for item in result["queue"]], ["real"])
        self.assertEqual([block["id"] for block in result["blocks"]], ["real"])
        self.assertEqual(sum(block["minutes"] for block in result["blocks"]), 30)
        self.assertEqual(result["unscheduled_minutes"], 0)
        self.assertEqual(len(result["excluded"]), 2)
        self.assertEqual(len(self.state()["tasks"]), 3)
        self.assertTrue(all(item["source_status"] == "open" for item in result["excluded"]))
        report = planner.markdown(self.plan())
        self.assertIn("Participation 记分项", report)
        self.assertIn("已拆成 real", report)

    def test_implicit_and_explicit_active_are_equivalent(self):
        self.sync(task())
        self.assertEqual(self.sync(task(planning_disposition="active"))["events"], [])
        self.assertEqual(self.sync(task())["events"], [])
        self.assertEqual(len(self.plan()["queue"]), 1)

    def test_active_planning_note_is_visible_in_queue_and_current_session(self):
        note = "先完成第 1–3 题，再整理展示提纲；用时按准备环节暂估。"
        self.sync(task(planning_note=note))
        result = self.plan()
        self.assertIn(note, result["queue"][0]["notes"])
        self.assertIn(note, planner.markdown(result))
        self.assertIn(note, planner.markdown(self.plan(minutes=20)))

    def test_invalid_disposition_rejected_without_modifying_state(self):
        self.sync(task())
        before = (self.state_dir / "state.json").read_bytes()
        for bad in [task(planning_disposition="done"), task(planning_note=123)]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.sync(bad)
        self.assertEqual((self.state_dir / "state.json").read_bytes(), before)

    def test_report_shows_scope_notes_and_retained_classroom_date(self):
        incoming = snapshot(task(due_at=None, due_date="2026-09-28", due_precision="date",
                                 planning_disposition="in_class", planning_note="课堂互评"))
        incoming["coverage"]["courses"] = ["Mathematics", "English"]
        incoming["coverage"]["notes"] = "两科均显示 Period 1，实际课表尚待核实。"
        planner.sync(self.state_dir, incoming)
        report = planner.markdown(self.plan())
        self.assertIn("已覆盖本次指定的可见课程列表", report)
        self.assertNotIn("完整扫描", report)
        self.assertIn("Mathematics、English", report)
        self.assertIn(incoming["coverage"]["notes"], report)
        row = next(line for line in report.splitlines() if "课堂互评" in line)
        self.assertIn("2026-09-28", row)
        self.assertNotIn("2026-09-27", row)

    def test_rejects_old_snapshot_without_replacing_state(self):
        self.sync(task())
        before = (self.state_dir / "state.json").read_bytes()
        with self.assertRaises(ValueError):
            self.sync(task(status="completed"), observed_at="2026-09-24T16:00:00+08:00")
        self.assertEqual((self.state_dir / "state.json").read_bytes(), before)

    def test_invalid_input_does_not_modify_state(self):
        self.sync(task())
        before = (self.state_dir / "state.json").read_bytes()
        invalid = [task(due_at="2026-09-26T09:00:00"), task(estimated_minutes=-10),
                   task(due_at=None, due_date=None, due_precision="date"), task(is_missing="yes")]
        for bad in invalid:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.sync(bad)
        self.assertEqual((self.state_dir / "state.json").read_bytes(), before)
        with self.assertRaises(ValueError):
            self.plan(availability={"windows": [{"start": NOW, "end": "2026-09-25T15:00:00+08:00"}]})

    def test_config_changes_provisional_defaults_and_chunk_size(self):
        self.sync(task())
        planner.write_json(self.state_dir / "config.json", {"default_estimates": {"assignment": 70},
                                                           "max_block_minutes": 25, "break_minutes": 10})
        result = self.plan(minutes=60)
        self.assertEqual([block["minutes"] for block in result["blocks"]], [25, 25])
        self.assertEqual(result["unscheduled_minutes"], 20)

    def test_cli_writes_markdown_and_json_and_keeps_private_state_separate(self):
        input_path, output_path = Path(self.temp.name) / "input.json", Path(self.temp.name) / "plan.md"
        input_path.write_text(json.dumps(snapshot(task())), encoding="utf-8")
        subprocess.run([sys.executable, str(SCRIPT), "sync", "--state", str(self.state_dir),
                        "--snapshot", str(input_path)], check=True, capture_output=True, text=True)
        completed = subprocess.run([sys.executable, str(SCRIPT), "plan", "--state", str(self.state_dir),
                                    "--now", NOW, "--minutes", "20", "--output", str(output_path)],
                                   check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(completed.stdout)["mode"], "current_session")
        self.assertTrue(output_path.is_file())
        self.assertEqual(json.loads(output_path.with_suffix(".json").read_text())["blocks"][0]["minutes"], 20)


if __name__ == "__main__":
    unittest.main()
