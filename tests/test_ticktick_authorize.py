import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import ticktick_authorize as auth


class FakeClient:
    def __init__(self, projects=None):
        self.projects = projects if projects is not None else [{"id": "p1", "name": "Jupiter 作业", "closed": False}]

    def list_projects(self):
        return self.projects


class TickTickAuthorizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ticktick-auth-")
        self.root = Path(self.temp.name)
        self.instance = self.root / "local-instance.json"
        self.instance.write_text(json.dumps({"state_dir": str(self.root), "ticktick_api_base": auth.API_BASE,
                                              "ticktick_project_name": "Jupiter 作业"}), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_callback_requires_exact_state_and_loopback_path(self):
        self.assertEqual(auth.callback_code("/callback?code=abc&state=state", "state"), "abc")
        with self.assertRaises(auth.SetupError):
            auth.callback_code("/callback?code=abc&state=wrong", "state")
        with self.assertRaises(auth.SetupError):
            auth.callback_code("https://evil.example/callback?code=abc&state=state", "state")

    def test_validate_target_requires_single_open_project(self):
        project = auth.validate_target("token", json.loads(self.instance.read_text()), client=FakeClient())
        self.assertEqual(project["id"], "p1")
        with self.assertRaises(auth.SetupError):
            auth.validate_target("token", json.loads(self.instance.read_text()), client=FakeClient([]))
        with self.assertRaises(auth.SetupError):
            auth.clean_token("a token")

    def test_save_authorization_writes_private_token_and_config(self):
        project = {"id": "p1", "name": "Jupiter 作业"}
        result = auth.save_authorization(self.instance, "secret-token", project, "personal-token")
        self.assertTrue(result["ok"])
        token_path = self.root / "ticktick-token"
        self.assertEqual(token_path.read_text(), "secret-token\n")
        self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
        config = json.loads(self.instance.read_text())
        self.assertTrue(config["ticktick_enabled"])
        self.assertEqual(config["ticktick_project_id"], "p1")
        self.assertEqual(json.loads((self.root / "ticktick-auth.json").read_text())["method"], "personal-token")

    def test_save_does_not_write_when_project_validation_fails(self):
        with self.assertRaises(auth.SetupError):
            auth.validate_target("secret-token", json.loads(self.instance.read_text()), client=FakeClient([]))
        self.assertFalse((self.root / "ticktick-token").exists())
        self.assertFalse(json.loads(self.instance.read_text()).get("ticktick_enabled", False))

    def test_source_authorization_uses_source_target_and_preserves_plan_target(self):
        config = json.loads(self.instance.read_text())
        config.update({"ticktick_sync_mode": "source_mirror", "ticktick_source_project_id": "raw",
                       "ticktick_source_project_name": "原始任务", "ticktick_project_id": "plan",
                       "ticktick_project_name": "Jupiter任务"})
        self.instance.write_text(json.dumps(config))
        raw = {"id": "raw", "name": "原始任务"}
        plan = {"id": "plan", "name": "Jupiter任务"}
        project = auth.validate_target("token", config, client=FakeClient([plan, raw]))
        self.assertEqual(project, raw)
        result = auth.save_authorization(self.instance, "secret-token", project, "personal-token")
        saved = json.loads(self.instance.read_text())
        self.assertEqual(result["project_name"], "原始任务")
        self.assertEqual(saved["ticktick_source_project_id"], "raw")
        self.assertEqual(saved["ticktick_source_project_name"], "原始任务")
        self.assertEqual(saved["ticktick_project_id"], "plan")
        self.assertEqual(saved["ticktick_project_name"], "Jupiter任务")

    def test_new_source_instance_authorizes_raw_list_by_name(self):
        from init_instance import initialize
        identity = {"expected_student": "Example Student", "expected_school_year": "Example School",
                    "expected_term": "First", "school_timezone": "UTC", "expected_courses": ["Math"]}
        result = initialize(self.root / "personal", identity, self.root / "plugin")
        instance = Path(result["instance"])
        config = json.loads(instance.read_text())
        raw = {"id": "raw", "name": "原始任务"}
        project = auth.validate_target("token", config, client=FakeClient([raw]))
        auth.save_authorization(instance, "secret-token", project, "personal-token")
        saved = json.loads(instance.read_text())
        self.assertEqual(saved["ticktick_sync_mode"], "source_mirror")
        self.assertEqual(saved["ticktick_source_project_id"], "raw")
        self.assertTrue(saved["ticktick_enabled"])

    def test_missing_source_target_never_falls_back_to_plan_target(self):
        config = {"ticktick_sync_mode": "source_mirror", "ticktick_source_project_id": "deleted",
                  "ticktick_project_id": "plan", "ticktick_project_name": "Jupiter任务"}
        projects = [{"id": "plan", "name": "Jupiter任务"}, {"id": "raw", "name": "原始任务"}]
        with self.assertRaises(auth.SetupError):
            auth.validate_target("token", config, client=FakeClient(projects))
        explicit = auth.validate_target("token", config, project_id="raw", client=FakeClient(projects))
        self.assertEqual(explicit["id"], "raw")

    def test_form_does_not_render_secret(self):
        html = auth.local_form("/connect/one", "nonce", "hello")
        self.assertIn('type="password"', html)
        self.assertNotIn("secret-token", html)
        self.assertIn('autocomplete="off"', html)

    def test_embedded_browser_may_omit_origin_but_foreign_origin_is_rejected(self):
        self.assertTrue(auth.origin_allowed(None, "http://127.0.0.1:1234"))
        self.assertTrue(auth.origin_allowed("null", "http://127.0.0.1:1234"))
        self.assertTrue(auth.origin_allowed("http://127.0.0.1:1234", "http://127.0.0.1:1234"))
        self.assertFalse(auth.origin_allowed("https://evil.example", "http://127.0.0.1:1234"))


if __name__ == "__main__":
    unittest.main()
