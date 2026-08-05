import unittest
from pathlib import Path

from webui.app import create_app


class WebUISingleInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def test_ui_query_parameters_render_the_same_modern_page(self):
        default = self.client.get("/")
        modern = self.client.get("/?ui=modern")
        legacy = self.client.get("/?ui=legacy")

        self.assertEqual(default.status_code, 200)
        self.assertEqual(modern.data, default.data)
        self.assertEqual(legacy.data, default.data)
        self.assertIn(b'class="accounts-table-v2"', default.data)
        self.assertNotIn(b"/?ui=legacy", default.data)

    def test_legacy_ui_cookie_is_ignored_and_not_refreshed(self):
        self.client.set_cookie("ui_mode", "legacy")

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'class="accounts-table-v2"', response.data)
        self.assertFalse(any("ui_mode=" in value for value in response.headers.getlist("Set-Cookie")))

    def test_legacy_template_has_been_removed(self):
        template_dir = Path(__file__).parents[1] / "webui" / "templates"

        self.assertTrue((template_dir / "index.html").is_file())
        self.assertFalse((template_dir / "index_legacy.html").exists())

    def test_sidebar_footer_links_to_current_repository_without_telegram(self):
        template = (
            Path(__file__).parents[1] / "webui" / "templates" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn('href="https://github.com/c4lnn/turb-gpt-free-register"', template)
        self.assertNotIn("https://github.com/myfanhua/turb-gpt-free-register", template)
        self.assertNotIn("t.me/", template)
        self.assertNotIn("TG 交流群", template)


if __name__ == "__main__":
    unittest.main()
