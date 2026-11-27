import ipaddress
import os
import socket
import ssl
import struct
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import requests
import wreq
import wreq.exceptions
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from core.infra.wreq_session import WreqSession, _is_transient_failure
from core.openai.transport.session import BrowserSession


_RST_LINGER = struct.pack("ii", 1, 0)
_FIXTURE_CERT = {}


def _fixture_keypair():
    """进程内只生成一次自签证书，避免每个用例都做一次 RSA 运算。"""
    if "cert" not in _FIXTURE_CERT:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder()
                .subject_name(name).issuer_name(name)
                .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=365))
                .serial_number(x509.random_serial_number())
                .public_key(key.public_key())
                .add_extension(x509.SubjectAlternativeName(
                    [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                    critical=False)
                .sign(key, hashes.SHA256()))
        _FIXTURE_CERT["cert"] = cert.public_bytes(serialization.Encoding.PEM)
        _FIXTURE_CERT["key"] = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return _FIXTURE_CERT["cert"], _FIXTURE_CERT["key"]


class _ScriptedListener:
    """共享的 TCP 监听骨架：accept 循环、连接计数、按脚本取动作。

    ``script`` 是每个连接要执行的动作名列表，第 N 个连接执行第 N 个动作；
    超出脚本长度的连接执行 ``default``。子类实现 ``_handle(conn, action)``。
    """

    def __init__(self, script=(), *, default):
        self._script = list(script)
        self._default = default
        self._release = threading.Event()
        self._lock = threading.Lock()
        self.connections = 0
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._release.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            with self._lock:
                self.connections += 1
                action = self._script.pop(0) if self._script else self._default
            threading.Thread(target=self._handle, args=(conn, action), daemon=True).start()

    @staticmethod
    def _close(conn, rst):
        if rst:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _RST_LINGER)
        conn.close()

    def close(self):
        self._release.set()
        self._sock.close()
        self._thread.join(timeout=5)


class _FaultServer(_ScriptedListener):
    """按脚本对每个连接注入故障的 TLS 服务端。动作：

    - ``close_before_handshake[_rst]``：accept 后不等握手直接关闭
    - ``close_after_clienthello[_rst]``：读到 ClientHello 后不回 ServerHello 就关闭
    - ``hang_handshake``：读到 ClientHello 后不回应，保持到用例结束
    - ``hang_after_request``：握手完成、读完请求后不回应，保持到用例结束
    - ``close_after_request[_rst]``：握手完成、读完请求后不回响应就关闭
    - ``truncated[_rst]``：回 ``Content-Length`` 大于实际 body 的响应头和部分 body 后关闭
    - ``ok``：回 200 和固定 body ``ok``；``cookie`` 非空时同时下发该 ``Set-Cookie``；收到的请求体记入 ``bodies``

    不带 ``_rst`` 后缀的关闭是 FIN。两者抛的 wreq 异常类不同，见 design D1「RST 的阶段」。
    """

    def __init__(self, script=(), *, default="ok", cookie=None):
        super().__init__(script, default=default)
        cert_pem, key_pem = _fixture_keypair()
        # load_cert_chain 只接受文件路径（certfile / keyfile），不能从内存加载，
        # 所以把进程内生成的 PEM 落到本实例专属的临时目录，close() 时一并删除。
        self._cert_dir = tempfile.TemporaryDirectory(prefix="wreq-fixture-")
        cert_path = os.path.join(self._cert_dir.name, "cert.pem")
        key_path = os.path.join(self._cert_dir.name, "key.pem")
        with open(cert_path, "wb") as handle:
            handle.write(cert_pem)
        with open(key_path, "wb") as handle:
            handle.write(key_pem)
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(cert_path, key_path)
        self._cookie = cookie
        self.requests = 0
        self.bodies = []
        # 每个到达服务端的请求的完整原始字节（请求行 + 头 + 体），用于比较重试前后是否逐字节相同。
        self.raw_requests = []

    @property
    def url(self):
        # 必须用 IP 而不是 localhost：http.cookiejar 只对单标签主机名 localhost 做
        # domain 改写（localhost -> localhost.local），WreqCookies.jar 回填 host-only Cookie
        # 时据此去原生 Jar 查询会查不到，Cookie 被丢弃。线上域名都是带点的 FQDN，不受影响。
        return f"https://127.0.0.1:{self.port}/x"

    def _reply(self, tls, body=b"ok"):
        # cookie 是 Set-Cookie 的值（如 "device=fixture; Path=/"），头名在这里补上。
        cookie = b"Set-Cookie: " + self._cookie.encode() + b"\r\n" if self._cookie else b""
        tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode()
                    + b"\r\n" + cookie + b"Connection: close\r\n\r\n" + body)

    def _handle(self, conn, action):
        rst = action.endswith("_rst")
        kind = action[:-4] if rst else action
        try:
            conn.settimeout(10)
            if kind == "close_before_handshake":
                self._close(conn, rst)
            elif kind == "close_after_clienthello":
                conn.recv(1)
                self._close(conn, rst)
            elif kind == "hang_handshake":
                # 处理线程不能直接返回：连接被回收时对端收到的是 EOF，客户端抛的是
                # RequestError 而不是 TimeoutError。必须把连接保持到用例结束。
                conn.recv(1)
                self._release.wait(30)
                conn.close()
            else:
                tls = self._ctx.wrap_socket(conn, server_side=True)
                request = tls.recv(65536)
                with self._lock:
                    self.requests += 1
                    self.bodies.append(_body_of(request))
                    self.raw_requests.append(request)
                if kind == "hang_after_request":
                    self._release.wait(30)
                    tls.close()
                elif kind == "close_after_request":
                    self._close(tls, rst)
                elif kind == "truncated":
                    tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 200\r\n\r\nshort")
                    self._close(tls, rst)
                elif kind == "ok":
                    self._reply(tls)
                    tls.close()
                else:
                    raise AssertionError(f"未知夹具动作: {action}")
        except (OSError, ssl.SSLError):
            pass

    def close(self):
        super().close()
        self._cert_dir.cleanup()

    def make_session(self, *, timeout=5.0):
        """构造信任夹具证书、指向本服务端的 WreqSession。"""
        cert_pem, _ = _fixture_keypair()
        return _make_session(cert_pem, timeout=timeout)


class _FaultProxy(_ScriptedListener):
    """按脚本对每个连接注入故障的 HTTP 代理。动作：

    - ``proxy_close``：accept 后不读请求直接关闭
    - ``proxy_407``：读完 CONNECT 请求头，回 ``407`` 后关闭
    - ``proxy_tunnel``：读完 CONNECT 请求头，回 ``200`` 建隧道，之后与 ``target``
      的连接双向透传。隧道内的故障由 ``target`` 的脚本决定，代理不做干预。
    """

    def __init__(self, target, script=(), *, default="proxy_tunnel"):
        super().__init__(script, default=default)
        self._target = target
        self.connects = 0

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def _handle(self, conn, action):
        try:
            conn.settimeout(10)
            if action == "proxy_close":
                conn.close()
                return
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                head += chunk
            with self._lock:
                self.connects += 1
            if action == "proxy_407":
                conn.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                             b"Content-Length: 0\r\nConnection: close\r\n\r\n")
                conn.close()
                return
            if action != "proxy_tunnel":
                raise AssertionError(f"未知夹具动作: {action}")
            upstream = socket.create_connection(("127.0.0.1", self._target.port), timeout=10)
            try:
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                # 先把读 CONNECT 头时多读进来的 TLS 字节交给目标，再双向中继。
                leftover = head.split(b"\r\n\r\n", 1)[1]
                if leftover:
                    upstream.sendall(leftover)
                forward = threading.Thread(target=self._pump, args=(conn, upstream), daemon=True)
                forward.start()
                self._pump(upstream, conn)
                forward.join(timeout=10)
            finally:
                upstream.close()
        except (OSError, ssl.SSLError):
            pass

    @staticmethod
    def _pump(source, sink):
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                sink.sendall(data)
        except OSError:
            pass
        finally:
            try:
                sink.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def make_session(self, cert_pem, *, timeout=5.0):
        """构造经本代理出站的 WreqSession。走代理的 Client 不能带 ``no_proxy=True``。"""
        session = _make_session(cert_pem, timeout=timeout, no_proxy=False)
        session.proxies = {"https": self.url, "http": self.url}
        return session


def _body_of(request):
    """从一次 recv 读到的原始请求字节里取出请求体；读不到请求体时返回空串。"""
    head, _, body = request.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    return body[:length]


def _make_session(cert_pem, *, timeout=5.0, no_proxy=True):
    session = WreqSession("chrome152")
    # tls_verify 必须放在 Client 构造参数里，逐请求传不生效。
    session._client = wreq.blocking.Client(
        emulation=wreq.Profile.Chrome152, no_proxy=no_proxy,
        tls_verify=wreq.tls.CertStore.from_pem_stack(cert_pem),
        cookie_provider=session.cookies.native,
    )
    session.timeout = timeout
    return session


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



class WreqRedirectHeaderTests(unittest.TestCase):
    def test_same_origin_auth_redirect_preserves_high_entropy_client_hints(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, {name.lower(): value for name, value in self.headers.items()}))
                if self.path == "/api/accounts/email-otp/send":
                    self.send_response(302)
                    self.send_header("Location", "/email-verification")
                else:
                    self.send_response(200)
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        session = BrowserSession(proxy="", detect_exit_geo=False)
        try:
            origin = f"http://127.0.0.1:{server.server_port}"
            headers = session.get_auth_navigate_headers(
                referer=origin + "/", target_origin=origin,
                include_high_entropy=True,
            )
            response = session.get(
                origin + "/api/accounts/email-otp/send",
                headers=headers, allow_redirects=True,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual([path for path, _headers in seen], [
                "/api/accounts/email-otp/send", "/email-verification",
            ])
            expected = {
                "sec-ch-ua-arch", "sec-ch-ua-bitness", "sec-ch-ua-full-version",
                "sec-ch-ua-full-version-list", "sec-ch-ua-model",
                "sec-ch-ua-platform-version",
            }
            for _path, sent_headers in seen:
                self.assertTrue(expected.issubset(sent_headers))
            self.assertEqual(
                seen[1][1]["sec-ch-ua-full-version"],
                session.browser_profile["chrome_full_version"],
            )
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
        session = WreqSession("chrome152", default_headers=False)
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


class WreqBypassSessionTests(unittest.TestCase):
    """旁路模块（邮箱 / 接码 / 管理 API）需要完整浏览器头，默认保留传输层默认头。"""

    def _capture_get(self, session, path="/ping", **kwargs):
        captured = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                captured["headers"] = {name.lower(): value for name, value in self.headers.items()}
                payload = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            session.get(f"http://127.0.0.1:{server.server_port}{path}", **kwargs)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        return captured["headers"]

    def test_bypass_session_keeps_transport_default_headers(self):
        session = WreqSession("chrome146")
        try:
            sent = self._capture_get(session)
        finally:
            session.close()
        self.assertTrue(sent.get("user-agent", "").startswith("Mozilla/5.0"))
        self.assertEqual(sent.get("sec-fetch-mode"), "navigate")
        self.assertEqual(sent.get("upgrade-insecure-requests"), "1")

    def test_session_headers_layer_overrides_defaults(self):
        session = WreqSession("chrome146")
        session.headers.update({"User-Agent": "custom-ua/1.0", "X-Session": "yes"})
        try:
            sent = self._capture_get(session, headers={"X-Request": "1"})
        finally:
            session.close()
        self.assertEqual(sent.get("user-agent"), "custom-ua/1.0")
        self.assertEqual(sent.get("x-session"), "yes")
        self.assertEqual(sent.get("x-request"), "1")



class FaultFixtureSelfCheckTests(unittest.TestCase):
    """夹具自检：每个注入点让 wreq 抛出的原生异常类，必须与 design D1「RST 的阶段」表一致。

    直接用原生 ``wreq.blocking.Client``，绕过 ``WreqSession`` 的映射与重试。
    这张表是 ``_is_transient_failure`` 判定规则的事实依据：表变了，判定规则就要重新评估。
    """

    def _raw_client(self, session):
        # 复用 make_session 已配好的证书信任与 Cookie Jar，只把会话对象换成裸 Client。
        return session._client

    def _assert_raw_raises(self, client, url, expected, read_body=False, **kwargs):
        # 原生 Client 的响应体是惰性读取的：只有显式读 bytes() 才会触发响应体阶段的错误。
        with self.assertRaises(Exception) as caught:
            with client.request(wreq.Method.GET, url, timeout=timedelta(seconds=3),
                                redirect=wreq.redirect.Policy.none(), **kwargs) as raw:
                if read_body:
                    raw.bytes()
        self.assertIs(type(caught.exception), expected, msg=repr(caught.exception))

    def test_stage_table_fin_and_rst(self):
        # (动作, 期望的 wreq 原生异常类, 目标站点收到的连接数, 收到的请求数, 是否需要读响应体)
        cases = [
            ("close_before_handshake", wreq.ConnectionError, 1, 0, False),
            ("close_before_handshake_rst", wreq.ConnectionError, 1, 0, False),
            ("close_after_clienthello", wreq.ConnectionError, 1, 0, False),
            ("close_after_clienthello_rst", wreq.ConnectionError, 1, 0, False),
            ("close_after_request", wreq.RequestError, 1, 1, False),
            ("close_after_request_rst", wreq.ConnectionResetError, 1, 1, False),
            ("truncated", wreq.DecodingError, 1, 1, True),
            ("truncated_rst", wreq.ConnectionResetError, 1, 1, True),
        ]
        for action, expected, connections, requests_seen, read_body in cases:
            with self.subTest(action=action):
                server = _FaultServer(default=action)
                session = server.make_session()
                try:
                    self._assert_raw_raises(self._raw_client(session), server.url, expected, read_body)
                finally:
                    session.close()
                    server.close()
                self.assertEqual(server.connections, connections)
                self.assertEqual(server.requests, requests_seen)

    def test_stage_table_hang_yields_timeout(self):
        for action, requests_seen in (("hang_handshake", 0), ("hang_after_request", 1)):
            with self.subTest(action=action):
                server = _FaultServer(default=action)
                session = server.make_session(timeout=0.5)
                try:
                    self._assert_raw_raises(self._raw_client(session), server.url, wreq.TimeoutError)
                finally:
                    session.close()
                    server.close()
                self.assertEqual(server.connections, 1)
                self.assertEqual(server.requests, requests_seen)

    def test_ok_replies_and_sets_cookie(self):
        server = _FaultServer(default="ok", cookie="device=fixture; Path=/; HttpOnly")
        session = server.make_session()
        try:
            response = session.get(server.url)
            cookie = next(cookie for cookie in session.cookies.jar if cookie.name == "device")
        finally:
            session.close()
            server.close()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ok")
        self.assertEqual(cookie.value, "fixture")

    def test_proxy_actions(self):
        target = _FaultServer(default="close_before_handshake")
        cert_pem, _ = _fixture_keypair()
        try:
            # 代理 accept 后关闭、回 407：客户端在隧道建立前失败，目标站点不应收到连接。
            for action in ("proxy_close", "proxy_407"):
                with self.subTest(action=action):
                    proxy = _FaultProxy(target, default=action)
                    session = proxy.make_session(cert_pem)
                    try:
                        self._assert_raw_raises(
                            self._raw_client(session), target.url, wreq.ProxyConnectionError,
                            proxy=wreq.Proxy.all(proxy.url))
                    finally:
                        session.close()
                        proxy.close()
                    self.assertEqual(proxy.connections, 1)
            self.assertEqual(target.connections, 0)
            # 建隧道后在隧道内握手失败：客户端看到的是握手阶段的 ConnectionError，
            # 而不是 ProxyConnectionError。这条路径决定了 3.2 必须把 ConnectionError
            # 而不是 ProxyConnectionError 当作「隧道内握手失败」的信号。
            proxy = _FaultProxy(target)
            session = proxy.make_session(cert_pem)
            try:
                self._assert_raw_raises(self._raw_client(session), target.url, wreq.ConnectionError,
                                        proxy=wreq.Proxy.all(proxy.url))
            finally:
                session.close()
                proxy.close()
            self.assertEqual(proxy.connects, 1)
            self.assertEqual(target.connections, 1)
        finally:
            target.close()

    def test_proxy_tunnel_relays_success(self):
        target = _FaultServer(default="ok")
        cert_pem, _ = _fixture_keypair()
        proxy = _FaultProxy(target)
        session = proxy.make_session(cert_pem)
        try:
            response = session.get(target.url)
        finally:
            session.close()
            proxy.close()
            target.close()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(proxy.connects, 1)
        self.assertEqual(target.requests, 1)


class TransientFailureClassifierTests(unittest.TestCase):
    """``_is_transient_failure`` 决定传输层重试哪些 wreq 异常（design D1、L）。"""

    TRANSIENT = {"ConnectionError", "TlsError", "ProxyConnectionError", "TimeoutError"}

    def test_every_wreq_exception_class_has_a_verdict(self):
        # 类清单从 wreq.exceptions 动态枚举：wreq 新增异常类时自动落到「不重试」，
        # 并由下面的全集断言逼出一次显式确认，而不是靠人记得补用例。
        classes = {name: cls for name, cls in vars(wreq.exceptions).items()
                   if isinstance(cls, type) and issubclass(cls, Exception)}
        self.assertEqual({name for name, cls in classes.items()
                          if _is_transient_failure(cls("probe"))}, self.TRANSIENT)
        self.assertEqual({name for name, cls in classes.items()
                          if not _is_transient_failure(cls("probe"))},
                         set(classes) - self.TRANSIENT)

    def test_request_sent_errors_are_not_transient(self):
        # 这三个类只在请求已发出后出现（design D1「RST 的阶段」）。重试它们等于重发
        # 一个可能已被服务端处理的请求。ConnectionResetError 与 OSS 不一致是有意为之（L1）。
        for cls in (wreq.ConnectionResetError, wreq.RequestError, wreq.DecodingError):
            with self.subTest(cls=cls.__name__):
                self.assertFalse(_is_transient_failure(cls("probe")))

    def test_non_wreq_exceptions_are_not_transient(self):
        for exc in (requests.ConnectionError("is_connect error"), ConnectionError("is_connect error"),
                    OSError("is_connect error"), TimeoutError("timeout"), Exception("timeout")):
            with self.subTest(cls=type(exc).__name__):
                self.assertFalse(_is_transient_failure(exc))

    def test_message_text_is_not_inspected(self):
        # 判定只看异常类。非瞬断类的异常即使消息带瞬断关键词也不重试；
        # 瞬断类的异常即使消息不含任何关键词也重试。
        self.assertFalse(_is_transient_failure(
            wreq.ConnectionResetError("is_connect error: tls connect error")))
        self.assertFalse(_is_transient_failure(wreq.DecodingError("is_connect error")))
        self.assertTrue(_is_transient_failure(wreq.ConnectionError("")))
        self.assertTrue(_is_transient_failure(wreq.TimeoutError("")))

    def test_transient_subclasses_stay_transient(self):
        class _Retryable(wreq.ConnectionError):
            pass

        self.assertTrue(_is_transient_failure(_Retryable("probe")))


class WreqTransientRetryTests(unittest.TestCase):
    """``WreqSession.request`` 对链路瞬断的同会话重试（传输 spec、design D1 / D2）。"""

    def setUp(self):
        # 真实间隔是 1.5s / 3s；patch 模块级 time.sleep，既不等待也能断言调用参数。
        patcher = mock.patch("time.sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def _sleeps(self):
        return [call.args[0] for call in self.sleep.call_args_list]

    def _get(self, script, *, default="ok", cookie=None, method="GET", **kwargs):
        server = _FaultServer(script, default=default, cookie=cookie)
        session = server.make_session()
        self.addCleanup(server.close)
        self.addCleanup(session.close)
        response = session.request(method, server.url, **kwargs)
        return server, response

    def test_handshake_close_then_success_retries_once(self):
        server, response = self._get(["close_before_handshake"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.connections, 2)
        self.assertEqual(server.requests, 1)
        self.assertEqual(self._sleeps(), [1.5])

    def test_retried_response_cookie_is_recorded_once(self):
        # 第 1 个连接握手前关闭，不可能带任何响应；第 2 个连接下发 Cookie。
        # Cookie 记录只应发生在成功那次：remember_response 恰好被调用 1 次。
        server = _FaultServer(["close_before_handshake"], cookie="device=fixture; Path=/; HttpOnly")
        session = server.make_session()
        self.addCleanup(server.close)
        self.addCleanup(session.close)
        with mock.patch.object(session.cookies, "remember_response",
                               wraps=session.cookies.remember_response) as remember:
            response = session.get(server.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(remember.call_count, 1)
        cookie = next(cookie for cookie in session.cookies.jar if cookie.name == "device")
        self.assertEqual(cookie.value, "fixture")
        self.assertEqual([c.name for c in session.cookies.jar], ["device"])

    def test_retry_sends_byte_identical_request_on_same_client(self):
        # 第 1 个请求已到达服务端、读响应超时；重试用同一会话、同一 Client，
        # 服务端收到的第 2 个请求与第 1 个逐字节相同（传输 spec「重试不改变请求内容」）。
        # 在服务端比较原始字节，而不是 patch Client.request：wreq 的 Client 是只读的扩展类型。
        server = _FaultServer(["hang_after_request"])
        session = server.make_session(timeout=0.3)
        self.addCleanup(server.close)
        self.addCleanup(session.close)
        client = session._client
        response = session.post(server.url, data=b"payload", headers={"X-Probe": "1"})
        self.assertIs(session._client, client)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.requests, 2)
        self.assertEqual(len(server.raw_requests), 2)
        self.assertEqual(server.raw_requests[0], server.raw_requests[1])
        self.assertTrue(server.raw_requests[0].startswith(b"POST /x HTTP/1.1\r\n"))
        self.assertEqual(server.bodies, [b"payload", b"payload"])

    def test_explicit_zero_attempts_disables_retry(self):
        server = _FaultServer(["close_before_handshake"])
        session = server.make_session()
        session.retry_max_attempts = 0
        self.addCleanup(server.close)
        self.addCleanup(session.close)
        with self.assertRaises(requests.ConnectionError):
            session.get(server.url)
        self.assertEqual(server.connections, 1)
        self.assertEqual(self._sleeps(), [])

    def test_config_drives_default_attempts_and_delay(self):
        import config
        with mock.patch.object(config, "WREQ_TRANSIENT_RETRY_MAX_ATTEMPTS", 1), \
                mock.patch.object(config, "WREQ_TRANSIENT_RETRY_BASE_DELAY", 0.25):
            session = WreqSession("chrome152")
        self.addCleanup(session.close)
        self.assertEqual((session.retry_max_attempts, session.retry_base_delay), (1, 0.25))
        # 已构造的会话不受之后配置变化影响（构造时读取一次）。
        with mock.patch.object(config, "WREQ_TRANSIENT_RETRY_MAX_ATTEMPTS", 5):
            self.assertEqual(session.retry_max_attempts, 1)

    # ---- 3.4 重试耗尽与不重试（传输 spec「连续三次链路瞬断后抛出」「请求发出后的连接中断不重试」）

    def test_exhausted_after_three_connect_failures(self):
        # 共 3 次尝试（首次 + 2 次重试），间隔 1.5s、3s；第 3 次失败后抛出，不再 sleep。
        server = _FaultServer(default="close_before_handshake")
        session = server.make_session()
        self.addCleanup(server.close)
        self.addCleanup(session.close)
        with self.assertRaises(requests.ConnectionError) as caught:
            session.get(server.url)
        self.assertEqual(server.connections, 3)
        self.assertEqual(self._sleeps(), [1.5, 3.0])
        self.assertIsInstance(caught.exception.__cause__, wreq.ConnectionError)
        self.assertIn("is_connect error", str(caught.exception))

    def test_handshake_rst_before_handshake_is_retried_like_fin(self):
        # 握手阶段的 RST 抛的也是 ConnectionError（design D1「RST 的阶段」），与 FIN 同样重试。
        server, response = self._get(["close_before_handshake_rst"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.connections, 2)
        self.assertEqual(self._sleeps(), [1.5])

    def test_handshake_rst_exhausted(self):
        server = _FaultServer(default="close_before_handshake_rst")
        session = server.make_session()
        self.addCleanup(server.close)
        self.addCleanup(session.close)
        with self.assertRaises(requests.ConnectionError):
            session.get(server.url)
        self.assertEqual(server.connections, 3)
        self.assertEqual(self._sleeps(), [1.5, 3.0])

    def test_close_after_request_is_not_retried(self):
        for action in ("close_after_request", "close_after_request_rst",
                       "truncated", "truncated_rst"):
            with self.subTest(action=action):
                server = _FaultServer(default=action)
                session = server.make_session()
                try:
                    with self.assertRaises(requests.ConnectionError):
                        session.get(server.url)
                finally:
                    session.close()
                    server.close()
                self.assertEqual(server.connections, 1)
                self.assertEqual(server.requests, 1)
                self.assertEqual(self._sleeps(), [])
                self.sleep.reset_mock()

    def test_exhausted_keeps_last_original_message(self):
        # 抛出的异常对应最后一次（第 3 次）尝试，消息与 __cause__ 的完整文本一致。
        server = _FaultServer(default="close_before_handshake")
        session = server.make_session()
        self.addCleanup(server.close)
        self.addCleanup(session.close)
        with self.assertRaises(requests.ConnectionError) as caught:
            session.get(server.url)
        self.assertEqual(server.connections, 3)
        self.assertIsInstance(caught.exception.__cause__, wreq.ConnectionError)
        self.assertEqual(str(caught.exception), str(caught.exception.__cause__))

    # ---- 3.5 超时与代理（传输 spec「请求超时后重试」「代理连接失败后重试」「POST 重试的重复提交约束」）
    # 超时类用例需要真实等待读超时：会话 timeout 设为 0.3s，time.sleep 仍由 setUp mock。
    TIMEOUT = 0.3

    def _proxy_get(self, script, *, target_script=(), target_default="ok", timeout=TIMEOUT):
        """经代理请求目标站点，返回 (proxy, target, response)。"""
        target = _FaultServer(target_script, default=target_default)
        proxy = _FaultProxy(target, script)
        session = proxy.make_session(_fixture_keypair()[0], timeout=timeout)
        self.addCleanup(proxy.close)
        self.addCleanup(target.close)
        self.addCleanup(session.close)
        return proxy, target, session.get(target.url)

    def test_handshake_timeout_then_success(self):
        server, response = self._get(["hang_handshake"], timeout=self.TIMEOUT)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.connections, 2)
        self.assertEqual(server.requests, 1)
        self.assertEqual(self._sleeps(), [1.5])

    def test_read_timeout_then_success(self):
        # 请求已发出、等响应超时；重试是同一个请求再发一次（design D1a）。
        server, response = self._get(["hang_after_request"], timeout=self.TIMEOUT)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.requests, 2)
        self.assertEqual(self._sleeps(), [1.5])

    def test_three_timeouts_raise_timeout(self):
        server = _FaultServer(default="hang_handshake")
        session = server.make_session(timeout=self.TIMEOUT)
        self.addCleanup(server.close)
        self.addCleanup(session.close)
        with self.assertRaises(requests.Timeout) as caught:
            session.get(server.url)
        self.assertEqual(server.connections, 3)
        self.assertEqual(self._sleeps(), [1.5, 3.0])
        self.assertIsInstance(caught.exception.__cause__, wreq.TimeoutError)
        self.assertEqual(str(caught.exception), str(caught.exception.__cause__))

    def test_three_read_timeouts_resend_identical_post(self):
        # 读响应超时连续 3 次：POST 被原样发出 3 次（重复提交由上层端点处理，design D1a）。
        server = _FaultServer(default="hang_after_request")
        session = server.make_session(timeout=self.TIMEOUT)
        self.addCleanup(server.close)
        self.addCleanup(session.close)
        with self.assertRaises(requests.Timeout):
            session.post(server.url, data=b"payload")
        self.assertEqual(len(server.raw_requests), 3)
        self.assertEqual(server.bodies, [b"payload"] * 3)
        self.assertEqual(len(set(server.raw_requests)), 1)

    def test_post_connect_failure_never_reaches_server_before_retry(self):
        # 连接建立阶段失败时请求体未发出：服务端只收到重试那一次完整请求。
        server, response = self._get(["close_before_handshake"], method="POST", data=b"payload")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.connections, 2)
        self.assertEqual(len(server.raw_requests), 1)
        self.assertEqual(server.bodies, [b"payload"])

    def test_proxy_407_then_success(self):
        proxy, target, response = self._proxy_get(["proxy_407"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(proxy.connections, 2)
        self.assertEqual(proxy.connects, 2)
        self.assertEqual(target.requests, 1)
        self.assertEqual(self._sleeps(), [1.5])

    def test_proxy_close_then_success(self):
        proxy, target, response = self._proxy_get(["proxy_close"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(proxy.connections, 2)
        self.assertEqual(target.requests, 1)

    def test_tunnel_handshake_failure_then_success(self):
        # 隧道已建立、隧道内 TLS 握手失败：抛 ConnectionError（不是 ProxyConnectionError），同样重试。
        proxy, target, response = self._proxy_get([], target_script=["close_before_handshake"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(proxy.connects, 2)
        self.assertEqual(target.connections, 2)
        self.assertEqual(target.requests, 1)
        self.assertEqual(self._sleeps(), [1.5])

    def test_three_proxy_407_raise_proxy_error(self):
        target = _FaultServer(default="ok")
        proxy = _FaultProxy(target, default="proxy_407")
        session = proxy.make_session(_fixture_keypair()[0], timeout=self.TIMEOUT)
        self.addCleanup(proxy.close)
        self.addCleanup(target.close)
        self.addCleanup(session.close)
        with self.assertRaises(requests.exceptions.ProxyError) as caught:
            session.get(target.url)
        # 代理失败映射为 ProxyError，而不是更宽的 ConnectionError（ProxyError 是它的子类）。
        self.assertIs(type(caught.exception), requests.exceptions.ProxyError)
        self.assertEqual(proxy.connects, 3)
        self.assertEqual(self._sleeps(), [1.5, 3.0])
        self.assertIsInstance(caught.exception.__cause__, wreq.ProxyConnectionError)
        self.assertEqual(target.connections, 0)

    def test_three_proxy_close_raise_proxy_error(self):
        target = _FaultServer(default="ok")
        proxy = _FaultProxy(target, default="proxy_close")
        session = proxy.make_session(_fixture_keypair()[0], timeout=self.TIMEOUT)
        self.addCleanup(proxy.close)
        self.addCleanup(target.close)
        self.addCleanup(session.close)
        with self.assertRaises(requests.exceptions.ProxyError):
            session.get(target.url)
        self.assertEqual(proxy.connections, 3)
        self.assertEqual(self._sleeps(), [1.5, 3.0])
        self.assertEqual(target.connections, 0)


class WreqExceptionMappingTests(unittest.TestCase):
    """传输库异常全部映射为 requests 异常，不以 wreq 原生类型穿透。"""

    def test_truncated_body_fin_maps_to_connection_error(self):
        # 必须用 FIN：同一位置用 RST 抛的是 ConnectionResetError，它本来就在 except 链里，
        # 改动前就能通过，验证不了 DecodingError 这条新增的映射。
        server = _FaultServer(default="truncated")
        session = server.make_session()
        try:
            with self.assertRaises(requests.ConnectionError) as caught:
                session.get(server.url)
        finally:
            session.close()
            server.close()
        self.assertIsInstance(caught.exception.__cause__, wreq.DecodingError)
        self.assertEqual(server.connections, 1)

    def test_body_error_maps_to_connection_error(self):
        # BodyError 没有可稳定复现的注入方式，只断言映射分支。
        session = WreqSession("chrome152")
        sentinel = wreq.BodyError("body stream failed")

        class _Client:
            def request(self, *args, **kwargs):
                raise sentinel

        session._client = _Client()
        with self.assertRaises(requests.ConnectionError) as caught:
            session.get("https://localhost/x")
        self.assertIs(caught.exception.__cause__, sentinel)
        self.assertEqual(str(caught.exception), "body stream failed")


if __name__ == "__main__":
    unittest.main()
