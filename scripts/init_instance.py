#!/usr/bin/env python3
"""Create a new private Jupiter instance without credentials, sync or scheduling."""
import argparse
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def validate_identity(identity):
    required = ("expected_student", "expected_school_year", "expected_term", "school_timezone")
    result = {}
    for key in required:
        value = identity.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"请填写 {key}，与自己的 Jupiter 页面一致。")
        result[key] = value.strip()
    try:
        ZoneInfo(result["school_timezone"])
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("school_timezone 必须是有效学校时区，例如 Asia/Shanghai。") from error
    courses = identity.get("expected_courses")
    if not isinstance(courses, list) or not courses or any(not isinstance(c, str) or not c.strip() for c in courses):
        raise ValueError("expected_courses 必须包含全部课程的页面名称。")
    result["expected_courses"] = [c.strip() for c in courses]
    if len(set(result["expected_courses"])) != len(result["expected_courses"]):
        raise ValueError("课程名称不能重复。")
    return result


def initialize(data_dir, identity, plugin_root=PLUGIN_ROOT):
    identity = validate_identity(identity)
    directory = Path(data_dir).expanduser().resolve()
    plugin_root = Path(plugin_root).resolve()
    if directory == plugin_root or plugin_root in directory.parents:
        raise ValueError("个人数据必须放在插件目录外，避免分享时带上账户资料。")
    state = directory / "state"
    instance = state / "local-instance.json"
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("这个目录已有实例或数据，未覆盖；请使用原配置或换一个新目录。")
    reports = directory / "reports"
    for path in (directory, state, state / "browser-profile", reports):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    config = {**identity, "state_dir": str(state),
              "profile_dir": str(state / "browser-profile"),
              "report_path": str(reports / "study-plan.md"),
              "collector_mode": "deterministic-local",
              "system_notifications": True,
              "deadline_reminders_enabled": True,
              "deadline_reminder_minutes": [1440, 120, 30],
              "schedule_interval_minutes": 60,
              "ticktick_enabled": False,
              "ticktick_api_base": "https://api.ticktick.com/open/v1",
              "ticktick_sync_mode": "source_mirror",
              "ticktick_source_project_name": "原始任务",
              "ticktick_project_name": "原始任务",
              "ticktick_token_file": str(state / "ticktick-token"),
              "ticktick_reminder_minutes": [1440, 120, 30],
              "jupiter_done_writeback_enabled": False,
              "automation_paused": False,
              "collection_paused": False,
              "launch_at_login": False,
              "autonomous_collection_verified": False}
    descriptor = os.open(instance, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    paths_file = directory / "paths.json"
    paths = {"instance": str(instance), "state": str(state),
             # Resolving this symlink can bypass the virtual environment.
             "python": str(Path(sys.executable).expanduser().absolute()),
             "scripts": str(plugin_root / "scripts"), "reports": str(reports)}
    descriptor = os.open(paths_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(paths, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return {"ok": True, "instance": str(instance), "report_directory": str(reports),
            "paths": str(paths_file),
            "logged_in": False, "synced": False, "schedule_enabled": False}


def ask_identity():
    result = {}
    for key, label in (("expected_student", "学生姓名（页面原文）"),
                       ("expected_school_year", "学校及学年（页面原文）"),
                       ("expected_term", "当前学期（页面原文）"),
                       ("school_timezone", "学校时区，例如 Asia/Shanghai")):
        result[key] = input(label + "：").strip()
    print("逐行输入全部课程名称；完成后直接按回车。")
    result["expected_courses"] = []
    while True:
        course = input("课程：").strip()
        if not course:
            break
        result["expected_courses"].append(course)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path, help="New private directory outside the plugin")
    parser.add_argument("--identity-json", type=Path, help="Your own identity configuration; otherwise prompts locally")
    args = parser.parse_args(argv)
    try:
        identity = json.loads(args.identity_json.expanduser().read_text(encoding="utf-8")) if args.identity_json else ask_identity()
        result = initialize(args.data_dir, identity)
    except (ValueError, OSError, EOFError) as error:
        print(json.dumps({"ok": False, "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("配置已创建。接下来请用专用 Chrome 登录，再运行首次同步；定时没有启动。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
