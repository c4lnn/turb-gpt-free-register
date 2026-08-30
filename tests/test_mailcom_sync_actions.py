# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import db
from core.mailcom_alias_service import MailComAliasError, MailComAliasService
import core.mailcom_alias_pool_service as alias_pool_service
from webui import app as web_app


class _SnapshotSettings:
    def __init__(self):
        self.addresses = [
            {"address": "mother@mail.com", "state": "ACTIVE", "deletable": False},
            {"address": "remote@example.com", "state": "ACTIVE", "deletable": True},
        ]
        self.list_calls = 0
        self.validate_calls = 0
        self.create_calls = 0

    def authenticate(self, email, password):
        self.authenticated = (email, password)

    def list_addresses(self):
        self.list_calls += 1
        return list(self.addresses)

    def validate_address(self, address):
        self.validate_calls += 1
        raise AssertionError("只同步不应调用 validate_address")

    def create_address(self, address):
        self.create_calls += 1
        raise AssertionError("只同步不应调用 create_address")


class MailComSyncActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}))
        for name, filename in {
            "_MAILCOM_EMAIL_JSON": "parents.json",
            "_MAILCOM_ALIAS_JSON": "aliases.json",
            "_ACCOUNTS_JSON": "accounts.json",
            "_JOBS_JSON": "jobs.json",
        }.items():
            self.stack.enter_context(patch.object(db, name, root / filename))
        with alias_pool_service._PENDING_LOCK:
            alias_pool_service._PENDING.clear()
        with alias_pool_service._SNAPSHOT_PENDING_LOCK:
            alias_pool_service._SNAPSHOT_PENDING.clear()
        db.import_mailcom_emails([{"email": "mother@mail.com", "password": "pw"}])

    def tearDown(self):
        with alias_pool_service._PENDING_LOCK:
            alias_pool_service._PENDING.clear()
        with alias_pool_service._SNAPSHOT_PENDING_LOCK:
            alias_pool_service._SNAPSHOT_PENDING.clear()
        self.stack.close()
        self.temp.cleanup()

    def test_snapshot_updates_local_pool_without_remote_create_or_validation(self):
        settings = _SnapshotSettings()
        result = MailComAliasService(settings_client_factory=lambda: settings).sync_parent_snapshot(
            db.get_mailcom_internal_record("mother@mail.com")
        )
        self.assertEqual(result["action"], "sync")
        self.assertEqual(result["remote_active_alias_count"], 1)
        self.assertEqual(result["local_added_count"], 1)
        self.assertEqual(settings.list_calls, 1)
        self.assertEqual(settings.validate_calls, 0)
        self.assertEqual(settings.create_calls, 0)
        self.assertEqual(db.get_mailcom_alias_internal("remote@example.com")["status"], "available")
        parent = db.get_mailcom_internal_record("mother@mail.com")
        self.assertEqual(parent["sync_action"], "sync")
        self.assertEqual(parent["sync_result"]["local_added_count"], 1)

    def test_action_endpoints_are_separate(self):
        patchers = []
        for name in (
            "recover_interrupted_plan_checks",
            "recover_interrupted_checkout_sessions",
            "recover_interrupted_extract_links",
            "recover_interrupted_live_checks",
        ):
            patcher = patch.object(web_app.db, name, return_value=0)
            patcher.start()
            patchers.append(patcher)
        try:
            client = web_app.create_app(auth_code="test-auth").test_client()
            headers = {"X-Auth-Code": "test-auth"}
            parent = client.get("/api/mailcom", headers=headers).get_json()["items"][0]
            with patch(
                "core.mailcom_alias_pool_service.enqueue_parent_snapshot_sync",
                return_value={"accepted": True, "busy": False, "action": "sync", "parent_email": "mother@mail.com"},
            ) as sync, patch(
                "core.mailcom_alias_pool_service.enqueue_parent_replenish",
                return_value={"accepted": True, "busy": False, "action": "replenish", "parent_email": "mother@mail.com"},
            ) as replenish:
                sync_response = client.post(f"/api/mailcom/parents/{parent['id']}/sync", headers=headers)
                replenish_response = client.post(f"/api/mailcom/parents/{parent['id']}/replenish", headers=headers)
            self.assertEqual(sync_response.status_code, 202)
            self.assertEqual(sync_response.get_json()["action"], "sync")
            self.assertEqual(replenish_response.status_code, 202)
            self.assertEqual(replenish_response.get_json()["action"], "replenish")
            sync.assert_called_once_with("mother@mail.com")
            replenish.assert_called_once_with("mother@mail.com")
        finally:
            for patcher in reversed(patchers):
                patcher.stop()

    def test_import_queues_snapshot_only(self):
        with patch(
            "core.mailcom_alias_pool_service.enqueue_parent_snapshot_sync",
            return_value={"accepted": True, "action": "sync", "parent_email": "imported@mail.com"},
        ) as snapshot, patch(
            "core.mailcom_alias_pool_service.enqueue_parent_replenish",
        ) as replenish:
            # The import endpoint imports first, then queues the read-only action.
            response = web_app.create_app(auth_code="test-auth").test_client().post(
                "/api/mailcom/import",
                json={"text": "imported@mail.com----pw"},
                headers={"X-Auth-Code": "test-auth"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["sync"][0]["action"], "sync")
        snapshot.assert_called_once_with("imported@mail.com")
        replenish.assert_not_called()

    def test_snapshot_and_replenish_queue_are_mutually_exclusive(self):
        with patch.object(alias_pool_service._SNAPSHOT_EXECUTOR, "submit") as submit:
            first = alias_pool_service.enqueue_parent_snapshot_sync("mother@mail.com")
            self.assertTrue(first["accepted"])
            blocked = alias_pool_service.enqueue_parent_replenish("mother@mail.com")
            self.assertTrue(blocked["busy"])
            self.assertEqual(blocked["action"], "replenish")
            submit.assert_called_once()

    def test_disabled_parent_snapshot_does_not_create_settings_client(self):
        db.disable_mailcom_parent("mother@mail.com", reason="暂停")
        settings = _SnapshotSettings()
        with self.assertRaisesRegex(MailComAliasError, "母号已停用"):
            MailComAliasService(settings_client_factory=lambda: settings).sync_parent_snapshot(
                db.get_mailcom_internal_record("mother@mail.com")
            )
        self.assertEqual(settings.list_calls, 0)
        self.assertEqual(settings.validate_calls, 0)
        self.assertEqual(settings.create_calls, 0)

    def test_deleted_parent_cannot_be_recreated_by_snapshot_worker(self):
        db.delete_mailcom_parent("mother@mail.com", reason="删除测试母号")
        result = alias_pool_service.sync_parent_snapshot_now("mother@mail.com")
        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "sync")
        self.assertEqual(result["error"], "parent_missing")

    def test_ui_uses_separate_sync_and_replenish_buttons(self):
        html = (Path(__file__).parents[1] / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("data-mailcom-parent-sync", html)
        self.assertIn("data-mailcom-parent-replenish", html)
        self.assertIn("/${action}`,", html)
        self.assertIn("runMailcomParentAction", html)
        self.assertNotIn("同步/补齐", html)


if __name__ == "__main__":
    unittest.main()
