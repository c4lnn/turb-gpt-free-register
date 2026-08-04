# -*- coding: utf-8 -*-
"""Read-only polling for an existing Masi job without creating a new job."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import db, masi_cdk_pool
from core.extract_link_providers import ProviderError, extract_error_message
from core.extract_link_service import _masi_provider, _normalize_success_result, resolve_route


def _timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _result_summary(job: dict) -> dict:
    output = job.get("output") if isinstance(job.get("output"), dict) else {}
    long_url = str(output.get("long_url") or "").strip()
    return {
        "status": str(job.get("status") or "").strip().lower(),
        "message": str(job.get("message") or "")[:300],
        "long_url_present": bool(long_url),
        "long_url_preview": f"{long_url[:24]}..." if long_url else "",
        "payment_method": output.get("payment_method"),
        "payment_link_type": output.get("payment_link_type"),
        "expires_at": output.get("expires_at"),
        "cdk_remaining": output.get("cdk_remaining"),
    }


def poll_existing_job(account_id: int, interval: float, timeout: float, direct: bool, write_result: bool) -> int:
    account = db.get_account(account_id)
    if not account:
        raise RuntimeError(f"Account {account_id} not found")

    job_id = str(account.get("extract_link_job_id") or "").strip()
    cdk_id = str(account.get("extract_link_cdk_id") or "").strip()
    if not job_id:
        raise RuntimeError(f"Account {account_id} has no saved extract_link_job_id")
    if not cdk_id:
        raise RuntimeError(f"Account {account_id} has no saved extract_link_cdk_id")

    bound = masi_cdk_pool.get_secret(cdk_id)
    if not bound or not str(bound.get("cdk") or "").strip():
        raise RuntimeError("The original CDK is no longer available")

    saved_fingerprint = str(account.get("extract_link_cdk_fingerprint") or "").strip()
    if saved_fingerprint and saved_fingerprint != str(bound.get("fingerprint") or "").strip():
        raise RuntimeError("The saved CDK fingerprint does not match the current CDK")

    route = resolve_route(link_type="kakao_pay", provider="masi", update_mode="poll")
    if direct:
        route["proxy"] = ""
    adapter = _masi_provider(route)
    deadline = time.monotonic() + timeout
    consecutive_errors = 0

    print(
        f"{_timestamp()} account_id={account_id} job_id={job_id} "
        f"read_only=true network={'direct' if direct else 'configured'}",
        flush=True,
    )
    try:
        while time.monotonic() < deadline:
            try:
                job = adapter.get_job(cdk=bound["cdk"], job_id=job_id)
                consecutive_errors = 0
            except ProviderError as exc:
                consecutive_errors += 1
                print(
                    f"{_timestamp()} poll_error={type(exc).__name__}: {exc} "
                    f"retryable={exc.retryable} consecutive_errors={consecutive_errors}",
                    flush=True,
                )
                if not exc.retryable or consecutive_errors >= 3:
                    return 2
                time.sleep(interval)
                continue

            status = str(job.get("status") or "").strip().lower()
            message = str(job.get("message") or "")[:300]
            print(f"{_timestamp()} status={status or '-'} message={message}", flush=True)

            if status == "completed":
                if write_result:
                    output = job.get("output") if isinstance(job.get("output"), dict) else {}
                    db.update_account_extract(account_id, {
                        "ok": True,
                        "status": "success",
                        "job_id": job_id,
                        "result": _normalize_success_result(output),
                        "link_type": "kakao_pay",
                        "provider": "masi",
                        "update_mode": "poll",
                        "checked_at": _timestamp(),
                        "message": "Masi Job completed",
                    })
                print(json.dumps(_result_summary(job), ensure_ascii=False, indent=2), flush=True)
                print(f"result_written={str(write_result).lower()}", flush=True)
                return 0
            if status in {"failed", "canceled"}:
                summary = _result_summary(job)
                summary["error"] = extract_error_message(job)
                print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
                return 2
            if status not in {"queued", "running"}:
                print(f"{_timestamp()} unexpected_status={status or '-'}", flush=True)
                return 2

            time.sleep(interval)

        print(f"{_timestamp()} timeout={timeout}s job_left_unchanged=true", flush=True)
        return 3
    finally:
        adapter.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Poll an existing Masi job without resubmitting it")
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--interval", type=float, default=2.5)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--direct", action="store_true", help="Bypass the configured proxy")
    parser.add_argument("--write-result", action="store_true", help="Persist a completed result to the account")
    args = parser.parse_args()
    if args.interval <= 0 or args.timeout <= 0:
        parser.error("--interval and --timeout must be positive")
    try:
        return poll_existing_job(args.account_id, args.interval, args.timeout, args.direct, args.write_result)
    except Exception as exc:
        print(f"{_timestamp()} fatal={type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
