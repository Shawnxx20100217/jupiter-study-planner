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
    if parsed.scheme != "https" or parsed.hostname != "login.jupitered.com":
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
    target = page.locator(selector)
    if name is not None:
        target = target.filter(has_text=re.compile(r'^\s*' + re.escape(name) + r'\s*$'))
    box = target.bounding_box()
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
    notices = collect_notices(page, sorted(set(c["teacher"] for c in courses)))
    identity(page, config)
    timezone = config.get("school_timezone", "Asia/Shanghai")
    return {"observed_at": datetime.now(ZoneInfo(timezone)).isoformat(), "timezone": timezone,
            "school_year": re.search(r"\d{4}-\d{2}", config["expected_school_year"]).group(),
            "courses": courses, "notices": notices, "model_calls": 0,
            "coverage": {"complete": True, "courses": names,
                         "scope": "All configured courses in the verified displayed semester; teacher message previews from Last 14 days.",
                         "calendar_months": calendar_months,
                         "notes": "Message previews are review items, not automatically interpreted deadline changes. Undated and ambiguous dates remain unknown."}}


def collect(instance_path, login=False):
    config = config_at(instance_path)
    try:
        from scrapling.fetchers import DynamicSession
    except ImportError as error:
        raise CollectorError("dependencies_missing", "Install the pinned collector requirements in a Python 3.10+ environment.") from error
    result = {}
    errors = []
    page_holder = {}
    session_path = Path(config['state_dir']) / 'collector-session.json'
    saved_session = json.loads(session_path.read_text(encoding='utf-8')) if session_path.exists() else {}
    entry_url = LOGIN_URL if login else saved_session.get('entry_url', config.get('entry_url', LOGIN_URL))
    parsed_entry = urlsplit(entry_url)
    if parsed_entry.scheme != 'https' or parsed_entry.hostname != 'login.jupitered.com':
        raise CollectorError('unexpected_origin', 'The saved entry URL is not on Jupiter.')

    def setup(page):
        # This file is created only from this program's own manual login. Never
        # inspect, import or copy another browser's profile or cookie database.
        if session_path.exists():
            saved = json.loads(session_path.read_text(encoding='utf-8'))
            page.context.add_cookies([c for c in saved.get('cookies', [])
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
        try:
            with DynamicSession(headless=not login, real_chrome=True,
                                user_data_dir=config["profile_dir"], google_search=False,
                                useragent=saved_session.get('user_agent'),
                                locale="en-US", timezone_id=config.get("school_timezone", "Asia/Shanghai"),
                                timeout=45000, retries=1,
                                additional_args={"viewport": {"width": 1280, "height": 900}}) as session:
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
