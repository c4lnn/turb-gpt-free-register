# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import live_check_service


class FakeTransport:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeBrowserSession:
    def __init__(self):
        self.proxy = "http://proxy.example:8080"
        self.session = FakeTransport()


class LivePlanChainTests(unittest.TestCase):
    def test_successful_live_check_queries_plan_with_new_token_and_same_session(self):
        env = FakeBrowserSession()
        live_result = {"ok": True, "status": "live", "access_token": "new-at", "_browser_session": env}
        plan_result = {"ok": True, "current_plan_type": "free"}
        live_check_service._QUEUE_SLOTS.acquire()
        with (
            patch.object(live_check_service.db, "mark_account_live_check_running", return_value=True),
            patch.object(live_check_service.db, "update_account_liveness", return_value=True) as save_live,
            patch.object(live_check_service.db, "update_account_plan_check", return_value=True) as save_plan,
            patch.object(live_check_service, "resolve_plan_check_route", return_value={"proxy": ""}),
            patch.object(live_check_service, "check_account_liveness", return_value=live_result),
            patch.object(live_check_service, "check_account_plan", return_value=plan_result) as check_plan,
            patch.object(live_check_service, "_append_log"),
        ):
            result = live_check_service._run_live_check(
                account_id=1, email="user@example.com", proxy="", trigger="manual",
            )
        check_plan.assert_called_once_with("new-at", env=env, timezone_offset_min="-")
        save_live.assert_called_once_with(1, live_result)
        save_plan.assert_called_once_with(acc_id=1, result=plan_result)
        self.assertEqual(result["plan_check"], plan_result)
        self.assertTrue(env.session.closed)

    def test_failed_live_check_does_not_query_plan(self):
        failed = {"ok": False, "status": "failed", "error": "OTP failed"}
        live_check_service._QUEUE_SLOTS.acquire()
        with (
            patch.object(live_check_service.db, "mark_account_live_check_running", return_value=True),
            patch.object(live_check_service.db, "update_account_liveness", return_value=True),
            patch.object(live_check_service, "resolve_plan_check_route", return_value={"proxy": ""}),
            patch.object(live_check_service, "check_account_liveness", return_value=failed),
            patch.object(live_check_service, "check_account_plan") as check_plan,
            patch.object(live_check_service, "_append_log"),
        ):
            live_check_service._run_live_check(
                account_id=1, email="user@example.com", proxy="", trigger="manual",
            )
        check_plan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
