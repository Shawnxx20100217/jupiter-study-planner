import sys
import unittest
from datetime import datetime
import json
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import calendar_output


class CalendarOutputTests(unittest.TestCase):
    def setUp(self):
        self.state = {"timezone": "Asia/Shanghai", "latest_sync": "2026-09-28T08:00:00+08:00"}
        self.plan = {"timezone": "Asia/Shanghai", "generated_at": "2026-09-28T08:00:00+08:00", "queue": [
            {"id": "a", "course": "Geo", "title": "Quiz", "due_date": "2026-09-29", "due_at": None, "due_precision": "date",
             "due_label": "2026-09-29（具体时间待确认）", "planning_disposition": "active", "excluded": False},
            {"id": "b", "course": "Calc", "title": "Test", "due_date": "2026-09-30", "due_at": "2026-09-30T12:00:00+08:00", "due_precision": "time",
             "due_label": "2026-09-30 12:00", "planning_disposition": "active", "excluded": False},
        ], "blocks": [{"id": "a", "course": "Geo", "title": "Quiz", "start": "2026-09-28T09:00:00+08:00", "end": "2026-09-28T09:30:00+08:00", "minutes": 30}]}

    def test_markdown_separates_deadlines_and_blocks(self):
        text = calendar_output.markdown(self.state, self.plan)
        self.assertIn("2026-09-29", text)
        self.assertIn("本地学习安排", text)
        self.assertIn("09:00", text)

    def test_ics_has_all_day_and_precise_events(self):
        text = calendar_output.ics(self.state, self.plan)
        self.assertIn("DTSTART;VALUE=DATE:20260929", text)
        self.assertIn("DTSTART:20260930T040000Z", text)
        self.assertIn("DTSTART:20260928T010000Z", text)
        self.assertIn("X-JUPITER-TYPE:DATE_ONLY_DEADLINE", text)
        self.assertNotIn("\nDTSTART;TZID", text)

    def test_no_date_only_is_safe(self):
        text = calendar_output.ics(self.state, self.plan, include_date_only=False)
        self.assertNotIn("20260929", text)
        self.assertIn("20260930T040000Z", text)

    def test_escape_and_fold(self):
        self.assertEqual(calendar_output.esc("a,b;c\\d\ne"), "a\\,b\\;c\\\\d\\ne")
        self.assertTrue(all(len(line.encode()) <= 75 for line in calendar_output.fold("SUMMARY:" + "中" * 80).split("\r\n")))

    def test_safe_default_api_omits_date_only_but_state_config_can_enable_it(self):
        text = calendar_output.calendar_ics(self.plan)
        self.assertNotIn("DTSTART;VALUE=DATE:20260929", text)
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            (state / "config.json").write_text(json.dumps({"include_date_only_events": True}), encoding="utf-8")
            text = calendar_output.calendar_ics(self.plan, state=state)
        self.assertIn("DTSTART;VALUE=DATE:20260929", text)

    def test_date_only_and_unknown_are_visible_in_markdown_and_grouped_by_month(self):
        text = calendar_output.calendar_markdown(self.plan)
        self.assertIn("2026年9月", text)
        self.assertIn("2026-09-29", text)
        self.assertIn("具体时间待确认", text)
        self.assertIn("本地规划块", text)

    def test_ics_uses_crlf_utf8_safe_folding_and_stable_uids(self):
        long_plan = dict(self.plan)
        long_plan["queue"] = [dict(self.plan["queue"][1], title="中文,含;特殊\\字符" + "很长" * 100)]
        first = calendar_output.calendar_ics(long_plan)
        second = calendar_output.calendar_ics(long_plan)
        self.assertEqual(first, second)
        self.assertTrue(first.endswith("\r\n"))
        self.assertTrue(all(len(line.encode("utf-8")) <= 75 for line in first.rstrip("\r\n").split("\r\n")))
        self.assertIn("\\,", first)
        self.assertIn("\\;", first)
        self.assertIn("\\\\", first)
        self.assertEqual(first.count("UID:"), 2)

    def test_deadline_uid_stays_stable_when_verified_time_changes(self):
        first = calendar_output.calendar_ics(self.plan)
        changed = dict(self.plan, queue=[dict(self.plan["queue"][1], due_at="2026-10-01T12:00:00+08:00")])
        second = calendar_output.calendar_ics(changed)
        # UID values are opaque hashes; compare the final (deadline) event.
        first_deadline = [line for line in first.splitlines() if line.startswith("UID:")][-1]
        second_deadline = [line for line in second.splitlines() if line.startswith("UID:")][-1]
        self.assertEqual(first_deadline, second_deadline)

    def test_bad_block_fails_closed_and_duplicate_blocks_are_deduped(self):
        bad = dict(self.plan)
        bad["blocks"] = [dict(self.plan["blocks"][0], end="2026-09-28T08:00:00+08:00")]
        with self.assertRaises(ValueError):
            calendar_output.calendar_ics(bad)
        block = self.plan["blocks"][0]
        duplicate = dict(self.plan, blocks=[block, dict(block)])
        self.assertEqual(calendar_output.calendar_ics(duplicate).count("BEGIN:VEVENT"), 2)

    def test_cli_writes_markdown_and_ics(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / "state"
            state.mkdir()
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
            markdown, ics = root / "calendar.md", root / "calendar.ics"
            result = subprocess.run([sys.executable, str(Path(calendar_output.__file__)), "calendar",
                                     "--state", str(state), "--plan", str(plan_path),
                                     "--markdown", str(markdown), "--ics", str(ics)],
                                    check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout)["events"], 2)
            self.assertTrue(markdown.is_file() and ics.is_file())


if __name__ == "__main__":
    unittest.main()
