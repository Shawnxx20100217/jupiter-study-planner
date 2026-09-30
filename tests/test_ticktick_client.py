import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import ticktick_client as tick


class FakeClient:
    def __init__(self):
        self.created = []
        self.updated = []
        self.completed = []
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
        self.assertEqual(client.lookups, [("p2", "overflow-1")])
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


if __name__ == "__main__":
    unittest.main()
