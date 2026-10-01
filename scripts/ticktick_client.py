#!/usr/bin/env python3
"""Small, deterministic TickTick Open API client and Jupiter task publisher.

Jupiter remains the source of assignment facts while completion flags can be
mirrored in both directions.  A private mapping file makes repeated syncs
idempotent, and no task is deleted when Jupiter has a partial or failed read.
"""
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, time, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


DEFAULT_BASE_URL = "https://api.ticktick.com/open/v1"
DEFAULT_REMINDERS = (1440, 120, 30)


class TickTickError(RuntimeError):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def atomic_json(path, value):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _clean_base(url):
    url = str(url or DEFAULT_BASE_URL).strip().rstrip("/")
    if not url.startswith(("https://", "http://")):
        raise ValueError("TickTick API 地址必须使用 http 或 https")
    return url


class TickTickClient:
    def __init__(self, token, base_url=DEFAULT_BASE_URL, timeout=20, opener=None):
        if not token or not isinstance(token, str):
            raise TickTickError("TickTick 尚未配置访问令牌", code="not_configured")
        self.token = token.strip()
        self.base_url = _clean_base(base_url)
        self.timeout = timeout
        self.opener = opener or urlopen

    def request(self, method, path, payload=None):
        url = self.base_url + "/" + str(path).lstrip("/")
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=body, method=method.upper(), headers={
            "Authorization": "Bearer " + self.token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Jupiter-Study-Planner/0.1",
        })
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read()
                if not raw:
                    return {}
                try:
                    return json.loads(raw.decode("utf-8"))
                except ValueError as exc:
                    raise TickTickError("TickTick 返回了无法解析的数据", status=response.status, code="invalid_response") from exc
        except HTTPError as exc:
            raw = exc.read(4096).decode("utf-8", errors="replace")
            detail = raw[:240]
            lowered = detail.lower()
            capacity = exc.code in {400, 413, 422} and any(word in lowered for word in (
                "capacity", "limit", "maximum", "max ", "too many", "exceed"))
            code = ("capacity" if capacity else
                    "unauthorized" if exc.code == 401 else
                    "rate_limited" if exc.code == 429 else "api_error")
            # Server bodies can contain task/account data.  Classify locally,
            # but never put the raw response in UI status, logs or chat output.
            explanation = {"capacity": "任务容量已达上限", "unauthorized": "连接已失效，请重新授权",
                           "rate_limited": "请求过于频繁，请稍后重试"}.get(code, "服务暂时无法完成请求")
            raise TickTickError(f"TickTick 请求失败（{exc.code}）：{explanation}", status=exc.code, code=code) from None
        except URLError as exc:
            raise TickTickError("无法连接 TickTick，请检查网络或 API 地址", code="network") from exc
        except TimeoutError as exc:
            raise TickTickError("TickTick 请求超时", code="timeout") from exc

    def list_projects(self):
        return self.request("GET", "project")

    def project_data(self, project_id):
        return self.request("GET", "project/" + quote(str(project_id), safe="") + "/data")

    def get_task(self, project_id, task_id):
        return self.request("GET", "project/" + quote(str(project_id), safe="") + "/task/" + quote(str(task_id), safe=""))

    def create_task(self, payload):
        return self.request("POST", "task", payload)

    def update_task(self, task_id, payload):
        return self.request("POST", "task/" + quote(str(task_id), safe=""), {**payload, "id": str(task_id)})

    def complete_task(self, project_id, task_id):
        return self.request("POST", "project/" + quote(str(project_id), safe="") + "/task/" + quote(str(task_id), safe="") + "/complete")

    def reopen_task(self, project_id, task_id):
        """Reopen a task through the normal update endpoint.

        TickTick represents an open task with status ``0``.  Keeping this as a
        small companion to ``complete_task`` lets the private Jupiter Done
        marker propagate in both directions without touching submission data.
        """
        return self.update_task(task_id, {"projectId": str(project_id), "status": 0})


def reminder_trigger(minutes):
    minutes = int(minutes)
    if minutes <= 0:
        return "TRIGGER:PT0S"
    days, remainder = divmod(minutes, 1440)
    hours, mins = divmod(remainder, 60)
    value = "TRIGGER:-P"
    if days:
        value += f"{days}D"
    if hours or mins or not days:
        value += "T"
        if hours:
            value += f"{hours}H"
        if mins or not hours:
            value += f"{mins}M"
    return value


def reminder_triggers(minutes_list=DEFAULT_REMINDERS):
    valid = sorted({int(value) for value in (minutes_list or DEFAULT_REMINDERS) if isinstance(value, int) and value > 0}, reverse=True)
    return [reminder_trigger(value) for value in valid]


def _task_due(item, zone):
    precision = item.get("due_precision")
    if precision == "time" and item.get("due_at"):
        return item["due_at"], False
    if precision == "date" and item.get("due_date"):
        day = date.fromisoformat(item["due_date"])
        # TickTick needs a concrete timestamp for relative reminders.  Keep the
        # task marked all-day and disclose that this is a date-only anchor.
        return datetime.combine(day, time(23, 59, 59), tzinfo=zone).isoformat(), True
    return None, True


def task_payload(item, plan, project_id, config=None):
    config = config or {}
    try:
        zone = ZoneInfo(plan.get("timezone") or config.get("school_timezone", "UTC"))
    except Exception:
        zone = timezone.utc
    due, all_day = _task_due(item, zone)
    marker = "jupiter:" + str(item.get("id", ""))
    course = str(item.get("course", "")).strip()
    title = str(item.get("title", "")).strip()
    content = [marker, f"Jupiter 课程：{course}",
               f"Jupiter 状态：{item.get('source_status', item.get('status', 'unknown'))}"]
    # Keep the handoff to GPT explicit and machine-readable.  This is a
    # private Jupiter checkbox, not teacher submission evidence; unknown is
    # preserved when the collector did not observe the control.
    if item.get("personal_done_observed") is True:
        personal_label = "已完成" if item.get("personal_status") == "completed" else "未完成"
    else:
        personal_label = "未核实"
    content.append(f"Jupiter 私人完成：{personal_label}")
    content.append(f"Jupiter 类型：{item.get('kind', 'assignment')}")
    if item.get("due_label"):
        content.append("截止：" + str(item["due_label"]))
    if item.get("due_precision") == "date":
        content.append("Jupiter 只提供日期，具体截止钟点待确认。")
    if item.get("source_url"):
        content.append("来源：" + str(item["source_url"]))
    if item.get("planning_note"):
        content.append("备注：" + str(item["planning_note"]))
    payload = {
        "projectId": str(project_id),
        "title": (course + " · " if course else "") + title,
        "content": "\n".join(content),
        "isAllDay": bool(all_day),
        "timeZone": str(plan.get("timezone") or config.get("school_timezone", "UTC")),
        "priority": 5 if item.get("priority_band") == "critical" else 3 if item.get("priority_band") == "high" else 1,
        "reminders": reminder_triggers(config.get("ticktick_reminder_minutes", DEFAULT_REMINDERS)),
    }
    if due:
        payload["dueDate"] = due
    return payload


def _payload_hash(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _config(config):
    nested = config.get("ticktick") if isinstance(config.get("ticktick"), dict) else {}
    merged = dict(nested)
    for key in ("ticktick_enabled", "ticktick_project_id", "ticktick_project_name", "ticktick_token_file",
                "ticktick_api_base", "ticktick_reminder_minutes", "ticktick_project_ids",
                "ticktick_destination_scope", "ticktick_sync_mode", "ticktick_source_project_id",
                "ticktick_source_project_ids", "ticktick_source_project_name"):
        if key in config:
            merged[key.removeprefix("ticktick_")] = config[key]
    return merged


def load_token(config, instance_path=None):
    cfg = _config(config)
    env_name = str(cfg.get("token_env", "TICKTICK_ACCESS_TOKEN"))
    if os.environ.get(env_name):
        return os.environ[env_name].strip()
    token_file = cfg.get("token_file")
    if token_file:
        path = Path(token_file).expanduser()
        if instance_path and not path.is_absolute():
            path = Path(instance_path).resolve().parent / path
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        if token:
            return token
    return ""


def _find_project(projects, project_id=None, project_name=None):
    if project_id:
        for project in projects or []:
            if str(project.get("id")) == str(project_id) and not project.get("closed"):
                return project
        # A name is not identity: another list can be created with the same
        # name and hold a separately edited study plan.
        return None
    if project_name:
        for project in projects or []:
            if project.get("name") == project_name and not project.get("closed"):
                return project
    return None


def _project_ids(config, default_id=None):
    """Return every configured destination project, preserving its order.

    The first project is the historical/default destination.  Additional
    projects are useful when a TickTick list is split across the service's
    per-project task limit.  Accept both the nested ``project_ids`` spelling
    and the older top-level ``ticktick_project_ids`` spelling.
    """
    cfg = _config(config)
    values = cfg.get("project_ids")
    if values is None:
        values = cfg.get("ticktick_project_ids")
    if values is None:
        values = []
    if isinstance(values, dict):
        values = list(values.values())
    elif isinstance(values, str):
        values = [values]
    ids = []
    for value in ([default_id] if default_id else []) + list(values or []):
        if isinstance(value, dict):
            value = value.get("id") or value.get("project_id")
        if value and str(value) not in ids:
            ids.append(str(value))
    return ids


def _marker_key(line):
    """Normalize both ``jupiter:id`` and ``Jupiter: id`` task markers."""
    text = str(line or "").strip()
    if ":" not in text:
        return None
    prefix, value = text.split(":", 1)
    if prefix.strip().lower() != "jupiter":
        return None
    value = value.strip()
    return "jupiter:" + value if value else None


def _task_marker(task):
    for line in str((task or {}).get("content", "")).splitlines():
        marker = _marker_key(line)
        if marker:
            return marker
    return None


def _capacity_error(error):
    """Recognize service responses that indicate a task/list capacity limit."""
    if not isinstance(error, TickTickError):
        return False
    if error.code == "capacity":
        return True
    detail = str(error).lower()
    return error.status in {400, 413, 422} and any(word in detail for word in (
        "capacity", "limit", "maximum", "max ", "too many", "exceed"))


def _remote_completed(value):
    return value in {2, "2", "completed", "COMPLETED", True}


def _remote_open(value):
    return value in {0, "0", "open", "OPEN", "normal", "NORMAL"}


def _local_personal_status(state_dir, jupiter_id):
    state = read_json(Path(state_dir) / "state.json", {}) or {}
    return ((state.get("tasks", {}).get(str(jupiter_id), {}) or {}).get("personal", {}) or {}).get("status")


def _set_local_personal_status(state_dir, jupiter_id, status, done_sync_pending=False):
    path = Path(state_dir) / "state.json"
    state = read_json(path, {}) or {}
    record = (state.setdefault("tasks", {}).setdefault(str(jupiter_id), {"personal": {}}))
    personal = record.setdefault("personal", {})
    changed = personal.get("status") != status
    if done_sync_pending and personal.get("done_sync_pending") is not True:
        personal["done_sync_pending"] = True
        changed = True
    if not changed:
        return False
    personal["status"] = status
    personal["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json(path, state)
    return True


def _personal_changed_since_mapping(item, record, personal_status):
    """Tell whether the local private marker changed after the last publish.

    This resolves the hourly race where a fresh Jupiter check must complete an
    open TickTick task, while an unchanged local check should accept a TickTick
    uncheck as the user's newer remote edit.
    """
    if "personal_status" not in record:
        return True
    return (record.get("personal_status") != personal_status or
            bool(record.get("personal_done_observed")) != bool(item.get("personal_done_observed")))


def sync_plan(plan, config, state_dir, instance_path=None, now=None, dry_run=False, client=None,
              mapping_filename="ticktick-state.json"):
    """Upsert active Jupiter tasks into TickTick; never deletes remote tasks."""
    cfg = _config(config)
    if cfg.get("enabled", config.get("ticktick_enabled", False)) is not True:
        return {"ok": True, "enabled": False, "skipped": True, "reason": "disabled", "created": 0, "updated": 0, "completed": 0}
    token = load_token(config, instance_path)
    if not token:
        return {"ok": False, "enabled": True, "skipped": True, "error_code": "ticktick_not_configured", "message": "TickTick 尚未配置 API token。", "created": 0, "updated": 0, "completed": 0}
    configured_project_ids = _project_ids(config, cfg.get("project_id"))
    if not configured_project_ids and not cfg.get("project_name"):
        return {"ok": False, "enabled": True, "skipped": True, "error_code": "ticktick_project_missing", "message": "TickTick 尚未指定目标项目。", "created": 0, "updated": 0, "completed": 0}
    if dry_run:
        return {"ok": True, "enabled": True, "dry_run": True, "created": 0, "updated": 0, "completed": 0,
                "would_sync": len([x for x in plan.get("queue", []) if x.get("planning_disposition", "active") == "active"])}
    client = client or TickTickClient(token, cfg.get("api_base", DEFAULT_BASE_URL))
    state_dir = Path(state_dir).expanduser().resolve()
    mapping_path = state_dir / mapping_filename
    state = read_json(mapping_path, {"version": 1, "tasks": {}}) or {"version": 1, "tasks": {}}
    state.setdefault("tasks", {})
    projects = client.list_projects()
    if not isinstance(projects, list) or any(not isinstance(p, dict) for p in projects):
        raise TickTickError("TickTick 项目清单格式无法识别", code="invalid_response")
    default_project_id = cfg.get("project_id") or (configured_project_ids[0] if configured_project_ids else None)
    project = _find_project(projects, default_project_id, cfg.get("project_name"))
    if not project:
        raise TickTickError("找不到配置的 TickTick 项目", code="project_not_found")
    project_id = str(project["id"])
    requested_project_ids = _project_ids(config, project_id)
    available_project_ids = {str(p.get("id")) for p in projects if p.get("id") and not p.get("closed")}
    configured_project_ids = [value for value in requested_project_ids if value in available_project_ids]
    missing_project_ids = set(requested_project_ids) - set(configured_project_ids)
    strict_destinations = cfg.get("destination_scope") == "configured_only"
    existing = {}
    inventory_by_id = {}
    inventory_read_ok = True
    inventory_error = None
    for inventory_project_id in configured_project_ids:
        try:
            data = client.project_data(inventory_project_id) or {}
            if not isinstance(data, dict):
                raise TickTickError("TickTick 项目任务清单格式无法识别", code="invalid_response")
            tasks = data.get("tasks", [])
            if not isinstance(tasks, list) or any(not isinstance(task, dict) for task in tasks):
                raise TickTickError("TickTick 项目任务清单格式无法识别", code="invalid_response")
            for task in tasks:
                if task.get("id"):
                    inventory_by_id[(str(inventory_project_id), str(task["id"]))] = task
                marker = _task_marker(task)
                if marker and marker not in existing:
                    existing[marker] = {"task": task, "project_id": str(inventory_project_id)}
        except TickTickError as error:
            # Without a reliable inventory, do not create an unmapped task: a
            # successful create with a lost response could otherwise duplicate it.
            inventory_read_ok = False
            inventory_error = error
    created = updated = completed = remote_completed = remote_reopened = local_updated = 0
    deferred = 0
    warnings = []
    if missing_project_ids:
        warnings.append({"code": "configured_project_unavailable",
                         "message": "部分配置清单已删除或关闭，已暂停向这些清单写入。",
                         "count": len(missing_project_ids)})
    if inventory_error:
        warnings.append({"code": "remote_inventory_unavailable",
                         "message": "TickTick 项目任务清单读取失败，本轮暂停无映射任务的新建。",
                         "error_type": type(inventory_error).__name__})
    results = []
    capacity_hit = None

    def checkpoint():
        """Persist the mapping after each remote mutation.

        This is deliberately small and synchronous: if a later task fails,
        already-created/updated TickTick tasks remain recoverable and the next
        run can discover them without creating duplicates.
        """
        state["project_id"] = project_id
        state["project_ids"] = list(configured_project_ids)
        atomic_json(mapping_path, state)

    all_items = []
    seen_ids = set()
    for item in list(plan.get("queue", [])) + list(plan.get("excluded", [])):
        if item.get("id") not in seen_ids:
            all_items.append(item)
            seen_ids.add(item.get("id"))
    for item in all_items:
        if not isinstance(item, dict) or not item.get("id") or item.get("planning_disposition", "active") != "active":
            continue
        jupiter_id = str(item["id"])
        marker = "jupiter:" + jupiter_id
        record = state["tasks"].get(jupiter_id, {})
        task_project_id = str(item.get("ticktick_project_id") or
                             item.get("project_id") or
                             record.get("ticktick_project_id") or
                             record.get("project_id") or project_id)
        remote_id = record.get("ticktick_task_id")
        if not remote_id and marker in existing:
            remote_id = existing[marker]["task"].get("id")
            task_project_id = existing[marker]["project_id"]
        if (task_project_id in missing_project_ids or
                (strict_destinations and task_project_id not in configured_project_ids)):
            deferred += 1
            warnings.append({"code": "task_destination_unavailable", "task_id": jupiter_id,
                             "message": "任务目标不在可用的同步清单中，已保留原任务并暂停写入。"})
            results.append({"jupiter_id": jupiter_id, "action": "deferred_destination_unavailable"})
            continue
        source_status = item.get("source_status", item.get("status", "unknown"))
        personal_status = item.get("personal_status") or _local_personal_status(state_dir, jupiter_id)
        local_personal_changed = _personal_changed_since_mapping(item, record, personal_status)
        remote_status = None
        if remote_id:
            try:
                remote = inventory_by_id.get((task_project_id, str(remote_id)))
                if remote is None:
                    remote = client.get_task(task_project_id, remote_id) or {}
                remote_status = remote.get("status")
            except TickTickError as error:
                if error.status == 404:
                    # A confirmed remote deletion is safe to recover from; a
                    # transient failure keeps the mapping authoritative.
                    replacement = existing.get(marker)
                    remote_id = (replacement or {}).get("task", {}).get("id")
                    if replacement:
                        task_project_id = replacement["project_id"]
                else:
                    warnings.append({"code": "remote_task_unavailable", "task_id": jupiter_id,
                                     "message": "TickTick 单项状态暂时无法读取，本轮保留原映射。",
                                     "error_type": type(error).__name__})
                    deferred += 1
                    results.append({"jupiter_id": jupiter_id, "action": "deferred_remote_unavailable"})
                    continue
                remote_status = None
        if remote_id and _remote_completed(remote_status):
            if source_status not in {"submitted", "completed"}:
                # A previously observed Jupiter uncheck is authoritative for
                # this private marker, so reopen TickTick instead of fighting
                # the user's change on the next cycle.
                if (personal_status == "open" and local_personal_changed and
                        item.get("personal_done_observed") is True):
                    try:
                        client.reopen_task(task_project_id, remote_id)
                    except TickTickError as error:
                        if _capacity_error(error):
                            capacity_hit = error
                            break
                        raise
                    remote_reopened += 1
                    state["tasks"][jupiter_id] = {**record, "ticktick_task_id": str(remote_id),
                                                     "project_id": task_project_id, "source_status": source_status,
                                                     "remote_status": "open", "personal_status": "open",
                                                     "personal_done_observed": True}
                    checkpoint()
                    continue
                local_updated += int(_set_local_personal_status(
                    state_dir, jupiter_id, "completed", done_sync_pending=True))
                personal_status = "completed"
                remote_completed += 1
            state["tasks"][jupiter_id] = {**record, "ticktick_task_id": str(remote_id), "project_id": task_project_id,
                                             "source_status": source_status, "remote_status": "completed",
                                             "personal_status": personal_status,
                                             "personal_done_observed": bool(item.get("personal_done_observed"))}
            continue
        if (remote_id and _remote_open(remote_status) and personal_status == "completed" and
                source_status not in {"submitted", "completed"} and
                item.get("personal_done_observed") is True and not local_personal_changed):
            local_updated += int(_set_local_personal_status(
                state_dir, jupiter_id, "open", done_sync_pending=True))
            personal_status = "open"
            state["tasks"][jupiter_id] = {**record, "ticktick_task_id": str(remote_id),
                                             "project_id": task_project_id, "source_status": source_status,
                                             "remote_status": "open", "personal_status": personal_status,
                                             "personal_done_observed": bool(item.get("personal_done_observed"))}
            continue
        if source_status in {"submitted", "completed"} or personal_status == "completed":
            if remote_id:
                try:
                    client.complete_task(task_project_id, remote_id)
                except TickTickError as error:
                    if _capacity_error(error):
                        capacity_hit = error
                        break
                    raise
                completed += 1
                state["tasks"][jupiter_id] = {**record, "ticktick_task_id": str(remote_id), "project_id": task_project_id,
                                                 "source_status": source_status, "remote_status": "completed",
                                                 "personal_status": personal_status,
                                                 "personal_done_observed": bool(item.get("personal_done_observed")),
                                                 "completed_at": datetime.now(timezone.utc).isoformat()}
                checkpoint()
            # Do not create a new remote task solely for an already-finished
            # local item; if it is reopened later it returns to the queue.
            continue
        payload = task_payload(item, plan, task_project_id, cfg)
        if not remote_id and not inventory_read_ok:
            deferred += 1
            results.append({"jupiter_id": jupiter_id, "action": "deferred_inventory_unavailable"})
            continue
        digest = _payload_hash(payload)
        if remote_id and record.get("payload_hash") == digest:
            action = "unchanged"
        elif remote_id:
            try:
                client.update_task(remote_id, payload)
            except TickTickError as error:
                if _capacity_error(error):
                    capacity_hit = error
                    break
                raise
            updated += 1
            action = "updated"
        else:
            try:
                response = client.create_task(payload) or {}
            except TickTickError as error:
                if _capacity_error(error):
                    capacity_hit = error
                    break
                raise
            remote_id = response.get("id") or response.get("taskId")
            if not remote_id:
                raise TickTickError("TickTick 创建任务后没有返回任务 ID", code="invalid_response")
            created += 1
            action = "created"
        state["tasks"][jupiter_id] = {"ticktick_task_id": str(remote_id), "project_id": task_project_id,
                                       "payload_hash": digest, "source_status": source_status,
                                       "personal_status": personal_status,
                                       "personal_done_observed": bool(item.get("personal_done_observed")),
                                       "updated_at": datetime.now(timezone.utc).isoformat()}
        checkpoint()
        results.append({"jupiter_id": jupiter_id, "ticktick_task_id": str(remote_id), "action": action})
    if capacity_hit:
        warnings.append({"code": "ticktick_capacity",
                         "message": "TickTick 已达到任务容量限制；已保留本轮已写入的映射，未删除任何任务。",
                         "error_type": type(capacity_hit).__name__})
    state["project_id"] = project_id
    state["project_ids"] = list(configured_project_ids)
    state["last_sync"] = (now or datetime.now(timezone.utc)).isoformat()
    error_code = ("capacity" if capacity_hit else "remote_inventory_unavailable" if inventory_error else
                  "configured_project_unavailable" if missing_project_ids else "sync_deferred" if deferred else None)
    message = (str(capacity_hit) if capacity_hit else "部分任务尚未同步，已保留原数据。" if error_code else None)
    state["last_error"] = message
    atomic_json(mapping_path, state)
    return {"ok": error_code is None, "enabled": True, "created": created, "updated": updated, "completed": completed,
            "deferred": deferred, "remote_completed": remote_completed, "local_updated": local_updated,
            "warnings": warnings, "project_id": project_id, "project_ids": configured_project_ids,
            "error_code": error_code,
            "message": message,
            "remote_reopened": remote_reopened, "results": results}


def _source_mirror_plan(state):
    """Convert the collector's fact store to a plan-shaped read-only mirror.

    The source mirror deliberately takes every task from ``state.json`` and
    marks it active for publication.  Its planning disposition is not used to
    filter source facts: a graded/reference item is still a fact that belongs
    in the user's raw Jupiter list.
    """
    queue = []
    tasks = state.get("tasks", {}) if isinstance(state, dict) else {}
    if not isinstance(tasks, dict):
        return {"timezone": state.get("timezone", "UTC"), "queue": []}
    for key, record in tasks.items():
        if not isinstance(record, dict):
            continue
        source = record.get("source") if isinstance(record.get("source"), dict) else {}
        item = dict(source)
        item["id"] = str(source.get("id") or key)
        item["course"] = str(source.get("course") or "")
        item["title"] = str(source.get("title") or key)
        item["source_status"] = source.get("source_display_status") or source.get("status") or "unknown"
        item["status"] = source.get("status") or item["source_status"]
        personal = record.get("personal") or {}
        item["personal_status"] = personal.get("status")
        item["personal_done_observed"] = personal.get("done_observed") is True
        item["planning_disposition"] = "active"
        queue.append(item)
    return {"timezone": state.get("timezone", "UTC"), "queue": queue}


def sync_source_mirror(instance_path, now=None, dry_run=False, client=None):
    """Publish Jupiter source facts to the explicitly configured raw list.

    This path has an independent mapping/status file and never reads or falls
    back to the plan-list project settings.  It is intentionally deterministic
    so it can run before an optional planning/model step.
    """
    instance_path = Path(instance_path).expanduser().resolve()
    config = read_json(instance_path, {}) or {}
    state_dir = Path(config.get("state_dir", instance_path.parent)).expanduser()
    if not state_dir.is_absolute():
        state_dir = (instance_path.parent / state_dir).resolve()
    source_path = state_dir / "state.json"
    source_state = read_json(source_path, None)
    if not isinstance(source_state, dict) or not isinstance(source_state.get("tasks"), dict):
        return {"ok": False, "enabled": True, "error_code": "source_state_missing",
                "message": "Jupiter 原始数据尚未生成，暂未同步。", "created": 0, "updated": 0,
                "completed": 0, "deferred": 0}
    source_id = config.get("ticktick_source_project_id")
    source_name = config.get("ticktick_source_project_name")
    if not source_id and not source_name:
        return {"ok": False, "enabled": True, "error_code": "source_project_missing",
                "message": "未指定 TickTick 原始任务清单，暂未同步。", "created": 0, "updated": 0,
                "completed": 0, "deferred": 0}
    source_config = dict(config)
    source_config["ticktick_project_id"] = source_id
    source_config["ticktick_project_name"] = source_name
    # Do not inherit the plan list's overflow IDs.  If an explicit source ID
    # is gone, sync_plan fails rather than choosing a same-named plan list.
    source_config["ticktick_project_ids"] = [source_id] if source_id else []
    source_config["ticktick_destination_scope"] = "configured_only"
    source_config["ticktick_enabled"] = config.get("ticktick_enabled", True)
    plan = _source_mirror_plan(source_state)
    try:
        result = sync_plan(plan, source_config, state_dir, instance_path=instance_path,
                           now=now, dry_run=dry_run, client=client,
                           mapping_filename="ticktick-source-state.json")
    except TickTickError as exc:
        if exc.code == "project_not_found":
            return {"ok": False, "enabled": True, "error_code": "source_project_not_found",
                    "message": "TickTick 原始任务清单已失效，暂未写入。", "created": 0,
                    "updated": 0, "completed": 0, "deferred": 0}
        raise
    # Keep the public result free of server/account response bodies.
    safe = dict(result)
    safe.pop("results", None)
    safe["project_name"] = source_name or "原始任务"
    if safe.get("message") and not isinstance(safe["message"], str):
        safe["message"] = "TickTick 原始任务同步未完成。"
    return safe


@contextmanager
def _sync_process_lock(state_dir):
    """Serialize manual, launchd, and post-collection TickTick publishers."""
    path = Path(state_dir) / ".ticktick-sync.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = path.open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TickTickError("TickTick 同步正在进行，本轮稍后重试。", code="sync_in_progress")
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def sync_instance(instance_path, now=None, dry_run=False):
    instance_path = Path(instance_path).expanduser().resolve()
    config = read_json(instance_path, {}) or {}
    state_dir = Path(config.get("state_dir", instance_path.parent)).expanduser()
    if not state_dir.is_absolute():
        state_dir = (instance_path.parent / state_dir).resolve()
    report = Path(config.get("report_path", state_dir / "study-plan.md")).expanduser()
    if not report.is_absolute():
        report = (instance_path.parent / report).resolve()
    plan = read_json(report.with_suffix(".json"), {}) or {}
    source_mirror = config.get("ticktick_sync_mode") == "source_mirror"
    try:
        with (_sync_process_lock(state_dir) if not dry_run else nullcontext()):
            if source_mirror:
                result = sync_source_mirror(instance_path, now=now, dry_run=dry_run)
            else:
                result = sync_plan(plan, config, state_dir, instance_path=instance_path, now=now, dry_run=dry_run)
    except TickTickError as exc:
        result = {"ok": False, "enabled": True, "error_code": exc.code or "ticktick_error", "message": str(exc),
                  "created": 0, "updated": 0, "completed": 0}
    status_path = state_dir / ("ticktick-source-status.json" if source_mirror else "ticktick-status.json")
    status = read_json(status_path, {}) or {}
    status.update({"last_attempt": datetime.now(timezone.utc).isoformat(), **result})
    if result.get("ok"):
        status["last_success"] = status["last_attempt"]
        # Do not leave a transient lock/network error visible after a later
        # successful run.  The status file is the handoff that both the
        # TickTick plugin and the local UI read, so a stale ``last_error`` can
        # make a healthy sync look failed to the GPT orchestration step.
        status["last_error"] = None
    else:
        status["last_error"] = result.get("message")
    if not dry_run:
        atomic_json(status_path, status)
    return result
