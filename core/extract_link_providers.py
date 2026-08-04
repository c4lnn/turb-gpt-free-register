# -*- coding: utf-8 -*-
"""提链服务提供方协议适配器。"""
from __future__ import annotations

import json
from urllib.parse import quote, urlencode
from urllib.request import ProxyHandler, Request, build_opener, urlopen

try:
    from curl_cffi import requests as curl_requests
except Exception:
    curl_requests = None


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def extract_error_message(data) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, dict):
        return str(data)
    err = data.get("error")
    if isinstance(err, dict):
        for key in ("message", "detail", "reason", "error", "msg", "description"):
            if err.get(key):
                return str(err[key]).strip()
        return json.dumps(err, ensure_ascii=False)[:500]
    if err:
        return str(err).strip()
    for key in ("message", "detail", "reason", "msg", "description", "raw"):
        if data.get(key):
            return str(data[key]).strip()
    return json.dumps(data, ensure_ascii=False)[:500]


def _status_error(status: int, data) -> ProviderError:
    message = extract_error_message(data) or f"HTTP {status}"
    return ProviderError(message[:500], status_code=status, retryable=status == 429 or status >= 500)


class _BaseProvider:
    def __init__(self, *, base_url: str, timeout: int, proxy: str | None = None, session=None):
        self.base_url = str(base_url or "").strip().rstrip("/")
        if not self.base_url:
            raise ValueError("提链服务地址为空")
        self.timeout = int(timeout)
        self.proxy = str(proxy or "").strip()
        self.session = session if session is not None else (curl_requests.Session() if curl_requests else None)
        self._owns_session = session is None

    def close(self) -> None:
        if self._owns_session and self.session is not None:
            try:
                self.session.close()
            except Exception:
                pass

    def _request_json(self, method: str, path: str, *, headers: dict | None = None, json_body=None) -> dict:
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json", **(headers or {})}
        secrets = [
            str(headers.get("X-CDK") or ""),
            self.proxy,
            *(
                str(json_body.get(key) or "")
                for key in ("access_token", "token", "cdk")
                if isinstance(json_body, dict)
            ),
        ]
        response = None
        try:
            if self.session is not None:
                kwargs = {"headers": headers, "json": json_body, "timeout": self.timeout}
                if self.proxy:
                    kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
                response = self.session.request(method, url, **kwargs)
                try:
                    data = response.json()
                except Exception:
                    data = {"error": str(response.text or "")[:300]}
                if response.status_code < 200 or response.status_code >= 300:
                    raise _status_error(response.status_code, data)
                if not isinstance(data, dict):
                    raise ProviderError("提链服务返回的 JSON 不是对象")
                return data

            body = None if json_body is None else json.dumps(json_body).encode("utf-8")
            if body is not None:
                headers["Content-Type"] = "application/json"
            request = Request(url, data=body, headers=headers, method=method)
            opener = build_opener(ProxyHandler({"http": self.proxy, "https": self.proxy})) if self.proxy else None
            with (opener.open(request, timeout=self.timeout) if opener else urlopen(request, timeout=self.timeout)) as response:
                data = json.loads(response.read().decode("utf-8", "replace") or "{}")
            if not isinstance(data, dict):
                raise ProviderError("提链服务返回的 JSON 不是对象")
            return data
        except ProviderError as exc:
            message = str(exc)
            for secret in secrets:
                if secret:
                    message = message.replace(secret, "***")
            raise ProviderError(message, status_code=exc.status_code, retryable=exc.retryable) from exc
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            for secret in secrets:
                if secret:
                    message = message.replace(secret, "***")
            raise ProviderError(message, retryable=True) from exc
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass


class LegacyExtractProvider(_BaseProvider):
    name = "legacy"

    def __init__(self, *, base_url: str, cdk: str, timeout: int, event_timeout: int, proxy: str | None = None, session=None):
        super().__init__(base_url=base_url, timeout=timeout, proxy=proxy, session=session)
        self.cdk = str(cdk or "").strip()
        if not self.cdk:
            raise ValueError("EXTRACT_LINK_CDK/CDK 为空")
        self.event_timeout = int(event_timeout)

    def query_quota(self) -> dict:
        return self._request_json("GET", f"/api/cdk?{urlencode({'code': self.cdk})}")

    def create_job(self, *, access_token: str, link_type: str) -> dict:
        data = self._request_json(
            "POST",
            "/api/extract",
            headers={"Content-Type": "application/json"},
            json_body={"link_type": link_type, "cdk": self.cdk, "token": access_token},
        )
        if not data.get("job_id"):
            raise ProviderError("提链服务未返回 job_id")
        return data

    def iter_events(self, *, job_id: str):
        path = f"/api/jobs/{quote(str(job_id), safe='')}/events?{urlencode({'cdk': self.cdk})}"
        url = f"{self.base_url}{path}"
        headers = {"Accept": "text/event-stream"}
        response = None
        try:
            if self.session is not None:
                kwargs = {"headers": headers, "timeout": self.event_timeout, "stream": True}
                if self.proxy:
                    kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
                response = self.session.get(url, **kwargs)
                if response.status_code < 200 or response.status_code >= 300:
                    raise _status_error(response.status_code, str(response.text or "")[:300])
                lines = response.iter_lines()
            else:
                request = Request(url, headers=headers)
                opener = build_opener(ProxyHandler({"http": self.proxy, "https": self.proxy})) if self.proxy else None
                response = opener.open(request, timeout=self.event_timeout) if opener else urlopen(request, timeout=self.event_timeout)
                lines = response

            event = "message"
            data_lines: list[str] = []
            for raw in lines:
                if raw is None:
                    continue
                line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                line = line.rstrip("\r\n")
                if line == "":
                    if data_lines:
                        text = "\n".join(data_lines)
                        try:
                            data = json.loads(text)
                        except Exception:
                            data = {"raw": text}
                        yield event, data
                    event = "message"
                    data_lines = []
                elif line.startswith(":"):
                    continue
                elif line.startswith("event:"):
                    event = line.split(":", 1)[1].strip() or "message"
                elif line.startswith("data:"):
                    data_lines.append(line.split(":", 1)[1].lstrip())
            if data_lines:
                text = "\n".join(data_lines)
                try:
                    data = json.loads(text)
                except Exception:
                    data = {"raw": text}
                yield event, data
        except ProviderError as exc:
            message = str(exc).replace(self.cdk, "***")
            if self.proxy:
                message = message.replace(self.proxy, "***")
            raise ProviderError(
                message,
                status_code=exc.status_code,
                retryable=exc.retryable,
            ) from exc
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}".replace(self.cdk, "***")
            if self.proxy:
                message = message.replace(self.proxy, "***")
            raise ProviderError(
                message,
                retryable=True,
            ) from exc
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass


class MasiKakaoProvider(_BaseProvider):
    name = "masi"

    @staticmethod
    def _headers(cdk: str) -> dict:
        value = str(cdk or "").strip()
        if not value:
            raise ValueError("Masi CDK 为空")
        return {"X-CDK": value, "Content-Type": "application/json"}

    def query_quota(self, *, cdk: str) -> dict:
        data = self._request_json("GET", "/v1/cdk/status", headers=self._headers(cdk))
        quota = data.get("cdk")
        if not isinstance(quota, dict):
            raise ProviderError("Masi 额度响应缺少 cdk 对象")
        normalized: dict[str, int] = {}
        for key in ("total_uses", "remaining_uses", "pending_uses", "available_uses"):
            value = quota.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ProviderError(f"Masi 额度响应字段 {key} 不是整数")
            normalized[key] = value
        return normalized

    def create_job(self, *, cdk: str, access_token: str) -> dict:
        data = self._request_json(
            "POST",
            "/v1/kakao/jobs",
            headers=self._headers(cdk),
            json_body={"access_token": access_token},
        )
        job = data.get("job")
        if not isinstance(job, dict) or not job.get("job_id"):
            raise ProviderError("Masi 创建任务响应缺少 job.job_id")
        return job

    def get_job(self, *, cdk: str, job_id: str) -> dict:
        data = self._request_json(
            "GET",
            f"/v1/kakao/jobs/{quote(str(job_id), safe='')}",
            headers=self._headers(cdk),
        )
        job = data.get("job")
        if not isinstance(job, dict) or not str(job.get("status") or "").strip():
            raise ProviderError("Masi 查询任务响应缺少 job.status")
        return job

    def cancel_job(self, *, cdk: str, job_id: str) -> dict:
        return self._request_json(
            "POST",
            f"/v1/kakao/jobs/{quote(str(job_id), safe='')}/cancel",
            headers=self._headers(cdk),
            json_body={},
        )
