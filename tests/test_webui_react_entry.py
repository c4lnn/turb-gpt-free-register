import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui.app import create_app


class WebUIReactEntryTests(unittest.TestCase):
    def test_entry_and_assets_share_flask_origin_without_changing_api_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory)
            assets = dist / "assets"
            assets.mkdir()
            (dist / "index.html").write_text(
                '<!doctype html><html lang="zh-CN"><div id="root"></div>'
                '<script src="/assets/app.js"></script></html>',
                encoding="utf-8",
            )
            (assets / "app.js").write_text("window.appLoaded = true", encoding="utf-8")
            (dist / "favicon.svg").write_text("<svg xmlns=\"http://www.w3.org/2000/svg\"/>", encoding="utf-8")

            with patch("webui.app.FRONTEND_DIST", dist):
                app = create_app(auth_code="test-auth")

            responses = []
            try:
                anonymous = app.test_client()
                anonymous_root = anonymous.get("/", buffered=True)
                responses.append(anonymous_root)
                self.assertEqual(anonymous_root.status_code, 302)
                self.assertEqual(anonymous.get("/api/summary", buffered=True).status_code, 401)
                anonymous_asset = anonymous.get("/assets/app.js", buffered=True)
                responses.append(anonymous_asset)
                self.assertEqual(anonymous_asset.status_code, 200)
                anonymous_favicon = anonymous.get("/favicon.svg", buffered=True)
                responses.append(anonymous_favicon)
                self.assertEqual(anonymous_favicon.status_code, 200)

                client = app.test_client()
                login = client.post("/login", data={"auth_code": "test-auth"}, buffered=True)
                responses.append(login)
                self.assertEqual(login.status_code, 302)
                entry = client.get("/", buffered=True)
                responses.append(entry)
                self.assertEqual(entry.status_code, 200)
                self.assertIn('id="root"', entry.get_data(as_text=True))
                authorized_asset = client.get("/assets/app.js", buffered=True)
                responses.append(authorized_asset)
                self.assertEqual(authorized_asset.status_code, 200)
                self.assertEqual(authorized_asset.mimetype, "text/javascript")
                self.assertEqual(client.get("/api/summary", buffered=True).status_code, 200)
            finally:
                for response in responses:
                    response.close()


if __name__ == "__main__":
    unittest.main()
