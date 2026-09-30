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
