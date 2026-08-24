# -*- coding: utf-8 -*-
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from core.mailcom_client import MailComError, MailComInvalidTokenError
from core.mailcom_alias_service import MailComAliasCapacityError, mother_alias_lock
from core.mailcom_provider import MailComProvider, MailComProviderError, _email_lock


class _Client:
    auth_calls = 0
    read_calls = 0
    mode = "ok"

    def __init__(self, access_token=""):
        self.access_token = access_token

    def authenticate(self, email, password):
        type(self).auth_calls += 1
        return type("Token", (), {"access_token": "new-at", "expires_at": 9_999_999_999.0})()

    def fetch_latest_otp(self, **kwargs):
        type(self).read_calls += 1
        if self.mode == "invalid" and self.access_token == "old-at":
            raise MailComInvalidTokenError()
        if self.mode == "forbidden":
            raise MailComError("forbidden", error_type="forbidden_or_risk")
        if self.mode == "unauthorized" and self.access_token == "old-at":
            raise MailComError("unauthorized", error_type="unauthorized")
        return "123456"


class _ConcurrentClient(_Client):
    barrier = threading.Barrier(2)

    def fetch_latest_otp(self, **kwargs):
        type(self).read_calls += 1
        if self.access_token == "old-at":
            try:
                type(self).barrier.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass
            raise MailComInvalidTokenError()
        return "123456"


class MailComProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "mailcom.json"
        self.alias_path = Path(self.temp.name) / "mailcom-aliases.json"
        self.path_patch = patch.object(db, "_MAILCOM_EMAIL_JSON", self.path)
        self.alias_path_patch = patch.object(db, "_MAILCOM_ALIAS_JSON", self.alias_path)
        self.env_patch = patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"})
        self.path_patch.start()
        self.alias_path_patch.start()
        self.env_patch.start()
        _Client.auth_calls = _Client.read_calls = 0
        _Client.mode = "ok"
        db.import_mailcom_emails([{"email": "worker@mail.com", "password": "password"}])

    def tearDown(self):
        self.env_patch.stop()
        self.alias_path_patch.stop()
        self.path_patch.stop()
        self.temp.cleanup()

    def _provider(self):
        return MailComProvider(client_factory=_Client, clock=lambda: 1_000.0)

    def test_valid_persisted_at_is_reused_without_login(self):
        db.update_mailcom_auth("worker@mail.com", "old-at", 2_000.0)

        self.assertEqual(self._provider().fetch_latest_otp("worker@mail.com", after_ts=0), "123456")
        self.assertEqual(_Client.auth_calls, 0)
        self.assertEqual(_Client.read_calls, 1)

    def test_exact_invalid_token_refreshes_once_and_retries_once(self):
        _Client.mode = "invalid"
        db.update_mailcom_auth("worker@mail.com", "old-at", 2_000.0)

        self.assertEqual(self._provider().fetch_latest_otp("worker@mail.com", after_ts=0), "123456")
        self.assertEqual(_Client.auth_calls, 1)
        self.assertEqual(_Client.read_calls, 2)
        self.assertEqual(db.get_mailcom_internal_record("worker@mail.com")["mail_access_token"], "new-at")

    def test_non_invalid_error_does_not_login_or_clear_at(self):
        _Client.mode = "forbidden"
        db.update_mailcom_auth("worker@mail.com", "old-at", 2_000.0)

        with self.assertRaisesRegex(RuntimeError, "未自动重新登录"):
            self._provider().fetch_latest_otp("worker@mail.com", after_ts=0)
        self.assertEqual(_Client.auth_calls, 0)
        self.assertEqual(db.get_mailcom_internal_record("worker@mail.com")["mail_access_token"], "old-at")

    def test_unauthorized_refreshes_mailbox_at_immediately(self):
        _Client.mode = "unauthorized"
        db.update_mailcom_auth("worker@mail.com", "old-at", 2_000.0)

        self.assertEqual(self._provider().fetch_latest_otp("worker@mail.com", after_ts=0), "123456")
        self.assertEqual(_Client.auth_calls, 1)
        self.assertEqual(_Client.read_calls, 2)

    def test_concurrent_invalid_token_refreshes_once(self):
        db.update_mailcom_auth("worker@mail.com", "old-at", 2_000.0)
        _ConcurrentClient.auth_calls = _ConcurrentClient.read_calls = 0
        _ConcurrentClient.barrier = threading.Barrier(2)
        provider = MailComProvider(client_factory=_ConcurrentClient, clock=lambda: 1_000.0)
        results = []
        errors = []

        def run():
            try:
                results.append(provider.fetch_latest_otp("worker@mail.com", after_ts=0))
            except Exception as exc:  # pragma: no cover - assertion below
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(results, ["123456", "123456"])
        self.assertEqual(_ConcurrentClient.auth_calls, 1)

    def test_alias_lifecycle_lock_and_at_refresh_lock_do_not_block_each_other(self):
        parent_email = "worker@mail.com"
        alias_lock = mother_alias_lock(parent_email)
        at_lock = _email_lock(parent_email)
        self.assertIsNot(alias_lock, at_lock)

        def assert_other_lock_acquires(held_lock, other_lock):
            acquired = threading.Event()
            with held_lock:
                worker = threading.Thread(target=lambda: (other_lock.acquire(), acquired.set(), other_lock.release()))
                worker.start()
                self.assertTrue(acquired.wait(timeout=1), "另一类锁不应被当前锁阻塞")
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())

        assert_other_lock_acquires(alias_lock, at_lock)
        assert_other_lock_acquires(at_lock, alias_lock)

    def test_pick_account_returns_existing_alias_and_holds_parent_lease(self):
        db.create_mailcom_alias(
            alias_email="new@example.com", parent_email="worker@mail.com",
            local_part="new", domain="example.com",
        )
        with patch("core.registration_service._THREAD_CTX.job_id", 73, create=True):
            picked = MailComProvider().pick_account()
        self.assertEqual(picked.email, "new@example.com")
        self.assertEqual(db.get_mailcom_internal_record("worker@mail.com")["status"], "registering")
        alias = db.get_mailcom_alias_internal("new@example.com")
        self.assertEqual(alias["parent_email"], "worker@mail.com")
        self.assertNotIn("job_id", alias)
        self.assertEqual(alias["status"], "registering")
        self.assertEqual(db.get_mailcom_internal_record("worker@mail.com")["registration_lease_job_id"], 73)

    def test_missing_alias_fails_without_creating_or_fallback(self):
        with self.assertRaisesRegex(MailComProviderError, "别名池没有可用地址"):
            MailComProvider().pick_account()
        parent = db.get_mailcom_internal_record("worker@mail.com")
        self.assertEqual(parent["status"], "available")

    def test_alias_routes_to_parent_and_uses_registration_time_lower_bound(self):
        class CapturingClient(_Client):
            received = []

            def fetch_latest_otp(self, **kwargs):
                type(self).received.append(kwargs)
                return "654321"

        db.create_mailcom_alias(
            alias_email="alias@example.com",
            parent_email="worker@mail.com",
            local_part="alias",
            domain="example.com",
            registration_started_at=500.0,
        )
        db.update_mailcom_auth("worker@mail.com", "old-at", 2_000.0)
        db.claim_next_mailcom_alias(job_id=1)
        CapturingClient.received = []
        provider = MailComProvider(client_factory=CapturingClient, clock=lambda: 1_000.0)
        self.assertEqual(provider.fetch_latest_otp("alias@example.com", after_ts=100.0), "654321")
        self.assertEqual(len(CapturingClient.received), 1)
        self.assertGreaterEqual(CapturingClient.received[0]["after_ts"], 500.0)
        self.assertEqual(CapturingClient.received[0]["recipient"], "alias@example.com")

    def test_unknown_or_inactive_alias_fails_closed(self):
        provider = self._provider()
        with self.assertRaisesRegex(MailComProviderError, "别名映射不存在"):
            provider.fetch_latest_otp("unknown@example.com", after_ts=0)

        db.create_mailcom_alias(
            alias_email="inactive@example.com",
            parent_email="worker@mail.com",
            local_part="inactive",
            domain="example.com",
        )
        db.mark_mailcom_alias_registration_failed("inactive@example.com", "test")
        with self.assertRaisesRegex(MailComProviderError, "别名已失效"):
            provider.fetch_latest_otp("inactive@example.com", after_ts=0)
        self.assertIsNone(provider.get_account_context("inactive@example.com"))


if __name__ == "__main__":
    unittest.main()
