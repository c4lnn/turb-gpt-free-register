# -*- coding: utf-8 -*-
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from core.mailcom_alias_cleanup import process_plan_result
from core.mailcom_alias_service import MailComAliasCapacityError, MailComAliasError, MailComAliasService
from core.mailcom_settings_client import MailComSettingsConflictError, MailComSettingsError


class _Settings:
    def __init__(self, addresses):
        self.addresses = addresses
        self.created = []
        self.deleted = []
        self.list_calls = 0

    def authenticate(self, email, password):
        self.authenticated = (email, password)

    def list_addresses(self):
        self.list_calls += 1
        return list(self.addresses)

    def validate_address(self, address):
        return None

    def create_address(self, address):
        self.created.append(address)
        self.addresses.append({"address": address, "state": "ACTIVE", "deletable": True})

    def delete_address(self, address):
        self.deleted.append(address)
        self.addresses[:] = [item for item in self.addresses if item["address"] != address]
        return True


class MailComAliasLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(db, "_MAILCOM_ALIAS_JSON", Path(self.temp.name) / "aliases.json")
        self.parent_path_patch = patch.object(db, "_MAILCOM_EMAIL_JSON", Path(self.temp.name) / "parents.json")
        self.env_patch = patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"})
        self.path_patch.start()
        self.parent_path_patch.start()
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.parent_path_patch.stop()
        self.path_patch.stop()
        self.temp.cleanup()

    def test_capacity_counts_only_active_deletable_and_rejects_tenth(self):
        addresses = [
            {"address": "mother@mail.com", "state": "ACTIVE", "deletable": False},
            *[
                {"address": f"a{i}@mail.com", "state": "ACTIVE", "deletable": True}
                for i in range(9)
            ],
        ]
        settings = _Settings(addresses)
        service = MailComAliasService(settings_client_factory=lambda: settings)
        with self.assertRaises(MailComAliasCapacityError):
            service.create_alias({"email": "mother@mail.com", "password": "pw"})
        self.assertEqual(settings.created, [])

    def test_create_persists_alias_without_parent_secrets(self):
        settings = _Settings([{"address": "mother@mail.com", "state": "ACTIVE", "deletable": False}])
        with patch("core.mailcom_alias_service.generate_alias_local_part", return_value="alice123"), \
                patch("core.mailcom_alias_service.choose_alias_domain", return_value="example.com"):
            alias = MailComAliasService(settings_client_factory=lambda: settings).create_alias(
                {"email": "mother@mail.com", "password": "pw"}, job_id=7
            )
        self.assertEqual(alias["alias_email"], "alice123@example.com")
        saved = db.get_mailcom_alias_internal(alias["alias_email"])
        self.assertEqual(saved["parent_email"], "mother@mail.com")
        self.assertNotIn("password", saved)
        self.assertNotIn("mail_access_token", saved)

    def test_conflict_retries_new_candidate_before_persisting(self):
        class ConflictingSettings(_Settings):
            def __init__(self, addresses):
                super().__init__(addresses)
                self.validated = []

            def validate_address(self, address):
                self.validated.append(address)
                if len(self.validated) == 1:
                    raise MailComSettingsConflictError()

        settings = ConflictingSettings([
            {"address": "mother@mail.com", "state": "ACTIVE", "deletable": False},
        ])
        with patch("core.mailcom_alias_service.generate_alias_local_part", side_effect=["first", "second"]), \
                patch("core.mailcom_alias_service.choose_alias_domain", return_value="example.com"):
            alias = MailComAliasService(settings_client_factory=lambda: settings).create_alias(
                {"email": "mother@mail.com", "password": "pw"}
            )
        self.assertEqual(settings.validated, ["first@example.com", "second@example.com"])
        self.assertEqual(settings.created, ["second@example.com"])
        self.assertEqual(alias["alias_email"], "second@example.com")
        self.assertIsNone(db.get_mailcom_alias_internal("first@example.com"))

    def test_near_capacity_concurrent_creates_never_exceed_remote_limit(self):
        addresses = [
            {"address": "mother@mail.com", "state": "ACTIVE", "deletable": False},
            *[
                {"address": f"old-{index}@example.com", "state": "ACTIVE", "deletable": True}
                for index in range(9)
            ],
        ]
        settings = _Settings(addresses)
        service = MailComAliasService(settings_client_factory=lambda: settings)
        results = []
        errors = []

        def create():
            try:
                results.append(service.create_alias({"email": "mother@mail.com", "password": "pw"}))
            except Exception as exc:  # pragma: no cover - assertion below
                errors.append(exc)

        with patch("core.mailcom_alias_service.generate_alias_local_part", side_effect=["first", "second"]), \
                patch("core.mailcom_alias_service.choose_alias_domain", return_value="example.com"):
            threads = [threading.Thread(target=create), threading.Thread(target=create)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)

        self.assertEqual(len(results), 0)
        self.assertEqual(len([item for item in settings.addresses if item.get("deletable")]), 9)
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(isinstance(error, MailComAliasCapacityError) for error in errors))

    def test_delete_success_does_not_reload_remote_address_list(self):
        db.import_mailcom_emails([{"email": "mother@mail.com", "password": "pw"}])
        db.create_mailcom_alias(
            alias_email="gone@example.com",
            parent_email="mother@mail.com",
            local_part="gone",
            domain="example.com",
        )
        settings = _Settings([{"address": "mother@mail.com", "state": "ACTIVE", "deletable": False}])
        self.assertTrue(
            MailComAliasService(settings_client_factory=lambda: settings).delete_alias(
                db.get_mailcom_alias_internal("gone@example.com")
            )
        )
        saved = db.get_mailcom_alias_internal("gone@example.com")
        self.assertEqual(saved["status"], "disabled")
        self.assertEqual(saved["cleanup_status"], "deleted")
        self.assertEqual(settings.list_calls, 0)

    def test_delete_protocol_failure_keeps_local_alias_active(self):
        class FailingSettings(_Settings):
            def delete_address(self, address):
                raise MailComSettingsError("forbidden", error_type="forbidden_or_risk")

        db.import_mailcom_emails([{"email": "mother@mail.com", "password": "pw"}])
        db.create_mailcom_alias(
            alias_email="keep@example.com",
            parent_email="mother@mail.com",
            local_part="keep",
            domain="example.com",
        )
        settings = FailingSettings([{"address": "mother@mail.com", "state": "ACTIVE", "deletable": False}])
        with self.assertRaisesRegex(MailComAliasError, "删除别名失败"):
            MailComAliasService(settings_client_factory=lambda: settings).delete_alias(
                db.get_mailcom_alias_internal("keep@example.com")
            )
        self.assertEqual(db.get_mailcom_alias_internal("keep@example.com")["status"], "available")

    def test_cleanup_requires_complete_false_and_is_idempotent(self):
        db.create_mailcom_alias(
            alias_email="alias@example.com", parent_email="mother@mail.com",
            local_part="alias", domain="example.com",
        )
        db.link_mailcom_alias_account("alias@example.com", 11)
        with patch.object(db, "get_account", return_value={"id": 11, "current_plan_type": "free", "archived": False}), \
                patch.object(db, "claim_mailcom_alias_cleanup", wraps=db.claim_mailcom_alias_cleanup) as claim:
            with patch("core.mailcom_alias_cleanup.email_cfg.MAILCOM_DELETE_ALIAS_IF_NO_TRIAL", True):
                # incomplete result must not claim or delete
                out = process_plan_result(account_id=11, result={"ok": True, "plus_trial_eligible": False})
                self.assertEqual(out["reason"], "trial_eligibility_unknown")
                self.assertEqual(claim.call_count, 0)

                deleted = []
                out = process_plan_result(
                    account_id=11,
                    result={"ok": True, "trial_eligibility_known": True, "plus_trial_eligible": False},
                    delete_alias_fn=lambda row: deleted.append(row["alias_email"]) or True,
                )
                self.assertTrue(out["deleted"])
                self.assertEqual(deleted, ["alias@example.com"])
                out2 = process_plan_result(
                    account_id=11,
                    result={"ok": True, "trial_eligibility_known": True, "plus_trial_eligible": False},
                    delete_alias_fn=lambda row: (_ for _ in ()).throw(AssertionError("重复删除")),
                )
                self.assertEqual(out2["reason"], "cleanup_already_handled")

    def test_cleanup_retains_alias_for_safe_non_delete_outcomes(self):
        cases = (
            ({"ok": False, "error": "network"}, "plan_query_failed", "failed", "not_requested"),
            ({"ok": True, "trial_eligibility_known": True, "plus_trial_eligible": True}, "trial_eligible", "success", "not_eligible"),
            ({"ok": True, "trial_eligibility_known": True, "plus_trial_eligible": False}, "cleanup_disabled", "success", "not_requested"),
        )
        for index, (result, reason, plan_status, cleanup_status) in enumerate(cases, start=1):
            alias_email = f"safe-{index}@example.com"
            db.create_mailcom_alias(
                alias_email=alias_email,
                parent_email="mother@mail.com",
                local_part=f"safe-{index}",
                domain="example.com",
            )
            db.link_mailcom_alias_account(alias_email, 100 + index)
            with patch.object(db, "get_account", return_value={"id": 100 + index, "current_plan_type": "free", "archived": False}), \
                    patch("core.mailcom_alias_cleanup.email_cfg.MAILCOM_DELETE_ALIAS_IF_NO_TRIAL", False):
                outcome = process_plan_result(
                    account_id=100 + index,
                    result=result,
                    delete_alias_fn=lambda _: (_ for _ in ()).throw(AssertionError("不应删除")),
                )
            self.assertEqual(outcome["reason"], reason)
            saved = db.get_mailcom_alias_internal(alias_email)
            self.assertEqual(saved["status"], "used")
            self.assertEqual(saved["plan_check_status"], plan_status)
            self.assertEqual(saved["cleanup_status"], cleanup_status)

    def test_cleanup_delete_failure_is_pending_and_not_retried(self):
        db.create_mailcom_alias(
            alias_email="pending@example.com",
            parent_email="mother@mail.com",
            local_part="pending",
            domain="example.com",
        )
        db.link_mailcom_alias_account("pending@example.com", 201)
        attempts = []
        with patch.object(db, "get_account", return_value={"id": 201, "current_plan_type": "free", "archived": False}), \
                patch("core.mailcom_alias_cleanup.email_cfg.MAILCOM_DELETE_ALIAS_IF_NO_TRIAL", True):
            first = process_plan_result(
                account_id=201,
                result={"ok": True, "trial_eligibility_known": True, "plus_trial_eligible": False},
                delete_alias_fn=lambda row: attempts.append(row["alias_email"]) or False,
            )
            second = process_plan_result(
                account_id=201,
                result={"ok": True, "trial_eligibility_known": True, "plus_trial_eligible": False},
                delete_alias_fn=lambda _: (_ for _ in ()).throw(AssertionError("不应重试")),
            )
        self.assertEqual(first["reason"], "delete_unconfirmed")
        self.assertEqual(second["reason"], "cleanup_already_handled")
        self.assertEqual(attempts, ["pending@example.com"])
        saved = db.get_mailcom_alias_internal("pending@example.com")
        self.assertEqual(saved["status"], "used")
        self.assertEqual(saved["cleanup_status"], "cleanup_pending")


if __name__ == "__main__":
    unittest.main()
