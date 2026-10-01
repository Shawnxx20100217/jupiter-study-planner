"""Pure-rule import of already collected Jupiter rows; no network, browser or AI.

build_snapshot(raw, planner_state, now=None) returns a planner-compatible snapshot
plus import_review and import_metadata. Persist import_metadata separately and
attach it to planner_state['import_metadata'] on the next run for notice deduping.
Unqualified school-year dates are deliberately unresolved when still ambiguous.
"""
from copy import deepcopy
from datetime import date, datetime
import hashlib
import re
from zoneinfo import ZoneInfo


class JupiterImportError(ValueError):
    pass


_MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_DAYS = {name: number for number, name in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))}
_NOTICE_KEYS = ("id", "author", "date_text", "title", "text", "read", "unread", "is_read", "seen")
_NOTICE_CONTENT_KEYS = ("id", "author", "title", "text")


def completion_mode(row_or_title, category=None):
    """Classify Jupiter writeback eligibility, never personal completion itself.

    Submission evidence takes precedence over a personal Done control or an
    explicit local designation. A course category or a title such as
    "presentation" cannot establish that no submission is required.
    """
    row = row_or_title if isinstance(row_or_title, dict) else {
        "title": row_or_title, "category": category}
    text = " ".join(str(row.get(field) or "") for field in
                    ("title", "category", "description", "notes", "instructions",
                     "requirements", "submission_instructions")).casefold()
    submission = re.search(
        r"\b(?:submit(?:ted|ting|s)?|submission|upload(?:ed|ing|s)?|"
        r"turn\s+in|hand\s+in|attach(?:ment|ments|ed|ing)?|"
        r"document|essay|paper|report|forum|discussion|response|worksheet|"
        r"written\s+answers?)\b|提交|上传|论坛|讨论帖|书面答复|文档", text)
    if (row.get("requires_submission") is True or
            row.get("completion_mode") == "submission" or submission):
        return "submission"
    # Only collector/user supplied designations or an observed private control
    # can authorize Jupiter writeback. Missing evidence remains unknown.
    if row.get("completion_mode") == "local" or type(row.get("personal_done")) is bool:
        return "local"
    return "unknown"


def _instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
        raise JupiterImportError("An explicit timezone offset is required")
    return result


def _review(items, kind, task_id, message, **evidence):
    items.append({"type": kind, "id": task_id, "message": message, **evidence})


def _years(value):
    match = re.fullmatch(r"(\d{4})(?:[-/](\d{2}|\d{4}))?", str(value or ""))
    if not match:
        return []
    start = int(match[1])
    end = start if match[2] is None else int(match[2])
    if match[2] is not None and len(match[2]) == 2:
        end += start // 100 * 100
        if end < start:
            end += 100
    return list(range(start, end + 1)) if 0 <= end - start <= 1 else []


def _date_parts(text):
    """Return month/day/optional explicit year/optional weekday without guessing."""
    text = " ".join(str(text or "").split())
    weekday = None
    for word in re.findall(r"[A-Za-z]+", text):
        if word[:3].lower() in _DAYS:
            weekday = _DAYS[word[:3].lower()]
            break
    match = re.search(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)", text)
    if match:
        return int(match[2]), int(match[3]), int(match[1]), weekday
    match = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?(?!\d)", text)
    if match:
        return int(match[1]), int(match[2]), int(match[3]) if match[3] else None, weekday
    match = re.search(r"\b([A-Za-z]+)\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?\b", text)
    if match and match[1][:3].lower() in _MONTHS:
        return _MONTHS[match[1][:3].lower()], int(match[2]), int(match[3]) if match[3] else None, weekday
    return None


def _old_day(source, zone):
    if source.get("due_at"):
        return _instant(source["due_at"]).astimezone(zone).date()
    return date.fromisoformat(source["due_date"]) if source.get("due_date") else None


def _deadline(row, old, school_year, zone, review, task_id):
    raw_date = row.get("date")
    prior_day = _old_day(old, zone)
    parts = _date_parts(raw_date)
    resolved, conflict = None, None
    confirmed = row.get("confirmed_due_date")
    if confirmed:
        try:
            resolved = date.fromisoformat(confirmed)
            if resolved.isoformat() != confirmed:
                raise ValueError()
        except (ValueError, TypeError):
            raise JupiterImportError("confirmed_due_date must be YYYY-MM-DD") from None
        if parts and ((resolved.month, resolved.day) != parts[:2]
                      or (parts[2] and resolved.year % (100 if parts[2] < 100 else 10000) != parts[2])
                      or (parts[3] is not None and resolved.weekday() != parts[3])):
            conflict = {"confirmed_due_date": confirmed, "raw_due": raw_date,
                        "previous_due_date": prior_day.isoformat() if prior_day else None}
            resolved = None
            _review(review, "deadline_conflict", task_id, "课程日期与已核实日历日期不一致，暂不安排截止时间。", **conflict)
    elif parts:
        month, day, year, weekday = parts
        candidates = [year] if year and year >= 100 else _years(school_year)
        if year and year < 100:
            candidates = [candidate for candidate in candidates if candidate % 100 == year]
        valid = []
        for candidate in candidates:
            try:
                candidate_day = date(candidate, month, day)
                if weekday is None or candidate_day.weekday() == weekday:
                    valid.append(candidate_day)
            except ValueError:
                pass
        if prior_day and (prior_day.month, prior_day.day) == (month, day) and (
                year is None or prior_day.year % (100 if year < 100 else 10000) == year) and (
                weekday is None or prior_day.weekday() == weekday):
            resolved = prior_day
        elif len(valid) == 1:
            resolved = valid[0]
        else:
            _review(review, "date_ambiguous", task_id, "日期年份尚不能唯一确定；不按固定开学月份猜测。",
                    raw_due=raw_date, candidates=[item.isoformat() for item in valid])
    elif raw_date:
        _review(review, "date_unrecognized", task_id, "当前日期原文尚无法可靠归一化。", raw_due=raw_date)
    elif prior_day:
        # No new date is not evidence that an explicitly recorded deadline changed.
        return {"due_date": old.get("due_date"), "due_at": old.get("due_at"),
                "due_precision": old["due_precision"], "raw_due": old.get("raw_due", ""),
                "deadline_verification": "previous_evidence_not_reconfirmed"}
    if resolved and old.get("due_at") and resolved == prior_day:
        return {"due_date": old.get("due_date"), "due_at": old["due_at"], "due_precision": "time",
                "raw_due": old.get("raw_due") or raw_date or "", "deadline_verification": "date_reconfirmed_exact_time_from_prior_evidence"}
    result = {"due_date": resolved.isoformat() if resolved else None, "due_at": None,
              "due_precision": "date" if resolved else "unknown", "raw_due": raw_date or ""}
    if conflict:
        result["deadline_conflict"] = conflict
    if old.get("due_at") and (resolved != prior_day):
        evidence = {"previous_due_at": old["due_at"], "current_due_date": result["due_date"], "raw_due": raw_date}
        result["deadline_conflict"] = {**(conflict or {}), **evidence}
        _review(review, "exact_deadline_needs_review", task_id, "日期信息有变，旧截止时刻不再沿用；请核实新的具体时间。", **evidence)
    return result


def _disposition(source, old, display, today, review):
    disposition = old.get("planning_disposition", "active")
    reason = None
    imported_reason = old.get("import_disposition_reason")
    original_note = old.get("import_original_planning_note")
    imported_note = "；".join(value for value in (original_note, imported_reason) if value)
    # Only reverse a still-intact automatic classification. A different disposition
    # or edited planning note is evidence of a later manual decision to preserve.
    if (display == "missing" and disposition == "reference" and imported_reason
            and old.get("planning_note") == imported_note):
        disposition = "active"
        for field in ("import_disposition_reason", "import_original_planning_note"):
            source.pop(field, None)
        if original_note is None:
            source.pop("planning_note", None)
        else:
            source["planning_note"] = original_note
    category = str(source.get("category") or "").strip().casefold()
    current_day = date.fromisoformat(source["due_date"]) if source.get("due_date") else None
    if source.get("due_at"):
        current_day = _instant(source["due_at"]).astimezone(today[1]).date()
    # Preserve explicit prior planning decisions, including classroom work and superseded notices.
    if disposition == "active" and display != "missing":
        if display == "excused":
            disposition, reason = "reference", "Jupiter 明确显示免做，保留记录，不计入工作量。"
        elif display == "information" or category == "information":
            disposition, reason = "reference", "Jupiter 明确标记为信息项，不计入工作量。"
        elif category == "participation":
            disposition, reason = "reference", "Jupiter 分类为 Participation 记分项，不据此新建作业工作量。"
        elif display == "graded":
            disposition, reason = "reference", "Jupiter 已评分；提交或完成证据仍需核实，不据此认定欠交。"
        elif not old and source.get("source_row_date") and source["due_precision"] == "unknown":
            disposition, reason = "reference", "新记录的日期尚不能确定属于历史还是待办，保留待核实，暂不计入工作量。"
        elif not old and current_day and current_day < today[0] and source.get("is_missing") is not True and source["status"] not in {"submitted", "completed"}:
            disposition, reason = "reference", "历史项目没有明确未完成或缺交证据，保留待核实，不计入当前工作量。"
            _review(review, "historical_unconfirmed", source["id"], reason)
    if display == "missing" and disposition != "active":
        _review(review, "missing_disposition_review", source["id"], "Jupiter 明确标记缺交，但已有非待办分类；保留原分类，请核实。")
    source["planning_disposition"] = disposition
    if reason:
        original = old.get("import_original_planning_note", old.get("planning_note"))
        source["import_original_planning_note"] = original
        source["import_disposition_reason"] = reason
        source["planning_note"] = "；".join(value for value in (original, reason) if value)


def build_snapshot(raw, state, now=None):
    """Convert collected rows to a snapshot without mutating raw or persisted state."""
    zone = ZoneInfo(raw["timezone"])
    observed = _instant(raw["observed_at"])
    today = (_instant(now) if now is not None else observed).astimezone(zone).date()
    coverage = deepcopy(raw["coverage"])
    if not isinstance(coverage.get("complete"), bool) or not isinstance(coverage.get("courses"), list):
        raise JupiterImportError("coverage requires complete and courses")
    sources = {task_id: deepcopy(record["source"]) for task_id, record in state.get("tasks", {}).items()}
    review, tasks, consumed, skipped = [], {}, set(), 0
    seen_raw = {}
    for course in raw["courses"]:
        name = course["name"]
        if not isinstance(name, str) or not name.strip() or not isinstance(course.get("rows"), list):
            raise JupiterImportError("Each course requires a name and rows")
        for row in course["rows"]:
            assignment_id, title = str(row["id"]), row["title"]
            if not assignment_id.isdigit() or not isinstance(title, str) or not title.strip():
                raise JupiterImportError("Rows require a numeric assignment ID and non-empty title")
            if row.get("course", name) != name:
                raise JupiterImportError("Row course does not match its containing course")
            key = (name, assignment_id)
            if key in seen_raw:
                if seen_raw[key] != row:
                    raise JupiterImportError("Conflicting duplicate assignment rows")
                continue
            seen_raw[key] = row
            matches = [key for key, source in sources.items() if not source.get("derived")
                       and source.get("course") == name and str(source.get("source_assignment_id", "")) == assignment_id]
            if not matches:
                matches = [key for key, source in sources.items() if not source.get("derived")
                           and not source.get("source_assignment_id") and source.get("planning_disposition") != "superseded"
                           and source.get("course") == name and source.get("title") == title]
            if len(matches) > 1:
                _review(review, "identity_ambiguous", name + ":" + assignment_id, "历史任务存在多个身份候选，本行暂不合并。", candidate_ids=matches)
                coverage["complete"] = False
                continue
            task_id = matches[0] if matches else "jupiter:" + hashlib.sha256(name.encode()).hexdigest()[:12] + ":" + assignment_id
            if task_id in consumed:
                _review(review, "identity_collision", task_id, "多个当前任务指向同一历史任务，需人工核对。")
                coverage["complete"] = False
                tasks.pop(task_id, None)
                continue
            consumed.add(task_id)
            old = sources.get(task_id, {})
            display = row.get("source_display_status", "unknown")
            mode = completion_mode(row)
            if not old and display == "graded":
                skipped += 1  # Do not import a course's entire scored history.
                continue
            source = deepcopy(old)
            for field in ("deadline_conflict", "deadline_verification"):
                source.pop(field, None)
            source.update(id=task_id, course=name, title=title, source_assignment_id=assignment_id,
                          source_display_status=display, category=row.get("category", ""),
                          completion_mode=mode,
                          source_teacher=course.get("teacher"), source_row_date=row.get("date"))
            # A private tick is personal bookkeeping, even on submission work.
            # No observation is not evidence of an unchecked control.
            if type(row.get("personal_done")) is bool:
                source["personal_done"] = row["personal_done"]
            source.update(_deadline(row, old, raw.get("school_year"), zone, review, task_id))
            source.setdefault("source_url", "https://login.jupitered.com/")
            source.setdefault("kind", "assignment")
            source.setdefault("estimated_minutes", None)
            source.setdefault("estimate_source", "provisional")
            if display == "missing":
                source.update(status="open", is_missing=True)
            elif display in {"submitted", "completed"}:
                source.update(status=display, is_missing=False)
            else:
                source["status"] = old["status"] if old.get("status") in {"submitted", "completed"} else "unknown"
                source["is_missing"] = False if display == "excused" else old.get("is_missing")
            _disposition(source, old, display, (today, zone), review)
            tasks[task_id] = source
    for task_id, source in sources.items():
        if task_id not in tasks and (source.get("derived") or source.get("planning_disposition") == "superseded"):
            source["import_retained"] = True
            source.setdefault("source_evidence_last_seen_at", state["tasks"][task_id].get("last_seen"))
            source["import_retention_note"] = "保留既有派生或替代关系；本次没有把它认定为重新读取到的原始课程行。"
            tasks[task_id] = source
    metadata = deepcopy(state.get("import_metadata", {}))
    notices = metadata.setdefault("notices", {})
    for incoming in raw.get("notices", []):
        notice = {key: incoming.get(key, "") for key in _NOTICE_KEYS if key in incoming}
        notice_id = str(notice["id"])
        if not notice_id:
            raise JupiterImportError("Notice ID is required")
        notice["id"] = notice_id
        previous_notice = notices.get(notice_id)
        # "Today" becoming "Yesterday" is display drift, not changed content.
        if previous_notice is None or any(previous_notice.get(key, "") != notice[key] for key in _NOTICE_CONTENT_KEYS):
            kind = "notice_added" if notice_id not in notices else "notice_changed"
            _review(review, kind, notice_id, "教师通知有新增或变化，保留原文待解读；尚未据此创建截止日期。", notice=notice)
        notices[notice_id] = notice
    metadata.update(observed_at=raw["observed_at"], skipped_new_graded=skipped)
    return {"observed_at": raw["observed_at"], "timezone": raw["timezone"], "coverage": coverage,
            "tasks": list(tasks.values()), "import_review": review, "import_metadata": metadata}
