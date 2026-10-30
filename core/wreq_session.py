"""将 wreq 的同步客户端适配为 BrowserSession 使用的 requests 风格接口。"""
from datetime import timedelta
from http.client import HTTPMessage, responses
from collections.abc import Mapping
from urllib.parse import unquote, urlparse, urlsplit

from requests import Request, Response, exceptions
from requests.cookies import MockRequest, MockResponse, RequestsCookieJar, create_cookie
from requests.structures import CaseInsensitiveDict
from requests.utils import get_encoding_from_headers


class WreqCookies:
    def __init__(self, native, wreq):
        self.native = native
        self._wreq = wreq
        self._host_cookies = RequestsCookieJar()

    def remember_response(self, url, headers):
        """原生 Jar 不暴露 host-only 来源；从真实响应及重定向恢复该元数据。"""
        message = HTTPMessage()
        for name, value in headers:
            if bytes(name).lower() == b"set-cookie":
                message.add_header("Set-Cookie", bytes(value).decode("latin-1"))
        if message.get_all("Set-Cookie"):
            self._host_cookies.extract_cookies(
                MockResponse(message), MockRequest(Request("GET", str(url)).prepare())
            )

    def set(self, name, value, domain="", path="/", secure=False, **kwargs):
        if not domain:
            raise ValueError("wreq Cookie 必须指定 domain")
        origin = f"{'https' if secure else 'http'}://{domain.lstrip('.')}{path}"
        self.native.add(self._wreq.Cookie(
            name, str(value), domain=domain, path=path, secure=secure, **kwargs
        ), origin)

    @property
    def jar(self):
        # 仅用于现有代码枚举 Cookie；原生 Jar 负责重定向中的 Cookie 更新及发送。
        jar = RequestsCookieJar()
        for cookie in self.native.get_all():
            if not cookie.domain:
                matches = [known for known in self._host_cookies
                           if not known.domain_specified and known.name == cookie.name
                           and known.value == cookie.value and known.path == (cookie.path or "/")]
                if matches:
                    for known in matches:
                        origin = f"{'https' if known.secure else 'http'}://{known.domain}{known.path}"
                        current = self.native.get(known.name, origin)
                        if current is not None and not current.domain and current.value == known.value:
                            jar.set_cookie(known)
                    continue
            jar.set_cookie(create_cookie(
                cookie.name, cookie.value, domain=cookie.domain or "",
                path=cookie.path or "/", secure=cookie.secure,
                expires=int(cookie.expires.timestamp()) if cookie.expires else None,
            ))
        return jar

    def get(self, name, default=None, domain=None, path=None):
        return self.jar.get(name, default, domain=domain, path=path)

    def clear(self):
        self.native.clear()
        self._host_cookies.clear()


class WreqSession:
    def __init__(self, impersonate):
        try:
            import wreq
            from wreq.blocking import Client
        except ImportError as exc:
            raise RuntimeError("使用 wreq 需要 Python >=3.11，并安装 requirements.txt 中的 wreq") from exc
        profile_name = next((name for name in dir(wreq.Profile)
                             if name.lower() == impersonate.lower()), "")
        profile = getattr(wreq.Profile, profile_name, None)
        if profile is None:
            raise ValueError(f"wreq 不支持指纹 {impersonate}，请检查浏览器配置及 wreq 版本")
        self.impersonate = profile_name
        self._wreq = wreq
        jar = wreq.Jar()
        self._client = Client(emulation=profile, cookie_provider=jar,
                              default_headers=False, no_proxy=True)
        self.cookies = WreqCookies(jar, wreq)
        self.proxies = {}
        self.timeout = 30

    def _build_proxy(self, proxy):
        """把 requests 风格的代理 URL 转成 wreq Proxy，并保留账号密码。

        wreq.Proxy.all 不会从 URL 中解析 ``user:pass@``，会把账号密码当作
        主机名的一部分导致连接失败；这里拆出账号密码作为单独参数传入。
        """
        parsed = urlsplit(str(proxy).strip())
        kwargs = {}
        if parsed.username is not None:
            kwargs["username"] = unquote(parsed.username)
        if parsed.password is not None:
            kwargs["password"] = unquote(parsed.password)
        if not kwargs:
            return self._wreq.Proxy.all(str(proxy))
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        clean = parsed._replace(netloc=host).geturl()
        return self._wreq.Proxy.all(clean, **kwargs)

    def request(self, method, url, **kwargs):
        if self._client is None:
            raise RuntimeError("wreq 会话已关闭")
        options = {}
        for key in ("headers", "json", "cookies"):
            if key in kwargs:
                options[key] = kwargs.pop(key)
        if "params" in kwargs:
            options["query"] = kwargs.pop("params")
        if "data" in kwargs:
            data = kwargs.pop("data")
            options["form" if isinstance(data, (Mapping, list, tuple)) else "body"] = data
        timeout = kwargs.pop("timeout", self.timeout)
        if timeout is not None:
            options["timeout"] = timedelta(seconds=float(timeout))
        options["redirect"] = (self._wreq.redirect.Policy.limited(30)
                               if kwargs.pop("allow_redirects", True)
                               else self._wreq.redirect.Policy.none())
        proxies = kwargs.pop("proxies", self.proxies) or {}
        proxy = proxies.get(urlparse(url).scheme) or proxies.get("all")
        if proxy:
            options["proxy"] = self._build_proxy(proxy)
        if kwargs:
            raise TypeError(f"wreq 暂不支持请求参数: {', '.join(sorted(kwargs))}")
        try:
            with self._client.request(getattr(self._wreq.Method, method.upper()), url, **options) as raw:
                for hop in raw.history:
                    self.cookies.remember_response(hop.previous, hop.headers)
                self.cookies.remember_response(raw.url, raw.headers)
                response = Response()
                response.status_code = raw.status.as_int()
                response.reason = responses.get(response.status_code, "")
                response.url = str(raw.url)
                response.headers = CaseInsensitiveDict((bytes(k).decode("ascii"), bytes(v).decode("latin-1"))
                                                       for k, v in raw.headers)
                response._content = bytes(raw.bytes())
                response._content_consumed = True
                response.encoding = get_encoding_from_headers(response.headers)
                response.cookies = self.cookies.jar
                return response
        except self._wreq.TimeoutError as exc:
            raise exceptions.Timeout(str(exc)) from exc
        except self._wreq.ProxyConnectionError as exc:
            raise exceptions.ProxyError(str(exc)) from exc
        except self._wreq.TlsError as exc:
            raise exceptions.SSLError(str(exc)) from exc
        except (self._wreq.ConnectionError, self._wreq.ConnectionResetError) as exc:
            raise exceptions.ConnectionError(str(exc)) from exc
        except self._wreq.RequestError as exc:
            raise exceptions.ConnectionError(str(exc)) from exc

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def close(self):
        # blocking.Client.close() 取消挂起请求并拒绝新请求，释放连接池。
        client, self._client = self._client, None
        if client is not None:
            client.close()
