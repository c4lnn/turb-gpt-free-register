# -*- coding: utf-8 -*-
"""
Sentinel SDK 宿主适配层。

通过 subprocess 启动项目根目录的 sentinel/sentinel-runner.js，在 Node 的
`node:vm` 沙箱中真实运行 sentinel/sdk.js，完成一次单 flow 的「requirements ->
Python 发起 challenge -> solve」双阶段交互。

契约（结构化 stdin/stdout，逐行 JSON，一个消息一行）：

    Python -> Node:
        {"type": "requirements", "flow": ..., "device_id": ..., ...}
        {"type": "solve", "flow": ..., "challenge": {...}}
        {"type": "close"}

    Node -> Python:
        {"type": "requirements_result", "flow": ..., "p": "<request p>"}
        {"type": "solve_result", "flow": ..., "token": {...}, "so": {...}|null}
        {"type": "error", "phase": ..., "category": ..., "message": ...}
        {"type": "closed"}

敏感值（Cookie / challenge / token / SO）一律通过 stdin/stdout 消息传递，不进
命令行、不进临时文件、不进日志；Node 的原始 stdout/stderr 仅作协议通道或直接
丢弃，不写入日志与错误。
"""
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from config import (
    USER_AGENT,
    CHROME_MAJOR,
    CHROME_FULL_VERSION,
    SEC_CH_UA,
    SEC_CH_UA_PLATFORM,
    SEC_CH_UA_FULL_VERSION_LIST,
    SEC_CH_UA_PLATFORM_VERSION,
    SEC_CH_UA_ARCH,
    SEC_CH_UA_BITNESS,
    SEC_CH_UA_MODEL,
    TIMEZONE_IANA,
    NAVIGATOR_LANGUAGE,
    NAVIGATOR_LANGUAGES,
    SCREEN_WIDTH,
    SCREEN_HEIGHT,
    HARDWARE_CONCURRENCY,
    JS_HEAP_SIZE_LIMIT,
    DEVICE_MEMORY,
    SENTINEL_SV,
    OPENAI_BUILD_ID,
)

logger = logging.getLogger(__name__)

# 项目根目录（core 的上一级）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Node 资源放在项目根的 sentinel/ 子目录下
_SENTINEL_DIR = _PROJECT_ROOT / "sentinel"
_RUNNER_PATH = _SENTINEL_DIR / "sentinel-runner.js"
_SDK_PATH = _SENTINEL_DIR / "sdk.js"

# 各 flow 对应的 page-url（与浏览器实际页面一致，影响 sdk.js 指纹生成）。
_FLOW_PAGE_URL = {
    "username_password_create": "https://auth.openai.com/create-account/password",
    "email_otp_validate": "https://auth.openai.com/email-verification",
    "authorize_continue": "https://auth.openai.com/email-verification",
    "oauth_create_account": "https://auth.openai.com/about-you",
    "password_verify": "https://auth.openai.com/log-in/password",
}

# 这些 Auth 页没有 data-build，也不存在 ChatGPT c/<build> 脚本，SDK 指纹不注入
# 前端 build。当前全部业务 flow 都属于 Auth 页，因此 build_id 恒为空。
_AUTH_FLOWS_NO_BUILD = frozenset({
    "username_password_create",
    "email_otp_validate",
    "authorize_continue",
    "oauth_create_account",
    "password_verify",
})

# 单阶段等待超时（秒）。sdk.js 内部可能要做 PoW，留充裕一点；沿用旧 60s 基线。
_RUNNER_TIMEOUT = 60

# 等待循环轮询间隔（秒），期间调用 check_stop_requested 检查取消。
_POLL_INTERVAL = 0.25


class SentinelSDKError(RuntimeError):
    """安全的结构化 SDK 宿主失败，不含原始 stdout/stderr 或凭据。"""

    def __init__(self, message: str, category: str, phase: str = ""):
        super().__init__(message)
        self.category = category
        self.stage = phase


def _resolve_node_executable() -> str:
    """解析 Node 可执行文件名；允许通过 NODE_EXECUTABLE 覆盖。"""
    override = os.environ.get("NODE_EXECUTABLE")
    if override:
        return override
    return "node.exe" if sys.platform.startswith("win") else "node"


def _ensure_runner_environment() -> None:
    """启动前的强制检查：runner.js / sdk.js 必须存在。"""
    if not _RUNNER_PATH.exists():
        raise SentinelSDKError(
            f"找不到 sentinel-runner.js: {_RUNNER_PATH}", "sdk_missing", "requirements"
        )
    if not _SDK_PATH.exists():
        raise SentinelSDKError(
            f"找不到 sdk.js: {_SDK_PATH}", "sdk_missing", "requirements"
        )


class SentinelSDK:
    """一次单 flow 的双阶段 SDK 宿主；同一 flow 共用同一个短生命周期子进程。"""

    def __init__(
        self,
        *,
        flow: str,
        device_id: str,
        browser_profile: dict | None = None,
        cookie: str = "",
        check_stop_requested=None,
    ):
        if not flow:
            raise ValueError("flow 不能为空")
        if not device_id:
            raise ValueError("device_id 不能为空")
        self.flow = str(flow)
        self.device_id = str(device_id)
        self.browser_profile = dict(browser_profile or {})
        self.cookie = str(cookie or "")
        self.check_stop_requested = check_stop_requested

        self._proc: subprocess.Popen | None = None
        self._stdin = None
        self._stdout = None
        self._stderr = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._out_queue: queue.Queue = queue.Queue()
        self._closed = False
        self._requirements_emitted = False
        self._deadline = 0.0
        self._timezone_iana = ""

    # ---- 上下文管理 -------------------------------------------------------

    def __enter__(self) -> "SentinelSDK":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        # 无论正常返回、HTTP 失败还是取消，finally 语义必须关闭子进程。
        self.close()
        return False

    # ---- 公开接口 ---------------------------------------------------------

    def requirements(self) -> str:
        """启动同一 VM/进程，用真实 SDK 采集并返回 request p。"""
        if self._closed:
            raise SentinelSDKError("Sentinel SDK 宿主已关闭", "closed", "requirements")
        if self._requirements_emitted:
            raise SentinelSDKError(
                "同一 SentinelSDK 实例只能生成一次 requirements", "reuse", "requirements"
            )
        _ensure_runner_environment()
        self._spawn()
        self._requirements_emitted = True

        self._send(self._build_requirements_message())
        resp = self._wait_for_message("requirements", "requirements_result")
        if resp.get("flow") != self.flow:
            raise SentinelSDKError(
                "requirements 返回 flow 与请求不一致", "flow_mismatch", "requirements"
            )
        p = resp.get("p")
        if not isinstance(p, str) or not p.strip():
            raise SentinelSDKError(
                "SDK 未返回有效 requirements p", "requirements_missing", "requirements"
            )
        return p

    def solve(self, challenge: dict) -> tuple[str, str | None]:
        """把当前 challenge 喂回同一 VM/进程，返回 (主 token JSON, SO header 或 None)。"""
        if self._closed:
            raise SentinelSDKError("Sentinel SDK 宿主已关闭", "closed", "solve")
        if not self._requirements_emitted or self._proc is None:
            raise SentinelSDKError(
                "solve 前必须先调用 requirements()", "no_requirements", "solve"
            )
        if (not isinstance(challenge, dict) or not isinstance(challenge.get("token"), str)
                or not challenge["token"].strip()):
            raise SentinelSDKError(
                "challenge 缺少有效 token", "invalid_challenge", "solve"
            )

        self._send({"type": "solve", "flow": self.flow, "challenge": challenge})
        resp = self._wait_for_message("solve", "solve_result")
        if resp.get("flow") != self.flow:
            raise SentinelSDKError(
                "solve 返回 flow 与请求不一致", "flow_mismatch", "solve"
            )

        token = resp.get("token")
        pow_info = challenge.get("proofofwork")
        proof_required = not (isinstance(pow_info, dict) and pow_info.get("required") is False)
        if (not isinstance(token, dict) or not token
                or (proof_required and (not isinstance(token.get("p"), str) or not token["p"].strip()))):
            raise SentinelSDKError("SDK 未返回有效主 token", "empty_main_token", "solve")
        if (token.get("c") != challenge["token"] or token.get("id") != self.device_id
                or token.get("flow") != self.flow):
            raise SentinelSDKError("SDK 主 token 与当前上下文不一致", "token_context_mismatch", "solve")
        token_str = json.dumps(token, separators=(",", ":"), ensure_ascii=False)

        so = resp.get("so")
        if isinstance(so, dict) and so.get("so"):
            so_str = json.dumps(so, separators=(",", ":"), ensure_ascii=False)
        else:
            so_str = None
        return token_str, so_str

    def close(self) -> None:
        """幂等关闭子进程与后台线程。"""
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is None:
            return

        # 先尝试优雅关闭（Node 收到 close 后退出）；随后兜底 terminate/kill。
        try:
            if self._stdin is not None and not self._stdin.closed:
                self._send_nowait({"type": "close"})
        except Exception:
            pass

        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                pass

        for thread in (self._reader_thread, self._stderr_thread):
            if thread is not None and thread is not threading.current_thread():
                try:
                    thread.join(timeout=1)
                except Exception:
                    pass
        for stream in (self._stdin, self._stdout, self._stderr):
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except (OSError, ValueError):
                    pass

    # ---- 子进程生命周期 ----------------------------------------------------

    def _spawn(self) -> None:
        node = _resolve_node_executable()
        env = os.environ.copy()
        # 禁用 sentinel.config.json 自动发现，避免外部配置干扰。
        env.pop("SENTINEL_CONFIG", None)
        env["SENTINEL_CONFIG"] = "__none__"
        # 让 Node VM 里的 Date.toString()/Intl 跟随冻结画像时区（含 DST 规则）。
        self._timezone_iana = str(
            self.browser_profile.get("timezone_iana", TIMEZONE_IANA)
        )
        if self._timezone_iana:
            env["TZ"] = self._timezone_iana

        try:
            self._proc = subprocess.Popen(
                [node, str(_RUNNER_PATH)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                cwd=str(_PROJECT_ROOT),
                env=env,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise SentinelSDKError(
                "未找到 Node 可执行文件，请确认已安装 Node.js 并加入 PATH，"
                "或通过 NODE_EXECUTABLE 环境变量指定绝对路径。",
                "node_missing",
                "requirements",
            ) from exc

        self._stdin = self._proc.stdin
        self._stdout = self._proc.stdout
        self._stderr = self._proc.stderr

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._stderr_thread.start()

    def _reader_loop(self) -> None:
        """把宿主 stdout 逐行塞进队列；EOF 用 None 标记。"""
        try:
            for line in self._stdout:
                self._out_queue.put(line)
        except Exception:
            pass
        finally:
            self._out_queue.put(None)

    def _stderr_loop(self) -> None:
        """丢弃宿主 stderr（协议通道之外的诊断不得进入日志/错误）。"""
        try:
            for _ in self._stderr:
                pass
        except Exception:
            pass

    def _send(self, message: dict) -> None:
        self._send_nowait(message)

    def _send_nowait(self, message: dict) -> None:
        if self._stdin is None or self._stdin.closed:
            return
        try:
            self._stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self._stdin.flush()
        except (BrokenPipeError, OSError):
            # 子进程已退出；后续读取会抛出提前退出的分类错误。
            pass

    def _wait_for_message(self, phase: str, expect_type: str) -> dict:
        # 每个阶段独立 60s 预算；Python 端 HTTP challenge 发生在两个阶段之间，
        # 不计入 SDK 宿主自身采集/求解超时。
        self._deadline = time.monotonic() + _RUNNER_TIMEOUT
        while True:
            self._check_stop()

            proc = self._proc
            if proc is not None and proc.poll() is not None:
                raise SentinelSDKError(
                    f"sentinel SDK 宿主在 {phase} 阶段提前退出（退出码 {proc.returncode}）",
                    "sdk_exec_failed",
                    phase,
                )

            try:
                line = self._out_queue.get(timeout=_POLL_INTERVAL)
            except queue.Empty:
                if time.monotonic() > self._deadline:
                    self._kill()
                    raise SentinelSDKError(
                        f"sentinel SDK 宿主 {phase} 阶段超时（>{_RUNNER_TIMEOUT}s）",
                        "timeout",
                        phase,
                    )
                continue

            if line is None:
                raise SentinelSDKError(
                    f"sentinel SDK 宿主在 {phase} 阶段提前退出",
                    "sdk_exec_failed",
                    phase,
                )

            message = self._parse_message(line, phase)
            if message.get("type") == "error":
                raise SentinelSDKError(
                    str(message.get("message") or "sentinel SDK 宿主执行失败"),
                    str(message.get("category") or "sdk_exec_failed"),
                    str(message.get("phase") or phase),
                )
            if message.get("type") == expect_type:
                return message
            raise SentinelSDKError(
                "sentinel SDK 宿主返回意外消息类型",
                "protocol_error",
                phase,
            )

    def _parse_message(self, line: str, phase: str) -> dict:
        try:
            message = json.loads(line)
        except (TypeError, ValueError):
            raise SentinelSDKError(
                "sentinel SDK 宿主输出不是合法 JSON", "protocol_error", phase
            ) from None
        if not isinstance(message, dict):
            raise SentinelSDKError(
                "sentinel SDK 宿主输出不是 JSON 对象", "protocol_error", phase
            )
        return message

    def _check_stop(self) -> None:
        if self.check_stop_requested is not None:
            self.check_stop_requested()

    def _kill(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass

    # ---- 画像到消息映射 ----------------------------------------------------

    def _build_requirements_message(self) -> dict:
        profile = self.browser_profile

        def pv(key, default):
            value = profile.get(key)
            return default if value is None else value

        browser_family = str(pv("browser_family", "chrome"))
        request_idle_callback = int(
            (pv("window_feature_flags", {}) or {}).get("requestIdleCallback", 0)
        )
        ua = str(pv("user_agent", USER_AGENT))
        screen_width = int(pv("screen_width", SCREEN_WIDTH))
        screen_height = int(pv("screen_height", SCREEN_HEIGHT))
        hardware_concurrency = int(pv("hardware_concurrency", HARDWARE_CONCURRENCY))
        js_heap_size_limit = int(pv("js_heap_size_limit", JS_HEAP_SIZE_LIMIT))
        device_memory = int(pv("device_memory", DEVICE_MEMORY))
        device_pixel_ratio = float(pv("device_pixel_ratio", 2))
        screen_avail_width = int(pv("screen_avail_width", screen_width))
        screen_avail_height = int(pv("screen_avail_height", max(0, screen_height - 25)))
        outer_width = int(pv("outer_width", screen_avail_width))
        outer_height = int(pv("outer_height", screen_avail_height))
        inner_width = int(pv("inner_width", pv("viewport_width", outer_width)))
        inner_height = int(
            pv("inner_height", pv("viewport_height", max(0, outer_height - 87)))
        )
        color_depth = int(pv("color_depth", 24))
        navigator_language = str(pv("navigator_language", NAVIGATOR_LANGUAGE))
        navigator_languages = list(pv("navigator_languages", NAVIGATOR_LANGUAGES))
        chrome_major = str(pv("chrome_major", CHROME_MAJOR))
        chrome_full_version = str(pv("chrome_full_version", CHROME_FULL_VERSION))
        sec_ch_ua = str(pv("sec_ch_ua", SEC_CH_UA))
        sec_ch_ua_platform = str(pv("sec_ch_ua_platform", SEC_CH_UA_PLATFORM))
        navigator_platform = str(pv("navigator_platform", "MacIntel"))
        navigator_vendor = str(pv("navigator_vendor", "Google Inc."))
        user_agent_data_platform = str(
            pv("user_agent_data_platform", sec_ch_ua_platform.strip('"') or "macOS")
        )
        sec_ch_ua_full_version_list = str(
            pv("sec_ch_ua_full_version_list", SEC_CH_UA_FULL_VERSION_LIST)
        )
        sec_ch_ua_platform_version = str(
            pv("sec_ch_ua_platform_version", SEC_CH_UA_PLATFORM_VERSION)
        )
        sec_ch_ua_arch = str(pv("sec_ch_ua_arch", SEC_CH_UA_ARCH))
        sec_ch_ua_bitness = str(pv("sec_ch_ua_bitness", SEC_CH_UA_BITNESS))
        sec_ch_ua_model = str(pv("sec_ch_ua_model", SEC_CH_UA_MODEL))

        page_url = _FLOW_PAGE_URL.get(
            self.flow, "https://auth.openai.com/create-account/password"
        )
        build_id = (
            "" if self.flow in _AUTH_FLOWS_NO_BUILD else str(pv("build_id", OPENAI_BUILD_ID))
        )
        # challenge 请求的初始 p[5] 都来自版本化 SDK；最终提交头由 Auth 页包装
        # SDK 生成：密码/资料/登录页为 backend-api，OTP 页为版本化地址。
        script_src = (
            f"https://sentinel.openai.com/sentinel/{SENTINEL_SV}/sdk.js"
            if self.flow == "email_otp_validate"
            else "https://sentinel.openai.com/backend-api/sentinel/sdk.js"
        )

        return {
            "type": "requirements",
            "flow": self.flow,
            "device_id": self.device_id,
            "cookie": self.cookie,
            "sdk_path": str(_SDK_PATH),
            "page_url": page_url,
            "script_src": script_src,
            "build_id": build_id,
            "browser_family": browser_family,
            "request_idle_callback": 1 if request_idle_callback else 0,
            "user_agent": ua,
            "navigator_platform": navigator_platform,
            "navigator_vendor": navigator_vendor,
            "user_agent_data_platform": user_agent_data_platform,
            "language": navigator_language,
            "languages": navigator_languages,
            "time_zone": self._timezone_iana,
            "screen": {
                "width": screen_width,
                "height": screen_height,
                "availWidth": screen_avail_width,
                "availHeight": screen_avail_height,
                "colorDepth": color_depth,
                "pixelDepth": color_depth,
            },
            "inner_width": inner_width,
            "inner_height": inner_height,
            "outer_width": outer_width,
            "outer_height": outer_height,
            "device_pixel_ratio": device_pixel_ratio,
            "hardware_concurrency": hardware_concurrency,
            "js_heap_size_limit": js_heap_size_limit,
            "device_memory": device_memory,
            "chrome_major": chrome_major,
            "chrome_full_version": chrome_full_version,
            "sec_ch_ua": sec_ch_ua,
            "sec_ch_ua_platform": sec_ch_ua_platform,
            "sec_ch_ua_full_version_list": sec_ch_ua_full_version_list,
            "sec_ch_ua_platform_version": sec_ch_ua_platform_version,
            "sec_ch_ua_arch": sec_ch_ua_arch,
            "sec_ch_ua_bitness": sec_ch_ua_bitness,
            "sec_ch_ua_model": sec_ch_ua_model,
            "webgl_vendor": str(pv("webgl_vendor", "")),
            "webgl_renderer": str(pv("webgl_renderer", "")),
            "react_listening_key": str(profile.get("react_listening_key") or ""),
            "react_container_key": str(profile.get("react_container_key") or ""),
            "react_resources_key": str(profile.get("react_resources_key") or ""),
        }
