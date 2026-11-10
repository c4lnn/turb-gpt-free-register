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


class WreqDefaultHeaderTests(unittest.TestCase):
    """锁住"抑制传输层默认头"的效果，防止把它挪回 Client 构造参数。

    Client 层没有 default_headers 字段，传了会被静默忽略，头会照常发出；
    只断言"头字典里没有该键"抓不到这种情况，所以这里断言实发报文。
    """

    FORM_BODY = "callbackUrl=https%3A%2F%2Fchatgpt.com%2F&csrfToken=" + "a" * 64 + "&json=true"

    XHR_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "sec-ch-ua": '"Chromium";v="152", "Not-A.Brand";v="24", "Google Chrome";v="152"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "content-type": "application/x-www-form-urlencoded",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "referer": "https://chatgpt.com/auth/login",
        "origin": "https://chatgpt.com",
        "priority": "u=1, i",
    }

    def _capture_post(self):
        captured = {}

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                captured["headers"] = [(name.lower(), value) for name, value in self.headers.items()]
                captured["body"] = self.rfile.read(length)
                payload = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        session = WreqSession("chrome152")
        try:
            origin = f"http://127.0.0.1:{server.server_port}"
            session.post(origin + "/api/auth/signin/openai?prompt=login",
                         headers=self.XHR_HEADERS, data=self.FORM_BODY)
        finally:
            session.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        return captured["headers"], captured["body"]

    def test_xhr_omits_navigation_only_headers(self):
        headers, _ = self._capture_post()
        sent = dict(headers)
        self.assertNotIn("upgrade-insecure-requests", sent)
        self.assertNotIn("sec-fetch-user", sent)

    def test_default_header_suppression_keeps_browser_headers(self):
        headers, body = self._capture_post()
        sent = dict(headers)
        self.assertEqual(body, self.FORM_BODY.encode())
        self.assertTrue(sent.get("accept-encoding"))
        for name in ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "priority", "referer", "origin"):
            self.assertTrue(sent.get(name), f"缺少头: {name}")
        self.assertEqual(sent.get("content-length"), str(len(body)))
        self.assertEqual([name for name, value in headers if value == ""], [])


if __name__ == "__main__":
    unittest.main()
