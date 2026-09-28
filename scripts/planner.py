#!/usr/bin/env python3
"""Local, read-only-source homework planner. Python 3.9+, standard library only."""
import argparse
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import json
import math
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

UTC = timezone.utc
STATUSES = {"open", "submitted", "completed", "unknown"}
DEFAULTS = {"assignment": 30, "assessment": 45}
DISPOSITIONS = {"active": None, "in_class": "课堂内完成，不安排课外用时",
                "reference": "信息或参考项，不计入待办用时", "superseded": "已由其他任务记录替代，避免重复安排"}


def instant(value):
    if not isinstance(value, str):
        raise ValueError("Time must be an ISO timestamp with an explicit UTC offset")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Time must include a UTC offset: " + value)
    return result.astimezone(UTC)


def integer(value, name, minimum=1, maximum=100000):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def read_json(path, default=None):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


@contextmanager
def locked_state(directory):
    # Prevent simultaneous scheduled and manual updates from losing progress.
    import fcntl
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield directory
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def load_state(directory):
    return read_json(Path(directory) / "state.json", {
        "version": 1, "timezone": "Asia/Shanghai", "tasks": {}, "events": [],
    })


def validate_snapshot(snapshot):
    observed = instant(snapshot["observed_at"])
    ZoneInfo(snapshot["timezone"])
    coverage = snapshot["coverage"]
    if not isinstance(coverage.get("complete"), bool) or not isinstance(coverage.get("courses"), list):
        raise ValueError("coverage requires complete:boolean and courses:list")
    seen = set()
    for task in snapshot["tasks"]:
        for key in ("id", "course", "title"):
            if not isinstance(task.get(key), str) or not task[key].strip():
                raise ValueError(f"Every task requires a non-empty {key}")
        if task["id"] in seen:
            raise ValueError("Duplicate task id: " + task["id"])
        seen.add(task["id"])
        if task.get("status") not in STATUSES:
            raise ValueError("Invalid source status")
        if task.get("kind") not in DEFAULTS:
            raise ValueError("kind must be assignment or assessment")
        precision = task.get("due_precision")
        if precision not in {"date", "time", "unknown"}:
            raise ValueError("Invalid due_precision")
        if task.get("due_date"):
            parsed_date = date.fromisoformat(task["due_date"])
            if parsed_date.isoformat() != task["due_date"]:
                raise ValueError("due_date must be YYYY-MM-DD")
        if precision == "time":
            instant(task.get("due_at"))
        elif task.get("due_at") is not None:
            raise ValueError("Only time precision may have due_at")
        if precision == "date" and not task.get("due_date"):
            raise ValueError("Date precision requires due_date")
        if precision == "unknown" and task.get("due_date") is not None:
            raise ValueError("Unknown precision must not invent due_date")
        if task.get("estimated_minutes") is not None:
            integer(task["estimated_minutes"], "estimated_minutes")
        if task.get("estimate_source", "provisional") not in {"provisional", "user", "observed"}:
            raise ValueError("Invalid estimate_source")
        if task.get("is_missing") is not None and not isinstance(task["is_missing"], bool):
            raise ValueError("is_missing must be true, false or null")
        if task.get("planning_disposition", "active") not in DISPOSITIONS:
            raise ValueError("Invalid planning_disposition")
        if task.get("planning_note") is not None and not isinstance(task["planning_note"], str):
            raise ValueError("planning_note must be a string or null")
    return observed


def deadline_key(task):
    value = task.get("due_at")
    return (task["due_precision"], instant(value).isoformat() if value else None, task.get("due_date"))


def sync(directory, snapshot):
    observed = validate_snapshot(snapshot)
    with locked_state(directory) as directory:
        state = load_state(directory)
        previous = state.get("latest_sync", {}).get("observed_at")
        if previous and observed < instant(previous):
            raise ValueError("Refusing an older snapshot; collect a fresh scan")
        events, seen = [], set()
        for source in snapshot["tasks"]:
            task_id = source["id"]
            seen.add(task_id)
            record = state["tasks"].get(task_id)
            changes = []
            if record is None:
                record = {"personal": {}, "first_seen": snapshot["observed_at"]}
                changes.append(("added", None, source))
            else:
                old = record["source"]
                if deadline_key(old) != deadline_key(source):
                    changes.append(("deadline_changed", list(deadline_key(old)), list(deadline_key(source))))
                if old["status"] != source["status"]:
                    changes.append(("status_changed", old["status"], source["status"]))
                if old.get("is_missing") != source.get("is_missing"):
                    changes.append(("missing_changed", old.get("is_missing"), source.get("is_missing")))
                if old.get("planning_disposition", "active") != source.get("planning_disposition", "active"):
                    changes.append(("planning_disposition_changed", old.get("planning_disposition", "active"),
                                    source.get("planning_disposition", "active")))
            for kind, before, after in changes:
                events.append({"type": kind, "id": task_id, "observed_at": snapshot["observed_at"],
                               "before": before, "after": after})
            record.update(source=dict(source), last_seen=snapshot["observed_at"],
                          unverified=False, verification_reason=None)
            state["tasks"][task_id] = record
        for task_id, record in state["tasks"].items():
            if task_id not in seen:
                record["unverified"] = True
                record["verification_reason"] = (
                    "Absent from latest complete scan; retained until explicitly verified"
                    if snapshot["coverage"]["complete"] else
                    "Latest scan is partial; this task was not observed")
        state["timezone"] = snapshot["timezone"]
        state["events"].extend(events)
        state["latest_sync"] = {"observed_at": snapshot["observed_at"],
                                "coverage": snapshot["coverage"], "events": events}
        write_json(directory / "state.json", state)
    return {"observed": len(seen), "retained": len(state["tasks"]), "events": events,
            "coverage": snapshot["coverage"]}


def update(directory, task_id, estimate=None, remaining=None, status=None):
    with locked_state(directory) as directory:
        state = load_state(directory)
        if task_id not in state["tasks"]:
            raise ValueError("Unknown task id: " + task_id)
        personal = state["tasks"][task_id]["personal"]
        if estimate is not None:
            personal["estimated_minutes"] = integer(estimate, "estimate")
        if remaining is not None:
            personal["remaining_minutes"] = integer(remaining, "remaining", minimum=0)
        if status is not None:
            if status not in {"open", "completed"}:
                raise ValueError("Personal status must be open or completed")
            personal["status"] = status
        personal["updated_at"] = datetime.now(UTC).isoformat()
        write_json(directory / "state.json", state)
        return {"id": task_id, "personal": personal, "source_status": state["tasks"][task_id]["source"]["status"]}


def configuration(directory):
    config = read_json(Path(directory) / "config.json", {})
    estimates = {**DEFAULTS, **config.get("default_estimates", {})}
    for kind in DEFAULTS:
        integer(estimates[kind], kind + " default estimate")
    block = integer(config.get("max_block_minutes", 40), "max_block_minutes", maximum=45)
    rest = integer(config.get("break_minutes", 5), "break_minutes", maximum=60)
    return estimates, block, rest


def _priority_details(item, now):
    """Return deterministic priority and uncertainty annotations for one task.

    This is deliberately a rule table rather than a language-model judgement.  A
    user can therefore see why an item moved up the queue and get the same result
    on every machine and every run.  The cutoff used here is already the
    conservative target produced by :func:`task_item` for date-only deadlines.
    """
    cutoff = item.get("planning_cutoff")
    due_precision = item.get("due_precision")
    flags, reasons = [], []
    score = 0
    if cutoff:
        remaining = instant(cutoff) - now
        hours = remaining.total_seconds() / 3600
        if hours <= 0:
            score += 100
            reasons.append("保守完成目标已过，先核实并处理")
            flags.append("cutoff_passed")
            band = "critical"
        elif hours <= 24:
            score += 90
            reasons.append("24 小时内到期")
            band = "high"
        elif hours <= 72:
            score += 70
            reasons.append("3 天内到期")
            band = "high"
        elif hours <= 168:
            score += 45
            reasons.append("一周内到期")
            band = "medium"
        else:
            score += 25
            reasons.append("按已知截止时间排队")
            band = "low"
    else:
        # Unknown dates must stay visible, but should never consume a user's
        # scarce current-session minutes before the date is confirmed.
        score += 15
        band = "low"
        flags.append("deadline_unknown")
        reasons.append("截止日期未知，先核对后安排")

    if due_precision == "date":
        flags.append("time_unknown")
        reasons.append("只有日期，具体钟点待确认")
        if band == "low":
            band = "medium"
        score += 3
    if item.get("is_missing") is True:
        flags.append("marked_missing")
        reasons.insert(0, "Jupiter 明确标记为缺交")
        score += 25
        if band == "low":
            band = "high"
    if item.get("source_status") == "unknown":
        flags.append("completion_unknown")
        reasons.append("完成状态未知")
        score += 4
    if item.get("unverified"):
        flags.append("scan_unverified")
        reasons.append("本次扫描未核实，保留历史记录")
        score += 8
    if item.get("effective_estimate_source") == "provisional" and not item.get("remaining_is_user_override"):
        flags.append("estimate_provisional")
        reasons.append("用时仍是暂估")

    # Keep the band a short stable vocabulary for the dashboard and future app.
    if score >= 100 or "cutoff_passed" in flags:
        band = "critical"
    elif score >= 70 or "marked_missing" in flags:
        band = "high"
    elif score >= 35 or any(flag != "estimate_provisional" for flag in flags):
        band = "medium"
    else:
        band = "low"
    if "deadline_unknown" in flags:
        action = "先核对截止日期，再决定学习时段"
    elif "cutoff_passed" in flags:
        action = "先核实是否已提交，再处理剩余工作"
    elif item.get("is_missing") is True:
        action = "先处理缺交项"
    else:
        action = "可按剩余用时拆成下一段学习"
    confidence = "low" if "deadline_unknown" in flags else ("medium" if flags else "high")
    return {"priority_score": score, "priority_band": band,
            "priority_reasons": reasons, "priority_reason": "；".join(reasons),
            "risk_flags": flags, "risk_level": band, "confidence": confidence,
            "next_action": action}


def task_item(record, zone, defaults, now):
    source, personal = record["source"], record.get("personal", {})
    disposition = source.get("planning_disposition", "active")
    precision, cutoff = source["due_precision"], None
    if precision == "time":
        cutoff = instant(source["due_at"])
        due_label = cutoff.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")
    elif precision == "date":
        day = date.fromisoformat(source["due_date"])
        cutoff = datetime.combine(day, time.min, zone).astimezone(UTC)
        due_label = (f"{day.isoformat()}（具体时间待确认；保守目标："
                     f"{(day - timedelta(days=1)).isoformat()} 当日结束前完成）")
        if disposition != "active":
            due_label = f"{day.isoformat()}（具体时间未提供）"
    else:
        due_label = "截止日期未知，需核实"
    estimate = personal.get("estimated_minutes", source.get("estimated_minutes"))
    estimate_source = "user" if "estimated_minutes" in personal else source.get("estimate_source", "provisional")
    if estimate is None:
        estimate, estimate_source = defaults[source["kind"]], "provisional"
    remaining = personal.get("remaining_minutes", estimate)
    status = source["status"]
    excluded = disposition != "active" or status in {"submitted", "completed"} or personal.get("status") == "completed"
    exclusion_reason = ((source.get("planning_note") or DISPOSITIONS[disposition]) if disposition != "active" else
                        ("Jupiter 已确认提交或完成" if status in {"submitted", "completed"} else
                         "本地已完成，Jupiter 提交状态待确认" if personal.get("status") == "completed" else None))
    notes = []
    if disposition == "active" and source.get("planning_note"):
        notes.append(source["planning_note"])
    if estimate_source == "provisional":
        notes.append("用时为暂估，尚未校准")
    if status == "unknown":
        notes.append("Jupiter 完成状态未知")
    if source.get("is_missing") is True:
        notes.append("Jupiter 明确标记为缺交")
    if record.get("unverified"):
        notes.append("本次未核实，保留历史记录")
    if cutoff and cutoff <= now:
        notes.append("保守完成目标已过，实际截止时间待核实" if precision == "date" else "已超过已知截止时间")
    if personal.get("status") == "completed" and status not in {"submitted", "completed"}:
        notes.append("已在本地完成；Jupiter 提交状态仍需确认")
    if remaining == 0 and not excluded:
        notes.append("剩余用时为 0；完成及提交状态仍需确认")
    item = {**source, "source_status": status, "personal_status": personal.get("status"),
            "planning_disposition": disposition, "exclusion_reason": exclusion_reason,
            "remaining_minutes": remaining, "effective_estimate_minutes": estimate,
            "effective_estimate_source": estimate_source, "remaining_is_user_override": "remaining_minutes" in personal,
            "due_label": due_label, "planning_cutoff": cutoff.isoformat() if cutoff else None,
            "cutoff_is_conservative": precision == "date", "unverified": record.get("unverified", False),
            "last_seen": record["last_seen"], "notes": notes, "excluded": excluded}
    item.update(_priority_details(item, now))
    return item


def merge(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def windows_from(availability, now):
    def parse(items):
        result = []
        for item in items:
            start, end = instant(item["start"]), instant(item["end"])
            if end <= start:
                raise ValueError("Every interval must have end after start")
            result.append((start, end))
        return merge(result)
    windows = parse(availability.get("windows", availability.get("availability", [])))
    busy = parse(availability.get("busy", []))
    free = [(max(start, now), end) for start, end in windows if end > now]
    for bs, be in busy:
        remaining = []
        for start, end in free:
            if be <= start or bs >= end:
                remaining.append((start, end))
            else:
                if start < bs:
                    remaining.append((start, bs))
                if be < end:
                    remaining.append((be, end))
        free = remaining
    return free


def make_plan(directory, now=None, availability=None, minutes=None, pending_review=None):
    now = instant(now) if isinstance(now, str) else (now or datetime.now(UTC))
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    if minutes is not None and availability is not None:
        raise ValueError("Use either --minutes or --availability, not both")
    state = load_state(directory)
    zone = ZoneInfo(state["timezone"])
    defaults, block_size, rest = configuration(directory)
    items = [task_item(record, zone, defaults, now) for record in state["tasks"].values()]
    queue = sorted((item for item in items if not item["excluded"]),
                   key=lambda item: (item["planning_cutoff"] is None,
                                     instant(item["planning_cutoff"]) if item["planning_cutoff"] else datetime.max.replace(tzinfo=UTC),
                                     -item.get("priority_score", 0),
                                     item["course"], item["title"], item["id"]))
    if minutes is not None:
        integer(minutes, "minutes")
        availability = {"windows": [{"start": now.isoformat(), "end": (now + timedelta(minutes=minutes)).isoformat()}]}
    calendar_mode = availability is not None
    windows = windows_from(availability, now) if calendar_mode else []
    cursors, blocks, last_end = [start for start, _ in windows], [], None
    for rank, item in enumerate(queue, 1):
        item["rank"] = rank
        left = item["remaining_minutes"]
        cutoff = instant(item["planning_cutoff"]) if item["planning_cutoff"] else None
        for index, (_, end) in enumerate(windows):
            cursor = cursors[index]
            if last_end is not None:
                cursor = max(cursor, last_end + timedelta(minutes=rest))
            limit = min(end, cutoff) if cutoff else end
            while left > 0 and cursor < limit:
                duration = min(left, block_size, math.floor((limit - cursor).total_seconds() / 60))
                if duration < 1:
                    break
                finish = cursor + timedelta(minutes=duration)
                blocks.append({"id": item["id"], "course": item["course"], "title": item["title"],
                               "start": cursor.astimezone(zone).isoformat(), "end": finish.astimezone(zone).isoformat(),
                               "minutes": duration})
                left -= duration
                last_end = finish
                cursor = finish + timedelta(minutes=rest)
            cursors[index] = max(cursors[index], cursor)
        item["scheduled_minutes"] = item["remaining_minutes"] - left
        item["unscheduled_minutes"] = left
        if calendar_mode and left:
            item["unscheduled_reason"] = ("截止时间或保守目标已过，需核实后再安排"
                                            if cutoff and cutoff <= now else "已确认的空闲时段不足，或截止前没有可用时段")
    pending_count = (sum(1 for record in pending_review if not record.get("resolved", False))
                     if isinstance(pending_review, list) else int(pending_review or 0))
    risk_summary = {level: sum(1 for item in queue if item.get("risk_level") == level)
                    for level in ("critical", "high", "medium", "low")}
    if pending_count:
        risk_summary["review"] = pending_count
    return {"generated_at": now.astimezone(zone).isoformat(), "timezone": state["timezone"],
            "mode": "current_session" if minutes is not None else ("explicit_windows" if calendar_mode else "ranked_queue"),
            "session_minutes": minutes, "latest_sync": state.get("latest_sync"),
            "pending_review_count": pending_count, "risk_summary": risk_summary,
            "queue": queue, "excluded": [item for item in items if item["excluded"]], "blocks": blocks,
            "free_windows": [{"start": s.astimezone(zone).isoformat(), "end": e.astimezone(zone).isoformat()} for s, e in windows],
            "max_block_minutes": block_size, "break_minutes": rest,
            "unscheduled_minutes": sum(item["unscheduled_minutes"] for item in queue)}


def markdown(plan):
    def safe(value):
        return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    latest = plan.get("latest_sync")
    lines = ["# Jupiter 作业计划", "", f"生成时间：{plan['generated_at']}", ""]
    if latest:
        coverage = latest["coverage"]
        lines += [f"最近读取：{latest['observed_at']}。" + ("已覆盖本次指定的可见课程列表。" if coverage["complete"] else "扫描尚未覆盖本次指定的可见课程列表；清单可能不完整。"), ""]
        lines += ["本次已检查课程：" + ("、".join(safe(course) for course in coverage["courses"]) or "未记录") + "。", ""]
        if coverage.get("notes"):
            notes = coverage["notes"]
            lines += ["覆盖说明：" + safe("；".join(map(str, notes)) if isinstance(notes, list) else notes), ""]
    else:
        lines += ["尚未读取 Jupiter；空清单不代表没有作业。", ""]
    if plan["mode"] == "current_session":
        lines += [f"## 现在的 {plan['session_minutes']} 分钟", "",
                  "仅安排这一次可用时间，不据此推断以后的空闲时间。", ""]
        selected = {block["id"] for block in plan["blocks"]}
        for block in plan["blocks"]:
            lines.append(f"- {block['start']} → {block['end']}：{safe(block['course'])} / {safe(block['title'])}（{block['minutes']} 分钟）")
        for item in plan["queue"]:
            if item["id"] in selected:
                lines.append(f"- {safe(item['title'])}：{safe(item['due_label'])}。优先级依据：{safe(item.get('priority_reason', ''))}。" + safe("；".join(item["notes"])))
        if not plan["blocks"]:
            lines.append("当前时段内没有可安全安排的作业；需要先核实截止信息，或增加已确认的空闲时间。")
        risks = [item for item in plan["queue"] if item["id"] not in selected
                 and item.get("planning_cutoff") and instant(item["planning_cutoff"]) <= instant(plan["generated_at"])]
        for item in risks[:2]:
            lines.append(f"- 需核实：{safe(item['title'])}，{safe(item['due_label'])}。")
        if len(plan["blocks"]) > 1:
            lines.append(f"\n工作段之间至少休息 {plan['break_minutes']} 分钟。")
        return "\n".join(lines) + "\n"
    if plan["mode"] == "ranked_queue":
        lines += ["尚未提供具体空闲时段，以下按截止时间排序，不预设每天做作业的时间。未知截止日期单独标记。", ""]
    else:
        lines += [f"仅使用已确认的空闲时段；每段最多 {plan['max_block_minutes']} 分钟，段间至少休息 {plan['break_minutes']} 分钟。", ""]
    lines += ["| 顺序 | 课程 / 作业 | 截止信息 | 剩余用时 | 说明 |", "|---|---|---|---|---|"]
    for item in plan["queue"]:
        duration = str(item["remaining_minutes"]) + " 分钟"
        if item["effective_estimate_source"] == "provisional" and not item["remaining_is_user_override"]:
            duration += "（暂估）"
        explanation = "；".join(filter(None, [item.get("priority_reason"), "；".join(item["notes"])]))
        lines.append(f"| {item['rank']} | {safe(item['course'])} / {safe(item['title'])} | {safe(item['due_label'])} | {duration} | {safe(explanation)} |")
    if not plan["queue"]:
        lines += ["", "当前记录中没有待安排的作业。"]
    if plan["mode"] == "explicit_windows":
        lines += ["", "## 已安排时间", ""]
        for block in plan["blocks"]:
            lines.append(f"- {block['start']} → {block['end']}：{safe(block['course'])} / {safe(block['title'])}（{block['minutes']} 分钟）")
        if not plan["blocks"]:
            lines.append("已确认的时间内没有可安排的作业时段。")
        lines += ["", f"尚未安排：{plan['unscheduled_minutes']} 分钟。", ""]
        for item in plan["queue"]:
            if item["unscheduled_minutes"]:
                lines.append(f"- {safe(item['title'])}：{item['unscheduled_minutes']} 分钟；{item['unscheduled_reason']}。")
    omitted = [item for item in plan["excluded"] if item["planning_disposition"] != "active"]
    if omitted:
        lines += ["", "## 保留记录，不安排课外用时", ""]
        lines.extend(f"- {safe(item['course'])} / {safe(item['title'])}：{safe(item['due_label'])}；{safe(item['exclusion_reason'])}（Jupiter 状态：{item['source_status']}）" for item in omitted)
    pending = [item for item in plan["excluded"] if item["planning_disposition"] == "active" and item["personal_status"] == "completed" and item["source_status"] not in {"submitted", "completed"}]
    if pending:
        lines += ["", "## 本地已完成，提交待确认", ""]
        lines.extend(f"- {safe(item['course'])} / {safe(item['title'])}：{safe(item['due_label'])}（Jupiter 状态：{item['source_status']}）" for item in pending)
    lines += ["", "日期型截止信息只提供日期：计划采用前一天结束前完成的保守目标，并未把截止时间认定为 23:59。用时暂估可随实际进度更新。", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sync_parser = commands.add_parser("sync", help="Merge an explicitly collected Jupiter snapshot")
    sync_parser.add_argument("--state", required=True)
    sync_parser.add_argument("--snapshot", type=Path, required=True)
    update_parser = commands.add_parser("update", help="Record personal progress without changing Jupiter")
    update_parser.add_argument("--state", required=True)
    update_parser.add_argument("--id", required=True)
    update_parser.add_argument("--estimate", type=int)
    update_parser.add_argument("--remaining", type=int)
    update_parser.add_argument("--status", choices=["open", "completed"])
    plan_parser = commands.add_parser("plan", help="Rank tasks, optionally using explicit free time")
    plan_parser.add_argument("--state", required=True)
    windows = plan_parser.add_mutually_exclusive_group()
    windows.add_argument("--availability", type=Path)
    windows.add_argument("--minutes", type=int)
    plan_parser.add_argument("--now", help="ISO timestamp with offset; defaults to the current instant")
    plan_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "sync":
            result = sync(args.state, read_json(args.snapshot))
        elif args.command == "update":
            if args.estimate is None and args.remaining is None and args.status is None:
                raise ValueError("Provide --estimate, --remaining or --status")
            result = update(args.state, args.id, args.estimate, args.remaining, args.status)
        else:
            if args.availability is not None and not args.availability.is_file():
                raise ValueError("Availability file does not exist")
            result = make_plan(args.state, args.now, read_json(args.availability) if args.availability else None, args.minutes)
            json_path = args.output.with_suffix(".json")
            if json_path == args.output:
                json_path = args.output.with_name(args.output.stem + ".plan.json")
            atomic_write(args.output, markdown(result))
            write_json(json_path, result)
            result = {"mode": result["mode"], "active_tasks": len(result["queue"]),
                      "scheduled_blocks": len(result["blocks"]), "unscheduled_minutes": result["unscheduled_minutes"],
                      "markdown": str(args.output), "json": str(json_path)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, TypeError, KeyError, OSError) as error:
        parser.exit(2, f"Error: {error}\n")


if __name__ == "__main__":
    main()
