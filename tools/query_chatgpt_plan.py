#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""使用 AT 请求 ChatGPT 套餐接口并输出响应体。

用法：
    . .\.venv\Scripts\Activate.ps1
    python tools/query_chatgpt_plan.py --at '<AT>'

说明：
    - 默认沿用项目的 PLAN_CHECK_PROXY_MODE / PLAN_CHECK_PROXY / PROXY_POOL。
    - `--proxy ''` 表示强制直连；传入具体地址表示使用该代理。
    - AT 仅用于当前进程，不写入文件、不打印到日志。
    - 响应体可能包含账号身份字段，请谨慎保存或转发脚本输出。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import quote

# 允许直接从项目根目录运行 tools/ 下的脚本。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from config import proxy as proxy_cfg  # noqa: E402
from core.chatgpt_plan import (  # noqa: E402
    ACCOUNTS_CHECK_PATH,
    _common_headers,
    normalize_token,
    resolve_plan_check_route,
)
from core.session import BrowserSession  # noqa: E402


def _parse_response(text: str):
    try:
        return json.loads(text), True
    except (TypeError, ValueError):
        return text, False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="请求 ChatGPT accounts/check 套餐接口并输出响应体",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--at", required=True, help="ChatGPT access token，可带或不带 Bearer 前缀")
    parser.add_argument(
        "--proxy",
        default=None,
        help="代理；不传沿用项目套餐网络策略，传空字符串强制直连",
    )
    parser.add_argument(
        "--timezone-offset-min",
        default="-",
        help="accounts/check 查询参数，默认 -",
    )
    args = parser.parse_args()

    token = normalize_token(args.at)
    if not token:
        parser.error("--at 不能为空")

    try:
        route = resolve_plan_check_route(args.proxy)
        timeout = float(getattr(proxy_cfg, "PLAN_CHECK_TIMEOUT", 15.0) or 15.0)
        timeout = max(1.0, min(60.0, timeout))
        url = (
            f"https://chatgpt.com{ACCOUNTS_CHECK_PATH}"
            f"?timezone_offset_min={quote(str(args.timezone_offset_min))}"
        )
        env = BrowserSession(proxy=route["proxy"], detect_exit_geo=False)
    except Exception as exc:
        print(f"初始化请求失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    try:
        response = env.session.get(
            url,
            headers=_common_headers(env, token),
            allow_redirects=False,
            timeout=timeout,
        )
        body = response.text or ""
        parsed, is_json = _parse_response(body)
        print(
            f"HTTP {int(response.status_code)}; content-type={response.headers.get('content-type', '')}",
            file=sys.stderr,
        )
        if is_json:
            print(json.dumps(parsed, ensure_ascii=False, indent=2))
        else:
            print(body)
        return 0 if 200 <= int(response.status_code) < 300 else 1
    except Exception as exc:
        print(f"请求失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            env.session.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
