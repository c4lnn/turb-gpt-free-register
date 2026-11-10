# -*- coding: utf-8 -*-
"""注册会话预检：出口地理冻结 + 服务端设备 cookie 初始化。

只服务 server_device_id 注册会话。普通登录（含 Codex 独立 OAuth）走
``auth.openai_auth.network_preflight`` 的非注册分支，两条路径互不依赖。
"""
from __future__ import annotations

import logging

from config import openai_protocol as _protocol_cfg
from core.infra import job_context
from core.openai.register.registration_geo import (
    CATEGORY_GEO_PARSE,
    CATEGORY_GEO_SERVICE,
    CATEGORY_PROXY,
    STAGE_DETECT,
    RegistrationGeoError,
    classify_geo_exception,
    validate_session_geo,
)
from core.openai.transport.session import reset_retryable_circuit

logger = logging.getLogger(__name__)


def registration_preflight(session) -> None:
    """初始 trace 冻结画像；有界 CSRF warmup 直接建立正式注册会话。"""
    job_context.check_stop_requested()
    fields = None
    try:
        logger.info("[注册预检:trace] 初始出口观测")
        response = session.get(
            "https://cloudflare.com/cdn-cgi/trace",
            headers=session.get_auth_navigate_headers(referer=""),
            timeout=15,
        )
        status = int(response.status_code)
        if status != 200:
            category = CATEGORY_PROXY if status == 407 else CATEGORY_GEO_SERVICE
            raise RegistrationGeoError(f"地域观测服务不可用：HTTP {status}", category, STAGE_DETECT)
    except job_context.StopRequested:
        raise
    except Exception as exc:
        if isinstance(exc, RegistrationGeoError):
            error = exc
        else:
            category, reason = classify_geo_exception(exc)
            error = RegistrationGeoError(f"地域观测不可用：{reason}", category, STAGE_DETECT)
        reset_retryable_circuit(session)
        if not session.geo_frozen:
            raise error from None
        logger.warning("[注册预检:trace] 后续观测不可用 category=%s，保留冻结画像", error.category)
    else:
        try:
            fields = dict(line.split("=", 1) for line in response.text.splitlines() if "=" in line)
            if not fields:
                raise ValueError("empty trace")
        except Exception:
            raise RegistrationGeoError("地域观测响应无法解析", CATEGORY_GEO_PARSE, STAGE_DETECT) from None
    if fields is not None:
        validate_session_geo(session, fields)

    headers = session.get_chatgpt_navigate_headers(referer="", user_initiated=False)
    headers.update({
        "accept": "application/json",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    })
    # 该请求是 XHR 而非文档导航：导航专属头必须去掉，否则与浏览器报文不一致。
    headers.pop("upgrade-insecure-requests", None)
    attempts = max(1, int(_protocol_cfg.OPENAI_REGISTRATION_WARMUP_ATTEMPTS))
    timeout = max(1.0, float(_protocol_cfg.OPENAI_REGISTRATION_WARMUP_TIMEOUT))
    for attempt in range(1, attempts + 1):
        job_context.check_stop_requested()
        if attempt > 1:
            job_context.interruptible_sleep(3 + 2 * (attempt - 1))
            session.reset_preflight_transport()
        try:
            logger.info("[注册预检:warmup] 初始化服务端设备 cookie (%s/%s)", attempt, attempts)
            response = session.get(
                "https://chatgpt.com/api/auth/csrf",
                headers=headers,
                allow_redirects=True,
                timeout=timeout,
            )
            response.raise_for_status()
        except job_context.StopRequested:
            raise
        except Exception as exc:
            logger.warning("[注册预检:warmup] 请求未正常完成 (%s)，检查已接收 cookie", type(exc).__name__)
        job_context.check_stop_requested()
        device_id = session.get_server_device_id()
        if device_id:
            session.device_id = device_id
            reset_retryable_circuit(session)
            logger.info("[注册预检:warmup] 服务端设备身份已绑定 (%s/%s)", attempt, attempts)
            return
        logger.warning("[注册预检:warmup] 缺少有效服务端设备 cookie，剩余尝试=%s", attempts - attempt)
    raise RuntimeError(f"注册初始化失败：{attempts} 次 warmup 后仍无有效服务端 oai-did")


__all__ = ["registration_preflight"]
