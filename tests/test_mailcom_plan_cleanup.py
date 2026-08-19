# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from core import account_export
from core.chatgpt_plan import parse_accounts_check
from core import plan_check_service


class MailComPlanResultTests(unittest.TestCase):
    def test_parser_marks_empty_promo_map_as_known_no_trial(self):
        result = parse_accounts_check({
            "accounts": {
                "default": {
                    "account": {"account_id": "acct-1", "plan_type": "free"},
                    "entitlement": {"subscription_plan": "chatgptfreeplan"},
                    "eligible_promo_campaigns": {},
                }
            }
        })
        self.assertTrue(result["ok"])
        self.assertTrue(result["trial_eligibility_known"])
        self.assertFalse(result["plus_trial_eligible"])

    def test_parser_marks_missing_or_malformed_promo_data_unknown(self):
        base = {
            "account": {"account_id": "acct-1", "plan_type": "free"},
            "entitlement": {"subscription_plan": "chatgptfreeplan"},
        }
        for promo in (None, [], "invalid"):
            payload = {"accounts": {"default": {**base}}}
            if promo is not None:
                payload["accounts"]["default"]["eligible_promo_campaigns"] = promo
            with self.subTest(promo=promo):
                result = parse_accounts_check(payload)
                self.assertFalse(result["trial_eligibility_known"])
                self.assertIsNone(result["plus_trial_eligible"])

    def test_parser_marks_malformed_plus_campaign_unknown_without_raising(self):
        for campaign in (None, {}, "malformed"):
            with self.subTest(campaign=campaign):
                result = parse_accounts_check({
                    "accounts": {
                        "default": {
                            "account": {"account_id": "acct-1", "plan_type": "free"},
                            "entitlement": {"subscription_plan": "chatgptfreeplan"},
                            "eligible_promo_campaigns": {"plus": campaign},
                        }
                    }
                })
                self.assertFalse(result["trial_eligibility_known"])
                self.assertIsNone(result["plus_trial_eligible"])

    def test_parser_reports_complete_positive_trial_shape(self):
        result = parse_accounts_check({
            "accounts": {
                "default": {
                    "account": {"account_id": "acct-1", "plan_type": "free"},
                    "entitlement": {"subscription_plan": "chatgptfreeplan"},
                    "eligible_promo_campaigns": {
                        "plus": {
                            "id": "plus-1-month-free",
                            "metadata": {
                                "title": "Plus trial",
                                "discount": {"percentage": 100},
                                "duration": {"num_periods": 1, "period": "month"},
                            },
                        }
                    },
                }
            }
        })
        self.assertTrue(result["trial_eligibility_known"])
        self.assertTrue(result["plus_trial_eligible"])
        self.assertEqual(result["plus_trial_campaign_id"], "plus-1-month-free")


class MailComPlanPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patches = [
            patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}),
            patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
            patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
            patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
            patch.object(db, "_MAILCOM_EMAIL_JSON", root / "parents.json"),
            patch.object(db, "_MAILCOM_ALIAS_JSON", root / "aliases.json"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_account_link_and_plan_write_back_update_alias_state(self):
        db.import_mailcom_emails([{"email": "mother@mail.com", "password": "pw"}])
        db.create_mailcom_alias(
            alias_email="alias@example.com",
            parent_email="mother@mail.com",
            local_part="alias",
            domain="example.com",
        )
        account_id = db.insert_account(
            email="alias@example.com",
            access_token="openai-at",
            email_source="mailcom",
        )
        self.assertEqual(account_id, 1)
        self.assertTrue(db.update_account_plan_check(
            acc_id=account_id,
            result={
                "ok": True,
                "checked_at": "2026-08-19T00:00:00",
                "trial_eligibility_known": True,
                "plus_trial_eligible": False,
                "current_plan_type": "free",
            },
        ))
        account = json.loads((Path(self.temp.name) / "accounts.json").read_text(encoding="utf-8"))[0]
        alias = db.get_mailcom_alias_internal("alias@example.com")
        self.assertTrue(account["trial_eligibility_known"])
        self.assertFalse(account["plus_trial_eligible"])
        self.assertEqual(alias["plan_check_status"], "success")

    def test_incomplete_plan_persists_unknown_trial_state(self):
        db.import_mailcom_emails([{"email": "mother@mail.com", "password": "pw"}])
        db.create_mailcom_alias(
            alias_email="unknown@example.com",
            parent_email="mother@mail.com",
            local_part="unknown",
            domain="example.com",
        )
        account_id = db.insert_account(
            email="unknown@example.com",
            access_token="openai-at",
            email_source="mailcom",
        )

        self.assertTrue(db.update_account_plan_check(
            acc_id=account_id,
            result={
                "ok": True,
                "checked_at": "2026-08-19T00:00:00",
                "trial_eligibility_known": False,
                "plus_trial_eligible": None,
                "current_plan_type": "free",
            },
        ))

        account = json.loads((Path(self.temp.name) / "accounts.json").read_text(encoding="utf-8"))[0]
        alias = db.get_mailcom_alias_internal("unknown@example.com")
        self.assertFalse(account["trial_eligibility_known"])
        self.assertIsNone(account["plus_trial_eligible"])
        self.assertEqual(alias["plan_check_status"], "incomplete")

    def test_save_account_data_links_active_alias(self):
        db.import_mailcom_emails([{"email": "mother@mail.com", "password": "pw"}])
        db.create_mailcom_alias(
            alias_email="saved@example.com",
            parent_email="mother@mail.com",
            local_part="saved",
            domain="example.com",
        )
        with (
            patch.object(account_export, "_ACCOUNTS_DIR", Path(self.temp.name) / "archives"),
            patch(
                "core.plan_check_service.enqueue_account_plan_check",
                return_value={"accepted": True},
            ),
        ):
            account_id = account_export.save_account_data(
                email="saved@example.com",
                access_token="openai-at",
                email_source="mailcom",
                extra={
                    "user": {"id": "user-1", "email": "saved@example.com"},
                    "account": {"planType": "free"},
                    "expires": "2026-12-31T00:00:00Z",
                },
            )

        alias = db.get_mailcom_alias_internal("saved@example.com")
        self.assertEqual(alias["registered_account_id"], account_id)
        self.assertEqual(alias["plan_check_status"], "queued")
        self.assertEqual(alias["cleanup_status"], "pending")

    def test_plan_service_cleanup_callback_receives_finished_result(self):
        result = {"ok": True, "trial_eligibility_known": True, "plus_trial_eligible": False}
        with patch("core.mailcom_alias_cleanup.process_plan_result", return_value={"handled": True, "reason": "cleanup_disabled"}) as process:
            plan_check_service._maybe_process_mailcom_alias_cleanup(account_id=19, result=result)
        process.assert_called_once_with(account_id=19, result=result)


if __name__ == "__main__":
    unittest.main()
