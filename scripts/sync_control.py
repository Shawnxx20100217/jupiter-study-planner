#!/usr/bin/env python3
"""Private-instance macOS sync switches. Never reads credentials or task bodies."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess

import jupiter_runtime as runtime


class ControlError(RuntimeError):
    pass


def _config(path):
    location, directory, _report, _plan = runtime.instance_config(path)
    return location, directory, runtime.read(location, {})


def _labels(instance):
    return {mode: "local.jupiter-study-planner." + runtime.digest([str(instance), mode])[:12]
            for mode in ("watch", "ticktick")}


def _call(args, runner=None):
    try:
        return (runner or subprocess.run)(["/bin/launchctl", *args], capture_output=True,
                                          text=True, timeout=12, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ControlError("无法控制本机后台同步，请稍后重试。") from exc


def _job(label, runner=None):
    result = _call(["print", f"gui/{os.getuid()}/{label}"], runner)
    return {"loaded": result.returncode == 0,
            "running": result.returncode == 0 and bool(re.search(r"^\s*pid = [1-9]\d*", result.stdout or "", re.M))}


def _owned_plists(instance, directory, home):
    """Inspect arguments, not filenames, before modifying any launch agent."""
    result = {}
    for folder in (home / "Library/LaunchAgents", directory / "agents"):
        if not folder.is_dir():
            continue
        for path in folder.glob("*.plist"):
            try:
                value = plistlib.loads(path.read_bytes())
                args = value.get("ProgramArguments", [])
                label = value.get("Label", "")
                if (not isinstance(args, list) or not isinstance(label, str) or
                    not label.startswith("local.jupiter") or len(args) < 5 or
                    Path(str(args[1])).name != "jupiter_runtime.py" or
                    args[2] not in ("run", "sync-ticktick", "remind")):
                    continue
                position = args.index("--instance")
                target = Path(args[position + 1]).expanduser().resolve()
                if target == instance:
                    result[path] = label
            except (OSError, ValueError, TypeError, IndexError, plistlib.InvalidFileException):
                continue
    return result


def _retire(label, runner=None):
    """Stop future runs immediately; allow an in-flight run to finish."""
    job = _job(label, runner)
    if not job["loaded"]:
        return False
    if job["running"]:
        result = _call(["disable", f"gui/{os.getuid()}/{label}"], runner)
        if result.returncode:
            raise ControlError("暂停设置已保存，但系统暂时未能停止后续调度。")
        return True
    result = _call(["bootout", f"gui/{os.getuid()}/{label}"], runner)
    if result.returncode and _job(label, runner)["loaded"]:
        raise ControlError("暂停设置已保存，但系统暂时未能卸载后台调度。")
    return False


def _reload(label, path, runner=None):
    """Reload one owned agent without terminating an in-flight collector.

    launchd has no non-disruptive reload operation.  If the job is idle we can
    safely boot it out and bootstrap the freshly-written plist.  If it is
    running, disabling future launches is safe and the old process is allowed
    to finish; the caller reports the reload as deferred instead of using
    ``kickstart -k`` or ``bootout`` (both of which can terminate the collector
    and its browser children).
    """
    current = _job(label, runner)
    if not current["loaded"]:
        result = _call(["bootstrap", f"gui/{os.getuid()}", str(path)], runner)
        if result.returncode and not _job(label, runner)["loaded"]:
            raise ControlError("设置已保存，但后台调度尚未启动。")
        return {"reloaded": True, "deferred": False}
    if current["running"]:
        result = _call(["disable", f"gui/{os.getuid()}/{label}"], runner)
        if result.returncode:
            raise ControlError("设置已保存；当前同步仍在运行，稍后会自动应用新频率。")
        return {"reloaded": False, "deferred": True}
    result = _call(["bootout", f"gui/{os.getuid()}/{label}"], runner)
    if result.returncode and _job(label, runner)["loaded"]:
        raise ControlError("设置已保存，但后台调度暂时无法重新加载。")
    result = _call(["bootstrap", f"gui/{os.getuid()}", str(path)], runner)
    if result.returncode and not _job(label, runner)["loaded"]:
        raise ControlError("设置已保存，但后台调度尚未启动。")
    return {"reloaded": True, "deferred": False}


_SAFE_TICKTICK_ERRORS = {
    "ticktick_not_configured": "TickTick 尚未配置访问令牌。",
    "ticktick_project_missing": "TickTick 目标清单未配置。",
    "ticktick_project_invalid": "TickTick 目标清单已失效，请重新选择清单。",
    "project_not_found": "TickTick 目标清单已失效，请重新选择清单。",
    "source_project_missing": "TickTick 原始同步清单未配置。",
    "source_project_not_found": "TickTick 原始同步清单已失效，请重新选择清单。",
    "configured_project_unavailable": "部分 TickTick 目标清单已删除或关闭，请重新选择清单。",
    "remote_inventory_unavailable": "无法读取 TickTick 目标清单，本轮未写入新任务。",
    "sync_deferred": "部分 TickTick 任务暂未同步，已保留原数据。",
    "unauthorized": "TickTick 授权已失效，请重新连接。",
    "capacity": "TickTick 拒绝了本次写入：目标清单或任务数量已达到容量限制。",
    "rate_limited": "TickTick 暂时限制了请求频率，请稍后重试。",
    "network": "无法连接 TickTick，请检查网络。",
    "timeout": "TickTick 请求超时，请稍后重试。",
    "api_error": "TickTick 服务暂时未接受本次写入，请稍后重试。",
    "invalid_response": "TickTick 返回了无法识别的结果。",
}


def _safe_sync_error(value):
    """Return a short allow-listed error; never surface a provider response body."""
    if not isinstance(value, dict):
        return ""
    nested = value.get("ticktick")
    if not isinstance(nested, dict) and isinstance(value.get("result"), dict):
        nested = value["result"].get("ticktick")
    if (isinstance(nested, dict) and
            (nested.get("ok") is False or nested.get("status") == "failed" or
             nested.get("error_code") not in (None, ""))):
        value = nested
    failed = (value.get("ok") is False or value.get("status") == "failed" or
              value.get("error_code") not in (None, ""))
    if not failed:
        return ""
    code = str(value.get("error_code") or "").strip()
    return _SAFE_TICKTICK_ERRORS.get(code, "最近一次 TickTick 写入未成功，已保留上次数据。")


def _success_at(value):
    """Only show a timestamp when the corresponding operation explicitly succeeded."""
    if not isinstance(value, dict):
        return None
    nested = value.get("ticktick")
    if not isinstance(nested, dict) and isinstance(value.get("result"), dict):
        nested = value["result"].get("ticktick")
    if isinstance(nested, dict):
        value = nested
    if value.get("ok") is False or value.get("status") == "failed":
        return None
    stamp = value.get("last_success") or value.get("synced_at")
    return stamp if isinstance(stamp, str) and stamp else None


def status(instance_path, *, home=None, runner=None):
    instance, directory, config = _config(instance_path)
    home = Path(home) if home else Path.home()
    paused = config.get("automation_paused") is True or config.get("collection_paused") is True
    stopping = False
    owned = _owned_plists(instance, directory, home)
    labels = _labels(instance)
    pending = config.get("automation_retiring_labels", [])
    if not isinstance(pending, list):
        pending = []
    # The label list is generated by configure only after ownership validation.
    # Intersect with the current instance's canonical labels or owned plists.
    known = set(labels.values()) | set(owned.values())
    retiring = {label for label in pending if label in known}
    if paused:
        retiring.update(known)
    for label in retiring:
        stopping = _retire(label, runner) or stopping
    jobs = {mode: _job(label, runner)["loaded"] for mode, label in labels.items()}
    jupiter = runtime.read(directory / "run-status.json", {})
    nested_ticktick = config.get("ticktick") if isinstance(config.get("ticktick"), dict) else {}
    source_mirror = (config.get("ticktick_sync_mode") == "source_mirror" or
                     nested_ticktick.get("sync_mode") == "source_mirror")
    ticktick = runtime.read(
        directory / ("ticktick-source-status.json" if source_mirror else "ticktick-status.json"), {})
    if not ticktick and not source_mirror:
        ticktick = runtime.read(directory / "ticktick-sync-status.json", {})
    error = ""
    if jupiter.get("status") == "failed":
        error = "最近一次 Jupiter 读取未成功，已保留上次数据。"
    sync_error = _safe_sync_error(ticktick)
    if sync_error:
        error = sync_error
    jupiter_success = _success_at(jupiter)
    ticktick_success = _success_at(ticktick)
    mode = "source_mirror" if source_mirror else "plan"
    return {"ok": True, "enabled": not paused,
            "launch_at_login": config.get("launch_at_login") is True,
            "jobs_loaded": all(jobs.values()), "stopping": stopping,
            "jupiter_last_success": jupiter_success,
            "ticktick_last_success": ticktick_success,
            "jupiter_ok": jupiter_success is not None,
            "ticktick_ok": ticktick_success is not None,
            "mode": mode,
            "mode_label": "原始同步" if source_mirror else "计划同步",
            "last_error": error}


def configure(instance_path, python, enabled, login, *, home=None, runner=None):
    instance, directory, config = _config(instance_path)
    home = Path(home) if home else Path.home()
    enabled, login = bool(enabled), bool(login)
    existing = _owned_plists(instance, directory, home)
    canonical = _labels(instance)
    destination = home / "Library/LaunchAgents" if login else directory / "agents"
    paths = {mode: destination / (label + ".plist") for mode, label in canonical.items()}
    # Save the kill switch before touching launchd, even when a sync holds the
    # runtime lock. Never delete tasks, credentials, cookies, or normal Chrome.
    runtime._write_instance_config(instance, {"automation_paused": not enabled,
        "collection_paused": not enabled, "launch_at_login": login,
        "schedule_enabled": enabled, "schedule_mode": "watch",
        "automation_retiring_labels": [], "automation_reload_pending": []})
    for mode, path in paths.items():
        runtime.generate_launch_agent(instance, python, path, mode=mode)
    retiring = []
    desired = set(canonical.values())
    for path, label in existing.items():
        if label not in desired or not enabled:
            if _retire(label, runner):
                retiring.append(label)
        if path not in paths.values():
            path.unlink(missing_ok=True)
    deferred = []
    if enabled:
        for mode, label in canonical.items():
            result = _call(["enable", f"gui/{os.getuid()}/{label}"], runner)
            if result.returncode:
                raise ControlError("无法恢复后台调度，请稍后重试。")
            reload_result = _reload(label, paths[mode], runner)
            if reload_result["deferred"]:
                deferred.append(label)
    else:
        for label in canonical.values():
            if _retire(label, runner):
                retiring.append(label)
    runtime._write_instance_config(instance, {
        "automation_retiring_labels": sorted(set(retiring)),
        "automation_reload_pending": sorted(set(deferred)),
    })
    result = status(instance, home=home, runner=runner)
    result["reload_deferred"] = bool(deferred)
    result["reload_pending_labels"] = deferred
    if not enabled:
        result["message"] = "后台同步已暂停。"
    elif deferred:
        result["message"] = "后台同步已恢复；当前同步完成后再应用新频率，未终止正在运行的浏览器。"
    else:
        result["message"] = "后台同步已恢复。"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    checking = commands.add_parser("status")
    checking.add_argument("--instance", required=True, type=Path)
    setting = commands.add_parser("configure")
    setting.add_argument("--instance", required=True, type=Path)
    setting.add_argument("--python", required=True, type=Path)
    setting.add_argument("--enabled", choices=("on", "off"), required=True)
    setting.add_argument("--login", choices=("on", "off"), required=True)
    args = parser.parse_args(argv)
    try:
        result = status(args.instance) if args.command == "status" else configure(
            args.instance, args.python, args.enabled == "on", args.login == "on")
    except Exception as exc:
        result = {"ok": False, "last_error": str(exc) if isinstance(exc, ControlError)
                  else "无法读取或更新本机同步设置；原作业数据已保留。"}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
