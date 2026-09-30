"""Deterministic parsing of an already-read Jupiter course HTML fragment.

This module does not access a browser/network or execute HTML/JavaScript. Its
result is display evidence, not a normalized deadline or a claim of completion.
``date`` retains the displayed text; None means unknown (including Future).
Only a controlled status label leaves the score cell: no score is returned.
"""
from html.parser import HTMLParser
import re


class DOMParseError(ValueError):
    """A page could not be safely interpreted; do not treat it as an empty course."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_IGNORE = {"script", "style", "template", "noscript", "svg"}
_HANDLER = re.compile(r"\s*goassign\(\s*([0-9]+)\s*\)\s*;?\s*\Z")
_HIDDEN_STYLE = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))\s*(?:!important\s*)?(?:;|$)", re.I)
_NUMBER = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
_GRADE = re.compile(rf"(?:{_NUMBER}\s*%?|{_NUMBER}\s*/\s*{_NUMBER}|[A-F][+-]?)\Z", re.I)
_WEEKDAY = r"(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)"
_MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_DATE = re.compile(
    rf"(?:{_WEEKDAY}[,.]?\s+)?(?:\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?|"
    rf"{_MONTH}\.?\s+\d{{1,2}}(?:,?\s+\d{{4}})?|\d{{1,2}}\s+{_MONTH}\.?(?:\s+\d{{4}})?|today|tomorrow|yesterday)"
    rf"(?:\s+{_WEEKDAY})?(?:\s+(?:at\s+)?\d{{1,2}}:\d{{2}}(?:\s*[ap]\.?m\.?)?)?\Z", re.I)


class _Node:
    def __init__(self, tag, attrs=(), parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []

    @property
    def hidden(self):
        return (self.tag in _IGNORE or "hidden" in self.attrs
                or (self.attrs.get("aria-hidden") or "").lower() == "true"
                or bool(_HIDDEN_STYLE.search(self.attrs.get("style") or "")))


class _Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack, self.nodes = [self.root], []

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        self.nodes.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _visible(node):
    while node is not None:
        if node.hidden:
            return False
        node = node.parent
    return True


def _text(node, omit_nested_rows=False):
    def walk(current):
        if current.hidden:
            return []
        parts = []
        for child in current.children:
            if isinstance(child, str):
                parts.append(child)
            elif not (omit_nested_rows and child.tag == "tr"):
                if child.tag in {"br", "p", "div"}:
                    parts.append(" ")
                parts.extend(walk(child))
                if child.tag in {"p", "div"}:
                    parts.append(" ")
        return parts
    return " ".join("".join(walk(node)).split())


def _cells(row):
    # Nested layout/blank rows must not shift fixed data-cell positions.
    return [child for child in row.children if isinstance(child, _Node) and child.tag in {"td", "th"}]


def _owner(row, owners):
    while row is not None:
        if row in owners:
            return owners[row]
        row = row.parent
    return None


def _status(display):
    value = display.casefold().strip()
    if value in {"", "-", "--", "–", "—", "−", "ungraded", "not graded", "not scored", "pending", "未评分"}:
        return "ungraded"
    if re.fullmatch(rf"/\s*{_NUMBER}", value):
        return "ungraded"
    if re.fullmatch(rf"excused\s*/\s*{_NUMBER}", value):
        return "excused"
    explicit = {"missing": "missing", "missing work": "missing", "缺交": "missing", "未交": "missing",
                "submitted": "submitted", "turned in": "submitted", "已提交": "submitted",
                "completed": "completed", "已完成": "completed",
                "excused": "excused", "exempt": "excused", "late": "late",
                "information": "information", "view": "view"}
    if value in explicit:
        return explicit[value]
    return "graded" if _GRADE.fullmatch(display) else "unknown"


def parse_course_html(html, course):
    """Return assignment display records or raise DOMParseError on unsafe structure.

    Return keys: id (numeric Jupiter ID as a string), course, date (raw display
    text or None), title, source_display_status, category. Callers own stable
    course namespacing and deadline interpretation; no year/time is guessed.
    Only identical duplicate records merge. Conflicting duplicates fail closed.
    A header-only/loading/empty page raises an error and needs separate explicit
    empty-state verification; it never silently proves a course has no work.
    """
    if not isinstance(html, str) or not isinstance(course, str) or not course.strip():
        raise DOMParseError("invalid_input", "HTML and a non-empty course name are required")
    document = _Document()
    document.feed(html)
    document.close()
    rows = [node for node in document.nodes if node.tag == "tr" and _visible(node)]
    required = {"due", "assignment", "score"}
    header_found = any(required <= {
        _text(cell).casefold().strip(" ▲▼↑↓↕⌄⌃") for cell in _cells(row)
    } for row in rows)
    if not header_found:
        raise DOMParseError("missing_headers", "Expected visible Due, Assignment and Score table headers")
    owners = {}
    for node in document.nodes:
        if not _visible(node):
            continue
        for attr in ("click", "onclick"):
            handler = node.attrs.get(attr) or ""
            if "goassign" not in handler:
                continue
            match = _HANDLER.fullmatch(handler)
            if not match:
                raise DOMParseError("unsupported_handler", "Assignment handler format changed; no code was executed")
            assignment_id = match.group(1)
            if node in owners and owners[node] != assignment_id:
                raise DOMParseError("conflicting_handler", "Assignment element contains conflicting identifiers")
            owners[node] = assignment_id
    records, seen_owner_ids = {}, set()
    for row in rows:
        assignment_id = _owner(row, owners)
        if assignment_id is None:
            continue
        cells = _cells(row)
        if not any(_text(cell, omit_nested_rows=True) for cell in cells):
            continue  # Blank subrows and layout rows contribute no assignment.
        if len(cells) < 12:
            raise DOMParseError("short_row", f"Assignment {assignment_id} has fewer than 12 direct cells")
        title, raw_date = _text(cells[2]), _text(cells[1])
        if not title:
            raise DOMParseError("missing_title", f"Assignment {assignment_id} has no visible title")
        if raw_date.casefold() in {"", "future", "no due date", "-", "–", "—"}:
            raw_date = None
        elif not _DATE.fullmatch(raw_date):
            raise DOMParseError("unexpected_date", f"Assignment {assignment_id} has an unsupported date layout")
        display_status = _text(cells[3])
        record = {"id": assignment_id, "course": course, "date": raw_date, "title": title,
                  "source_display_status": _status(display_status), "category": _text(cells[11])}
        # Jupiter's student To Do list uses a personal "Done" marker.  Keep it
        # separate from submission/grade evidence so it can safely sync with
        # TickTick without claiming that the teacher received the work.
        if display_status.casefold().strip() == "done":
            record["personal_done"] = True
        if assignment_id in records and records[assignment_id] != record:
            raise DOMParseError("conflicting_duplicate", f"Assignment {assignment_id} has conflicting visible records")
        records[assignment_id] = record
        seen_owner_ids.add(assignment_id)
    if not records:
        raise DOMParseError("no_records", "No verified assignment rows; explicitly verify empty/loading/login state")
    if set(owners.values()) - seen_owner_ids:
        raise DOMParseError("incomplete_records", "Some assignment elements have no verified data row")
    return list(records.values())
