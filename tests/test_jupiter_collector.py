"""Collector boundary tests: fabricated page data only; no browser or network."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import jupiter_collector as collector


def child(text, *classes):
    return {"text": text, "classes": list(classes)}


def cell(day, *children):
    return {"date": day, "children": list(children)}


def course(name="Course A", title="Practice", date="Mon 9/28"):
    return {"name": name, "rows": [{"id": "123", "title": title, "date": date,
                                     "source_display_status": "ungraded", "category": "Homework"}]}


def record(name="Course A", title="Practice", date="2026-09-28"):
    return {"course": name, "title": title, "date": date}


class CalendarTests(unittest.TestCase):
    def test_calendar_records_preserve_course_groups(self):
        cells = [cell("2026-09-28", child("Course A", "bold"), child("Practice", "hw"),
                      child("Course B", "bold"), child("Essay", "hw"))]
        self.assertEqual(collector.calendar_records(cells, ["Course A", "Course B"]),
                         [record(), record("Course B", "Essay")])

    def test_calendar_course_scope_resets_at_each_day(self):
        cells = [cell("2026-09-28", child("Course A", "bold"), child("Practice", "hw")),
                 cell("2026-09-29", child("Unscoped event", "hw"))]
        self.assertEqual(collector.calendar_records(cells, ["Course A"]), [record()])

    def test_unknown_bold_header_clears_previous_course(self):
        cells = [cell("2026-09-28", child("Course A", "bold"), child("Practice", "hw"),
                      child("School notices", "bold"), child("No school", "hw"),
                      child("Unknown course", "bold"), child("Unrelated assignment", "hw"))]
        self.assertEqual(collector.calendar_records(cells, ["Course A"]), [record()])

    def test_announcements_and_plain_text_do_not_become_assignments(self):
        cells = [cell("2026-09-28", child("School announcement", "event"), child("Practice", "hw"),
                      child("Course A", "bold"), child("Important announcement", "notice"),
                      child("Assembly", "event"), child("General text"), child("Practice", "hw"))]
        self.assertEqual(collector.calendar_records(cells, ["Course A"]), [record()])

    def test_calendar_filters_date_format_and_empty_rows(self):
        cells = [cell("09/28", child("Course A", "bold"), child("Practice", "hw")),
                 cell("2026-9-28", child("Course A", "bold"), child("Practice", "hw")),
                 cell("2026-09-28", child("Course A", "bold"), child(" \n ", "hw")),
                 {"date": "2026-09-28"}]
        self.assertEqual(collector.calendar_records(cells, ["Course A"]), [])

    def test_calendar_normalizes_whitespace_without_mutating_input(self):
        cells = [cell("2026-09-28", child("  Course\nA ", "bold"), child("  Reading\n practice ", "hw"))]
        before = copy.deepcopy(cells)
        self.assertEqual(collector.calendar_records(cells, ["Course A"]), [record(title="Reading practice")])
        self.assertEqual(cells, before)

    def test_confirm_dates_keeps_identical_titles_in_other_courses_separate(self):
        courses = [course("Course A"), course("Course B")]
        collector.confirm_dates(courses, [record("Course A", date="2026-09-28"), record("Course B", date="2027-09-28")])
        self.assertEqual(courses[0]["rows"][0]["confirmed_due_date"], "2026-09-28")
        self.assertEqual(courses[1]["rows"][0]["confirmed_due_date"], "2027-09-28")

    def test_confirm_dates_requires_matching_course(self):
        courses = [course()]
        collector.confirm_dates(courses, [record("Course B")])
        self.assertNotIn("confirmed_due_date", courses[0]["rows"][0])

    def test_exact_title_and_score_annotations_can_confirm(self):
        for title in ("Practice", "Practice (10/10)", "Practice (87%)", "Practice (9.5 / 10)",
                      "Practice (1,000)", "Practice (−2)", "Practice missing", "Practice information", "Practice excused"):
            with self.subTest(title=title):
                courses = [course()]
                collector.confirm_dates(courses, [record(title=title)])
                row = courses[0]["rows"][0]
                self.assertEqual(row["confirmed_due_date"], "2026-09-28")
                self.assertEqual(row["title"], "Practice")
                self.assertNotIn("score", row)

    def test_title_prefixes_extensions_and_unrecognized_annotations_rejected(self):
        for title in ("Practices", "Practice 2", "Practice essay", "Practice (extension)",
                      "Practice (10/10) extra", "Practice (10/10) (revised)", "Practice 10/10",
                      "Practice (A+)", "Practice missing homework", "Practice(10/10)"):
            with self.subTest(title=title):
                courses = [course()]
                collector.confirm_dates(courses, [record(title=title)])
                self.assertNotIn("confirmed_due_date", courses[0]["rows"][0])

    def test_two_years_for_same_month_day_remains_ambiguous(self):
        courses = [course(date="9/28")]
        collector.confirm_dates(courses, [record(date="2026-09-28"), record(date="2027-09-28")])
        row = courses[0]["rows"][0]
        self.assertNotIn("confirmed_due_date", row)
        self.assertTrue(row["calendar_date_ambiguous"])

    def test_duplicate_calendar_records_are_one_date_not_ambiguous(self):
        courses = [course()]
        collector.confirm_dates(courses, [record(), record(), record(title="Practice (10/10)")])
        row = courses[0]["rows"][0]
        self.assertEqual(row["confirmed_due_date"], "2026-09-28")
        self.assertNotIn("calendar_date_ambiguous", row)

    def test_month_and_day_must_both_match(self):
        courses = [course()]
        collector.confirm_dates(courses, [record(date="2026-10-28"), record(date="2026-09-29")])
        self.assertNotIn("confirmed_due_date", courses[0]["rows"][0])

    def test_missing_dates_are_not_filled_from_matching_title(self):
        for date in (None, "", "Future", "Tomorrow", "Unknown"):
            with self.subTest(date=date):
                courses = [course(date=date)]
                collector.confirm_dates(courses, [record()])
                self.assertNotIn("confirmed_due_date", courses[0]["rows"][0])


class ConfigAndLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="jupiter-collector-test-")
        self.root = Path(self.temporary.name).resolve()
        self.instance = self.root / "instance.json"
        self.config = {"expected_student": "Fictional Student", "expected_school_year": "Fictional School 2026-27",
                       "expected_courses": ["Course A", "Course B"], "state_dir": "private", "profile_dir": "private/profile"}

    def tearDown(self):
        self.temporary.cleanup()

    def save(self, value=None):
        self.instance.write_text(json.dumps(self.config if value is None else value), encoding="utf-8")

    def test_missing_or_empty_account_and_school_year_rejected(self):
        for field in ("expected_student", "expected_school_year"):
            for empty in (None, "", False):
                with self.subTest(field=field, empty=empty):
                    config = {**self.config, field: empty}
                    self.save(config)
                    with self.assertRaises(collector.CollectorError) as raised:
                        collector.config_at(self.instance)
                    self.assertEqual(raised.exception.code, "identity_not_configured")
            config = dict(self.config)
            config.pop(field)
            self.save(config)
            with self.assertRaises(collector.CollectorError):
                collector.config_at(self.instance)

    def test_missing_course_list_rejected(self):
        self.save({**self.config, "expected_courses": []})
        with self.assertRaises(collector.CollectorError) as raised:
            collector.config_at(self.instance)
        self.assertEqual(raised.exception.code, "courses_not_configured")

    def test_browser_preference_cannot_turn_string_false_into_system_chrome(self):
        self.save({**self.config, "real_chrome": "false"})
        with self.assertRaises(collector.CollectorError) as raised:
            collector.config_at(self.instance)
        self.assertEqual(raised.exception.code, "invalid_browser_config")

    def test_relative_paths_resolved_against_instance_directory(self):
        self.save()
        value = collector.config_at(self.instance)
        self.assertEqual(Path(value["state_dir"]), self.root / "private")
        self.assertEqual(Path(value["profile_dir"]), self.root / "private" / "profile")

    def test_default_private_profile_is_inside_state_directory(self):
        config = dict(self.config)
        config.pop("profile_dir")
        self.save(config)
        value = collector.config_at(self.instance)
        self.assertEqual(Path(value["profile_dir"]), self.root / "private" / "browser-profile")

    def test_browser_lock_reentry_rejected_and_released_after_exit(self):
        self.save()
        config = collector.config_at(self.instance)
        with collector.browser_lock(config):
            with self.assertRaises(collector.CollectorError) as raised:
                with collector.browser_lock(config):
                    self.fail("Second browser lock should not enter")
            self.assertEqual(raised.exception.code, "browser_busy")
        with collector.browser_lock(config):
            pass
        self.assertEqual(Path(config["profile_dir"]).stat().st_mode & 0o777, 0o700)
        self.assertEqual((Path(config["state_dir"]) / ".browser.lock").stat().st_mode & 0o777, 0o600)

    def test_browser_lock_released_when_body_raises(self):
        self.save()
        config = collector.config_at(self.instance)
        with self.assertRaises(RuntimeError):
            with collector.browser_lock(config):
                raise RuntimeError("Synthetic failure")
        with collector.browser_lock(config):
            pass


class BrowserStartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="jupiter-browser-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.instance = self.root / "instance.json"
        self.config = {"expected_student": "Fictional Student", "expected_school_year": "School 2026-27",
                       "expected_courses": ["Course A"], "state_dir": str(self.root),
                       "profile_dir": str(self.root / "profile")}
        self.saved = {"entry_url": collector.LOGIN_URL, "user_agent": "Dedicated Jupiter Browser",
                      "cookies": [{"name": "session", "value": "fixture", "domain": "login.jupitered.com"}]}
        self.session_file = self.root / "collector-session.json"
        self.session_file.write_text(json.dumps(self.saved))
        self.page = SimpleNamespace(url=collector.LOGIN_URL, context=Mock(), evaluate=Mock(return_value="Bundled Browser"))
        self.page.context.storage_state.return_value = {"cookies": self.saved["cookies"]}
        self.factory = MagicMock()
        self.session = self.factory.return_value.__enter__.return_value
        self.session.fetch.side_effect = lambda url, page_setup, page_action: (page_setup(self.page), page_action(self.page))
        modules = {"scrapling.fetchers": SimpleNamespace(DynamicSession=self.factory),
                   "scrapling.engines.constants": SimpleNamespace(DEFAULT_ARGS=[], HARMFUL_ARGS=[])}
        for stub in (patch.dict(sys.modules, modules), patch.object(collector, "ensure_profile_idle"),
                     patch.object(collector, "workflow", return_value={"course_count": 1}),
                     patch.object(collector.logging, "disable")):
            stub.start()
            self.addCleanup(stub.stop)

    def run_collect(self):
        self.instance.write_text(json.dumps(self.config))
        return collector.collect(self.instance)

    def test_default_uses_bundled_browser_and_restores_dedicated_session(self):
        self.assertEqual(self.run_collect(), {"course_count": 1})
        self.assertFalse(self.factory.call_args.kwargs["real_chrome"])
        self.assertTrue(self.factory.call_args.kwargs["headless"])
        self.assertEqual(self.factory.call_args.kwargs["user_data_dir"], str(self.root / "profile"))
        self.page.context.add_cookies.assert_called_once_with(self.saved["cookies"])
        self.assertEqual(self.session_file.stat().st_mode & 0o777, 0o600)

    def test_system_chrome_requires_explicit_boolean_opt_in(self):
        self.config["real_chrome"] = True
        self.run_collect()
        self.assertTrue(self.factory.call_args.kwargs["real_chrome"])

    def test_browser_start_failure_preserves_saved_session_without_fallback(self):
        self.factory.return_value.__enter__.side_effect = RuntimeError("Browser unavailable")
        with self.assertRaises(collector.CollectorError) as raised:
            self.run_collect()
        self.assertEqual(raised.exception.code, "browser_unavailable")
        self.factory.assert_called_once()
        self.assertEqual(json.loads(self.session_file.read_text()), self.saved)


class IdentityTests(unittest.TestCase):
    def test_origin_requires_https_and_exact_jupiter_hostname(self):
        collector.check_origin(SimpleNamespace(url="https://login.jupitered.com/login/"))
        for url in ("http://login.jupitered.com/", "https://evil.example/", "https://login.jupitered.com.evil.example/",
                    "https://login.jupitered.com@evil.example/", "about:blank"):
            with self.subTest(url=url):
                with self.assertRaises(collector.CollectorError) as raised:
                    collector.check_origin(SimpleNamespace(url=url))
                self.assertEqual(raised.exception.code, "unexpected_origin")

    def test_identity_accepts_only_expected_student_year_and_complete_courses(self):
        config = {"expected_student": "Fictional Student", "expected_school_year": "School 2026-27",
                  "expected_courses": ["Course A", "Course B"]}
        actual = {"student": " Fictional\nStudent ", "school": "School 2026-27", "courses": ["Course B", "Course A"]}
        page = SimpleNamespace(url=collector.LOGIN_URL, evaluate=Mock(return_value=actual))
        self.assertEqual(collector.identity(page, config), ["Course B", "Course A"])
        for field, value, code in (("student", "Other Student", "student_mismatch"),
                                   ("school", "School 2027-28", "school_year_changed"),
                                   ("courses", ["Course A"], "courses_changed"),
                                   ("courses", ["Course A", "Course B", "Course C"], "courses_changed"),
                                   ("courses", ["Course A", "Course A"], "courses_changed")):
            with self.subTest(field=field, value=value):
                page.evaluate.return_value = {**actual, field: value}
                with self.assertRaises(collector.CollectorError) as raised:
                    collector.identity(page, config)
                self.assertEqual(raised.exception.code, code)


class PersonalDoneTests(unittest.TestCase):
    def test_observations_are_merged_into_matching_rows(self):
        courses = [{"name": "Course A", "rows": [
            {"id": "123", "title": "Practice"}, {"id": "456", "title": "Essay"}]}]
        page = SimpleNamespace(url=collector.LOGIN_URL)
        page.evaluate = Mock(return_value={"observations": [
            {"id": "123", "personal_done": True}, {"id": "456", "personal_done": False}]})
        original = collector.open_personal_done_page
        collector.open_personal_done_page = lambda *_: True
        try:
            result = collector.collect_personal_done(page, {"entry_url": collector.LOGIN_URL}, courses)
        finally:
            collector.open_personal_done_page = original
        self.assertEqual(result, [{"id": "123", "personal_done": True}, {"id": "456", "personal_done": False}])
        self.assertTrue(courses[0]["rows"][0]["personal_done"])
        self.assertFalse(courses[0]["rows"][1]["personal_done"])

    def test_dom_helper_is_shared_and_does_not_guess_icon_state(self):
        # The same helper is passed to page.evaluate for both read and write.
        self.assertIn("controlState", collector.PERSONAL_DONE_SCRIPT)
        self.assertIn("write = false", collector.PERSONAL_DONE_SCRIPT)
        self.assertNotIn("getAttribute('src')", collector.PERSONAL_DONE_SCRIPT)
        self.assertNotIn("getAttribute('class')", collector.PERSONAL_DONE_SCRIPT)

    def test_dom_helper_fixture_rejects_mixed_and_multiple_controls(self):
        """Run the pure page.evaluate function against a fake DOM (no browser)."""
        import shutil
        import subprocess
        node = shutil.which("node") or "/Users/bugaoxing/Documents/Codex/2026-09-25/new-chat/work/jupiter-runtime/lib/python3.12/site-packages/playwright/driver/node"
        if not Path(node).exists():
            self.skipTest("bundled JavaScript runtime unavailable")
        script = r'''const source = %s;
const done = eval(source);
class E {
  constructor(tag, attrs={}, text='', opts={}) { this.tagName=tag.toUpperCase(); this.attrs={...attrs}; this.innerText=text; this.type=opts.type||''; this.checked=!!opts.checked; this.indeterminate=!!opts.indeterminate; this.disabled=!!opts.disabled; this.offsetWidth=1; this.offsetHeight=1; this.children=[]; }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs,k) ? this.attrs[k] : null; }
  hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs,k); }
  matches(s) { return (s.includes('input') && this.tagName==='INPUT' && this.type==='checkbox') || (s.includes('[role="checkbox"]') && this.attrs.role==='checkbox') || (s.includes('[aria-checked]') && this.hasAttribute('aria-checked')) || (s.includes('[aria-pressed]') && this.hasAttribute('aria-pressed')); }
  querySelectorAll(s) { if (s.includes('[click]')) return this.children.filter(e => e.hasAttribute('click') || e.hasAttribute('onclick')); return this.children.filter(e => e.matches(s)); }
  closest() { return this.scope || this; }
  getClientRects() { return [1]; }
  click() { this.checked=true; if (this.hasAttribute('aria-checked')) this.attrs['aria-checked']='true'; if (this.hasAttribute('aria-pressed')) this.attrs['aria-pressed']='true'; }
}
function run(controls, write=true, scopeText='Task') {
  const scope = new E('div', {}, scopeText); scope.scope=scope;
  const owner = new E('a', {onclick:'goassign(1)'}, 'Task'); owner.scope=scope; scope.children=[owner, ...controls];
  global.getComputedStyle=()=>({display:'block', visibility:'visible'});
  global.document={querySelectorAll:s => s.includes('[click]') ? [owner] : []};
  return done({targets:[{id:'1', title:'Task'}], write});
}
const checkbox = (checked=false) => new E('input', {}, 'Done', {type:'checkbox', checked});
const result={unchecked:run([checkbox(false)]), checked:run([checkbox(true)]), line:run([new E('input',{},'',{type:'checkbox'})], true, 'Task\nDone'), mixed:run([new E('div',{role:'checkbox','aria-checked':'mixed'},'Done')]), multiple:run([checkbox(false),checkbox(false)])};
process.stdout.write(JSON.stringify(result));''' % json.dumps(collector.PERSONAL_DONE_SCRIPT)
        completed = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
        result = json.loads(completed.stdout)
        self.assertEqual(result["unchecked"]["changed"], 1)
        self.assertEqual(result["unchecked"]["observations"], [{"id": "1", "personal_done": False}])
        self.assertEqual(result["checked"]["changed"], 0)
        self.assertEqual(result["checked"]["observations"], [{"id": "1", "personal_done": True}])
        self.assertEqual(result["line"]["changed"], 1)
        self.assertEqual(result["line"]["observations"], [{"id": "1", "personal_done": False}])
        self.assertEqual(result["mixed"]["observations"], [])
        self.assertEqual(result["mixed"]["changed"], 0)
        self.assertEqual(result["multiple"]["observations"], [])
        self.assertEqual(result["multiple"]["changed"], 0)


if __name__ == "__main__":
    unittest.main()
