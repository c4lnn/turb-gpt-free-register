# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core.mail import cf_temp_mail_client
from core.infra import db
from core.mail import email_provider
from core.openai.register import registration_service
from core.infra.contracts.email_pool import (
    EMAIL_POOL_STATUSES,
    can_transition,
    canonical_status,
    status_counts,
)
from core.infra.sqlite_store import SQLiteRuntimeStore
from config import email as email_config
from webui import app as web_app


class EmailPoolStatusContractTests(unittest.TestCase):
    def test_contract_and_legacy_mapping(self):
        self.assertEqual(
            EMAIL_POOL_STATUSES,
            ("available", "registering", "used", "failed", "disabled"),
        )
        self.assertEqual(canonical_status("leased"), "registering")
        self.assertEqual(canonical_status("registered"), "used")
        self.assertEqual(canonical_status("registration_failed"), "failed")
        self.assertEqual(canonical_status("deleted"), "disabled")
        self.assertEqual(canonical_status(None), "disabled")
        self.assertEqual(canonical_status("unknown-value"), "disabled")

    def test_terminal_transitions_cannot_revive_a_row(self):
        self.assertTrue(can_transition("available", "registering"))
        self.assertTrue(can_transition("registering", "used"))
        self.assertTrue(can_transition("registering", "failed"))
        self.assertFalse(can_transition("failed", "available"))
        self.assertFalse(can_transition("failed", "registering"))
        self.assertFalse(can_transition("disabled", "available"))
        self.assertEqual(
            status_counts([
                {"status": "available"},
                {"status": "registering"},
                {"status": "used"},
                {"status": "failed"},
                {"status": "disabled"},
                {"status": "not-a-status"},
            ]),
            {
                "available": 1,
                "registering": 1,
                "used": 1,
                "failed": 1,
                "disabled": 2,
                "total": 6,
            },
        )


class EmailPoolProviderStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}))
        self.stack.enter_context(patch.object(email_config, "EMAIL_SOURCE", "outlook"))
        for name, filename in {
            "_OUTLOOK_JSON": "outlook.json",
            "_OUTLOOK_TXT": "outlook.txt",
            "_GENERIC_API_EMAIL_JSON": "generic.json",
            "_GENERIC_API_EMAIL_TXT": "generic.txt",
            "_DOMAIN_EMAIL_JSON": "domain.json",
            "_ICLOUD_EMAIL_JSON": "icloud.json",
            "_ACCOUNTS_JSON": "accounts.json",
            "_ACCOUNTS_TXT": "accounts.txt",
            "_TOKENS_TXT": "tokens.txt",
            "_JOBS_JSON": "jobs.json",
        }.items():
            self.stack.enter_context(patch.object(db, name, root / filename))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def test_persistent_sources_claim_registering_and_failed_is_not_claimable(self):
        db.import_outlook_accounts([{
            "email": "outlook@example.com",
            "password": "pw",
            "client_id": "client",
            "refresh_token": "refresh",
        }])
        db.import_generic_api_emails([{"email": "generic@example.com", "code_url": "https://code.test/1"}])
        db.import_icloud_emails(["icloud@example.com"])

        outlook = db.claim_next_outlook(job_id=1)
        generic = db.claim_next_generic_api_email(job_id=2)
        icloud = db.claim_next_icloud_email(job_id=3)
        self.assertEqual(outlook["status"], "registering")
        self.assertEqual(generic["status"], "registering")
        self.assertEqual(icloud["status"], "registering")

        self.assertTrue(db.mark_registration_failed("outlook@example.com", "OTP failed", stage="otp", job_id=1))
        self.assertTrue(db.mark_registration_failed("generic@example.com", "network", stage="create", job_id=2))
        self.assertTrue(db.mark_registration_failed("icloud@example.com", "stopped", stage="stop", job_id=3))
        self.assertIsNone(db.claim_next_outlook(job_id=4))
        self.assertIsNone(db.claim_next_generic_api_email(job_id=5))
        self.assertIsNone(db.claim_next_icloud_email(job_id=6))
        self.assertEqual(db.get_outlook_by_email("outlook@example.com")["status"], "failed")
        self.assertEqual(db.get_generic_api_email_by_email("generic@example.com")["status"], "failed")
        self.assertEqual(db.get_icloud_email_by_email("icloud@example.com")["status"], "failed")

    def test_domain_claim_success_and_failure_boundaries(self):
        domain = db.claim_next_domain_email("domain@example.com", job_id=10)
        self.assertEqual(domain["status"], "registering")
        self.assertTrue(db.release_unconsumed_domain_email("domain@example.com", note="driver failed"))
        self.assertIsNone(db.claim_next_domain_email("domain@example.com", job_id=11))

    def test_registered_account_post_failure_keeps_used(self):
        db.import_outlook_accounts([{
            "email": "saved@example.com",
            "password": "pw",
            "client_id": "client",
            "refresh_token": "refresh",
        }])
        db.claim_next_outlook(job_id=20)
        db.insert_account(email="saved@example.com", access_token="at", email_source="outlook")
        self.assertEqual(db.get_outlook_by_email("saved@example.com")["status"], "used")
        self.assertTrue(db.mark_registration_failed("saved@example.com", "Codex failed", stage="codex", job_id=20))
        self.assertEqual(db.get_outlook_by_email("saved@example.com")["status"], "used")

    class _RecordingExecutor:
        """只记录 submit 调用，不执行任务；重试子任务的邮箱在提交前就已确定。"""

        def __init__(self):
            self.calls = []

        def submit(self, fn, *args):
            self.calls.append((fn, args))

    def _import_and_claim_outlook(self, address, *, job_id):
        db._save_outlook([])
        db.import_outlook_accounts([{
            "email": address, "password": "pw", "client_id": "client", "refresh_token": "refresh",
        }])
        claimed = db.claim_next_outlook(job_id=job_id)
        self.assertEqual(claimed["email"].lower(), address.lower(), "领到的不是目标邮箱")

    def _failed_job(self, address, *, failure_category=None):
        job = db.create_job(email_source="outlook", email=address)
        db.update_job(job["id"], status="failed", email=address, failure_category=failure_category)
        return db.get_job(job["id"])

    def test_service_release_marks_failed_and_unknown_failure_retry_takes_new_email(self):
        address = "retry-source@example.com"
        self._import_and_claim_outlook(address, job_id=50)
        registration_service._release_unconsumed_job_email(
            address, "create failed", reusable=False, job_id=50,
        )
        self.assertEqual(db.get_outlook_by_email(address)["status"], "failed")

        # 没有失败类别的任务等同 unknown：重试不复用原邮箱，执行时领新邮箱。
        source = self._failed_job(address)
        executor = self._RecordingExecutor()
        with patch.object(registration_service, "get_executor", return_value=executor), \
                patch.object(db, "claim_specific_email") as claim:
            result = registration_service.retry_job(source["id"], workers=1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["retry_action"], "registration")
        self.assertEqual(len(executor.calls), 1)
        claim.assert_not_called()
        retry = db.get_job(result["job"]["id"])
        self.assertIsNone(retry["email"])
        self.assertEqual(db.get_outlook_by_email(address)["status"], "failed")
        self.assertEqual(retry.get("failure_category"), None)

        account_id = db.insert_account(
            email="post@example.com", access_token="at", email_source="outlook"
        )
        post = db.create_job(email_source="outlook", email="post@example.com", account_id=account_id)
        db.update_job(post["id"], status="failed", email="post@example.com", account_id=account_id)
        with patch.object(registration_service.codex_retry_service, "reserve", return_value=True), \
                patch.object(registration_service, "get_executor", return_value=executor):
            post_result = registration_service.retry_job(post["id"], workers=1)
        self.assertTrue(post_result["ok"])
        self.assertEqual(post_result["retry_action"], "codex")

    def test_network_failure_retry_reuses_released_email(self):
        # network 类别失败：上一轮已把邮箱回池，重试子任务认领它并归到自己名下。
        address = "retry-released@example.com"
        self._import_and_claim_outlook(address, job_id=60)
        self.assertTrue(db.settle_failed_registration_email(address, reusable=True))
        self.assertEqual(db.get_outlook_by_email(address)["status"], "available")
        source = self._failed_job(address, failure_category="network")

        executor = self._RecordingExecutor()
        with patch.object(registration_service, "get_executor", return_value=executor):
            result = registration_service.retry_job(source["id"], workers=1)

        self.assertTrue(result["ok"])
        retry = db.get_job(result["job"]["id"])
        self.assertEqual(retry["email"], address)
        self.assertEqual(retry.get("failure_category"), None, "重试子任务不继承原任务的失败类别")
        row = db.get_outlook_by_email(address)
        self.assertEqual(row["status"], "registering")
        self.assertEqual(row["registration_job_id"], retry["id"])
        self.assertEqual(row["network_release_count"], 1, "回池的邮箱重新领取时保留上次的计数")
        self.assertEqual(len(executor.calls), 1)

    def test_network_failure_retry_restores_email_that_hit_release_limit(self):
        # 回池次数达上限后邮箱被标 failed；network 类别的重试仍复用它，并把计数清零。
        address = "retry-exhausted@example.com"
        self._import_and_claim_outlook(address, job_id=61)
        max_count = int(email_config.EMAIL_NETWORK_RELEASE_MAX_COUNT)
        for _ in range(max_count - 1):
            self.assertTrue(db.settle_failed_registration_email(address, reusable=True))
            self.assertEqual(db.get_outlook_by_email(address)["status"], "available")
            self.assertEqual(db.claim_next_outlook(job_id=61)["email"].lower(), address)
        # 第 max_count 次回池时计数达到上限，邮箱被标 failed 而不是回池。
        self.assertTrue(db.settle_failed_registration_email(address, reusable=True))
        self.assertEqual(db.get_outlook_by_email(address)["status"], "failed")
        self.assertEqual(db.get_outlook_by_email(address)["network_release_count"], max_count)
        source = self._failed_job(address, failure_category="network")

        executor = self._RecordingExecutor()
        with patch.object(registration_service, "get_executor", return_value=executor):
            result = registration_service.retry_job(source["id"], workers=1)

        self.assertTrue(result["ok"])
        retry = db.get_job(result["job"]["id"])
        self.assertEqual(retry["email"], address)
        row = db.get_outlook_by_email(address)
        self.assertEqual(row["status"], "registering")
        self.assertEqual(row["registration_job_id"], retry["id"])
        self.assertEqual(row["network_release_count"], 0)
        self.assertEqual(row["status_change_source"], "retry")
        self.assertEqual(row["manual_reactivated_from"], "failed")
        self.assertEqual(row["manual_reactivated_by"], f"retry-job-{retry['id']}")

    def test_network_failure_retry_takes_new_email_when_original_is_unusable(self):
        # 原邮箱被人工停用、已被永久删除、或被别的活动任务占用时，重试不复用它，也不改动池行。
        # 邮箱已有完整账号的情形由 get_retry_info 分流到 Codex 补跑，不经过邮箱认领。
        cases = ("disabled", "deleted", "active_job")
        for case in cases:
            with self.subTest(case=case):
                address = f"retry-{case}@example.com"
                self._import_and_claim_outlook(address, job_id=62)
                db.settle_failed_registration_email(address, reusable=True)
                if case == "disabled":
                    self.assertTrue(db.delete_outlook(address))
                elif case == "deleted":
                    self.assertTrue(db.delete_outlook(address, physical=True))
                else:
                    db.claim_next_outlook(job_id=63)
                before = db.get_outlook_by_email(address)
                source = self._failed_job(address, failure_category="network")
                self.assertEqual(source["status"], "failed")

                executor = self._RecordingExecutor()
                with patch.object(registration_service, "get_executor", return_value=executor):
                    result = registration_service.retry_job(source["id"], workers=1)

                self.assertTrue(result["ok"])
                retry = db.get_job(result["job"]["id"])
                self.assertIsNone(retry["email"], "原邮箱不可用时子任务不应绑定它")
                self.assertEqual(len(executor.calls), 1)
                after = db.get_outlook_by_email(address)
                if case == "deleted":
                    self.assertIsNone(after, "已永久删除的邮箱不能被重试复活")
                    continue
                for field in ("status", "registration_job_id", "network_release_count",
                              "status_change_source", "manual_reactivated_from"):
                    self.assertEqual(after[field], before[field], f"{case}: {field} 被改动")

    def test_delete_keeps_disabled_tombstone_and_reimport_cannot_revive(self):
        db.import_icloud_emails(["tombstone@example.com"])
        self.assertTrue(db.delete_icloud_email("tombstone@example.com"))
        self.assertEqual(db.get_icloud_email_by_email("tombstone@example.com")["status"], "disabled")
        inserted, skipped = db.import_icloud_emails(["tombstone@example.com"])
        self.assertEqual((inserted, skipped), (0, 1))
        self.assertIsNone(db.claim_next_icloud_email(job_id=30))

    def test_temporary_email_failure_clears_context_without_persistent_row(self):
        cf_temp_mail_client._CONTEXT_CACHE.clear()
        cf_temp_mail_client._CONTEXT_CACHE["temp@example.com"] = cf_temp_mail_client.CFTempMailAccount(
            email="temp@example.com", jwt="jwt", domain="example.com"
        )
        with patch.object(email_provider, "resolve_email_source", return_value="cloudflare"):
            self.assertTrue(email_provider.release_email_if_unconsumed("temp@example.com", note="driver failed"))
        self.assertIsNone(cf_temp_mail_client.get_account_context("temp@example.com"))

    def test_active_registration_job_id_prevents_migration_reclaim(self):
        job = db.create_job(email_source="outlook")
        db.update_job(job["id"], status="running", email="active@example.com")
        rows_path = db._OUTLOOK_JSON
        rows_path.write_text(
            '[{"id": 1, "email": "active@example.com", "status": "registering", '
            f'"registration_job_id": {job["id"]}' + "}]",
            encoding="utf-8",
        )
        db.migrate_email_pool_statuses()
        self.assertEqual(db.get_outlook_by_email("active@example.com")["status"], "registering")

    # ---- settle_failed_registration_email：失败类别驱动的邮箱去向（D14 / D18）----

    def _claim_outlook(self, email, job_id, *, fresh_pool=True):
        # claim_next_outlook 不接受邮箱参数，领的是 id 最小的 available 行。
        # 多轮回池的用例要求目标邮箱是池里唯一的 available 行，所以默认先清空本池。
        if fresh_pool:
            db._save_outlook([])
        db.import_outlook_accounts([{
            "email": email, "password": "pw", "client_id": "client", "refresh_token": "refresh",
        }])
        claimed = db.claim_next_outlook(job_id=job_id)
        self.assertEqual(claimed["email"].lower(), email.lower(), "领到的不是目标邮箱")
        return claimed

    def _claim_again(self, email, claim):
        # 目标邮箱上一轮已回池：确认它确实 available，再重新领取并核对身份。
        self.assertEqual(db.get_outlook_by_email(email)["status"], "available")
        claimed = claim()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["email"].lower(), email.lower(), "重新领到的不是目标邮箱")
        return claimed

    def test_settle_reusable_returns_email_to_available(self):
        self._claim_outlook("settle-ok@example.com", job_id=101)
        self.assertTrue(db.settle_failed_registration_email(
            "settle-ok@example.com", reusable=True, note="network failure"))
        row = db.get_outlook_by_email("settle-ok@example.com")
        self.assertEqual(row["status"], "available")
        self.assertEqual(row["status_change_source"], "registration_failure")
        self.assertEqual(row["network_release_count"], 1)

    def test_settle_not_reusable_marks_failed(self):
        self._claim_outlook("settle-bad@example.com", job_id=102)
        self.assertTrue(db.settle_failed_registration_email(
            "settle-bad@example.com", reusable=False, note="account rejected"))
        row = db.get_outlook_by_email("settle-bad@example.com")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["status_change_source"], "registration_failure")
        self.assertEqual(row["network_release_count"], 0)

    def test_settle_returns_false_when_account_already_complete(self):
        self._claim_outlook("settle-used@example.com", job_id=103)
        db.insert_account(email="settle-used@example.com", access_token="at", email_source="outlook")
        self.assertFalse(db.settle_failed_registration_email(
            "settle-used@example.com", reusable=True))
        self.assertEqual(db.get_outlook_by_email("settle-used@example.com")["status"], "used")

    def test_settle_returns_false_when_row_is_not_registering(self):
        self._claim_outlook("settle-failed@example.com", job_id=104)
        db.settle_failed_registration_email("settle-failed@example.com", reusable=False)
        # 已是 failed：不再迁移，也不能被 reusable=True 复活。
        self.assertFalse(db.settle_failed_registration_email(
            "settle-failed@example.com", reusable=True))
        self.assertEqual(db.get_outlook_by_email("settle-failed@example.com")["status"], "failed")

    def test_settle_returns_false_for_unknown_email(self):
        self.assertFalse(db.settle_failed_registration_email("nobody@example.com", reusable=True))
        self.assertFalse(db.settle_failed_registration_email("", reusable=True))

    def test_settle_temporary_email_without_pool_row_is_not_an_error(self):
        # 临时邮箱没有池行，上层沿用清理分支；即便误调本函数也只是返回 False。
        self.assertFalse(db.settle_failed_registration_email("temp@example.com", reusable=True))

    def test_settle_email_on_failure_delegates_to_pool_for_persisted_email(self):
        self._claim_outlook("settle-delegate@example.com", job_id=105)
        self.assertTrue(email_provider.settle_email_on_failure(
            "settle-delegate@example.com", reusable=True, note="network failure"))
        row = db.get_outlook_by_email("settle-delegate@example.com")
        self.assertEqual(row["status"], "available")
        self.assertEqual(row["status_change_source"], "registration_failure")
        self._claim_again("settle-delegate@example.com", lambda: db.claim_next_outlook(job_id=106))
        self.assertTrue(email_provider.settle_email_on_failure(
            "settle-delegate@example.com", reusable=False, note="unknown failure"))
        row = db.get_outlook_by_email("settle-delegate@example.com")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["network_release_count"], 1)

    def test_settle_email_on_failure_temporary_email_is_cleaned_up(self):
        # 临时邮箱没有池行：沿用 release_email_if_unconsumed 的清理分支，reusable 对它无意义。
        temp = "settle-temp@example.com"
        cf_temp_mail_client._CONTEXT_CACHE.clear()
        cf_temp_mail_client._CONTEXT_CACHE[temp] = cf_temp_mail_client.CFTempMailAccount(
            email=temp, jwt="jwt", domain="example.com"
        )
        with patch.object(email_provider, "resolve_email_source", return_value="cloudflare"):
            self.assertTrue(email_provider.settle_email_on_failure(temp, reusable=True, note="driver failed"))
        self.assertIsNone(cf_temp_mail_client.get_account_context(temp))

    def test_settle_email_on_failure_does_not_use_resolve_email_source(self):
        # 是否持久化池只能靠查四个池判断。resolve_email_source 查不到池行时会兜底返回
        # 第一个配置的来源，手动输入、未入池的地址会被误判为持久化池。
        self._claim_outlook("settle-no-resolve@example.com", job_id=107)
        with patch.object(email_provider, "resolve_email_source",
                          side_effect=AssertionError("settle_email_on_failure 不得调用 resolve_email_source")):
            self.assertTrue(email_provider.settle_email_on_failure(
                "settle-no-resolve@example.com", reusable=True))
        self.assertEqual(db.get_outlook_by_email("settle-no-resolve@example.com")["status"], "available")

    def test_settle_email_on_failure_unpooled_address_is_not_treated_as_pool_email(self):
        # 四个池都查不到的地址，即便 resolve_email_source 兜底成一个持久化来源，
        # 也不能走池迁移：池里不会凭空出现这一行。
        manual = "settle-manual@example.com"
        with patch.object(email_provider, "resolve_email_source", return_value="outlook"):
            self.assertFalse(email_provider.settle_email_on_failure(manual, reusable=True))
        self.assertIsNone(db.get_outlook_by_email(manual))

    def test_settle_works_for_every_persistent_pool(self):
        db.import_generic_api_emails([{"email": "gen@example.com", "code_url": "https://code.test/1"}])
        db.import_icloud_emails(["icloud@example.com"])
        db.claim_next_generic_api_email(job_id=110)
        db.claim_next_icloud_email(job_id=111)
        db.claim_next_domain_email("dom@example.com", job_id=112)
        for email, getter in (
            ("gen@example.com", db.get_generic_api_email_by_email),
            ("icloud@example.com", db.get_icloud_email_by_email),
            ("dom@example.com", db.get_domain_email_by_email),
        ):
            with self.subTest(email=email):
                self.assertTrue(db.settle_failed_registration_email(email, reusable=True))
                self.assertEqual(getter(email)["status"], "available")
                self.assertEqual(getter(email)["network_release_count"], 1)


    def test_claim_specific_email_works_for_every_persistent_pool(self):
        # 回池后的 available 行直接认领；达上限的 failed 行恢复后认领，计数清零、保留恢复痕迹。
        out, gen, dom, cloud = (
            "claim-out@example.com", "claim-gen@example.com",
            "claim-dom@example.com", "claim-icloud@example.com",
        )
        db.import_outlook_accounts([{
            "email": out, "password": "pw", "client_id": "c", "refresh_token": "r",
        }])
        db.import_generic_api_emails([{"email": gen, "code_url": "https://code.test/2"}])
        db.import_icloud_emails([cloud])
        db.claim_next_outlook(job_id=120)
        db.claim_next_generic_api_email(job_id=121)
        db.claim_next_icloud_email(job_id=122)
        db.claim_next_domain_email(dom, job_id=123)
        max_count = int(email_config.EMAIL_NETWORK_RELEASE_MAX_COUNT)
        pools = (
            ("outlook", out, db.get_outlook_by_email, lambda: db.claim_next_outlook(job_id=131)),
            ("generic_api", gen, db.get_generic_api_email_by_email,
             lambda: db.claim_next_generic_api_email(job_id=131)),
            ("domain", dom, db.get_domain_email_by_email,
             lambda: db.claim_next_domain_email(dom, job_id=131)),
            ("icloud", cloud, db.get_icloud_email_by_email, lambda: db.claim_next_icloud_email(job_id=131)),
        )
        for pool, email, getter, claim in pools:
            name = pool
            with self.subTest(pool=name, state="available"):
                self.assertTrue(db.settle_failed_registration_email(email, reusable=True))
                self.assertTrue(db.claim_specific_email(email, job_id=130))
                row = getter(email)
                self.assertEqual(row["status"], "registering")
                self.assertEqual(row["registration_job_id"], 130)
                self.assertEqual(row["network_release_count"], 1)
                self.assertIsNone(row["manual_reactivated_from"])
            with self.subTest(pool=name, state="failed"):
                # 上面的 subTest 已回池 1 次（计数 1）。再回池到第 max_count - 1 次仍是 available，
                # 第 max_count 次回池时计数达到上限，邮箱被标 failed 而不是回池。
                for count in range(2, max_count):
                    self.assertTrue(db.settle_failed_registration_email(email, reusable=True))
                    self.assertEqual(getter(email)["status"], "available")
                    self.assertEqual(getter(email)["network_release_count"], count)
                    self.assertEqual(claim()["email"].lower(), email, "重新领到的不是目标邮箱")
                self.assertTrue(db.settle_failed_registration_email(email, reusable=True))
                before = getter(email)
                self.assertEqual(before["status"], "failed", "构造前提：回池达上限后邮箱应被标 failed")
                self.assertEqual(before["network_release_count"], max_count)
                self.assertTrue(db.claim_specific_email(email, job_id=132))
                row = getter(email)
                self.assertEqual(row["status"], "registering")
                self.assertEqual(row["registration_job_id"], 132)
                self.assertEqual(row["network_release_count"], 0, "恢复后重新计数")
                self.assertEqual(row["status_change_source"], "retry")
                self.assertEqual(row["manual_reactivated_from"], "failed")
                self.assertEqual(row["manual_reactivated_by"], "retry-job-132")
                self.assertIsNone(row["note"], "上一轮的失败说明不应带到新任务")

    def test_claim_specific_email_rejects_rows_it_must_not_revive(self):
        # used / disabled 行、已有完整账号的 failed 行、被活动任务占用的行、不存在的邮箱：一律拒绝，且不改池行。
        address = "reject-used@example.com"
        self._import_and_claim_outlook(address, job_id=140)
        account_id = db.insert_account(email=address, access_token="at", email_source="outlook")
        self.assertTrue(db.mark_registration_success(address, account_id=account_id))
        self.assertEqual(db.get_outlook_by_email(address)["status"], "used")
        before = db.get_outlook_by_email(address)
        self.assertFalse(db.claim_specific_email(address, job_id=141))
        self.assertEqual(db.get_outlook_by_email(address), before)

        address = "reject-disabled@example.com"
        self._import_and_claim_outlook(address, job_id=140)
        db.settle_failed_registration_email(address, reusable=True)
        self.assertTrue(db.delete_outlook(address))
        self.assertEqual(db.get_outlook_by_email(address)["status"], "disabled")
        before = db.get_outlook_by_email(address)
        self.assertFalse(db.claim_specific_email(address, job_id=141))
        self.assertEqual(db.get_outlook_by_email(address), before)

        # restore_email_pool_entry 是人工边界、不查账号；自动重试必须自己挡住「邮箱已有完整账号」。
        address = "reject-account@example.com"
        self._import_and_claim_outlook(address, job_id=140)
        self.assertTrue(db.settle_failed_registration_email(address, reusable=False))
        db.insert_account(email=address, access_token="at", email_source="outlook")
        before = db.get_outlook_by_email(address)
        self.assertEqual(before["status"], "failed", "构造前提：已有账号的 failed 行用于验证账号守卫")
        self.assertFalse(db.claim_specific_email(address, job_id=141))
        self.assertEqual(db.get_outlook_by_email(address), before)

        # 行已 failed、但 registration_job_id 仍指向未结束的任务（任务线程还在跑时又标了失败）：
        # 不恢复也不认领，留给那个任务自己收尾。
        address = "reject-busy@example.com"
        owner = db.create_job(email_source="outlook", email=address)
        self._import_and_claim_outlook(address, job_id=owner["id"])
        self.assertTrue(db.settle_failed_registration_email(address, reusable=False))
        before = db.get_outlook_by_email(address)
        self.assertEqual(before["status"], "failed", "构造前提：行应为 failed")
        self.assertEqual(before["registration_job_id"], owner["id"], "构造前提：行应指向活动任务")
        self.assertEqual(db.get_job(owner["id"])["status"], "pending", "构造前提：占用它的任务仍未结束")
        self.assertFalse(db.claim_specific_email(address, job_id=owner["id"] + 1))
        self.assertEqual(db.get_outlook_by_email(address), before)

        self.assertFalse(db.claim_specific_email("missing@example.com", job_id=141))
        self.assertFalse(db.claim_specific_email("", job_id=141))
        self.assertFalse(db.claim_specific_email(None, job_id=141))

    # ---- 回池次数上限（D18）----


    def test_network_release_count_increments_until_limit(self):
        email = "limit@example.com"
        self._claim_outlook(email, job_id=201)
        self.assertTrue(db.settle_failed_registration_email(email, reusable=True))
        row = db.get_outlook_by_email(email)
        self.assertEqual(row["status"], "available")
        self.assertEqual(row["network_release_count"], 1)
        self._claim_again(email, lambda: db.claim_next_outlook(job_id=202))
        self.assertTrue(db.settle_failed_registration_email(email, reusable=True))
        row = db.get_outlook_by_email(email)
        self.assertEqual(row["status"], "available")
        self.assertEqual(row["network_release_count"], 2)
        # 第 3 次网络类失败：达到上限，不再回池，直接标 failed；这一次仍计入。
        self._claim_again(email, lambda: db.claim_next_outlook(job_id=203))
        self.assertTrue(db.settle_failed_registration_email(email, reusable=True))
        row = db.get_outlook_by_email(email)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["network_release_count"], 3)
        self.assertIn("3", row["status_change_reason"])
        self.assertIn("停止自动回池", row["status_change_reason"])

    def test_network_release_limit_disabled_when_max_count_is_zero(self):
        email = "nolimit@example.com"
        self._claim_outlook(email, job_id=211)
        with patch.object(email_config, "EMAIL_NETWORK_RELEASE_MAX_COUNT", 0):
            for round_no in (1, 2, 3, 4):
                if round_no > 1:
                    self._claim_again(email, lambda: db.claim_next_outlook(job_id=210 + round_no))
                self.assertTrue(db.settle_failed_registration_email(email, reusable=True))
                row = db.get_outlook_by_email(email)
                self.assertEqual(row["status"], "available", "第 %d 次" % round_no)
                self.assertEqual(row["network_release_count"], round_no)

    def test_not_reusable_does_not_change_network_release_count(self):
        email = "mixed@example.com"
        self._claim_outlook(email, job_id=220)
        db.settle_failed_registration_email(email, reusable=True)
        self._claim_again(email, lambda: db.claim_next_outlook(job_id=221))
        db.settle_failed_registration_email(email, reusable=False)
        row = db.get_outlook_by_email(email)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["network_release_count"], 1)

    def test_network_release_count_resets_on_manual_restore_and_success(self):
        email = "reset@example.com"
        self._claim_outlook(email, job_id=230)
        db.settle_failed_registration_email(email, reusable=True)
        self._claim_again(email, lambda: db.claim_next_outlook(job_id=231))
        db.settle_failed_registration_email(email, reusable=False)
        self.assertEqual(db.get_outlook_by_email(email)["network_release_count"], 1)
        # 人工恢复后重新计数。
        db.restore_email_pool_entry(email, reason="manual restore", actor="tester")
        self.assertEqual(db.get_outlook_by_email(email)["network_release_count"], 0)
        self._claim_again(email, lambda: db.claim_next_outlook(job_id=232))
        db.settle_failed_registration_email(email, reusable=True)
        self.assertEqual(db.get_outlook_by_email(email)["network_release_count"], 1)
        # 注册成功后计数清零。
        self._claim_again(email, lambda: db.claim_next_outlook(job_id=233))
        self.assertTrue(db.mark_registration_success(email, account_id=None))
        row = db.get_outlook_by_email(email)
        self.assertEqual(row["status"], "used")
        self.assertEqual(row["network_release_count"], 0)

    def _raise_network_release_count_to_two(self, email, claim):
        # 两轮网络类失败回池，计数到 2；再一次非网络类失败把池行标 failed。
        for job_id in (0, 1):
            claimed = claim(job_id)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["email"].lower(), email.lower(), "领到的不是目标邮箱")
            self.assertTrue(db.settle_failed_registration_email(email, reusable=True))
        claimed = claim(2)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["email"].lower(), email.lower(), "领到的不是目标邮箱")
        self.assertTrue(db.settle_failed_registration_email(email, reusable=False))

    def test_explicit_reimport_resets_network_release_count(self):
        # 四个池的显式重新导入：restore 之后调用方会用自己的旧字典再保存一次，
        # 清零必须落在那个字典上，否则会被旧计数覆盖。每池单独核对，防止只改了其中一处。
        outlook = "reimport@example.com"
        outlook_record = {"email": outlook, "password": "pw", "client_id": "c", "refresh_token": "r"}
        db._save_outlook([])
        db.import_outlook_accounts([outlook_record])
        self._raise_network_release_count_to_two(
            outlook, lambda n: db.claim_next_outlook(job_id=240 + n))
        self.assertEqual(db.get_outlook_by_email(outlook)["network_release_count"], 2)
        inserted, _ = db.import_outlook_accounts([outlook_record], reactivate_existing=True)
        self.assertEqual(inserted, 1)
        self.assertEqual(db.get_outlook_by_email(outlook)["network_release_count"], 0)

        generic = "reimport-gen@example.com"
        generic_record = {"email": generic, "code_url": "https://code.test/2"}
        db._save_generic_api_emails([])
        db.import_generic_api_emails([generic_record])
        self._raise_network_release_count_to_two(
            generic, lambda n: db.claim_next_generic_api_email(job_id=243 + n))
        self.assertEqual(db.get_generic_api_email_by_email(generic)["network_release_count"], 2)
        inserted, _ = db.import_generic_api_emails([generic_record], reactivate_existing=True)
        self.assertEqual(inserted, 1)
        self.assertEqual(db.get_generic_api_email_by_email(generic)["network_release_count"], 0)

        icloud = "reimport-icloud@example.com"
        db._save_icloud_emails([])
        db.import_icloud_emails([icloud])
        self._raise_network_release_count_to_two(
            icloud, lambda n: db.claim_next_icloud_email(job_id=246 + n))
        self.assertEqual(db.get_icloud_email_by_email(icloud)["network_release_count"], 2)
        inserted, _ = db.import_icloud_emails([icloud], reactivate_existing=True)
        self.assertEqual(inserted, 1)
        self.assertEqual(db.get_icloud_email_by_email(icloud)["network_release_count"], 0)

        # 域名邮箱的领取接受邮箱参数；池行为 failed 时 reactivate_existing=True 走 restore 分支，
        # 恢复后直接迁 registering，不停在 available。
        domain = "reimport-dom@example.com"
        db._save_domain_pool([])
        for job_id in (249, 250):
            db.claim_next_domain_email(domain, job_id=job_id)
            self.assertTrue(db.settle_failed_registration_email(domain, reusable=True))
        db.claim_next_domain_email(domain, job_id=251)
        db.settle_failed_registration_email(domain, reusable=False)
        self.assertEqual(db.get_domain_email_by_email(domain)["network_release_count"], 2)
        row = db.claim_next_domain_email(domain, job_id=252, reactivate_existing=True)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "registering")
        self.assertEqual(db.get_domain_email_by_email(domain)["network_release_count"], 0)

    def test_legacy_pool_row_without_count_reads_as_zero(self):
        db._OUTLOOK_JSON.write_text(
            '[{"id": 1, "email": "legacy-count@example.com", "status": "available"}]', encoding="utf-8",
        )
        self.assertEqual(db.get_outlook_by_email("legacy-count@example.com")["network_release_count"], 0)

    def test_startup_migration_keeps_network_release_count(self):
        db._OUTLOOK_JSON.write_text(json.dumps([{
            "id": 1, "email": "interrupted@example.com", "status": "registering",
            "registration_job_id": None, "network_release_count": 2,
        }]), encoding="utf-8")
        db.migrate_email_pool_statuses()
        row = db.get_outlook_by_email("interrupted@example.com")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["network_release_count"], 2)


class EmailPoolMigrationTests(unittest.TestCase):
    def test_json_and_sqlite_apply_safe_legacy_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}))
                stack.enter_context(patch.object(db, "_OUTLOOK_JSON", root / "outlook.json"))
                stack.enter_context(patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"))
                stack.enter_context(patch.object(db, "_JOBS_JSON", root / "jobs.json"))
                (root / "outlook.json").write_text(
                    '[{"id": 1, "email": "old@example.com", "status": "used"}, '
                    '{"id": 2, "email": "bad@example.com", "status": "mystery"}]',
                    encoding="utf-8",
                )
                result = db.migrate_email_pool_statuses()
                self.assertGreaterEqual(result["rows_failed"], 1)
                rows = {row["email"]: row for row in db.list_outlook_pool()}
                self.assertEqual(rows["old@example.com"]["status"], "failed")
                self.assertEqual(rows["bad@example.com"]["status"], "disabled")
                self.assertTrue(rows["bad@example.com"].get("status_migration_reason"))

            runtime = root / "runtime.db"
            store = SQLiteRuntimeStore(runtime)
            store.initialize()
            store.replace_all("outlook_emails", [
                {"id": 1, "email": "leased@example.com", "status": "leased"},
                {"id": 2, "email": "unknown@example.com", "status": "???"},
            ])
            bindings = dict(db._SQLITE_PATH_BINDINGS)
            with (
                patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "sqlite"}),
                patch.object(db, "_RUNTIME_DB", runtime),
                patch.object(db, "_SQLITE_STORE", store),
                patch.object(db, "_SQLITE_PATH_BINDINGS", bindings),
            ):
                db.migrate_email_pool_statuses()
                rows = {row["email"]: row for row in db.list_outlook_pool()}
                self.assertEqual(rows["leased@example.com"]["status"], "failed")
                self.assertEqual(rows["unknown@example.com"]["status"], "disabled")


    def test_startup_recovery_runs_before_pool_migration(self):
        # 迁移把 pending / running / stopping 的注册任务算作占用。上个进程遗留的任务若不先收口，
        # 它们领取的池行会永远留在 registering。本用例先复现缺口，再验证「先收口、后迁移」的顺序。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}))
                for name, filename in (
                    ("_OUTLOOK_JSON", "outlook.json"), ("_ACCOUNTS_JSON", "accounts.json"),
                    ("_JOBS_JSON", "jobs.json"),
                ):
                    stack.enter_context(patch.object(db, name, root / filename))
                stack.enter_context(patch.object(db, "_LOG_DIR", root / "logs"))
                stack.enter_context(patch.object(db, "_DATA_DIR", root))

                main = "interrupted@example.com"
                with_account = "has-account@example.com"
                released = "released@example.com"
                db.import_outlook_accounts([{
                    "email": address, "password": "pw", "client_id": "c", "refresh_token": "r",
                } for address in (main, with_account, released)])

                main_job = db.create_job(email_source="outlook", email=main)
                db.claim_next_outlook(job_id=main_job["id"])
                account_job = db.create_job(email_source="outlook", email=with_account)
                db.claim_next_outlook(job_id=account_job["id"])
                db.insert_account(email=with_account, access_token="at", email_source="outlook")
                # 复现中断窗口：账号已落库，任务还没来得及把池行标 used 进程就退出了。
                # insert_account 会顺手把池行标 used，这里只把状态拨回 registering，账号关联保留。
                db._save_outlook([
                    dict(row, status="registering") if row.get("email") == with_account else row
                    for row in db._load_outlook()
                ])
                released_job = db.create_job(email_source="outlook", email=released)
                db.claim_next_outlook(job_id=released_job["id"])
                db.settle_failed_registration_email(released, reusable=True)
                # 上个进程里的状态：主行与「已有账号」两行的线程在跑；已回池的那行尚未被领走，
                # 它的任务还在排队。
                db.update_job(account_job["id"], status="running")
                for address in (main, with_account):
                    self.assertEqual(db.get_outlook_by_email(address)["status"], "registering",
                                     "构造前提：两个池行应处于 registering")
                self.assertEqual(db.get_job(main_job["id"])["status"], "pending")
                self.assertEqual(db.get_job(account_job["id"])["status"], "running")
                self.assertEqual(db.get_job(released_job["id"])["status"], "pending")
                self.assertIsNotNone(db.get_outlook_by_email(with_account)["registered_account_id"],
                                     "构造前提：池行已关联本地账号")
                row = db.get_outlook_by_email(released)
                self.assertEqual((row["status"], row["network_release_count"]), ("available", 1))

                # 复现缺口：任务仍活动时，迁移不会动它领取的池行。
                db.migrate_email_pool_statuses()
                self.assertEqual(db.get_outlook_by_email(main)["status"], "registering")
                with self.assertRaises(db.EmailPoolLifecycleError) as blocked:
                    db.check_email_pool_delete(main, source="outlook")
                self.assertEqual(blocked.exception.code, "registration_busy")

                self.assertEqual(db.recover_interrupted_registration_jobs(), 3,
                                 "pending 与 running 的注册任务全部收口")
                db.migrate_email_pool_statuses()

                row = db.get_outlook_by_email(main)
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["network_release_count"], 0)
                self.assertEqual(row["status_change_reason"], "邮箱池状态启动迁移")
                # 池行不再被占用：可以删除，也可以人工恢复后重新使用。
                db.check_email_pool_delete(main, source="outlook")
                db.restore_email_pool_entry(main, source="manual", reason="重新导入", actor="test")
                self.assertEqual(db.get_outlook_by_email(main)["status"], "available")

                # 对照：已有完整账号的 registering 行迁为 used；已回池的行保持 available 且计数不变。
                self.assertEqual(db.get_outlook_by_email(with_account)["status"], "used")
                row = db.get_outlook_by_email(released)
                self.assertEqual(row["status"], "available")
                self.assertEqual(row["network_release_count"], 1)

class RegistrationJobFailureCategoryTests(unittest.TestCase):
    """任务行的 failure_category 与重启收口：新建为 None，update_job 非空才写入，旧记录读出按 None 处理；
    服务重启时把上个进程遗留的活动任务收口为已结束。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}))
        self.jobs_json = root / "jobs.json"
        self.stack.enter_context(patch.object(db, "_JOBS_JSON", self.jobs_json))
        # _new_job_row 会在 _LOG_DIR 下建目录；指向临时目录，不碰仓库里的真实日志目录。
        self.stack.enter_context(patch.object(db, "_LOG_DIR", root / "logs"))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def test_new_job_has_no_failure_category(self):
        job = db.create_job(email_source="outlook", email="job@example.com")
        self.assertIsNone(job["failure_category"])
        self.assertIsNone(db.get_job(job["id"])["failure_category"])

    def test_update_job_writes_failure_category(self):
        job = db.create_job(email_source="outlook", email="job@example.com")
        db.update_job(job["id"], status="failed", error="x", failure_category="network")
        self.assertEqual(db.get_job(job["id"])["failure_category"], "network")
        db.update_job(job["id"], failure_category="unknown")
        self.assertEqual(db.get_job(job["id"])["failure_category"], "unknown")
        db.update_job(job["id"], failure_category="account")
        self.assertEqual(db.get_job(job["id"])["failure_category"], "account")

    def test_update_job_without_failure_category_keeps_existing_value(self):
        # 与 update_job 其余参数同一语义：None 表示不改。
        job = db.create_job(email_source="outlook", email="job@example.com")
        db.update_job(job["id"], failure_category="network")
        db.update_job(job["id"], status="failed", error="x")
        row = db.get_job(job["id"])
        self.assertEqual(row["failure_category"], "network")
        self.assertEqual(row["status"], "failed")

    def test_retry_job_does_not_inherit_failure_category(self):
        # 重试子任务是新的任务行，不继承原任务的失败类别。
        source = db.create_job(email_source="outlook", email="job@example.com")
        db.update_job(source["id"], status="failed", error="x", failure_category="network")
        child, created = db.create_retry_job(source["id"], job_type="registration", email_source="outlook")
        self.assertTrue(created)
        self.assertIsNone(child["failure_category"])
        self.assertIsNone(db.get_job(child["id"])["failure_category"])
        # 原任务的类别不被改动。
        self.assertEqual(db.get_job(source["id"])["failure_category"], "network")

    def test_legacy_job_record_reads_as_none(self):
        # 引入该字段之前写入的任务记录没有这个键。读取方按 None 处理（等同 unknown），
        # 所以这里断言的是读取合同 .get("failure_category")，而不是键是否存在。
        self.jobs_json.write_text(json.dumps([{
            "id": 7,
            "job_uuid": "legacy",
            "job_type": "registration",
            "parent_job_id": None,
            "root_job_id": None,
            "retry_attempt": 0,
            "retry_action": None,
            "email_source": "outlook",
            "email": "legacy@example.com",
            "status": "failed",
            "error_message": "old failure",
            "log_file": "legacy.log",
            "started_at": None,
            "completed_at": None,
            "account_id": None,
            "network_traffic": None,
            "created_at": "2020-01-01T00:00:00",
        }], ensure_ascii=False), encoding="utf-8")

        self.assertNotIn("failure_category", db.get_job(7))
        self.assertIsNone(db.get_job(7).get("failure_category"))

        # 对旧记录改别的字段，不会凭空写入该键。
        db.update_job(7, error="edited")
        self.assertNotIn("failure_category", db.get_job(7))
        self.assertEqual(db.get_job(7)["error_message"], "edited")

        # 对旧记录显式写入后即可读出。
        db.update_job(7, failure_category="network")
        self.assertEqual(db.get_job(7)["failure_category"], "network")

    def test_startup_recovery_closes_jobs_left_active_by_previous_process(self):
        active = {}
        for status in ("pending", "running", "stopping"):
            job = db.create_job(email_source="outlook", email=f"left-{status}@example.com")
            db.update_job(job["id"], status=status)
            active[status] = job["id"]
        finished = {}
        for status in ("success", "failed", "stopped", "cancelled"):
            job = db.create_job(email_source="outlook", email=f"done-{status}@example.com")
            db.update_job(job["id"], status=status, error=f"original {status}", completed_at="2026-01-01T00:00:00")
            finished[status] = db.get_job(job["id"])
        codex, created = db.create_retry_job(
            finished["failed"]["id"], job_type="codex_retry", email_source="outlook",
            email="done-failed@example.com", account_id=None,
        )
        self.assertTrue(created)
        self.assertEqual(codex["job_type"], "codex_retry")

        self.assertEqual(db.recover_interrupted_registration_jobs(), 4)

        expected = {
            "pending": "cancelled", "running": "stopped", "stopping": "stopped",
        }
        for status, target in expected.items():
            with self.subTest(status=status):
                row = db.get_job(active[status])
                self.assertEqual(row["status"], target)
                self.assertIn("WebUI 重启", row["error_message"])
                self.assertTrue(row["completed_at"])
        self.assertIn("未执行", db.get_job(active["pending"])["error_message"])
        self.assertIn("执行中断", db.get_job(active["running"])["error_message"])
        codex_row = db.get_job(codex["id"])
        self.assertEqual(codex_row["status"], "cancelled", "pending 的补跑任务同样收口")
        self.assertIn("WebUI 重启", codex_row["error_message"])
        for status, before in finished.items():
            with self.subTest(untouched=status):
                self.assertEqual(db.get_job(before["id"]), before, "已结束的任务记录不应被改动")

        # 幂等：再跑一次没有可收口的行，也不再写盘。
        self.assertEqual(db.recover_interrupted_registration_jobs(), 0)
        self.assertEqual(db.get_job(active["running"])["status"], "stopped")

    def test_startup_recovery_does_not_touch_failure_category(self):
        # 收口只改状态、说明与完成时间，不写失败类别：它不是对异常的分类结果。
        job = db.create_job(email_source="outlook", email="category@example.com")
        db.update_job(job["id"], status="running")
        self.assertEqual(db.recover_interrupted_registration_jobs(), 1)
        row = db.get_job(job["id"])
        self.assertEqual(row["status"], "stopped")
        self.assertIsNone(row.get("failure_category"))


    def test_retry_after_startup_recovery_takes_new_email(self):
        # 收口出来的任务没有失败类别，重试时不会复用原邮箱：子任务执行时领新邮箱。
        for previous, recovered in (("pending", "cancelled"), ("running", "stopped")):
            with self.subTest(previous=previous):
                # 上一轮 retry_job 留下的 pending 子任务不能进入本轮收口。
                db._save_jobs([])
                source = db.create_job(email_source="outlook", email=f"recovered-{previous}@example.com")
                db.update_job(source["id"], status=previous)
                self.assertEqual(db.recover_interrupted_registration_jobs(), 1)
                closed = db.get_job(source["id"])
                self.assertEqual(closed["status"], recovered)
                self.assertIsNone(closed.get("failure_category"))

                class _Executor:
                    def __init__(self):
                        self.calls = []

                    def submit(self, fn, *args):
                        self.calls.append((fn, args))

                executor = _Executor()
                with patch.object(db, "_ACCOUNTS_JSON", Path(self.temp.name) / f"accounts-{previous}.json"), \
                        patch.object(registration_service, "get_executor", return_value=executor), \
                        patch.object(db, "claim_specific_email") as claim:
                    result = registration_service.retry_job(source["id"], workers=1)

                self.assertTrue(result["ok"], result.get("error"))
                self.assertEqual(result["retry_action"], "registration")
                child = db.get_job(result["job"]["id"])
                self.assertEqual(child["job_type"], "registration")
                self.assertIsNone(child["email"], "收口后的重试不复用原邮箱")
                self.assertEqual(len(executor.calls), 1)
                claim.assert_not_called()
                self.assertIsNone(db.get_job(source["id"]).get("failure_category"))


class EmailPoolApiStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}))
        self.stack.enter_context(patch.object(email_config, "EMAIL_SOURCE", "outlook"))
        for name, filename in {
            "_OUTLOOK_JSON": "outlook.json",
            "_GENERIC_API_EMAIL_JSON": "generic.json",
            "_DOMAIN_EMAIL_JSON": "domain.json",
            "_ICLOUD_EMAIL_JSON": "icloud.json",
            "_ACCOUNTS_JSON": "accounts.json",
            "_JOBS_JSON": "jobs.json",
        }.items():
            self.stack.enter_context(patch.object(db, name, root / filename))
        for name in (
            "recover_interrupted_plan_checks",
            "recover_interrupted_live_checks",
        ):
            self.stack.enter_context(patch.object(web_app.db, name, return_value=0))
        self.client = web_app.create_app(auth_code="test-auth").test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def test_api_exposes_five_state_filter_and_rejects_failed_revival(self):
        db.import_outlook_accounts([{
            "email": "api@example.com",
            "password": "pw",
            "client_id": "client",
            "refresh_token": "refresh",
        }])
        db.claim_next_outlook(job_id=40)
        db.mark_registration_failed("api@example.com", "create failed", job_id=40)

        listed = self.client.get(
            "/api/outlook?source=outlook&status=failed",
            headers=self.headers,
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()[0]["status"], "failed")

        revive = self.client.post(
            "/api/outlook/status",
            json={"email": "api@example.com", "source": "outlook", "status": "available"},
            headers=self.headers,
        )
        self.assertEqual(revive.status_code, 409)
        self.assertEqual(db.get_outlook_by_email("api@example.com")["status"], "failed")

        summary = self.client.get("/api/summary", headers=self.headers)
        self.assertEqual(summary.status_code, 200)
        pool = summary.get_json()["email_pool"]
        self.assertEqual(set(EMAIL_POOL_STATUSES), set(pool) - {"total"})
        self.assertEqual(pool["failed"], 1)


if __name__ == "__main__":
    unittest.main()
