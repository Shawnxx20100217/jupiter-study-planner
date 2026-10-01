import json
import io
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import ticktick_client as tick


class FakeClient:
    def __init__(self):
        self.created = []
        self.updated = []
        self.completed = []
        self.reopened = []
        self.status = {}

    def list_projects(self):
        return [{"id": "p1", "name": "Jupiter 作业", "closed": False}]

    def project_data(self, project_id):
        return {"tasks": []}

    def create_task(self, payload):
        self.created.append(payload)
        task_id = "t" + str(len(self.created))
        self.status[task_id] = 0
        return {"id": task_id}

    def get_task(self, project_id, task_id):
        return {"id": task_id, "status": self.status.get(task_id, 0)}

    def update_task(self, task_id, payload):
        self.updated.append((task_id, payload))
        return {}

    def complete_task(self, project_id, task_id):
        self.completed.append((project_id, task_id))
        self.status[task_id] = 2
        return {}

    def reopen_task(self, project_id, task_id):
        self.reopened.append((project_id, task_id))
        self.status[task_id] = 0
        return {}


class TickTickTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ticktick-test-")
        self.root = Path(self.temp.name)
        self.plan = {"timezone": "Asia/Shanghai", "queue": [{
            "id": "math-1", "course": "Math", "title": "Worksheet",
            "due_precision": "time", "due_at": "2026-10-01T12:00:00+08:00",
            "due_label": "2026-10-01 12:00", "planning_disposition": "active",
            "source_status": "open", "priority_band": "high", "source_url": "https://example.org"
        }]}

    def tearDown(self):
        self.temp.cleanup()

    def test_reminder_trigger_uses_negative_offsets(self):
        self.assertEqual(tick.reminder_trigger(1440), "TRIGGER:-P1D")
        self.assertEqual(tick.reminder_trigger(120), "TRIGGER:-PT2H")
        self.assertEqual(tick.reminder_trigger(30), "TRIGGER:-PT30M")

    def test_date_only_payload_discloses_unknown_time(self):
        item = dict(self.plan["queue"][0], due_precision="date", due_at=None, due_date="2026-10-01")
        payload = tick.task_payload(item, self.plan, "p1")
        self.assertTrue(payload["isAllDay"])
        self.assertIn("具体截止钟点待确认", payload["content"])
        self.assertEqual(payload["reminders"], ["TRIGGER:-P1D", "TRIGGER:-PT2H", "TRIGGER:-PT30M"])

    def test_source_payload_exposes_private_completion_for_gpt_handoff(self):
        item = dict(self.plan["queue"][0], personal_status="completed", personal_done_observed=True,
                    kind="assignment")
        payload = tick.task_payload(item, self.plan, "p1")
        self.assertIn("Jupiter 私人完成：已完成", payload["content"])
        self.assertIn("Jupiter 类型：assignment", payload["content"])
        unknown = dict(item, personal_status="open", personal_done_observed=False)
        unknown_payload = tick.task_payload(unknown, self.plan, "p1")
        self.assertIn("Jupiter 私人完成：未核实", unknown_payload["content"])

    def test_sync_is_idempotent_and_updates_changed_payload(self):
        client = FakeClient()
        config = {"ticktick_enabled": True, "ticktick_project_id": "p1",
                  "ticktick_token_file": str(self.root / "token")}
        (self.root / "token").write_text("secret")
        first = tick.sync_plan(self.plan, config, self.root, client=client)
        self.assertEqual(first["created"], 1)
        self.assertEqual(len(client.created), 1)
        second = tick.sync_plan(self.plan, config, self.root, client=client)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["updated"], 0)
        self.plan["queue"][0]["title"] = "Revised Worksheet"
        third = tick.sync_plan(self.plan, config, self.root, client=client)
        self.assertEqual(third["updated"], 1)
        self.assertEqual(client.updated[-1][0], "t1")

    def test_submitted_source_completes_existing_task(self):
        client = FakeClient()
        config = {"ticktick_enabled": True, "ticktick_project_id": "p1",
                  "ticktick_token_file": str(self.root / "token")}
        (self.root / "token").write_text("secret")
        tick.sync_plan(self.plan, config, self.root, client=client)
        self.plan["queue"][0]["source_status"] = "submitted"
        result = tick.sync_plan(self.plan, config, self.root, client=client)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(client.completed, [("p1", "t1")])

    def test_local_completion_completes_ticktick_task(self):
        client = FakeClient()
        config = {"ticktick_enabled": True, "ticktick_project_id": "p1",
                  "ticktick_token_file": str(self.root / "token")}
        (self.root / "token").write_text("secret")
        tick.sync_plan(self.plan, config, self.root, client=client)
        state = {"tasks": {"math-1": {"personal": {"status": "completed"}}}}
        (self.root / "state.json").write_text(json.dumps(state))
        result = tick.sync_plan(self.plan, config, self.root, client=client)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(client.completed, [("p1", "t1")])


    def test_inventory_failure_defers_unmapped_creation(self):
        class BrokenInventory(FakeClient):
            def project_data(self, project_id):
                raise tick.TickTickError("offline", status=503, code="api_error")
        client = BrokenInventory()
        config = {"ticktick_enabled": True, "ticktick_project_id": "p1",
                  "ticktick_token_file": str(self.root / "token")}
        (self.root / "token").write_text("secret")
        result = tick.sync_plan(self.plan, config, self.root, client=client)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["deferred"], 1)
        self.assertEqual(result["warnings"][0]["code"], "remote_inventory_unavailable")

    def test_confirmed_remote_deletion_can_recreate_task(self):
        class DeletedTask(FakeClient):
            def get_task(self, project_id, task_id):
                raise tick.TickTickError("gone", status=404, code="api_error")
        client = DeletedTask()
        config = {"ticktick_enabled": True, "ticktick_project_id": "p1",
                  "ticktick_token_file": str(self.root / "token")}
        (self.root / "token").write_text("secret")
        tick.sync_plan(self.plan, config, self.root, client=client)
        result = tick.sync_plan(self.plan, config, self.root, client=client)
        self.assertEqual(result["created"], 1)
        self.assertEqual(len(client.created), 2)

    def test_ticktick_completion_updates_local_personal_status(self):
        client = FakeClient()
        config = {"ticktick_enabled": True, "ticktick_project_id": "p1",
                  "ticktick_token_file": str(self.root / "token")}
        (self.root / "token").write_text("secret")
        tick.sync_plan(self.plan, config, self.root, client=client)
        client.status["t1"] = 2
        result = tick.sync_plan(self.plan, config, self.root, client=client)
        self.assertEqual(result["remote_completed"], 1)
        state = json.loads((self.root / "state.json").read_text()) if (self.root / "state.json").exists() else {}
        # A plan without a corresponding local Jupiter state is allowed to
        # remain unmapped; the real runtime has state.json before publishing.
        self.assertEqual(result["local_updated"], 1)
        self.assertEqual(state["tasks"]["math-1"]["personal"]["status"], "completed")

    def test_ticktick_reopen_updates_local_done_marker_without_recompleting(self):
        client = FakeClient()
        config = {"ticktick_enabled": True, "ticktick_project_id": "p1",
                  "ticktick_token_file": str(self.root / "token")}
        (self.root / "token").write_text("secret")
        tick.sync_plan(self.plan, config, self.root, client=client)
        state = {"tasks": {"math-1": {"personal": {
            "status": "open", "done_observed": True}}}}
        (self.root / "state.json").write_text(json.dumps(state))
        client.status["t1"] = 2
        self.plan["queue"][0]["personal_status"] = "open"
        self.plan["queue"][0]["personal_done_observed"] = True
        result = tick.sync_plan(self.plan, config, self.root, client=client)
        self.assertEqual(result["remote_reopened"], 1)
        self.assertEqual(client.reopened, [("p1", "t1")])
        self.assertEqual(client.completed, [])

    def test_ticktick_uncheck_updates_local_status_to_open(self):
        client = FakeClient()
        config = {"ticktick_enabled": True, "ticktick_project_id": "p1",
                  "ticktick_token_file": str(self.root / "token")}
        (self.root / "token").write_text("secret")
        tick.sync_plan(self.plan, config, self.root, client=client)
        state = {"tasks": {"math-1": {"personal": {
            "status": "completed", "done_observed": True}}}}
        (self.root / "state.json").write_text(json.dumps(state))
        self.plan["queue"][0]["personal_status"] = "completed"
        self.plan["queue"][0]["personal_done_observed"] = True
        tick.sync_plan(self.plan, config, self.root, client=client)
        client.completed.clear()
        client.status["t1"] = 0
        result = tick.sync_plan(self.plan, config, self.root, client=client)
        self.assertEqual(result["local_updated"], 1)
        state = json.loads((self.root / "state.json").read_text())
        self.assertEqual(state["tasks"]["math-1"]["personal"]["status"], "open")
        self.assertEqual(client.completed, [])

    def test_inventory_scans_configured_overflow_and_accepts_connector_marker(self):
        class SplitClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.lookups = []

            def list_projects(self):
                return [{"id": "p1", "name": "Jupiter 作业", "closed": False},
                        {"id": "p2", "name": "Jupiter 作业 2", "closed": False}]

            def project_data(self, project_id):
                if project_id == "p2":
                    return {"tasks": [{"id": "overflow-1", "content": "Jupiter: math-1\nMath · Worksheet"}]}
                return {"tasks": []}

            def get_task(self, project_id, task_id):
                self.lookups.append((project_id, task_id))
                return {"id": task_id, "status": 0}

        client = SplitClient()
        config = {"ticktick_enabled": True, "ticktick_project_id": "p1",
                  "ticktick_project_ids": ["p1", "p2"],
                  "ticktick_token_file": str(self.root / "token")}
        (self.root / "token").write_text("secret")
        result = tick.sync_plan(self.plan, config, self.root, client=client)
        self.assertEqual(result["created"], 0)
        # The task was already present in the inventory response; syncing it
        # must not issue a second per-task GET (which is both slow and flaky).
        self.assertEqual(client.lookups, [])
        state = json.loads((self.root / "ticktick-state.json").read_text())
        self.assertEqual(state["tasks"]["math-1"]["project_id"], "p2")

    def test_mapping_checkpoint_survives_later_mutation_failure(self):
        class FailsSecondCreate(FakeClient):
            def create_task(self, payload):
                if len(self.created) == 1:
                    raise tick.TickTickError("offline", status=503, code="api_error")
                return super().create_task(payload)

        plan = {"timezone": "Asia/Shanghai", "queue": [self.plan["queue"][0],
                dict(self.plan["queue"][0], id="math-2", title="Second") ]}
        client = FailsSecondCreate()
        config = {"ticktick_enabled": True, "ticktick_project_id": "p1",
                  "ticktick_token_file": str(self.root / "token")}
        (self.root / "token").write_text("secret")
        with self.assertRaises(tick.TickTickError):
            tick.sync_plan(plan, config, self.root, client=client)
        state = json.loads((self.root / "ticktick-state.json").read_text())
        self.assertEqual(state["tasks"]["math-1"]["ticktick_task_id"], "t1")
        self.assertNotIn("math-2", state["tasks"])

    def test_capacity_error_stops_without_deleting_and_reports_capacity(self):
        class FullClient(FakeClient):
            def create_task(self, payload):
                raise tick.TickTickError("task limit reached", status=400, code="capacity")

        client = FullClient()
        config = {"ticktick_enabled": True, "ticktick_project_id": "p1",
                  "ticktick_token_file": str(self.root / "token")}
        (self.root / "token").write_text("secret")
        result = tick.sync_plan(self.plan, config, self.root, client=client)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "capacity")
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["warnings"][0]["code"], "ticktick_capacity")

    def test_explicit_missing_project_id_does_not_fall_back_to_same_name(self):
        class RenamedProject(FakeClient):
            def list_projects(self):
                return [{"id": "new-id", "name": "Jupiter 作业", "closed": False}]

        client = RenamedProject()
        config = {"ticktick_enabled": True, "ticktick_project_id": "old-id",
                  "ticktick_project_name": "Jupiter 作业",
                  "ticktick_token_file": str(self.root / "token")}
        (self.root / "token").write_text("secret")
        with self.assertRaises(tick.TickTickError) as context:
            tick.sync_plan(self.plan, config, self.root, client=client)
        self.assertEqual(context.exception.code, "project_not_found")
        self.assertEqual(client.created, [])

    def test_http_error_does_not_echo_server_body(self):
        def opener(request, timeout=0):
            raise HTTPError(request.full_url, 500, "failure", {},
                            io.BytesIO(b'{"errorId":"private-server-id","data":"private"}'))

        client = tick.TickTickClient("secret", opener=opener)
        with self.assertRaises(tick.TickTickError) as context:
            client.request("GET", "project")
        self.assertNotIn("errorId", str(context.exception))
        self.assertNotIn("private", str(context.exception))

    def test_strict_destination_scope_defers_mapping_to_unconfigured_project(self):
        class Recording(FakeClient):
            def get_task(self, project_id, task_id):
                raise AssertionError("unconfigured destination must not be contacted")

        client = Recording()
        config = {"ticktick_enabled": True, "ticktick_project_id": "p1",
                  "ticktick_destination_scope": "configured_only",
                  "ticktick_token_file": str(self.root / "token")}
        (self.root / "token").write_text("secret")
        (self.root / "ticktick-state.json").write_text(json.dumps({"version": 1, "tasks": {
            "math-1": {"ticktick_task_id": "old-task", "project_id": "old-project"}
        }}))
        result = tick.sync_plan(self.plan, config, self.root, client=client)
        self.assertFalse(result["ok"])
        self.assertEqual(result["deferred"], 1)
        self.assertEqual(result["results"][0]["action"], "deferred_destination_unavailable")

    def test_source_mirror_uses_independent_mapping_and_explicit_source_project(self):
        class SourceClient(FakeClient):
            def list_projects(self):
                return [{"id": "source-list", "name": "原始任务", "closed": False},
                        {"id": "plan-list", "name": "Jupiter任务", "closed": False}]

        state = {"version": 1, "timezone": "Asia/Shanghai", "tasks": {
            "math-1": {"personal": {"status": "open"}, "source": {
                "id": "math-1", "course": "Math", "title": "Worksheet",
                "status": "open", "source_display_status": "Assigned",
                "due_precision": "date", "due_date": "2026-10-01",
                "due_label": "2026-10-01", "planning_disposition": "reference"
            }}
        }}
        (self.root / "state.json").write_text(json.dumps(state))
        config = {"ticktick_enabled": True, "ticktick_project_id": "plan-list",
                  "ticktick_project_name": "Jupiter任务", "ticktick_source_project_id": "source-list",
                  "ticktick_source_project_name": "原始任务",
                  "ticktick_token_file": str(self.root / "token")}
        (self.root / "token").write_text("secret")
        instance = self.root / "instance.json"
        instance.write_text(json.dumps({**config, "state_dir": str(self.root)}))
        result = tick.sync_source_mirror(instance, client=SourceClient())
        self.assertEqual(result["created"], 1)
        self.assertTrue((self.root / "ticktick-source-state.json").exists())
        self.assertFalse((self.root / "ticktick-state.json").exists())

    def test_source_mirror_does_not_fall_back_to_plan_project(self):
        (self.root / "state.json").write_text(json.dumps({"tasks": {}}))
        config = {"ticktick_enabled": True, "ticktick_project_id": "plan-list",
                  "ticktick_project_name": "Jupiter任务", "ticktick_token_file": str(self.root / "token")}
        (self.root / "token").write_text("secret")
        instance = self.root / "instance.json"
        instance.write_text(json.dumps({**config, "state_dir": str(self.root)}))
        result = tick.sync_source_mirror(instance, client=FakeClient())
        self.assertEqual(result["error_code"], "source_project_missing")

    def test_source_mirror_reports_stale_explicit_project_without_name_fallback(self):
        (self.root / "state.json").write_text(json.dumps({"tasks": {}}))
        config = {"ticktick_enabled": True, "ticktick_project_id": "plan-list",
                  "ticktick_project_name": "Jupiter任务", "ticktick_source_project_id": "old-source",
                  "ticktick_source_project_name": "原始任务", "ticktick_token_file": str(self.root / "token")}
        (self.root / "token").write_text("secret")
        instance = self.root / "instance.json"
        instance.write_text(json.dumps({**config, "state_dir": str(self.root)}))
        result = tick.sync_source_mirror(instance, client=FakeClient())
        self.assertEqual(result["error_code"], "source_project_not_found")

    def test_successful_sync_clears_previous_error_in_status(self):
        config = {"ticktick_enabled": True, "ticktick_sync_mode": "source_mirror",
                  "ticktick_source_project_id": "p1"}
        instance = self.root / "instance.json"
        instance.write_text(json.dumps({**config, "state_dir": str(self.root)}))
        (self.root / "ticktick-source-status.json").write_text(json.dumps({
            "ok": False, "last_error": "TickTick 同步正在进行，本轮稍后重试。",
            "last_success": "2026-10-01T00:00:00+00:00"
        }))
        with patch.object(tick, "sync_source_mirror", return_value={
                "ok": True, "enabled": True, "created": 0, "updated": 0,
                "completed": 0}):
            result = tick.sync_instance(instance)
        self.assertTrue(result["ok"])
        status = json.loads((self.root / "ticktick-source-status.json").read_text())
        self.assertIsNone(status["last_error"])


if __name__ == "__main__":
    unittest.main()
