# -*- coding: utf-8 -*-
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import db
from core.mailcom_capacity import (
    CAPACITY_LIFETIME_FULL,
    CAPACITY_QUERY_UNKNOWN,
    MailComCapacitySnapshot,
    aggregate_history_payload,
)
from core.mailcom_alias_service import MailComAliasError, MailComAliasService, _coerce_history_snapshot
import core.mailcom_alias_pool_service as alias_pool_service
from core.mailcom_settings_client import (
    MailComSettingsClient,
    MailComSettingsError,
    MailComSettingsRemoteConflictError,
)
from core.sqlite_store import SQLiteRuntimeStore


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload
        self.headers = {}

    def json(self):
        if self.payload is None:
            raise ValueError("invalid json")
        return self.payload


def _history_rows(lifetime=99, active=0):
    rows = [{"address": "mother@mail.com", "state": "ACTIVE", "deletable": False}]
    rows.extend(
        {
            "address": f"old-{index}@example.test",
            "state": "ACTIVE" if index < active else "INACTIVE",
            "deletable": True,
        }
        for index in range(lifetime)
    )
    return rows


class _HistorySession:
    def __init__(self, payload, *, first_get_status=200, create_status=201):
        self.payload = payload
        self.first_get_status = first_get_status
        self.create_status = create_status
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if self.first_get_status != 200:
            status, self.first_get_status = self.first_get_status, 200
            return _Response(status)
        return _Response(payload=self.payload)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return _Response(self.create_status, payload={})


class MailComLifetimeCapacityTests(unittest.TestCase):
    def test_aggregate_99_history_rows_excludes_mother_and_counts_active(self):
        snapshot = aggregate_history_payload({"mailaddresslist": _history_rows(99, 4)})
        self.assertEqual(snapshot.lifetime_alias_count, 99)
        self.assertEqual(snapshot.active_alias_count, 4)
        self.assertEqual(snapshot.status, CAPACITY_LIFETIME_FULL)

    def test_unknown_state_counts_lifetime_but_incomplete_page_is_fail_closed(self):
        rows = _history_rows(2, 1)
        rows[-1]["state"] = "FUTURE_STATE"
        snapshot = aggregate_history_payload({"mailaddresslist": rows})
        self.assertEqual(snapshot.lifetime_alias_count, 2)
        self.assertEqual(snapshot.active_alias_count, 1)
        self.assertEqual(snapshot.unknown_state_count, 1)

        incomplete = aggregate_history_payload({"mailaddresslist": rows, "totalCount": 10})
        self.assertFalse(incomplete.complete)
        self.assertEqual(incomplete.status, CAPACITY_QUERY_UNKNOWN)
        with self.assertRaises(ValueError):
            aggregate_history_payload({"mailaddresslist": [{"address": "bad@example.test", "state": "ACTIVE"}]})

    def test_raw_history_mapping_preserves_pagination_incomplete_state(self):
        class Settings:
            def history_snapshot(self):
                return {
                    "mailaddresslist": _history_rows(2, 0),
                    "next": "/mailaccount/primary/emailAddresses?page=2",
                }

        snapshot = _coerce_history_snapshot(Settings().history_snapshot())
        self.assertFalse(snapshot.complete)
        self.assertEqual(snapshot.status, CAPACITY_QUERY_UNKNOWN)

    def test_history_url_omits_active_filter(self):
        session = _HistorySession({"mailaddresslist": _history_rows(99, 0)})
        snapshot = MailComSettingsClient(session=session).history_snapshot()
        self.assertEqual(snapshot.lifetime_alias_count, 99)
        call = session.calls[0]
        self.assertEqual(call[0], "GET")
        self.assertEqual(call[2]["params"], {"absoluteURI": "false", "q.type.in": "MANAGED,DOMAIN_HOSTING"})
        self.assertNotIn("q.state.in", call[2]["params"])

    def test_history_401_refreshes_once_and_409_is_distinct(self):
        first = _HistorySession({"mailaddresslist": _history_rows(1, 0)}, first_get_status=401)
        second = _HistorySession({"mailaddresslist": _history_rows(2, 1)})
        sessions = [first, second]

        class LoginClient:
            def __init__(self, session):
                self.session = session

            def login(self, email, password):
                pass

            def bootstrap_settings_session(self):
                return SimpleNamespace(access_token="refreshed", expires_at=9999999999)

        def factory():
            return LoginClient(sessions.pop(0))

        client = MailComSettingsClient(login_client_factory=factory, now=lambda: 1000)
        client.authenticate("mother@mail.com", "password")
        snapshot = client.history_snapshot()
        self.assertEqual(snapshot.lifetime_alias_count, 2)
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(len(second.calls), 1)

        conflict = _HistorySession({"mailaddresslist": _history_rows(0, 0)}, create_status=409)
        with self.assertRaises(MailComSettingsRemoteConflictError) as ctx:
            MailComSettingsClient(session=conflict).create_address("candidate@example.test")
        self.assertEqual(ctx.exception.error_type, "remote_create_conflict")
        self.assertNotIn("candidate@example.test", str(ctx.exception))

    def test_json_and_sqlite_snapshots_are_redacted_and_failures_keep_old_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}), patch.object(
                db, "_MAILCOM_EMAIL_JSON", root / "parents.json"
            ):
                db.import_mailcom_emails([{"email": "mother@mail.com", "password": "secret"}])
                snapshot = MailComCapacitySnapshot(99, 2)
                self.assertTrue(db.update_mailcom_capacity_snapshot("mother@mail.com", snapshot))
                public = db.get_mailcom_email_by_email("mother@mail.com")
                self.assertEqual(public["remote_lifetime_remaining"], 0)
                self.assertEqual(public["remote_capacity_status"], "lifetime_full")
                self.assertNotIn("password", public)
                synced_at = public["remote_history_synced_at"]
                self.assertTrue(db.update_mailcom_capacity_snapshot("mother@mail.com", local_active_delta=-1))
                local_delta = db.get_mailcom_email_by_email("mother@mail.com")
                self.assertEqual(local_delta["remote_active_alias_count"], 1)
                self.assertEqual(local_delta["remote_lifetime_alias_count"], 99)
                self.assertEqual(local_delta["remote_history_synced_at"], synced_at)
                self.assertEqual(local_delta["remote_history_error"], "local_activity_delta")
                self.assertTrue(db.update_mailcom_capacity_snapshot("mother@mail.com", error="network_error"))
                failed = db.get_mailcom_email_by_email("mother@mail.com")
                self.assertEqual(failed["remote_lifetime_alias_count"], 99)
                self.assertEqual(failed["remote_history_synced_at"], synced_at)
                self.assertEqual(failed["remote_capacity_status"], "unknown")
                db.update_mailcom_capacity_snapshot(
                    "mother@mail.com", error="Authorization: Bearer leaked-token mother@mail.com"
                )
                self.assertEqual(
                    db.get_mailcom_email_by_email("mother@mail.com")["remote_history_error"], "[redacted]"
                )
                raw = db._read_json(root / "parents.json", [])
                raw[0]["remote_history_error"] = "Bearer old-token old@mail.com"
                db._write_json(root / "parents.json", raw)
                self.assertEqual(
                    db.get_mailcom_email_by_email("mother@mail.com")["remote_history_error"], "[redacted]"
                )

            runtime = root / "runtime.db"
            store = SQLiteRuntimeStore(runtime)
            store.initialize()
            bindings = dict(db._SQLITE_PATH_BINDINGS)
            with (
                patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "sqlite"}),
                patch.object(db, "_RUNTIME_DB", runtime),
                patch.object(db, "_SQLITE_STORE", store),
                patch.object(db, "_SQLITE_PATH_BINDINGS", bindings),
            ):
                db.import_mailcom_emails([{"email": "sqlite@mail.com", "password": "secret"}])
                db.update_mailcom_capacity_snapshot("sqlite@mail.com", MailComCapacitySnapshot(3, 2))
                row = store.load("mailcom_emails")[0]
                self.assertEqual(row["remote_lifetime_alias_count"], 3)
                self.assertNotIn("history", row.get("remote_history_error") or "")

    def test_lifetime_full_stops_sync_before_any_create_post(self):
        class Settings:
            def __init__(self):
                self.create_calls = 0
                self.history_calls = 0

            def authenticate(self, email, password):
                pass

            def history_snapshot(self):
                self.history_calls += 1
                return MailComCapacitySnapshot(99, 0)

            def list_addresses(self):
                raise AssertionError("lifetime full should not perform active/create flow")

        settings = Settings()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}
        ), patch.object(db, "_MAILCOM_EMAIL_JSON", Path(directory) / "parents.json"):
            db.import_mailcom_emails([{"email": "mother@mail.com", "password": "secret"}])
            service = MailComAliasService(settings_client_factory=lambda: settings)
            with self.assertRaises(MailComAliasError) as ctx:
                service.sync_parent_aliases(db.get_mailcom_internal_record("mother@mail.com"))
            self.assertEqual(ctx.exception.error_type, "lifetime_capacity_full")
            self.assertEqual(settings.create_calls, 0)
            self.assertEqual(settings.history_calls, 1)

    def test_first_409_stops_batch_and_classifies_after_one_forced_history_read(self):
        class Settings:
            def __init__(self):
                self.history_calls = 0
                self.create_calls = 0

            def authenticate(self, email, password):
                pass

            def history_snapshot(self):
                self.history_calls += 1
                return MailComCapacitySnapshot(10, 0)

            def validate_address(self, address):
                pass

            def create_address(self, address):
                self.create_calls += 1
                raise MailComSettingsError("masked", error_type="remote_create_conflict")

            def list_addresses(self):
                raise AssertionError("409 without a successful create must not issue a final active GET")

        settings = Settings()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}
        ), patch.object(db, "_MAILCOM_EMAIL_JSON", Path(directory) / "parents.json"):
            db.import_mailcom_emails([{"email": "mother@mail.com", "password": "secret"}])
            service = MailComAliasService(settings_client_factory=lambda: settings)
            with self.assertRaises(MailComAliasError) as ctx:
                service.sync_parent_aliases(db.get_mailcom_internal_record("mother@mail.com"), target=9)
            self.assertEqual(ctx.exception.error_type, "remote_create_conflict")
            self.assertEqual(settings.create_calls, 1)
            self.assertEqual(settings.history_calls, 2)

    def test_409_after_success_does_not_repeat_history_calibration(self):
        class Settings:
            def __init__(self):
                self.history_calls = 0
                self.create_calls = 0
                self.rows = [{"address": "mother@mail.com", "state": "ACTIVE", "deletable": False}]

            def authenticate(self, email, password):
                pass

            def history_snapshot(self):
                self.history_calls += 1
                return MailComCapacitySnapshot(97, 0) if self.history_calls == 1 else MailComCapacitySnapshot(98, 1)

            def validate_address(self, address):
                pass

            def create_address(self, address):
                self.create_calls += 1
                if self.create_calls == 1:
                    self.rows.append({"address": address, "state": "ACTIVE", "deletable": True})
                    return
                raise MailComSettingsRemoteConflictError()

            def list_addresses(self):
                return list(self.rows)

        settings = Settings()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}
        ), patch.object(db, "_MAILCOM_EMAIL_JSON", Path(directory) / "parents.json"), patch.object(
            db, "_MAILCOM_ALIAS_JSON", Path(directory) / "aliases.json"
        ), patch(
            "core.mailcom_alias_service.generate_alias_local_part", side_effect=["one", "two"]
        ), patch(
            "core.mailcom_alias_service.choose_alias_domain", return_value="example.test"
        ):
            db.import_mailcom_emails([{"email": "mother@mail.com", "password": "secret"}])
            with self.assertRaises(MailComAliasError) as ctx:
                MailComAliasService(settings_client_factory=lambda: settings).sync_parent_aliases(
                    db.get_mailcom_internal_record("mother@mail.com"), target=2
                )
            self.assertEqual(ctx.exception.error_type, "remote_create_conflict")
            self.assertEqual(settings.create_calls, 2)
            self.assertEqual(settings.history_calls, 2)

    def test_concurrent_forced_history_refresh_reuses_inflight_snapshot(self):
        class CoordinatedLock:
            def __init__(self):
                self._lock = threading.Lock()
                self.second_waiting = threading.Event()

            def acquire(self, blocking=True):
                acquired = self._lock.acquire(blocking)
                if not acquired:
                    self.second_waiting.set()
                return acquired

            def release(self):
                self._lock.release()

        class Settings:
            def __init__(self):
                self.history_calls = 0
                self.started = threading.Event()
                self.release = threading.Event()

            def history_snapshot(self):
                self.history_calls += 1
                self.started.set()
                self.release.wait(timeout=2)
                return MailComCapacitySnapshot(20, 0)

        settings = Settings()
        refresh_lock = CoordinatedLock()
        results = []
        errors = []

        def refresh(service):
            try:
                results.append(service._refresh_history(settings, "mother@mail.com", force=True))
            except Exception as exc:  # pragma: no cover - assertion captures failures below
                errors.append(exc)

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}
        ), patch.object(db, "_MAILCOM_EMAIL_JSON", Path(directory) / "parents.json"), patch(
            "core.mailcom_alias_service.history_refresh_lock", return_value=refresh_lock
        ):
            db.import_mailcom_emails([{"email": "mother@mail.com", "password": "secret"}])
            service = MailComAliasService()
            first = threading.Thread(target=refresh, args=(service,))
            second = threading.Thread(target=refresh, args=(service,))
            first.start()
            self.assertTrue(settings.started.wait(timeout=1))
            second.start()
            self.assertTrue(refresh_lock.second_waiting.wait(timeout=1))
            settings.release.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(settings.history_calls, 1)

    def test_409_history_classifies_lifetime_and_active_limits(self):
        for expected, history in (
            ("lifetime_capacity_full", MailComCapacitySnapshot(99, 0)),
            ("active_capacity_full", MailComCapacitySnapshot(40, 9)),
        ):
            class Settings:
                def __init__(self):
                    self.calls = 0

                def authenticate(self, email, password):
                    pass

                def history_snapshot(self):
                    self.calls += 1
                    return MailComCapacitySnapshot(1, 0) if self.calls == 1 else history

                def validate_address(self, address):
                    pass

                def create_address(self, address):
                    raise MailComSettingsRemoteConflictError()

                def list_addresses(self):
                    raise AssertionError("no final active GET after first 409")

            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}
            ), patch.object(db, "_MAILCOM_EMAIL_JSON", Path(directory) / "parents.json"):
                db.import_mailcom_emails([{"email": "mother@mail.com", "password": "secret"}])
                with self.assertRaises(MailComAliasError) as ctx:
                    MailComAliasService(settings_client_factory=Settings).sync_parent_aliases(
                        db.get_mailcom_internal_record("mother@mail.com"), target=1
                    )
                self.assertEqual(ctx.exception.error_type, expected)

    def test_fresh_snapshot_uses_active_check_without_history_get(self):
        class Settings:
            def __init__(self):
                self.history_calls = 0
                self.list_calls = 0

            def authenticate(self, email, password):
                pass

            def history_snapshot(self):
                self.history_calls += 1
                return MailComCapacitySnapshot(20, 0)

            def list_addresses(self):
                self.list_calls += 1
                return [{"address": "mother@mail.com", "state": "ACTIVE", "deletable": False}]

        settings = Settings()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}
        ), patch.object(db, "_MAILCOM_EMAIL_JSON", Path(directory) / "parents.json"):
            db.import_mailcom_emails([{"email": "mother@mail.com", "password": "secret"}])
            db.update_mailcom_capacity_snapshot("mother@mail.com", MailComCapacitySnapshot(20, 0))
            parent = db.get_mailcom_internal_record("mother@mail.com")
            service = MailComAliasService(settings_client_factory=lambda: settings)
            result = service.sync_parent_aliases(parent, target=0)
            self.assertEqual(result["create_opportunity_count"], 0)
            self.assertEqual(settings.history_calls, 0)
            self.assertGreaterEqual(settings.list_calls, 1)

    def test_history_refresh_queue_deduplicates_by_parent_and_persists_only_aggregate(self):
        class Executor:
            def __init__(self):
                self.calls = []

            def submit(self, fn, *args):
                self.calls.append((fn, args))
                return object()

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}
        ), patch.object(db, "_MAILCOM_EMAIL_JSON", Path(directory) / "parents.json"):
            db.import_mailcom_emails([{"email": "mother@mail.com", "password": "secret"}])
            executor = Executor()
            with alias_pool_service._HISTORY_PENDING_LOCK:
                alias_pool_service._HISTORY_PENDING.clear()
            with patch.object(alias_pool_service, "_HISTORY_EXECUTOR", executor):
                first = alias_pool_service.enqueue_parent_history_refresh("mother@mail.com")
                second = alias_pool_service.enqueue_parent_history_refresh("mother@mail.com")
            self.assertTrue(first["accepted"])
            self.assertTrue(second["busy"])
            self.assertEqual(len(executor.calls), 1)
            with alias_pool_service._HISTORY_PENDING_LOCK:
                alias_pool_service._HISTORY_PENDING.clear()

            refreshed = alias_pool_service.refresh_parent_history_now(
                "mother@mail.com",
                refresh_fn=lambda _: {"snapshot": MailComCapacitySnapshot(90, 2)},
            )
            self.assertTrue(refreshed["ok"])
            public = db.get_mailcom_email_by_email("mother@mail.com")
            self.assertEqual(public["remote_lifetime_alias_count"], 90)
            self.assertEqual(public["remote_lifetime_remaining"], 9)
            self.assertNotIn("password", refreshed)

    def test_cache_policy_marks_expired_and_near_snapshots_for_refresh(self):
        service = MailComAliasService()
        fresh = {
            "remote_lifetime_alias_count": 20,
            "remote_lifetime_alias_limit": 99,
            "remote_history_synced_at": "2099-01-01T00:00:00",
            "remote_capacity_status": "normal",
        }
        expired = {**fresh, "remote_history_synced_at": "2000-01-01T00:00:00"}
        near = {**fresh, "remote_lifetime_alias_count": 90, "remote_capacity_status": "near_limit"}
        self.assertFalse(service._snapshot_needs_refresh(fresh))
        self.assertTrue(service._snapshot_needs_refresh(expired))
        self.assertTrue(service._snapshot_needs_refresh(near))

    def test_partial_lifetime_budget_batches_three_creates_then_one_calibration(self):
        class Settings:
            def __init__(self):
                self.rows = [{"address": "mother@mail.com", "state": "ACTIVE", "deletable": False}]
                self.create_calls = []
                self.history_calls = 0

            def authenticate(self, email, password):
                pass

            def history_snapshot(self):
                self.history_calls += 1
                return MailComCapacitySnapshot(96 if self.history_calls == 1 else 99, len(self.create_calls))

            def validate_address(self, address):
                pass

            def create_address(self, address):
                self.create_calls.append(address)
                self.rows.append({"address": address, "state": "ACTIVE", "deletable": True})

            def list_addresses(self):
                return list(self.rows)

        settings = Settings()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}
        ), patch.object(db, "_MAILCOM_EMAIL_JSON", Path(directory) / "parents.json"), patch.object(
            db, "_MAILCOM_ALIAS_JSON", Path(directory) / "aliases.json"
        ), patch(
            "core.mailcom_alias_service.generate_alias_local_part", side_effect=["one", "two", "three"]
        ), patch(
            "core.mailcom_alias_service.choose_alias_domain", return_value="example.test"
        ):
            db.import_mailcom_emails([{"email": "mother@mail.com", "password": "secret"}])
            result = MailComAliasService(settings_client_factory=lambda: settings).sync_parent_aliases(
                db.get_mailcom_internal_record("mother@mail.com"), target=9
            )
            self.assertEqual(result["create_opportunity_count"], 3)
            self.assertEqual(result["create_request_count"], 3)
            self.assertEqual(result["created_count"], 3)
            self.assertEqual(len(settings.create_calls), 3)
            self.assertEqual(settings.history_calls, 2)


if __name__ == "__main__":
    unittest.main()
