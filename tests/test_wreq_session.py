import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core.wreq_session import WreqSession


class WreqCookieOriginTests(unittest.TestCase):
    def test_redirect_host_only_cookie_keeps_origin_scope_and_deletion(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", "/finish")
                    self.send_header("Set-Cookie", "device=fixture; Path=/; HttpOnly; Max-Age=3600")
                elif self.path == "/delete":
                    self.send_response(200)
                    self.send_header("Set-Cookie", "device=; Path=/; Max-Age=0")
                else:
                    self.send_response(200)
                self.end_headers()
                self.wfile.write(self.headers.get("Cookie", "").encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        session = WreqSession("chrome152")
        try:
            origin = f"http://127.0.0.1:{server.server_port}"
            response = session.get(origin + "/start")
            self.assertIn("device=fixture", response.text)
            cookie = next(cookie for cookie in session.cookies.jar if cookie.name == "device")
            self.assertEqual(cookie.domain, "127.0.0.1")
            self.assertFalse(cookie.domain_specified)
            self.assertEqual(cookie.path, "/")
            self.assertFalse(cookie.is_expired())
            self.assertIsNotNone(cookie.expires)
            session.get(origin + "/delete")
            self.assertNotIn("device", [cookie.name for cookie in session.cookies.jar])
            self.assertNotIn("device=", session.get(origin + "/finish").text)
        finally:
            session.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
