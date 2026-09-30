"""Synthetic fixtures only; no account data, browser, network or AI is used."""
import importlib.util
import json
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "jupiter_dom.py"
SPEC = importlib.util.spec_from_file_location("jupiter_dom", SCRIPT)
dom = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dom)


HEADER = "<tr><th>Due</th><th>Assignment</th><th>Score</th><th>Worth</th><th>Impact on grade</th><th>Category</th></tr>"


def row(assignment_id="101", date="Mon 9/28", title="Synthetic homework", score="—", category="Homework", extra=""):
    cells = ["", date, title, score, "", "", "", "", "", "", "", category]
    return f'<table click="goassign({assignment_id})"><tr>' + "".join(f"<td>{cell}</td>" for cell in cells) + f"</tr>{extra}</table>"


def page(*records, header=HEADER):
    return "<main><table>" + header + "</table>" + "".join(records) + "</main>"


class JupiterDOMTests(unittest.TestCase):
    def parse(self, *records, **kwargs):
        return dom.parse_course_html(page(*records, **kwargs), "Synthetic course")

    def error(self, code, html):
        with self.assertRaises(dom.DOMParseError) as raised:
            dom.parse_course_html(html, "Synthetic course")
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_fixed_data_columns_with_six_visible_headers(self):
        self.assertEqual(self.parse(row()), [{"id": "101", "course": "Synthetic course", "date": "Mon 9/28",
                                              "title": "Synthetic homework", "source_display_status": "ungraded", "category": "Homework"}])

    def test_blank_nested_rows_are_ignored_and_identical_records_deduplicate(self):
        nested = "<tr><td><table><tr><td> </td></tr></table></td></tr><tr></tr>"
        self.assertEqual(len(self.parse(row(extra=nested), row())), 1)

    def test_nested_layout_row_does_not_shift_data_cells(self):
        inner = row().replace('<table click="goassign(101)">', "<table>")
        wrapper = '<table click="goassign(101)"><tr><td>' + inner + "</td></tr></table>"
        self.assertEqual(self.parse(wrapper)[0]["title"], "Synthetic homework")

    def test_nearest_assignment_ancestor_is_used(self):
        nested = '<div click="goassign(101)">' + row() + row("202", title="Synthetic essay") + "</div>"
        self.assertEqual([item["id"] for item in self.parse(nested)], ["101", "202"])

    def test_duplicate_id_with_different_title_or_date_fails(self):
        for changed in (row(title="Conflicting synthetic title"), row(date="Tue 9/29")):
            self.error("conflicting_duplicate", page(row(), changed))

    def test_empty_and_future_dates_stay_unknown(self):
        for date in ("", " ", "&nbsp;", "Future", "No due date", "—"):
            with self.subTest(date=date):
                self.assertIsNone(self.parse(row(date=date))[0]["date"])

    def test_date_is_display_text_without_invented_year_or_time(self):
        for date in ("9/28", "Sep 28", "Monday 9/28", "2026-09-28", "9/28/2026", "Tomorrow", "Sep 28 at 10:00 AM"):
            with self.subTest(date=date):
                self.assertEqual(self.parse(row(date=date))[0]["date"], date)

    def test_blank_grades_are_ungraded_never_missing(self):
        for score in ("", "&nbsp;", "—", "&mdash;", "-", "Not graded"):
            with self.subTest(score=score):
                self.assertEqual(self.parse(row(score=score))[0]["source_display_status"], "ungraded")

    def test_numeric_grades_only_emit_graded_no_scores(self):
        for score in ("0", "0.0", "0%", "93.75", "93.75%", "17 / 20", "A-"):
            with self.subTest(score=score):
                result = self.parse(row("777", score=score))
                self.assertEqual(result[0]["source_display_status"], "graded")
                self.assertEqual(set(result[0]), {"id", "course", "date", "title", "source_display_status", "category"})
                self.assertNotIn(score, json.dumps(result))

    def test_all_graded_past_course_remains_nonempty(self):
        result = self.parse(row(date="Mon 1/12", score="88.5"), row("202", date="Tue 1/13", score="9/10"))
        self.assertEqual(len(result), 2)
        self.assertTrue(all(item["source_display_status"] == "graded" for item in result))

    def test_only_explicit_missing_marker_means_missing(self):
        for score, expected in (("Missing", "missing"), ("Late", "late"), ("Not missing", "unknown"),
                                ("M", "unknown"), ("Submitted", "submitted"), ("Done", "unknown")):
            with self.subTest(score=score):
                self.assertEqual(self.parse(row(score=score))[0]["source_display_status"], expected)

    def test_done_is_a_personal_marker_separate_from_submission(self):
        result = self.parse(row(score="Done"))[0]
        self.assertEqual(result["source_display_status"], "unknown")
        self.assertTrue(result["personal_done"])

    def test_additional_display_markers_do_not_leak_points_or_imply_submission(self):
        for score, expected in (("/ 73.5", "ungraded"), ("information", "information"),
                                ("excused / 64.5", "excused"), ("view", "view")):
            with self.subTest(score=score):
                result = self.parse(row(score=score))
                self.assertEqual(result[0]["source_display_status"], expected)
                self.assertNotIn("73.5", json.dumps(result))
                self.assertNotIn("64.5", json.dumps(result))

    def test_entities_visible_inline_text_and_line_breaks(self):
        title = "Read <span>A &amp; B</span><br> then&nbsp;write <b>3</b> points"
        self.assertEqual(self.parse(row(title=title))[0]["title"], "Read A & B then write 3 points")

    def test_script_style_template_and_hidden_decoration_are_not_text(self):
        title = ('Essay<script>malicious()</script><style>secret</style><template>hidden</template>'
                 '<span hidden>hidden</span><span aria-hidden="true">hidden</span>'
                 '<span style="display: none !important">hidden</span><span style="visibility:hidden">hidden</span>')
        self.assertEqual(self.parse(row(title=title))[0]["title"], "Essay")

    def test_hidden_columns_keep_positions_but_do_not_expose_text(self):
        fixture = row().replace("<td></td>", '<td hidden>Decoration</td>', 1)
        self.assertEqual(self.parse(fixture)[0]["date"], "Mon 9/28")

    def test_hidden_entire_records_do_not_enter_visible_course(self):
        result = self.parse(row(), '<div hidden>' + row("202") + "</div>")
        self.assertEqual([item["id"] for item in result], ["101"])

    def test_missing_or_hidden_headers_fail_instead_of_empty_result(self):
        self.error("missing_headers", page(row(), header="<tr><td>Wrong</td></tr>"))
        self.error("missing_headers", '<div hidden><table>' + HEADER + "</table></div>" + row())
        self.error("missing_headers", "<html><body>Please sign in</body></html>")

    def test_loading_or_empty_header_only_page_requires_verification(self):
        self.error("no_records", page())
        self.error("no_records", page('<table click="goassign(101)"><tr><td></td></tr></table>'))

    def test_incomplete_record_is_not_silently_dropped(self):
        empty = '<div click="goassign(202)"></div>'
        self.error("incomplete_records", page(row(), empty))

    def test_short_row_bad_date_or_missing_title_fails_without_grade_leak(self):
        short = '<div click="goassign(101)"><table><tr><td>93.75</td></tr></table></div>'
        error = self.error("short_row", page(short))
        self.assertNotIn("93.75", str(error))
        error = self.error("unexpected_date", page(row(date="93.75%")))
        self.assertNotIn("93.75", str(error))
        self.error("missing_title", page(row(title="", score="93.75")))

    def test_strict_handler_is_read_as_data_never_executed(self):
        for handler in ("goassign(101);dangerous()", "goassign(foo)", "goassign(101,2)", "return goassign(101)"):
            fixture = row().replace("goassign(101)", handler)
            self.error("unsupported_handler", page(fixture))
        fixture = row().replace("goassign(101)", "  goassign( 101 ); ")
        self.assertEqual(self.parse(fixture)[0]["id"], "101")

    def test_header_sort_decoration_does_not_shift_columns(self):
        header = HEADER.replace("Due</th>", "Due ▲</th>")
        self.assertEqual(len(self.parse(row(), header=header)), 1)


if __name__ == "__main__":
    unittest.main()
