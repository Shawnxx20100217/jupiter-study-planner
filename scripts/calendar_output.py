#!/usr/bin/env python3
"""Render planner JSON as a local RFC 5545 calendar and Chinese Markdown view."""
import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

UTC = timezone.utc


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_json(path, default=None):
    path = Path(path)
    return load(path) if path.exists() else default


def esc(value):
    value = str(value or "").replace("\\", "\\\\")
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    return value.replace(";", "\\;").replace(",", "\\,")


def ics_escape(value):
    return esc(value)


def fold(line):
    """Fold one iCalendar content line at <=75 UTF-8 octets, safely."""
    chunks, current, budget = [], "", 75
    for char in str(line):
        size = len(char.encode("utf-8"))
        if current and len(current.encode("utf-8")) + size > budget:
            chunks.append(current)
            current, budget = " ", 74
        current += char
    if current or not chunks:
        chunks.append(current)
    return "\r\n".join(chunks)


def instant(value):
    if not isinstance(value, str):
        raise ValueError("Calendar timestamps must be ISO strings with an offset")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Calendar timestamps must include a UTC offset")
    return result


def uid(prefix, value):
    return hashlib.sha256((prefix + ":" + str(value)).encode("utf-8")).hexdigest()[:32] + "@jupiter-study-planner"


def _text(value):
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def _utc(value):
    return instant(value).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _items(plan):
    seen, result = set(), []
    for item in plan.get("queue", []):
        task_id = item.get("id")
        if task_id in seen or item.get("excluded") or item.get("planning_disposition", "active") != "active":
            continue
        seen.add(task_id)
        result.append(item)
    return result


def _include_date_only(state, include_date_only):
    if include_date_only is not None:
        return include_date_only
    config_path = state if isinstance(state, (str, Path)) else None
    config = read_json(Path(config_path) / "config.json", {}) if config_path else {}
    value = (config or {}).get("include_date_only_events", False)
    if not isinstance(value, bool):
        raise ValueError("config.include_date_only_events must be boolean")
    return value


def _event_lines(event):
    lines = ["BEGIN:VEVENT", "UID:" + event["uid"], "DTSTAMP:" + event["stamp"]]
    if event.get("date_only"):
        lines += ["DTSTART;VALUE=DATE:" + event["start"], "DTEND;VALUE=DATE:" + event["end"]]
    else:
        lines.append("DTSTART:" + event["start"])
        if event.get("end"):
            lines.append("DTEND:" + event["end"])
    lines += ["SUMMARY:" + esc(event["summary"]), "DESCRIPTION:" + esc(event["description"]),
              "X-JUPITER-TYPE:" + event["type"], "END:VEVENT"]
    return lines


def _block_events(plan):
    events, seen = [], set()
    stamp = _utc(plan["generated_at"])
    for block in plan.get("blocks", []):
        start, end = instant(block["start"]), instant(block["end"])
        if end <= start:
            raise ValueError("A local planning block must end after it starts")
        # Canonical UTC instants make equivalent offsets produce the same UID.
        key = f"{block.get('id', '')}|{start.astimezone(UTC).isoformat()}|{end.astimezone(UTC).isoformat()}"
        event_uid = uid("block", key)
        if event_uid in seen:
            continue
        seen.add(event_uid)
        events.append({"uid": event_uid, "stamp": stamp,
                       "start": _utc(block["start"]), "end": _utc(block["end"]),
                       "summary": f"学习：{_text(block.get('course'))} / {_text(block.get('title'))}",
                       "description": "本地规划块；不会写回 Jupiter。", "type": "LOCAL_PLAN"})
    return events


def _deadline_events(plan, include_date_only):
    events, stamp = [], _utc(plan["generated_at"])
    for item in _items(plan):
        task_id = item.get("id", "")
        summary = f"截止：{_text(item.get('course'))} / {_text(item.get('title'))}"
        precision = item.get("due_precision") or ("time" if item.get("due_at") else "date" if item.get("due_date") else "unknown")
        if precision == "time" and item.get("due_at"):
            when = instant(item["due_at"])
            # Keep one stable calendar item per assignment as its deadline changes.
            events.append({"uid": uid("deadline", task_id),
                           "stamp": stamp, "start": _utc(item["due_at"]), "end": None,
                           "summary": summary, "description": "Jupiter 截止时间（来源精确时间）。", "type": "DEADLINE"})
        elif include_date_only and precision == "date" and item.get("due_date"):
            try:
                day = date.fromisoformat(item["due_date"])
            except (TypeError, ValueError):
                continue
            events.append({"uid": uid("deadline", task_id), "stamp": stamp,
                           "start": day.strftime("%Y%m%d"), "end": (day + timedelta(days=1)).strftime("%Y%m%d"),
                           "summary": summary + "（日期；时间待确认）", "description": "Jupiter 截止日期；具体截止时间未提供。",
                           "type": "DATE_ONLY_DEADLINE", "date_only": True})
    return events


def calendar_ics(plan, state=None, include_date_only_events=None):
    if not isinstance(plan, dict) or not isinstance(plan.get("generated_at"), str):
        raise ValueError("A plan with generated_at is required")
    instant(plan["generated_at"])
    include = _include_date_only(state, include_date_only_events) if state is not None else include_date_only_events
    events = _block_events(plan) + _deadline_events(plan, include)
    output = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Jupiter Study Planner//EN", "CALSCALE:GREGORIAN",
              "METHOD:PUBLISH", "X-WR-CALNAME:" + esc("Jupiter 本地作业日历")]
    for event in events:
        output.extend(_event_lines(event))
    output.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in output) + "\r\n"


def ics(state, plan, include_date_only=True):
    """Backward-compatible wrapper; CLI uses the safer config-controlled default."""
    return calendar_ics(plan, state=state, include_date_only_events=include_date_only)


def _date_info(item, zone):
    precision = item.get("due_precision") or ("time" if item.get("due_at") else "date" if item.get("due_date") else "unknown")
    if precision == "time" and item.get("due_at"):
        when = instant(item["due_at"]).astimezone(zone)
        return when.date(), when.strftime("%H:%M"), "Jupiter 精确截止时间"
    if precision == "date" and item.get("due_date"):
        try:
            return date.fromisoformat(item["due_date"]), "", "Jupiter 仅提供日期，具体时间待确认"
        except ValueError:
            pass
    return None, "", "Jupiter 截止日期未知，需核实"


def calendar_markdown(plan):
    zone = ZoneInfo(plan.get("timezone", "UTC"))
    queue_items = _items(plan)
    queue_ids = {item.get("id") for item in queue_items}
    all_items = queue_items + [item for item in plan.get("excluded", []) if item.get("id") not in queue_ids]
    grouped, unknown = {}, []
    for item in all_items:
        day, clock, label = _date_info(item, zone)
        entry = {"item": item, "clock": clock, "label": label}
        (grouped.setdefault(day, []) if day else unknown).append(entry)
    blocks = {}
    for block in plan.get("blocks", []):
        start, end = instant(block["start"]).astimezone(zone), instant(block["end"]).astimezone(zone)
        blocks.setdefault(start.date(), []).append((start, end, block))
    lines = ["# Jupiter 超级日历", "", f"更新时间：{plan.get('generated_at', '未知')}", "",
             "Jupiter 截止日期与本地规划块分开显示；未知日期不会写入 ICS。", "", "## Jupiter 截止日期", ""]
    if grouped:
        for year, month in sorted({(day.year, day.month) for day in grouped}):
            lines.append(f"### {year}年{month}月")
            for day in sorted(d for d in grouped if (d.year, d.month) == (year, month)):
                lines.append(f"#### {day.isoformat()}")
                for entry in sorted(grouped[day], key=lambda x: (x["clock"], x["item"].get("course", ""), x["item"].get("title", ""))):
                    item = entry["item"]
                    lines.append(f"- {entry['clock'] + '，' if entry['clock'] else ''}{item.get('course', '')} / {item.get('title', '')}（{entry['label']}；状态：{item.get('source_status', item.get('status', ''))}；规划：{item.get('planning_disposition', 'active')}）")
    else:
        lines.append("暂无已知日期的 Jupiter 截止项。")
    if unknown:
        lines += ["", "### 日期待核实", ""]
        for entry in sorted(unknown, key=lambda x: (x["item"].get("course", ""), x["item"].get("title", ""))):
            item = entry["item"]
            lines.append(f"- {item.get('course', '')} / {item.get('title', '')}（{entry['label']}）")
    lines += ["", "## 本地学习安排（本地规划块）", ""]
    if blocks:
        for day in sorted(blocks):
            lines.append(f"### {day.isoformat()}")
            for start, end, block in sorted(blocks[day], key=lambda x: x[0]):
                minutes = block.get("minutes", int((end - start).total_seconds() / 60))
                lines.append(f"- {start.strftime('%H:%M')}–{end.strftime('%H:%M')}：{block.get('course', '')} / {block.get('title', '')}（本地安排 {minutes} 分钟）")
    else:
        lines.append("暂无本地规划块；队列模式只排序任务，不虚构时间。")
    return "\n".join(lines) + "\n"


def markdown(state, plan=None):
    """Backward-compatible wrapper retaining the old (state, plan) API."""
    if plan is None:
        plan = state
    return calendar_markdown(plan)


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def write_calendar(plan_path, markdown_path, ics_path, state_dir=None, include_date_only_events=None):
    plan = load(plan_path)
    md, calendar = calendar_markdown(plan), calendar_ics(plan, state=state_dir, include_date_only_events=include_date_only_events)
    atomic_write(markdown_path, md); atomic_write(ics_path, calendar)
    return {"markdown": str(markdown_path), "ics": str(ics_path), "events": calendar.count("BEGIN:VEVENT")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest="command", required=True).add_parser("calendar")
    command.add_argument("--state", required=True, type=Path)
    command.add_argument("--plan", required=True, type=Path)
    command.add_argument("--markdown", required=True, type=Path)
    command.add_argument("--ics", required=True, type=Path)
    command.add_argument("--include-date-only-events", action="store_true")
    command.add_argument("--no-date-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        include = False if args.no_date_only else (True if args.include_date_only_events else None)
        result = write_calendar(args.plan, args.markdown, args.ics, args.state, include)
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(2, f"Error: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
