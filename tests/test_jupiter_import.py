"""Pure-rule importer regression fixtures; all assignments and notices are fictional."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


imp = module("jupiter_import")
planner = module("planner")
NOW = "2026-09-25T16:00:00+08:00"


def row(**extra):
    result = {"id": "123", "course": "Fictional Studies", "date": "9/28", "title": "Fictional project",
              "source_display_status": "ungraded", "category": "Homework"}
    result.update(extra)
    return result


def raw(*rows, **extra):
    result = {"observed_at": NOW, "timezone": "Asia/Shanghai", "school_year": "2026-27",
              "courses": [{"name": "Fictional Studies", "teacher": "Teacher Example", "rows": list(rows)}],
              "notices": [], "coverage": {"complete": True, "courses": ["Fictional Studies"], "notes": "Synthetic fixture"}}
    result.update(extra)
    return result


def source(**extra):
    result = {"id": "stable-original-id", "course": "Fictional Studies", "title": "Fictional project",
              "due_date": "2026-09-28", "due_at": "2026-09-28T09:00:00+08:00", "due_precision": "time",
              "raw_due": "Teacher explicitly said Sep 28 at 9 am", "status": "unknown", "kind": "assignment",
              "source_url": "https://login.jupitered.com/", "source_assignment_id": "123",
              "estimated_minutes": 70, "estimate_source": "user", "planning_disposition": "active"}
    result.update(extra)
    return result


def state(*sources):
    return {"version": 1, "timezone": "Asia/Shanghai", "events": [], "tasks": {
        item["id"]: {"source": item, "personal": {"estimated_minutes": 80, "remaining_minutes": 25},
                     "first_seen": NOW, "last_seen": NOW} for item in sources}}


class ImportTests(unittest.TestCase):
    def test_assignment_id_preserves_identity_exact_time_metadata_and_personal_progress(self):
        old = state(source(planning_note="Keep these teacher steps", custom_evidence={"origin": "notice"}))
        incoming = raw(row(title="Renamed fictional project"))
        result = imp.build_snapshot(incoming, old)
        item = result["tasks"][0]
        self.assertEqual(item["id"], "stable-original-id")
        self.assertEqual(item["due_at"], "2026-09-28T09:00:00+08:00")
        self.assertEqual(item["planning_note"], "Keep these teacher steps")
        self.assertEqual(item["custom_evidence"], {"origin": "notice"})
        with tempfile.TemporaryDirectory() as folder:
            planner.write_json(Path(folder) / "state.json", old)
            planner.sync(folder, result)
            saved = planner.load_state(folder)
            self.assertEqual(saved["tasks"][item["id"]]["personal"]["remaining_minutes"], 25)
            self.assertEqual(planner.sync(folder, result)["events"], [])

    def test_unique_exact_course_title_fallback_adds_source_assignment_id(self):
        previous = source()
        previous.pop("source_assignment_id")
        item = imp.build_snapshot(raw(row()), state(previous))["tasks"][0]
        self.assertEqual(item["id"], "stable-original-id")
        self.assertEqual(item["source_assignment_id"], "123")

    def test_ambiguous_identity_is_reviewed_not_duplicated(self):
        previous = source(id="copy-a")
        duplicate = source(id="copy-b")
        result = imp.build_snapshot(raw(row()), state(previous, duplicate))
        self.assertEqual(result["tasks"], [])
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["import_review"][0]["type"], "identity_ambiguous")

    def test_confirmed_calendar_date_is_used_and_changed_day_clears_old_clock(self):
        result = imp.build_snapshot(raw(row(date="10/2", confirmed_due_date="2026-10-02")), state(source()))
        item = result["tasks"][0]
        self.assertEqual(item["due_date"], "2026-10-02")
        self.assertIsNone(item["due_at"])
        self.assertEqual(item["due_precision"], "date")
        self.assertEqual(item["deadline_conflict"]["previous_due_at"], "2026-09-28T09:00:00+08:00")
        self.assertIn("exact_deadline_needs_review", [entry["type"] for entry in result["import_review"]])

    def test_calendar_mismatch_is_unknown_and_old_clock_is_not_retained(self):
        result = imp.build_snapshot(raw(row(date="10/2", confirmed_due_date="2026-10-03")), state(source()))
        self.assertEqual(result["tasks"][0]["due_precision"], "unknown")
        self.assertIsNone(result["tasks"][0]["due_at"])
        self.assertIn("deadline_conflict", [entry["type"] for entry in result["import_review"]])

    def test_unknown_current_date_preserves_explicit_previous_deadline_evidence(self):
        item = imp.build_snapshot(raw(row(date=None)), state(source()))["tasks"][0]
        self.assertEqual(item["due_at"], "2026-09-28T09:00:00+08:00")
        self.assertEqual(item["deadline_verification"], "previous_evidence_not_reconfirmed")

    def test_ambiguous_new_month_day_does_not_guess_school_start_or_year(self):
        result = imp.build_snapshot(raw(row(date="5/10")), state())
        item = result["tasks"][0]
        self.assertEqual(item["due_precision"], "unknown")
        self.assertIsNone(item["due_date"])
        self.assertEqual(item["planning_disposition"], "reference")
        self.assertEqual(result["import_review"][0]["candidates"], ["2026-05-10", "2027-05-10"])

    def test_explicit_year_weekday_or_previous_mapping_disambiguate(self):
        for incoming, expected in ((row(date="9/28/2026"), "2026-09-28"),
                                   (row(date="2026-09-28"), "2026-09-28"),
                                   (row(date="Mon 9/28"), "2026-09-28"),
                                   (row(date="1/4/27"), "2027-01-04")):
            with self.subTest(incoming=incoming):
                item = imp.build_snapshot(raw(incoming), state())["tasks"][0]
                self.assertEqual(item["due_date"], expected)
        existing = source(due_date="2027-01-04", due_at=None, due_precision="date")
        item = imp.build_snapshot(raw(row(date="1/4")), state(existing))["tasks"][0]
        self.assertEqual(item["due_date"], "2027-01-04")

    def test_changed_ambiguous_date_drops_wrong_existing_exact_deadline(self):
        result = imp.build_snapshot(raw(row(date="1/4")), state(source()))
        item = result["tasks"][0]
        self.assertIsNone(item["due_at"])
        self.assertIsNone(item["due_date"])
        self.assertIn("exact_deadline_needs_review", [entry["type"] for entry in result["import_review"]])

    def test_source_status_is_evidence_based_and_ungraded_is_unknown(self):
        for display, status, missing in (("ungraded", "unknown", None), ("missing", "open", True),
                                          ("submitted", "submitted", False), ("completed", "completed", False)):
            with self.subTest(display=display):
                item = imp.build_snapshot(raw(row(date="2026-09-28", source_display_status=display)), state())["tasks"][0]
                self.assertEqual((item["status"], item["is_missing"]), (status, missing))
        old = state(source(status="submitted"))
        self.assertEqual(imp.build_snapshot(raw(row()), old)["tasks"][0]["status"], "submitted")

    def test_new_graded_history_is_skipped_existing_graded_is_reference_not_completed(self):
        incoming = raw(row(source_display_status="graded"), row(id="456", title="Old scored work", source_display_status="graded"))
        result = imp.build_snapshot(incoming, state(source()))
        self.assertEqual(len(result["tasks"]), 1)
        item = result["tasks"][0]
        self.assertEqual(item["status"], "unknown")
        self.assertEqual(item["planning_disposition"], "reference")
        self.assertIn("已评分", item["planning_note"])
        self.assertEqual(result["import_metadata"]["skipped_new_graded"], 1)

    def test_historical_ungraded_is_reference_but_explicit_missing_stays_active(self):
        for status, disposition in (("ungraded", "reference"), ("missing", "active")):
            result = imp.build_snapshot(raw(row(date="2026-05-15", source_display_status=status)), state())
            self.assertEqual(result["tasks"][0]["planning_disposition"], disposition)
            if status == "ungraded":
                self.assertIn("historical_unconfirmed", [entry["type"] for entry in result["import_review"]])

    def test_existing_active_ungraded_remains_active_after_deadline_passes(self):
        old = state(source(planning_note="Known work still needs attention"))
        result = imp.build_snapshot(raw(row()), old, now="2026-09-29T16:00:00+08:00")
        item = result["tasks"][0]
        self.assertEqual(item["planning_disposition"], "active")
        self.assertEqual(item["planning_note"], "Known work still needs attention")
        self.assertNotIn("historical_unconfirmed", [entry["type"] for entry in result["import_review"]])
        self.assertNotIn("import_disposition_reason", item)

    def test_automatic_graded_reference_restores_active_on_explicit_missing(self):
        previous = state(source(planning_note="Teacher steps to preserve"))
        graded = imp.build_snapshot(raw(row(source_display_status="graded")), previous)["tasks"][0]
        self.assertEqual(graded["planning_disposition"], "reference")
        missing = imp.build_snapshot(raw(row(source_display_status="missing")), state(graded))
        item = missing["tasks"][0]
        self.assertEqual(item["planning_disposition"], "active")
        self.assertEqual(item["planning_note"], "Teacher steps to preserve")
        self.assertEqual((item["status"], item["is_missing"]), ("open", True))
        self.assertNotIn("import_disposition_reason", item)
        self.assertNotIn("missing_disposition_review", [entry["type"] for entry in missing["import_review"]])
        repeated = imp.build_snapshot(raw(row(source_display_status="missing")), state(item))
        self.assertEqual(repeated["tasks"][0], item)

    def test_automatic_historical_reference_restores_without_stale_note(self):
        historical = imp.build_snapshot(raw(row(date="2026-05-15")), state())["tasks"][0]
        self.assertEqual(historical["planning_disposition"], "reference")
        result = imp.build_snapshot(raw(row(date="2026-05-15", source_display_status="missing")), state(historical))
        item = result["tasks"][0]
        self.assertEqual(item["planning_disposition"], "active")
        self.assertNotIn("planning_note", item)
        self.assertNotIn("import_original_planning_note", item)

    def test_manual_reference_and_classroom_decisions_are_not_reactivated(self):
        for disposition in ("reference", "in_class", "superseded"):
            with self.subTest(disposition=disposition):
                previous = source(planning_disposition=disposition, planning_note="User confirmed handling")
                result = imp.build_snapshot(raw(row(source_display_status="missing")), state(previous))
                self.assertEqual(result["tasks"][0]["planning_disposition"], disposition)
                self.assertIn("missing_disposition_review", [entry["type"] for entry in result["import_review"]])

    def test_later_manual_note_preserves_reference_despite_old_import_reason(self):
        graded = imp.build_snapshot(raw(row(source_display_status="graded")), state(source()))["tasks"][0]
        graded["planning_note"] = "User confirmed this record is reference only"
        result = imp.build_snapshot(raw(row(source_display_status="missing")), state(graded))
        self.assertEqual(result["tasks"][0]["planning_disposition"], "reference")
        self.assertEqual(result["tasks"][0]["planning_note"], graded["planning_note"])
        self.assertIn("missing_disposition_review", [entry["type"] for entry in result["import_review"]])

    def test_excused_information_and_participation_are_reference_with_basis(self):
        for incoming in (row(source_display_status="excused"), row(source_display_status="information"), row(category="Participation")):
            with self.subTest(incoming=incoming):
                item = imp.build_snapshot(raw(incoming), state())["tasks"][0]
                self.assertEqual(item["planning_disposition"], "reference")
                self.assertTrue(item["import_disposition_reason"])

    def test_existing_classroom_classification_and_derived_superseded_links_survive(self):
        classroom = source(planning_disposition="in_class", planning_note="Already confirmed classroom peer review")
        derived = source(id="derived-1", source_assignment_id=None, derived=True, derived_from=[classroom["id"]])
        superseded = source(id="summary-1", source_assignment_id=None, planning_disposition="superseded", related_task_ids=[classroom["id"]])
        result = imp.build_snapshot(raw(row()), state(classroom, derived, superseded))
        by_id = {item["id"]: item for item in result["tasks"]}
        self.assertEqual(by_id[classroom["id"]]["planning_disposition"], "in_class")
        self.assertEqual(by_id[classroom["id"]]["planning_note"], classroom["planning_note"])
        self.assertEqual(by_id["derived-1"]["derived_from"], [classroom["id"]])
        self.assertTrue(by_id["derived-1"]["import_retained"])
        self.assertEqual(by_id["summary-1"]["related_task_ids"], [classroom["id"]])

    def test_notices_are_review_only_and_repeat_deduplicates_via_metadata(self):
        notice = {"id": "notice-example", "author": "Teacher Example", "date_text": "Yesterday",
                  "title": "Fictional update", "text": "We will discuss a date next class."}
        incoming = raw(notices=[notice])
        initial = imp.build_snapshot(incoming, state())
        self.assertEqual(initial["tasks"], [])
        self.assertEqual(initial["import_review"][0]["type"], "notice_added")
        old = {**state(), "import_metadata": initial["import_metadata"]}
        self.assertEqual(imp.build_snapshot(incoming, old)["import_review"], [])
        incoming["notices"][0] = {**notice, "text": "The discussion moved; check the next announcement."}
        changed = imp.build_snapshot(incoming, old)
        self.assertEqual(changed["import_review"][0]["type"], "notice_changed")
        self.assertEqual(changed["tasks"], [])

    def test_notice_relative_display_date_drift_does_not_reopen_review(self):
        notice = {"id": "notice-example", "author": "Teacher Example", "date_text": "Today",
                  "title": "Fictional update", "text": "Read the shared instructions."}
        initial = imp.build_snapshot(raw(notices=[notice]), state())
        previous = {**state(), "import_metadata": initial["import_metadata"]}
        for date_text in ("Yesterday", "2 days ago", "Sep 25"):
            result = imp.build_snapshot(raw(notices=[{**notice, "date_text": date_text}]), previous)
            self.assertEqual(result["import_review"], [])
            self.assertEqual(result["import_metadata"]["notices"][notice["id"]]["date_text"], date_text)
            previous = {**state(), "import_metadata": result["import_metadata"]}
        changed = imp.build_snapshot(raw(notices=[{**notice, "date_text": "Sep 25", "text": "Instructions changed."}]), previous)
        self.assertEqual([entry["type"] for entry in changed["import_review"]], ["notice_changed"])

    def test_deterministic_ids_inputs_unchanged_and_output_validates(self):
        incoming, old = raw(row(date="2026-09-28")), state()
        originals = deepcopy((incoming, old))
        first, second = imp.build_snapshot(incoming, old), imp.build_snapshot(incoming, old)
        self.assertEqual(first, second)
        self.assertEqual((incoming, old), originals)
        planner.validate_snapshot(first)

    def test_conflicting_raw_duplicates_and_invalid_calendar_dates_raise(self):
        with self.assertRaises(imp.JupiterImportError):
            imp.build_snapshot(raw(row(), row(title="Different")), state())
        with self.assertRaises(imp.JupiterImportError):
            imp.build_snapshot(raw(row(confirmed_due_date="not-a-date")), state())

    def test_known_different_assignment_id_does_not_merge_only_because_titles_match(self):
        result = imp.build_snapshot(raw(row(id="999", date="2026-09-28")), state(source()))
        self.assertNotEqual(result["tasks"][0]["id"], "stable-original-id")
        self.assertEqual(result["tasks"][0]["source_assignment_id"], "999")

    def test_confirmed_calendar_weekday_conflict_requires_review(self):
        result = imp.build_snapshot(raw(row(date="Tue 9/28", confirmed_due_date="2026-09-28")), state())
        self.assertEqual(result["tasks"][0]["due_precision"], "unknown")
        self.assertEqual(result["import_review"][0]["type"], "deadline_conflict")


if __name__ == "__main__":
    unittest.main()
