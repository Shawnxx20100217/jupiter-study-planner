#!/usr/bin/env python3
"""Zero-model local runner and launchd plist generator; Python standard library only.

The collector owns authenticated browser access. This module never opens a browser,
contacts an AI service, submits work, or enables a launch agent. Schedules use the
Mac's local timezone, at 07:00 and 17:00, while the computer is available.
"""
import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import fcntl
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import plistlib
import re
import tempfile
import subprocess
from urllib.parse import urlsplit, urlunsplit

import planner


class AlreadyRunning(Exception):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def digest(value):
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def read(path, fallback):
    if not path.exists():
        return fallback
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def instance_config(instance_path):
    instance_path = Path(instance_path).expanduser().resolve()
    config = read(instance_path, None)
    if not isinstance(config, dict):
        raise ValueError("Instance configuration must be a JSON object")

    def resolve(value):
        path = Path(value).expanduser()
        return (path if path.is_absolute() else instance_path.parent / path).resolve()

    directory = resolve(config.get("state_dir", "."))
    report = resolve(config.get("report_path", str(directory / "study-plan.md")))
    report_json = report.with_suffix(".json")
    if report_json == report:
        report_json = report.with_name(report.stem + ".plan.json")
    protected = {instance_path} | {directory / name for name in (
        "state.json", "config.json", "run-status.json", "notification-state.json",
        "notifications.json", "pending-review.json", "import-metadata.json",
        "raw-latest.json", "snapshot-latest.json", ".lock", ".runtime.lock")}
    if report in protected or report_json in protected:
        raise ValueError("Report path conflicts with a private state file")
    return instance_path, directory, report, report_json


def calendar_paths(report):
    """Sibling user-facing exports for the super-calendar layer."""
    report = Path(report)
    return (report.with_name(report.stem + "-calendar.md"),
            report.with_name(report.stem + ".ics"))


def dashboard_path(report):
    """Sibling offline dashboard; it contains only a presentation-safe copy."""
    return Path(report).with_suffix(".html")


def render_dashboard(instance_path, plan, status, report, notifications=None):
    from dashboard import render_dashboard as render
    if notifications is None:
        _, directory, _, _ = instance_config(instance_path)
        notifications = read(directory / "notifications.json", [])
    return render(plan, status, read(instance_path, {}), Path(report).name, notifications)


def refresh_dashboard(instance_path):
    """Refresh the dashboard after a failure while preserving old plan data."""
    instance_path, directory, report, report_json = instance_config(instance_path)
    return render_dashboard(instance_path, read(report_json, {}),
                            read(directory / "run-status.json", {}), report)


def _system_notification(title, message, config):
    """Show one best-effort macOS notification when explicitly enabled."""
    if not config.get("system_notifications", False) or os.name != "posix":
        return False
    if not Path("/usr/bin/osascript").is_file():
        return False
    # Escape for an AppleScript string without exposing raw page markup.
    safe_title = str(title).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    safe_message = str(message).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    script = f'display notification "{safe_message}" with title "{safe_title}"'
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


@contextmanager
def run_lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(directory / ".runtime.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise AlreadyRunning() from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def commit_files(files):
    """Prepare every replacement first; roll back handled commit failures.

    Each individual file is atomic and private. This is not a filesystem-wide
    transaction across power loss; the last raw/snapshot files aid recovery.
    """
    prepared, old, replaced = {}, {}, []
    try:
        for path, content in files.items():
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            old[path] = path.read_bytes() if path.exists() else None
            descriptor, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
            prepared[path] = Path(name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content.encode("utf-8") if isinstance(content, str) else content)
                handle.flush()
                os.fsync(handle.fileno())
        for path, staging in prepared.items():
            os.replace(staging, path)
            replaced.append(path)
    except BaseException:
        for path in reversed(replaced):
            if old[path] is None:
                path.unlink(missing_ok=True)
            else:
                descriptor, name = tempfile.mkstemp(prefix="." + path.name + ".restore.", dir=path.parent)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(old[path])
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(name, path)
        raise
    finally:
        for staging in prepared.values():
            staging.unlink(missing_ok=True)


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignored += 1
        elif tag in {"p", "div", "br", "li"}:
            self.text.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.ignored:
            self.ignored -= 1
        elif tag in {"p", "div", "li"}:
            self.text.append(" ")

    def handle_data(self, data):
        if not self.ignored:
            self.text.append(data)


def clean_text(value):
    parser = PlainText()
    parser.feed(str(value))
    text = " ".join("".join(parser.text).split())

    def clean_url(match):
        try:
            parts = urlsplit(match.group())
            host = parts.hostname or ""
            return urlunsplit((parts.scheme, host, parts.path, "", ""))
        except ValueError:
            return "[链接]"

    return re.sub(r"https?://[^\s<>]+", clean_url, text)


def select_fields(item, keys):
    if not isinstance(item, dict):
        return {}
    result = {}
    for key in keys:
        value = item.get(key)
        if isinstance(value, str):
            result[key] = clean_text(value)
        elif value is None or isinstance(value, (bool, int, float)):
            if key in item:
                result[key] = value
    return result


TASK_FIELDS = ("id", "assignment_id", "course", "title", "date", "date_text", "date_raw",
               "due_date", "confirmed_due_date", "due_at", "due_raw", "raw_due", "due_precision", "source_display_status",
               "category", "status", "kind", "is_missing", "planning_disposition", "planning_note")
NOTICE_FIELDS = ("id", "author", "course", "date_text", "title", "text", "content", "body",
                 "read", "unread", "is_read", "seen")


def private_raw(raw):
    """Whitelist homework/message fields; never save whole HTML or credentials."""
    result = select_fields(raw, ("observed_at", "timezone", "school_timezone", "year", "academic_year", "school_year"))
    for name in ("tasks", "assignments"):
        if isinstance(raw.get(name), list):
            result[name] = [select_fields(item, TASK_FIELDS) for item in raw[name] if isinstance(item, dict)]
    result["courses"] = []
    for course in raw.get("courses", []):
        if isinstance(course, str):
            result["courses"].append(clean_text(course))
        elif isinstance(course, dict):
            item = select_fields(course, ("id", "course", "name", "title", "complete"))
            for name in ("tasks", "assignments", "rows"):
                if isinstance(course.get(name), list):
                    item[name] = [select_fields(task, TASK_FIELDS) for task in course[name] if isinstance(task, dict)]
            result["courses"].append(item)
    for name in ("notices", "teacher_notices"):
        if isinstance(raw.get(name), list):
            result[name] = [select_fields(item, NOTICE_FIELDS) for item in raw[name] if isinstance(item, dict)]
    coverage = raw.get("coverage")
    if isinstance(coverage, dict):
        result["coverage"] = select_fields(coverage, ("complete", "scope"))
        result["coverage"]["courses"] = [clean_text(v) for v in coverage.get("courses", []) if isinstance(v, str)]
    return result


def merge_reviews(previous, incoming, at):
    records = {record["key"]: dict(record) for record in previous}
    for item in incoming:
        if not isinstance(item, dict) or not item.get("type") or not item.get("id"):
            raise ValueError("Import review requires type and id")
        safe = select_fields(item, ("type", "id", "message", "raw_due", "confirmed_due_date",
                                  "previous_due_date", "current_due_date", "previous_due_at"))
        for field in ("candidates", "candidate_ids"):
            if isinstance(item.get(field), list):
                safe[field] = [clean_text(value) for value in item[field] if isinstance(value, str)]
        if isinstance(item.get("notice"), dict):
            safe["notice"] = select_fields(item["notice"], NOTICE_FIELDS)
        category = "notice" if str(safe["type"]).startswith("notice_") else str(safe["type"])
        key = category + ":" + str(safe["id"])
        fingerprint = digest(safe)
        old = records.get(key)
        if old is None or old.get("fingerprint") != fingerprint:
            records[key] = {"key": key, "fingerprint": fingerprint, "item": safe,
                            "resolved": False, "first_seen": old["first_seen"] if old else at,
                            "updated_at": at}
        # If Jupiter explicitly supplies an unread flag, mirror its read state;
        # when no flag is available the local checkbox remains authoritative.
        notice = safe.get("notice") or {}
        if "unread" in notice and notice.get("unread") is False:
            records[key]["resolved"] = True
            records[key]["resolved_at"] = at
        elif "unread" in notice and notice.get("unread") is True and old is not None and old.get("fingerprint") != fingerprint:
            records[key]["resolved"] = False
    return list(records.values())


def task_signatures(state):
    fields = ("id", "course", "title", "kind", "status", "due_precision", "due_date", "due_at",
              "due_raw", "raw_due", "is_missing", "planning_disposition", "planning_note")
    return {key: digest({field: record["source"].get(field) for field in fields})
            for key, record in state.get("tasks", {}).items()}


def risk_signatures(plan):
    now = planner.instant(plan["generated_at"])
    result = {}
    for task in plan["queue"]:
        cutoff = task.get("planning_cutoff")
        # Planner annotations are deterministic and explainable; retain a small
        # compatibility subset for old state files while tracking all material
        # changes that can alter what the user should do next.
        risk = {"cutoff_passed": bool(cutoff and planner.instant(cutoff) <= now),
                "conservative": task.get("cutoff_is_conservative", False),
                "missing": task.get("is_missing") is True, "unverified": task.get("unverified", False),
                "unknown_due": task.get("due_precision") == "unknown",
                # A user entering remaining minutes removes only the
                # provisional-estimate flag; that personal edit should not
                # produce a sync notification by itself.
                "risk_flags": [flag for flag in task.get("risk_flags", [])
                               if flag != "estimate_provisional"],
                "next_action": task.get("next_action")}
        if any(risk[key] for key in ("cutoff_passed", "missing", "unverified", "unknown_due", "risk_flags")):
            result[task["id"]] = digest(risk)
    return result


def reviews_markdown(records):
    pending = [record for record in records if not record.get("resolved", False)]
    if not pending:
        return ""
    lines = ["", "## 待核对的老师通知与日期", "", "以下内容尚未自动解释成截止日期；请核对后更新任务。", ""]
    for record in pending:
        item = record["item"]
        lines.append("- " + clean_text(item.get("message", "待核对")))
        notice = item.get("notice", {})
        if notice:
            content = " / ".join(str(notice[key]) for key in ("author", "date_text", "title", "text") if notice.get(key))
            lines.append("  - " + clean_text(content))
    return "\n".join(lines) + "\n"


def _rewrite_local_view(instance_path, records=None):
    """Rebuild the presentation files after a local checkbox action."""
    instance_path, directory, report, report_json = instance_config(instance_path)
    records = read(directory / "pending-review.json", []) if records is None else records
    plan = read(report_json, {})
    plan["pending_review"] = [record["item"] for record in records if not record.get("resolved", False)]
    plan["pending_review_count"] = len(plan["pending_review"])
    config = read(instance_path, {})
    status = read(directory / "run-status.json", {})
    page = dashboard_path(report)
    commit_files({report_json: encoded(plan), report: planner.markdown(plan) + reviews_markdown(records),
                  page: render_dashboard(instance_path, plan, status, report,
                                         read(directory / "notifications.json", [])),
                  directory / "pending-review.json": encoded(records)})
    return {"ok": True, "pending_review": len(plan["pending_review"]), "dashboard": str(page)}


def mark_notice_read(instance_path, notice_id):
    """Mark one teacher notice read locally; a changed Jupiter notice reopens it."""
    instance_path, directory, _, _ = instance_config(instance_path)
    notice_id = str(notice_id)
    with run_lock(directory):
        records = read(directory / "pending-review.json", [])
        changed = 0
        for record in records:
            item = record.get("item", {})
            notice = item.get("notice", {}) if isinstance(item, dict) else {}
            if (str(item.get("id", "")) == notice_id or str(notice.get("id", "")) == notice_id
                    or str(record.get("key", "")) == "notice:" + notice_id):
                if not record.get("resolved", False):
                    record["resolved"] = True
                    record["resolved_at"] = datetime.now(timezone.utc).isoformat()
                    changed += 1
        if changed:
            return {**_rewrite_local_view(instance_path, records), "changed": changed}
        return {"ok": True, "changed": 0, "pending_review": sum(not r.get("resolved", False) for r in records)}


def mark_task_complete(instance_path, task_id, completed=True):
    """Record a reversible personal completion flag and redraw the local view."""
    instance_path, directory, report, report_json = instance_config(instance_path)
    with run_lock(directory):
        result = planner.update(directory, str(task_id), status="completed" if completed else "open")
        pending = read(directory / "pending-review.json", [])
        plan = planner.make_plan(directory, pending_review=pending)
        plan["pending_review"] = [record["item"] for record in pending if not record.get("resolved", False)]
        status = read(directory / "run-status.json", {})
        commit_files({report_json: encoded(plan), report: planner.markdown(plan) + reviews_markdown(pending),
                      dashboard_path(report): render_dashboard(instance_path, plan, status, report,
                                                              read(directory / "notifications.json", []))})
        return {"ok": True, "task_id": str(task_id), "personal_status": result["personal"].get("status"),
                "source_status": result.get("source_status")}


def default_collector(instance_path, login=False):
    from jupiter_collector import collect
    return collect(instance_path, login=login)


def default_normalizer(raw, state):
    from jupiter_import import build_snapshot
    return build_snapshot(raw, state)


def failure_info(stage, error):
    # Exception text can contain account URLs, source HTML, or credentials.
    supplied = getattr(error, "code", "")
    known = {
        "login_required": ("login_required", "Jupiter 登录已失效，需要重新登录；旧计划已保留。"),
        "session_expired": ("login_required", "Jupiter 登录已失效，需要重新登录；旧计划已保留。"),
        "layout_changed": ("page_changed", "Jupiter 页面结构发生变化，需要检查读取器；旧计划已保留。"),
    }
    if supplied in known:
        code, message = known[supplied]
    elif isinstance(error, (ImportError, ModuleNotFoundError)):
        code, message = "runtime_not_ready", "自动读取组件尚未安装完整；旧计划已保留。"
    elif isinstance(error, TimeoutError):
        code, message = "collection_timeout", "本次读取超时；旧计划已保留，稍后可重试。"
    elif stage == "collect":
        code, message = "collection_failed", "未能读取 Jupiter；请检查登录状态与网络，旧计划已保留。"
    elif stage in {"normalize", "plan"}:
        code, message = "invalid_source_data", "本次数据未通过校验，未更新作业与计划；需要检查读取结果。"
    else:
        code, message = "storage_failed", "本次本地保存失败；请检查文件权限和可用空间。"
    return {"code": code, "message": message, "stage": stage, "type": type(error).__name__}


def failure_result(directory, previous_status, stage, error, at, dry_run):
    issue = failure_info(stage, error)
    result = {"ok": False, "error_code": issue["code"], "message": issue["message"]}
    if dry_run:
        return result
    try:
        notices = read(directory / "notifications.json", [])
        tracking = read(directory / "notification-state.json", {})
        fingerprint = digest({"code": issue["code"], "stage": stage})
        if tracking.get("failure") != fingerprint:
            notices.append({"id": digest([at, fingerprint]), "created_at": at, "type": "failure",
                            "title": "Jupiter 自动读取需要处理", "message": issue["message"]})
        tracking["failure"] = fingerprint
        status = {**previous_status, "status": "failed", "last_attempt": at, "error": issue}
        commit_files({directory / "run-status.json": encoded(status),
                      directory / "notifications.json": encoded(notices),
                      directory / "notification-state.json": encoded(tracking)})
    except Exception:
        # A full disk must not trigger a second leaking traceback or alter data.
        result["status_saved"] = False
    return result


def run(instance_path, collector=None, normalize=None, now=None, dry_run=False):
    """Collect/normalize/plan with injectable pure-Python test hooks.

    dry_run still calls the collector but does not modify planner data, reports,
    status, or notification files. Browser session internals belong to collector.
    """
    at = (planner.instant(now) if isinstance(now, str) else (now or datetime.now(timezone.utc))).isoformat()
    try:
        instance_path, directory, report, report_json = instance_config(instance_path)
    except Exception:
        return {"ok": False, "error_code": "invalid_instance", "message": "本机实例配置不可用，请检查实例文件与输出路径。"}
    try:
        with (nullcontext() if dry_run else run_lock(directory)):
            previous_status = read(directory / "run-status.json", {})
            stage = "collect"
            try:
                raw = (collector or default_collector)(str(instance_path), login=False)
                if not isinstance(raw, dict):
                    raise ValueError("Collector must return an object")
                minimal_raw = private_raw(raw)
                stage = "normalize"
                # Manual progress edits are serialized with the final state read
                # and commit, but never blocked for the browser collection itself.
                with (nullcontext() if dry_run else planner.locked_state(directory)):
                    old_state = planner.load_state(directory)
                    old_state["import_metadata"] = read(directory / "import-metadata.json", {})
                    snapshot = (normalize or default_normalizer)(raw, old_state)
                    planner.validate_snapshot(snapshot)
                    reviews = merge_reviews(read(directory / "pending-review.json", []), snapshot.get("import_review", []), at)
                    stage = "plan"
                    with tempfile.TemporaryDirectory(prefix="jupiter-run-") as temporary:
                        staging = Path(temporary)
                        planner.write_json(staging / "state.json", old_state)
                        if (directory / "config.json").exists():
                            (staging / "config.json").write_bytes((directory / "config.json").read_bytes())
                        sync_result = planner.sync(staging, snapshot)
                        plan = planner.make_plan(staging, now=at, pending_review=reviews)
                        updated = planner.load_state(staging)
                    from calendar_output import markdown as calendar_markdown, ics as calendar_ics
                    calendar_md, calendar_ics_path = calendar_paths(report)
                    calendar_include_date_only = True
                    # Metadata has its own file; don't pollute the planner schema.
                    updated.pop("import_metadata", None)
                    tasks = task_signatures(updated)
                    risks = risk_signatures(plan)
                    tracking = read(directory / "notification-state.json", {})
                    before = tracking.get("tasks", task_signatures(old_state))
                    review_hashes = {r["key"]: r["fingerprint"] for r in reviews if not r.get("resolved", False)}
                    changes = {"added": sum(key not in before for key in tasks),
                               "updated": sum(key in before and before[key] != value for key, value in tasks.items()),
                               "review_changed": sum(tracking.get("reviews", {}).get(key) != value for key, value in review_hashes.items()),
                               "risk_changed": sum(tracking.get("risks", {}).get(key) != value for key, value in risks.items())
                                               + sum(key not in risks for key in tracking.get("risks", {})),
                               "recovered": bool(tracking.get("failure"))}
                    coverage = snapshot["coverage"]["complete"]
                    changes["coverage_changed"] = "coverage_complete" in tracking and tracking["coverage_complete"] != coverage
                    notify = any(changes.values())
                    result = {"ok": True, "dry_run": dry_run, "observed": sync_result["observed"],
                              "active_tasks": len(plan["queue"]), "pending_review": len(review_hashes),
                              "coverage_complete": coverage, "changes": changes,
                              "new_notifications": int(notify)}
                    if dry_run:
                        return result
                    notifications = read(directory / "notifications.json", [])
                    if notify:
                        summary = (f"新增 {changes['added']} 项，变更 {changes['updated']} 项；"
                                   f"新增或更新待核对事项 {changes['review_changed']} 项，风险变化 {changes['risk_changed']} 项。")
                        if changes["recovered"]:
                            summary = "自动读取已恢复。" + summary
                        if not coverage:
                            summary += "本次仅完成部分覆盖。"
                        notifications.append({"id": digest([at, tasks, review_hashes, risks]), "created_at": at,
                                              "type": "update", "title": "Jupiter 作业清单已更新",
                                              "message": summary, "changes": changes})
                    status = {"status": "ok", "last_attempt": at, "last_success": at,
                              "last_observed_at": snapshot["observed_at"], "coverage_complete": coverage,
                              "observed": sync_result["observed"], "pending_review": len(review_hashes), "error": None}
                    plan["pending_review"] = [record["item"] for record in reviews if not record.get("resolved", False)]
                    dashboard = dashboard_path(report)
                    files = {directory / "state.json": encoded(updated),
                             directory / "raw-latest.json": encoded(minimal_raw),
                             directory / "snapshot-latest.json": encoded(snapshot),
                             directory / "import-metadata.json": encoded(snapshot.get("import_metadata", {})),
                             directory / "pending-review.json": encoded(reviews),
                             report: planner.markdown(plan) + reviews_markdown(reviews), report_json: encoded(plan),
                             calendar_md: calendar_markdown(updated, plan),
                             calendar_ics_path: calendar_ics(updated, plan, include_date_only=calendar_include_date_only),
                             dashboard: render_dashboard(instance_path, plan, status, report, notifications),
                             directory / "notifications.json": encoded(notifications),
                             directory / "notification-state.json": encoded({"tasks": tasks, "reviews": review_hashes,
                                                                             "risks": risks, "coverage_complete": coverage,
                                                                             "failure": None}),
                             directory / "run-status.json": encoded(status)}
                    stage = "commit"
                    commit_files(files)
                    if notify:
                        _system_notification("Jupiter 作业管家", summary, read(instance_path, {}))
                    return result
            except Exception as error:
                result = failure_result(directory, previous_status, stage, error, at, dry_run)
                if not dry_run:
                    try:
                        commit_files({dashboard_path(report): refresh_dashboard(instance_path)})
                    except Exception:
                        pass
                return result
    except AlreadyRunning:
        return {"ok": False, "skipped": True, "error_code": "already_running", "message": "已有一次同步正在运行，本次已跳过。"}
    except Exception:
        return {"ok": False, "error_code": "storage_failed", "message": "无法读取或锁定本机状态文件；旧数据未更新。"}


def generate_launch_agent(instance_path, python, output):
    instance_path, directory, _, _ = instance_config(instance_path)
    # Resolving symlinks here would bypass a virtual environment and lose its
    # installed collector dependencies; launch the exact supplied interpreter.
    python = Path(os.path.abspath(Path(python).expanduser()))
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("Python path must be an executable file")
    label = "local.jupiter-study-planner." + digest(str(instance_path))[:12]
    content = {"Label": label,
               "ProgramArguments": [str(python), str(Path(__file__).resolve()), "run", "--instance", str(instance_path)],
               "WorkingDirectory": str(Path(__file__).resolve().parent.parent),
               # launchd starts agents with a very small environment.  Scrapling
               # and Chrome need a writable temporary directory and a stable
               # home directory even when the agent runs outside a shell.
               "EnvironmentVariables": {"HOME": str(Path.home()),
                                         "TMPDIR": "/tmp",
                                         "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
               "StartCalendarInterval": [{"Hour": 7, "Minute": 0}, {"Hour": 17, "Minute": 0}],
               "RunAtLoad": False, "KeepAlive": False, "ProcessType": "Background",
               "StandardOutPath": str(directory / "runtime.log"),
               "StandardErrorPath": str(directory / "runtime-error.log")}
    output = Path(output).expanduser().resolve()
    if output == instance_path or output == python or output == Path(__file__).resolve() or output.name in {
            "state.json", "config.json", "run-status.json", "notification-state.json", "notifications.json"}:
        raise ValueError("Unsafe launch agent output path")
    commit_files({output: plistlib.dumps(content, sort_keys=False)})
    return {"ok": True, "generated": True, "enabled": False, "label": label,
            "schedule": "每天本机当地时间 07:00、17:00；仅生成配置，尚未启用。"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    running = commands.add_parser("run", help="Collect and update locally without model calls")
    running.add_argument("--instance", required=True, type=Path)
    running.add_argument("--dry-run", action="store_true", help="Collect/validate, print counts; do not update planner files")
    notice = commands.add_parser("mark-notice-read", help="Mark one locally visible teacher notice as read")
    notice.add_argument("--instance", required=True, type=Path)
    notice.add_argument("--notice-id", required=True)
    task = commands.add_parser("mark-task-complete", help="Record a personal completion flag")
    task.add_argument("--instance", required=True, type=Path)
    task.add_argument("--task-id", required=True)
    task.add_argument("--open", action="store_true", help="Reopen a locally completed task")
    generating = commands.add_parser("generate-launch-agent", help="Generate, but do not enable, 07:00/17:00 local schedule")
    generating.add_argument("--instance", required=True, type=Path)
    generating.add_argument("--python", required=True, type=Path)
    generating.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "run":
        result = run(args.instance, dry_run=args.dry_run)
    elif args.command == "mark-notice-read":
        try:
            result = mark_notice_read(args.instance, args.notice_id)
        except Exception:
            result = {"ok": False, "error_code": "notice_update_failed", "message": "未能更新通知状态；原数据已保留。"}
    elif args.command == "mark-task-complete":
        try:
            result = mark_task_complete(args.instance, args.task_id, completed=not args.open)
        except Exception:
            result = {"ok": False, "error_code": "task_update_failed", "message": "未能更新本地完成状态；原数据已保留。"}
    else:
        try:
            result = generate_launch_agent(args.instance, args.python, args.output)
        except Exception:
            result = {"ok": False, "error_code": "invalid_launch_agent", "message": "未生成定时配置；请检查实例、Python 和输出路径。"}
    print(encoded(result), end="")
    return 0 if result.get("ok") or result.get("skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
