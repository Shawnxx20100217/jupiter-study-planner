#!/usr/bin/env python3
"""Read Jupiter's normal web UI with Scrapling; no LLM, screenshots or private API.

Run `login --instance ...` once to sign into this program's separate browser.
The browser keeps its normal session in a private, dedicated profile directory.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from jupiter_dom import parse_course_html
from jupiter_browser_session import ensure_profile_idle, native_profile_launch_options

LOGIN_URL = "https://login.jupitered.com/login/"


class CollectorError(RuntimeError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details


def normalize(text):
    return " ".join(str(text or "").split())


def config_at(instance_path):
    location = Path(instance_path).expanduser().resolve()
    config = json.loads(location.read_text(encoding="utf-8"))
    for key in ("state_dir", "profile_dir"):
        value = config.get(key)
        if value:
            path = Path(value).expanduser()
            config[key] = str((location.parent / path).resolve()) if not path.is_absolute() else str(path)
    config.setdefault("state_dir", str(location.parent))
    config.setdefault("profile_dir", str(Path(config["state_dir"]) / "browser-profile"))
    if not config.get("expected_student") or not config.get("expected_school_year"):
        raise CollectorError("identity_not_configured", "Configure the expected student and school/year before collection.")
    if not config.get("expected_courses"):
        raise CollectorError("courses_not_configured", "Configure the expected course list before collection.")
    if "real_chrome" in config and not isinstance(config["real_chrome"], bool):
        raise CollectorError("invalid_browser_config", "real_chrome must be a JSON boolean.")
    return config


@contextmanager
def browser_lock(config):
    directory = Path(config["state_dir"])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile = Path(config["profile_dir"])
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(profile, 0o700)
    descriptor = os.open(directory / ".browser.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise CollectorError("browser_busy", "The collector browser is already in use.") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def check_origin(page):
    parsed = urlsplit(page.url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"login.jupitered.com", "login.jupitered.com:443"}:
        raise CollectorError("unexpected_origin", "The browser left the Jupiter login site; collection stopped.")


def identity(page, config):
    check_origin(page)
    actual = page.evaluate("""() => ({
      student: document.querySelector('#leftmast .toptabnull')?.innerText || '',
      school: document.querySelector('#schoolyeartab')?.innerText || '',
      courses: Array.from(document.querySelectorAll('#sidebar .classnav'), e => e.textContent)
    })""")
    if normalize(actual["student"]) != normalize(config["expected_student"]):
        raise CollectorError("student_mismatch", "The signed-in student does not match this local instance.")
    if normalize(actual["school"]) != normalize(config["expected_school_year"]):
        raise CollectorError("school_year_changed", "The school or year changed; review the instance before importing.")
    actual_courses = [normalize(c) for c in actual["courses"]]
    expected = [normalize(c) for c in config["expected_courses"]]
    if len(set(actual_courses)) != len(actual_courses) or sorted(actual_courses) != sorted(expected):
        raise CollectorError("courses_changed", "The course list changed; review it before importing.",
                             {"expected_courses": expected, "observed_courses": actual_courses})
    return actual_courses


def nav(page, selector, name=None):
    # Jupiter replaces navrow with navlit on the current page. A navigation
    # request for that page is already satisfied, not a missing menu item.
    target = page.locator(selector.replace('.navrow', ':is(.navrow,.navlit)'))
    if name is not None:
        target = target.filter(has_text=re.compile(r'^\s*' + re.escape(name) + r'\s*$'))
    target.wait_for(state='attached', timeout=10000)
    if target.evaluate("element => element.classList.contains('navlit')"):
        return
    box = target.bounding_box(timeout=5000)
    if not box or box["x"] < 0:
        page.locator('#touchnavbtn').click()
    # launchd can start Chrome with a smaller or not-yet-laid-out window.  Give
    # Playwright a chance to scroll the row into view, then use the row's own
    # DOM click handler as a deterministic fallback instead of treating a
    # responsive sidebar layout as a failed login or empty course list.
    try:
        target.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    try:
        target.click(timeout=15000)
    except Exception:
        try:
            target.click(force=True, timeout=5000)
        except Exception:
            target.evaluate("element => element.click()")


def calendar_records(cells, course_names):
    """Map rendered calendar text to dates, without storing scores/announcements."""
    known = set(course_names)
    records = []
    for cell in cells:
        date = cell.get("date", "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            continue
        course = None
        for child in cell.get("children", []):
            text = normalize(child.get("text"))
            classes = set(child.get("classes", []))
            if "bold" in classes:
                course = text if text in known else None
            elif course and "hw" in classes and text:
                records.append({"course": course, "title": text, "date": date})
    return records


def confirm_dates(courses, records):
    for course in courses:
        for row in course["rows"]:
            raw_date = normalize(row.get("date"))
            match = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)", raw_date)
            if not match:
                continue
            month, day = map(int, match.groups())
            explicit_year = re.search(r"(?<!\d)\d{1,2}/\d{1,2}/(\d{4}|\d{2})(?!\d)", raw_date)
            title = normalize(row["title"])
            dates = set()
            for record in records:
                if record["course"] != course["name"]:
                    continue
                shown = record["title"]
                # Only accept the exact title or a recognized display annotation.
                suffix = shown[len(title):] if shown.startswith(title) else None
                if shown != title and not (suffix and re.fullmatch(
                    r" (?:\([\d.,%+−\-/ ]+\)|information|excused|missing)", suffix, re.I
                )):
                    continue
                date = record["date"]
                if explicit_year and not date[:4].endswith(explicit_year.group(1)):
                    continue
                if int(date[5:7]) == month and int(date[8:10]) == day:
                    dates.add(date)
            if len(dates) == 1:
                row["confirmed_due_date"] = dates.pop()
            elif len(dates) > 1:
                row["calendar_date_ambiguous"] = True


def collect_calendar(page, courses):
    nav(page, '#sidebar .navrow[val="calendar"]')
    page.locator('#calview_closed').wait_for(state='visible', timeout=30000)
    page.locator('#calview_closed').click()
    menu = page.locator('#menulist_calview')
    menu.wait_for(state='visible', timeout=10000)
    values = menu.locator('[val]').evaluate_all("es => es.map(e => e.getAttribute('val'))")
    available = sorted(set(v for v in values if re.fullmatch(r"\d{4}-\d{2}", v or '')))
    if not available:
        raise CollectorError("calendar_changed", "The calendar month controls were not recognized.")
    needed_months = set()
    for course in courses:
        for row in course["rows"]:
            if row["source_display_status"] == "graded":
                continue
            match = re.search(r"(?<!\d)(\d{1,2})/\d{1,2}(?!\d)", row.get("date") or '')
            if match:
                needed_months.add(int(match.group(1)))
    selected = [value for value in available if int(value[-2:]) in needed_months]
    records = []
    for index, month in enumerate(selected):
        if index:
            page.locator('#calview_closed').click()
        menu.locator('[val="' + month + '"]').click()
        page.wait_for_function("month => !!document.querySelector('[id=\"d' + month + '-15\"]')", arg=month, timeout=30000)
        cells = page.locator('#mainpage td[id^="c20"]').evaluate_all("""es => es.map(e => ({
            date:e.id.slice(1),children:Array.from(e.children, c=>({text:c.innerText,classes:Array.from(c.classList)}))
        }))""")
        records.extend(calendar_records(cells, [c["name"] for c in courses]))
    confirm_dates(courses, records)
    return selected


def collect_notices(page, teachers):
    nav(page, '#sidebar .navrow[val="inbox"]')
    page.locator('#showdays_closed').wait_for(state='visible', timeout=30000)
    scope = normalize(page.locator('#showdays_closed').inner_text())
    if scope != "Last 14 days":
        raise CollectorError("message_scope_changed", "Messages must display Last 14 days before automatic collection.")
    notices = page.evaluate("""teachers => Array.from(document.querySelectorAll('#mainpage [id^="text"]'))
        .filter(e => /^text\\d+$/.test(e.id))
        .map(e => {
          const author = Array.from(e.children).find(n => n.tagName === 'B')?.innerText?.trim();
          if (!teachers.includes(author)) return null;
          const parts = Array.from(e.childNodes);
          const start = parts.findIndex(n => n.nodeType === 1 && n.tagName === 'BR');
          const rest = parts.slice(start + 1);
          const lineEnd = rest.findIndex(n => n.nodeType === 1 && n.tagName === 'BR');
          const unread = e.classList.contains('unread') || !!e.closest('.unread') ||
            e.getAttribute('data-unread') === 'true' || e.getAttribute('aria-label')?.toLowerCase().includes('unread');
          return {id:e.id.slice(4), author, date_text:e.querySelector('.date')?.innerText || '',
            ...(unread ? {unread:true} : {}),
            title:(lineEnd < 0 ? '' : rest.slice(0,lineEnd).map(n=>n.textContent).join('')).trim(),
            text:rest.map(n=>n.nodeType===1 && n.tagName==='BR' ? '\\n' : n.textContent).join('').trim()};
        }).filter(Boolean)""", teachers)
    return notices


# Jupiter renders the student Done marker on the assignment detail page.  The
# checkbox itself is custom markup, so only the hidden ``flag`` value together
# with Jupiter's exact flag controls are accepted as evidence.  No icon or CSS
# class is interpreted as a state.
PERSONAL_DONE_DETAIL_SCRIPT = r"""({write = false}) => {
  const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
  const input = document.querySelector('input[type="hidden"][name="flag"]');
  const label = document.querySelector('#flag_script');
  const click = label && (label.getAttribute('click') || label.getAttribute('onclick'));
  const dochange = label && label.getAttribute('dochange');
  const output = {status: 'unknown', changed: 0};
  if (!input || !label || normalize(label.innerText) !== 'Done' ||
      click !== "clickcheck('flag')" || dochange !== "doit('studflagdone')") return output;
  if (input.value !== '' && input.value !== '1') return output;
  const done = input.value === '1';
  output.status = 'known';
  output.personal_done = done;
  if (write && !done) {
    if (typeof window.clickcheck !== 'function') return {...output, status: 'unconfirmed'};
    window.clickcheck('flag');
    if (input.value !== '1') return {...output, status: 'unconfirmed'};
    output.personal_done = true;
    output.changed = 1;
  }
  return output;
}"""
# Reader and writer deliberately use the same state/ownership rules.  Image
# names and CSS classes are not evidence of a checked state.  Unknown controls
# remain unmatched until their actual Jupiter semantics have been verified.
PERSONAL_DONE_LIST_SCRIPT = r"""({targets, write = false}) => {
  const visible = element => {
    if (!element) return false;
    const style = getComputedStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
  };
  const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
  const assignmentIds = element => {
    const handlers = [element.getAttribute('click'), element.getAttribute('onclick')];
    return handlers.flatMap(handler => Array.from(String(handler || '').matchAll(
      /\bgoassign\(\s*(\d+)\s*\)/g), match => match[1]));
  };
  const handlersIn = scope => [scope, ...scope.querySelectorAll('[click],[onclick]')];
  const controlState = element => {
    const values = [];
    if (element.tagName === 'INPUT' && element.type === 'checkbox') {
      if (element.indeterminate || typeof element.checked !== 'boolean') return null;
      values.push(element.checked);
    }
    for (const attribute of ['aria-checked', 'aria-pressed']) {
      if (!element.hasAttribute(attribute)) continue;
      const raw = element.getAttribute(attribute);
      if (raw !== 'true' && raw !== 'false') return null;
      values.push(raw === 'true');
    }
    return values.length && values.every(value => value === values[0]) ? values[0] : null;
  };
  const isDoneControl = (element, scope) => {
    const labels = [element.innerText, element.getAttribute('aria-label'),
      element.getAttribute('title'), ...Array.from(element.labels || [], label => label.innerText)];
    const exactLabel = labels.some(label => /^(done|complete|mark (as )?(done|complete)|完成|标记完成)$/i.test(normalize(label)));
    // Jupiter may render the native checkbox next to a separate text node.
    // Accept only an exact standalone line in the already isolated assignment.
    const scopeLines = String(scope?.innerText || '').split(/\r?\n/).map(normalize);
    const exactScopeLine = scopeLines.some(line => /^(done|complete|完成|标记完成)$/i.test(line));
    return exactLabel || exactScopeLine;
  };
  const selector = 'input[type="checkbox"],[role="checkbox"],[aria-checked],[aria-pressed]';
  const output = {observations: [], changed: 0, matched: [], unmatched: [], unconfirmed: []};
  const targetCounts = new Map();
  for (const target of targets) targetCounts.set(target.id, (targetCounts.get(target.id) || 0) + 1);
  for (const target of targets) {
    const reject = () => output.unmatched.push(target.id);
    if (!/^\d+$/.test(target.id) || !normalize(target.title) || targetCounts.get(target.id) !== 1) {
      reject(); continue;
    }
    const owners = Array.from(document.querySelectorAll('[click],[onclick]')).filter(element =>
      visible(element) && assignmentIds(element).includes(target.id) &&
      normalize(element.innerText).includes(normalize(target.title)));
    if (owners.length !== 1) { reject(); continue; }
    const owner = owners[0];
    const scope = owner.closest('li,article,tr,.todo,.task,.assignment,.card') || owner;
    // A broad layout row/card must never associate a checkbox with another task.
    const ids = new Set(handlersIn(scope).flatMap(assignmentIds));
    if (ids.size !== 1 || !ids.has(target.id)) { reject(); continue; }
    const controls = [scope, ...scope.querySelectorAll(selector)].filter(element =>
      element.matches(selector) && visible(element));
    // Even differently labelled or nested controls make an unverified card
    // ambiguous. Do not guess which one is Jupiter's personal Done control.
    if (controls.length !== 1 || !isDoneControl(controls[0], scope)) { reject(); continue; }
    const control = controls[0];
    const checked = controlState(control);
    if (checked === null) { reject(); continue; }
    output.observations.push({id: target.id, personal_done: checked});
    if (write && checked === false) {
      if (control.disabled || control.getAttribute('aria-disabled') === 'true' ||
          control.getAttribute('aria-readonly') === 'true') { reject(); continue; }
      control.click();
      // A click is only reported as a change when the DOM confirms completion.
      if (controlState(control) !== true) { output.unconfirmed.push(target.id); continue; }
      output.changed += 1;
    }
    output.matched.push(target.id);
  }
  return output;
}"""


# The detail-page reader is the production path; the list helper remains a
# compatibility boundary for callers that already provide a list-page DOM.
PERSONAL_DONE_SCRIPT = PERSONAL_DONE_LIST_SCRIPT

ASSIGNMENT_NAV_SCRIPT = r"""target => {
  const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
  const ids = element => [element.getAttribute('click'), element.getAttribute('onclick')]
    .flatMap(handler => Array.from(String(handler || '').matchAll(
      /\bgoassign\(\s*(\d+)\s*\)/g), match => match[1]));
  const owners = Array.from(document.querySelectorAll('[click],[onclick]')).filter(element =>
    ids(element).includes(String(target.id)) && normalize(element.innerText).includes(normalize(target.title)));
  if (owners.length !== 1) return false;
  owners[0].click();
  return true;
}"""

def open_personal_done_page(page, config):
    """Verify the configured entry and the resulting account before reading/writing."""
    entry_url = config.get("entry_url")
    if not entry_url:
        return False
    parsed = urlsplit(entry_url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"login.jupitered.com", "login.jupitered.com:443"}:
        raise CollectorError("unexpected_origin", "The configured To Do URL is not on Jupiter.")
    page.goto(entry_url)
    check_origin(page)
    page.wait_for_function("() => !!document.querySelector('#mainpage')", timeout=30000)
    identity(page, config)
    return True


def _detail_marker_ready(page, timeout=8000):
    page.wait_for_function("""() => {
      const input = document.querySelector('input[type=\\"hidden\\"][name=\\"flag\\"]');
      const label = document.querySelector('#flag_script');
      return !!input && !!label;
    }""", timeout=timeout)


def _sync_personal_done_details(page, config, courses, targets, write=False):
    """Visit verified course rows and inspect their assignment detail pages.

    Each detail read has a short timeout and always returns to the verified
    course page. A missing/changed control is recorded as unmatched so one
    assignment cannot stall the full Jupiter refresh.
    """
    result = {"observations": [], "changed": 0, "matched": [], "unmatched": []}
    if not targets or not open_personal_done_page(page, config):
        result["unmatched"] = [target["id"] for target in targets]
        return result
    for course in courses:
        course_targets = [target for target in targets if target.get("course") == course["name"]]
        if not course_targets:
            continue
        try:
            nav(page, '#sidebar .navrow', course["name"])
            page.wait_for_function(
                "name => Array.from(document.querySelectorAll('#mainpage .big')).some(e => e.innerText.trim() === name)",
                arg=course["name"], timeout=15000)
        except Exception:
            result["unmatched"].extend(str(target["id"]) for target in course_targets)
            continue
        for target in course_targets:
            try:
                opened = page.evaluate(ASSIGNMENT_NAV_SCRIPT,
                                       {"id": str(target["id"]), "title": target["title"]})
                if not opened:
                    raise CollectorError("assignment_row_not_found", "The verified course row was not uniquely found.")
                _detail_marker_ready(page, timeout=8000)
                check_origin(page)
                detail = page.evaluate(PERSONAL_DONE_DETAIL_SCRIPT, {"write": write}) or {}
                if detail.get("status") != "known":
                    raise CollectorError("done_state_unknown", "The assignment Done control did not expose a verified state.")
                result["observations"].append({"id": str(target["id"]),
                                                "personal_done": bool(detail["personal_done"])})
                result["matched"].append(str(target["id"]))
                result["changed"] += int(detail.get("changed") or 0)
            except Exception:
                result["unmatched"].append(str(target["id"]))
            finally:
                try:
                    nav(page, '#sidebar .navrow', course["name"])
                    page.wait_for_function(
                        "name => Array.from(document.querySelectorAll('#mainpage .big')).some(e => e.innerText.trim() === name)",
                        arg=course["name"], timeout=10000)
                except Exception:
                    # Stop this course; the next one will attempt a fresh
                    # verified navigation. Never leave a detail page active.
                    break
    return result


def write_personal_done_markers(page, config, courses=None):
    """Complete only verified, explicitly unchecked personal Done markers."""
    if config.get("jupiter_done_writeback_enabled") is not True:
        return {"enabled": False, "changed": 0, "unmatched": []}
    state_path = Path(config["state_dir"]) / "state.json"
    if not state_path.exists():
        return {"enabled": True, "changed": 0, "unmatched": []}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"enabled": True, "changed": 0, "unmatched": []}
    targets = []
    for record in (state.get("tasks") or {}).values():
        source = record.get("source") or {}
        personal = record.get("personal") or {}
        if (personal.get("status") != "completed" or source.get("status") in {"submitted", "completed"}
                or source.get("completion_mode") != "local"):
            continue
        assignment_id = str(source.get("source_assignment_id") or "")
        title = str(source.get("title") or "").strip()
        course = str(source.get("course") or source.get("course_name") or "").strip()
        if (assignment_id.isdigit() and title and course and
                source.get("source_display_status") != "graded"):
            targets.append({"id": assignment_id, "title": title, "course": course})
    if not targets:
        return {"enabled": True, "changed": 0, "unmatched": []}
    if courses is None:
        return {"enabled": True, "changed": 0, "unmatched": [target["id"] for target in targets]}
    result = _sync_personal_done_details(page, config, courses, targets, write=True)
    return {"enabled": True, **result}


def collect_personal_done(page, config, courses):
    """Read verified private Done observations from course assignment details."""
    targets = [{"id": str(row["id"]), "title": str(row.get("title", "")), "course": course["name"]}
               for course in courses for row in course.get("rows", [])
               if str(row.get("id", "")).isdigit() and row.get("source_display_status") != "graded"]
    # Keep the pure page.evaluate seam used by offline callers and tests. Real
    # Scrapling pages expose ``locator`` and use the detail-page workflow.
    if not hasattr(page, "locator"):
        if not open_personal_done_page(page, config):
            return []
        result = page.evaluate(PERSONAL_DONE_SCRIPT, {"targets": targets, "write": False}) or {}
        observations = result.get("observations", [])
    else:
        result = _sync_personal_done_details(page, config, courses, targets, write=False)
        observations = result.get("observations", [])
    by_id = {str(item.get("id")): item["personal_done"] for item in observations
             if type(item.get("personal_done")) is bool}
    for course in courses:
        for row in course.get("rows", []):
            if str(row.get("id")) in by_id:
                row["personal_done"] = by_id[str(row["id"])]
    return observations


def workflow(page, config, login=False):
    check_origin(page)
    if login:
        page.bring_to_front()
        print('Collector login window is ready.', flush=True)
    try:
        page.locator('#sidebar .classnav').first.wait_for(state='attached', timeout=600000 if login else 25000)
    except Exception as error:
        raise CollectorError("login_required", "Sign in using the collector's login command; previous task data is preserved.",
                             {'page_title': page.title(), 'page_path': urlsplit(page.url).path}) from error
    names = identity(page, config)
    if login:
        return {"login_ready": True, "course_count": len(names), "model_calls": 0}
    courses = []
    for name in names:
        nav(page, '#sidebar .navrow', name)
        loaded = False
        for attempt in range(2):
            try:
                page.wait_for_function("name => Array.from(document.querySelectorAll('#mainpage .big')).some(e=>e.innerText.trim()===name) && !!document.querySelector('#termmenu_label')", arg=name, timeout=30000)
                loaded = True
                break
            except Exception:
                if attempt:
                    raise
                # A launchd-started Chrome can acknowledge the sidebar click
                # before Jupiter finishes replacing the main panel. Retry the
                # same verified row once instead of treating that race as a
                # page-layout failure.
                nav(page, '#sidebar .navrow', name)
        if not loaded:
            raise CollectorError("course_load_timeout", "A configured Jupiter course did not finish loading.")
        info = page.evaluate("""name => {
          const h=Array.from(document.querySelectorAll('#mainpage .big')).find(e=>e.innerText.trim()===name);
          const lines=h.parentElement.innerText.split('\\n').map(s=>s.trim()).filter(Boolean);
          return {teacher:lines[1]||'',term:document.querySelector('#termmenu_label').innerText};
        }""", name)
        if not info["teacher"]:
            raise CollectorError("course_header_changed", "A course teacher header could not be verified.")
        expected_term = config.get("expected_term")
        if expected_term and normalize(info["term"]) != normalize(expected_term):
            raise CollectorError("term_changed", "The displayed semester changed; review before importing.")
        html = page.locator('#mainpage').inner_html()
        rows = parse_course_html(html, name)
        del html
        courses.append({"name": name, "teacher": info["teacher"], "term": info["term"], "rows": rows})
    calendar_months = collect_calendar(page, courses)
    # Do not open every assignment detail page during a refresh.  Jupiter's
    # private Done flag is only relevant to local classroom records; submission
    # rows (documents, answers and forum posts) are deliberately left to
    # Jupiter/TickTick's real submission state.  Any safe list-row marker was
    # already captured by parse_course_html without another browser navigation.
    done_observations = [{"id": str(row["id"]), "personal_done": bool(row["personal_done"])}
                         for course in courses for row in course.get("rows", [])
                         if row.get("personal_done") is True]
    notices = collect_notices(page, sorted(set(c["teacher"] for c in courses)))
    identity(page, config)
    done_writeback = {"enabled": False, "changed": 0, "unmatched": [],
                      "skipped": True,
                      "reason": "submission tasks use Jupiter/TickTick status; local classroom checks stay local"}
    timezone = config.get("school_timezone", "Asia/Shanghai")
    return {"observed_at": datetime.now(ZoneInfo(timezone)).isoformat(), "timezone": timezone,
            "school_year": re.search(r"\d{4}-\d{2}", config["expected_school_year"]).group(),
            "courses": courses, "notices": notices, "model_calls": 0,
            "done_observations": done_observations, "done_writeback": done_writeback,
            "coverage": {"complete": True, "courses": names,
                         "scope": "All configured courses in the verified displayed semester; teacher message previews from Last 14 days.",
                         "calendar_months": calendar_months,
                         "notes": "Message previews are review items, not automatically interpreted deadline changes. Undated and ambiguous dates remain unknown."}}


def collect(instance_path, login=False):
    config = config_at(instance_path)
    if config.get("collection_paused") is True:
        raise CollectorError("collection_paused", "Browser collection is paused; previous data is preserved.")
    try:
        from scrapling.fetchers import DynamicSession
        from scrapling.engines.constants import DEFAULT_ARGS, HARMFUL_ARGS
    except ImportError as error:
        raise CollectorError("dependencies_missing", "Install the pinned collector requirements in a Python 3.10+ environment.") from error
    result = {}
    errors = []
    page_holder = {}
    session_path = Path(config['state_dir']) / 'collector-session.json'
    saved_session = {}

    def setup(page):
        # This file is created only from this program's own manual login. Never
        # inspect, import or copy another browser's profile or cookie database.
        if saved_session:
            page.context.add_cookies([c for c in saved_session.get('cookies', [])
                                      if c.get('domain', '').lstrip('.') in {'jupitered.com', 'login.jupitered.com'}])

    def action(page):
        page_holder['page'] = page
        try:
            result.update(workflow(page, config, login))
        except Exception as error:
            errors.append(error)
        return page

    def save_session(page):
        fd, name = tempfile.mkstemp(dir=session_path.parent, prefix='.session-')
        os.close(fd)
        try:
            session_state = page.context.storage_state()
            saved_entry = config.get('entry_url', LOGIN_URL)
            session_state['entry_url'] = (saved_entry if urlsplit(saved_entry).path.rstrip('/').endswith('/student.php')
                                          else page.url)
            session_state['user_agent'] = page.evaluate('() => navigator.userAgent')
            Path(name).write_text(json.dumps(session_state), encoding='utf-8')
            os.chmod(name, 0o600)
            os.replace(name, session_path)
        finally:
            Path(name).unlink(missing_ok=True)

    # The library's normal request logs contain account URLs; suppress them.
    logging.disable(logging.CRITICAL)
    with browser_lock(config):
        ensure_profile_idle(config['profile_dir'])
        saved_session = json.loads(session_path.read_text(encoding='utf-8')) if session_path.exists() else {}
        entry_url = LOGIN_URL if login else saved_session.get('entry_url', config.get('entry_url', LOGIN_URL))
        parsed_entry = urlsplit(entry_url)
        if parsed_entry.scheme != 'https' or parsed_entry.hostname != 'login.jupitered.com':
            raise CollectorError('unexpected_origin', 'The saved entry URL is not on Jupiter.')
        try:
            # System Chrome can be unavailable in a restricted macOS runner
            # (Crashpad aborts before the page is created).  Use Scrapling's
            # bundled Chromium for unattended refreshes; an explicit config
            # opt-in keeps the old visible Chrome path available for manual
            # login when needed.
            with DynamicSession(headless=not login,
                                real_chrome=config.get("real_chrome", False),
                                user_data_dir=config["profile_dir"], google_search=False,
                                useragent=saved_session.get('user_agent'),
                                locale="en-US", timezone_id=config.get("school_timezone", "Asia/Shanghai"),
                                timeout=45000, retries=1,
                                additional_args={**native_profile_launch_options(DEFAULT_ARGS, HARMFUL_ARGS),
                                                 "viewport": {"width": 1280, "height": 900}}) as session:
                session.fetch(entry_url, page_setup=setup, page_action=action)
                if login and result and not errors:
                    print('登录校验成功。请在这个窗口完成 Settings 中的 Stay logged in 设置，回到课程主页后在终端按回车保存会话。', flush=True)
                    input()
                    identity(page_holder['page'], config)
                    save_session(page_holder['page'])
                elif result and not errors:
                    save_session(page_holder['page'])
        except Exception as error:
            if not errors:
                raise CollectorError("browser_unavailable", "The collector browser could not load Jupiter; previous data is preserved.") from error
    if errors:
        error = errors[0]
        if isinstance(error, CollectorError) or hasattr(error, "code"):
            raise error
        import traceback
        frames = traceback.extract_tb(error.__traceback__)
        relevant = [f for f in frames if Path(f.filename).name == 'jupiter_collector.py']
        raise CollectorError("page_changed", "A Jupiter page could not be verified; previous task data is preserved.",
                             {'stage': relevant[-1].name if relevant else 'page',
                              'line': relevant[-1].lineno if relevant else None,
                              'type': type(error).__name__}) from error
    if not result:
        raise CollectorError("empty_collection", "No verified collector result was returned.")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['login', 'login-visible', 'collect'])
    parser.add_argument('--instance', required=True)
    parser.add_argument('--output')
    args = parser.parse_args()
    if args.command == 'login-visible':
        try:
            from jupiter_login import launch_visible_login
            result = launch_visible_login(args.instance)
        except Exception as error:
            print(json.dumps({'ok': False, 'code': getattr(error, 'code', 'collector_failed'),
                              'details': getattr(error, 'details', None)}, ensure_ascii=False))
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.command == 'login':
        print('Opening the separate collector browser. Sign in to Jupiter there; no password is read by this program.', flush=True)
    try:
        result = collect(args.instance, login=args.command == 'login')
        if args.output:
            path = Path(args.output).resolve()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.collector-')
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, path)
        print(json.dumps({'ok': True, 'login_ready': result.get('login_ready', False), 'course_count': result.get('course_count', len(result.get('courses', []))), 'model_calls': 0}))
    except Exception as error:
        print(json.dumps({'ok': False, 'code': getattr(error, 'code', 'collector_failed'),
                          'details': getattr(error, 'details', None)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
