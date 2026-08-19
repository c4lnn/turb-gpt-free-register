# -*- coding: utf-8 -*-
import threading
import time
import unittest
from types import SimpleNamespace

import core.mailcom_settings_client as settings_module
from core.mailcom_settings_client import (
    MailComSettingsClient,
    MailComSettingsConfirmationError,
    MailComSettingsConflictError,
    MailComSettingsError,
)


class _Response:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Session:
    def __init__(self):
        self.calls = []
        self.addresses = [
            {"address": "mother@mail.com", "state": "ACTIVE", "deletable": False},
            {"address": "old@example.com", "state": "ACTIVE", "deletable": True},
        ]
        self.create_status = 201
        self.delete_status = 204

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return _Response(payload={"mailaddresslist": list(self.addresses)})

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/emailAddressValidations"):
            return _Response(payload={})
        if url.endswith("/emailAddresses"):
            if self.create_status == 201:
                self.addresses.append({
                    "address": kwargs["json"]["address"], "state": "ACTIVE", "deletable": True,
                })
            return _Response(self.create_status)
        if "/emailAddressesRemovals/" in url:
            if self.delete_status == 204:
                encoded = url.split("/emailAddressesRemovals/", 1)[1].split("/removals", 1)[0]
                self.addresses = [item for item in self.addresses if item["address"].replace("@", "%40") != encoded]
            return _Response(self.delete_status)
        raise AssertionError(url)


class _LoginClient:
    def __init__(self, session, *, access_token, expires_at, login_delay=0.0):
        self.session = session
        self.access_token = access_token
        self.expires_at = expires_at
        self.login_delay = login_delay
        self.login_calls = []
        self.bootstrap_calls = 0

    def login(self, email, password):
        self.login_calls.append((email, password))
        if self.login_delay:
            time.sleep(self.login_delay)

    def bootstrap_settings_session(self):
        self.bootstrap_calls += 1
        return SimpleNamespace(access_token=self.access_token, expires_at=self.expires_at)


class MailComSettingsClientTests(unittest.TestCase):
    def setUp(self):
        with settings_module._SETTINGS_TOKEN_LOCKS_GUARD:
            settings_module._SETTINGS_TOKEN_CACHE.clear()
            settings_module._SETTINGS_TOKEN_LOCKS.clear()

    def tearDown(self):
        self.setUp()

    def test_har_contract_for_list_validate_create_and_encoded_delete(self):
        session = _Session()
        client = MailComSettingsClient(session=session, timeout=7)

        listed = client.list_addresses()
        self.assertEqual(listed[1]["address"], "old@example.com")
        self.assertTrue(listed[1]["deletable"])
        client.validate_address("Alias@Example.com")
        client.create_address("Alias@Example.com")
        self.assertTrue(client.delete_address("Alias@Example.com"))

        list_call, validate_call, create_call, delete_call = session.calls
        self.assertEqual(list_call[1], "https://settings-cats.mail.com/mailaccount/primary/emailAddresses")
        self.assertEqual(list_call[2]["params"]["q.state.in"], "ACTIVE")
        self.assertIn("X-Request-ID", list_call[2]["headers"])
        self.assertEqual(validate_call[2]["json"], ["alias@example.com"])
        self.assertEqual(create_call[2]["json"]["address"], "alias@example.com")
        self.assertEqual(create_call[2]["json"]["state"], "ACTIVE")
        self.assertIn("alias%40example.com", delete_call[1])
        self.assertEqual(delete_call[2]["params"], {"absoluteURI": "false"})

    def test_list_missing_protocol_fields_fails_closed(self):
        session = _Session()
        session.addresses = [{"address": "bad@example.com", "state": "ACTIVE"}]
        with self.assertRaisesRegex(MailComSettingsError, "缺少") as ctx:
            MailComSettingsClient(session=session).list_addresses()
        self.assertEqual(ctx.exception.error_type, "protocol_error")

    def test_http_error_is_classified_without_response_body(self):
        session = _Session()
        session.create_status = 401
        with self.assertRaises(MailComSettingsError) as ctx:
            MailComSettingsClient(session=session).create_address("alias@example.com")
        self.assertEqual(ctx.exception.error_type, "unauthorized")
        self.assertNotIn("password", str(ctx.exception).lower())

    def test_create_conflict_and_missing_delete_are_explicit_and_idempotent(self):
        session = _Session()
        session.create_status = 412
        with self.assertRaises(MailComSettingsConflictError) as ctx:
            MailComSettingsClient(session=session).create_address("alias@example.com")
        self.assertEqual(ctx.exception.error_type, "address_conflict")

        session.delete_status = 404
        self.assertFalse(MailComSettingsClient(session=session).delete_address("missing@example.com"))
        delete_call = session.calls[-1]
        self.assertIn("missing%40example.com", delete_call[1])

    def test_authentication_keeps_settings_session_ephemeral(self):
        settings_session = _Session()

        class LoginClient:
            def __init__(self):
                self.session = settings_session
                self.login_calls = []

            def login(self, email, password):
                self.login_calls.append((email, password))

            def bootstrap_settings_session(self):
                return SimpleNamespace(access_token="settings-at", expires_at=time.time() + 3600)

        login_client = LoginClient()
        client = MailComSettingsClient(login_client_factory=lambda: login_client)
        client.authenticate("mother@mail.com", "test-password")

        self.assertIs(client.session, settings_session)
        self.assertTrue(client._authenticated)
        self.assertEqual(login_client.login_calls, [("mother@mail.com", "test-password")])
        client.list_addresses()
        self.assertEqual(settings_session.calls[-1][2]["headers"]["Authorization"], "Bearer settings-at")
        # 认证数据只留在当前 client；公开诊断从不包含密码或 Cookie。
        with self.assertRaises(MailComSettingsError) as ctx:
            MailComSettingsClient(session=None)._request(
                "GET", "/test", endpoint="test", expected={200}, headers={}
            )
        self.assertEqual(ctx.exception.error_type, "invalid_credentials")
        self.assertNotIn("test-password", str(ctx.exception))

    def test_authentication_reuses_cached_token_with_new_transport_session(self):
        clock = [1_000.0]
        login_clients = []

        def factory():
            client = _LoginClient(
                _Session(),
                access_token="settings-token-secret",
                expires_at=clock[0] + 3_600,
            )
            login_clients.append(client)
            return client

        first = MailComSettingsClient(login_client_factory=factory, now=lambda: clock[0])
        second = MailComSettingsClient(login_client_factory=factory, now=lambda: clock[0])
        first.authenticate("mother@mail.com", "test-password")
        with self.assertLogs("core.mailcom_settings_client", level="INFO") as logs:
            second.authenticate("mother@mail.com", "test-password")
            second.list_addresses()

        self.assertEqual(len(login_clients), 2)
        self.assertEqual([len(client.login_calls) for client in login_clients], [1, 0])
        self.assertIsNot(first.session, second.session)
        self.assertEqual(
            second.session.calls[-1][2]["headers"]["Authorization"],
            "Bearer settings-token-secret",
        )
        output = "\n".join(logs.output)
        self.assertIn("stage=settings_token_cache action=hit", output)
        self.assertIn("stage=email_addresses", output)
        self.assertNotIn("settings-token-secret", output)
        self.assertNotIn("test-password", output)
        self.assertNotIn("mother@mail.com", output)

    def test_authentication_refreshes_token_at_sixty_second_skew(self):
        clock = [1_000.0]
        login_clients = []

        def factory():
            index = len(login_clients)
            client = _LoginClient(
                _Session(),
                access_token=f"settings-token-{index}",
                expires_at=clock[0] + (60 if index == 0 else 3_600),
            )
            login_clients.append(client)
            return client

        first = MailComSettingsClient(login_client_factory=factory, now=lambda: clock[0])
        second = MailComSettingsClient(login_client_factory=factory, now=lambda: clock[0])
        first.authenticate("mother@mail.com", "test-password")
        second.authenticate("mother@mail.com", "test-password")

        self.assertEqual(len(login_clients), 2)
        self.assertEqual([len(client.login_calls) for client in login_clients], [1, 1])
        self.assertEqual(second._settings_access_token, "settings-token-1")

    def test_concurrent_authentication_for_same_parent_logs_in_once(self):
        clock = [1_000.0]
        login_clients = []
        factory_lock = threading.Lock()
        start = threading.Barrier(2)
        errors = []

        def factory():
            client = _LoginClient(
                _Session(),
                access_token="settings-token-shared",
                expires_at=clock[0] + 3_600,
                login_delay=0.05,
            )
            with factory_lock:
                login_clients.append(client)
            return client

        def authenticate():
            try:
                client = MailComSettingsClient(login_client_factory=factory, now=lambda: clock[0])
                start.wait(timeout=2)
                client.authenticate("mother@mail.com", "test-password")
            except Exception as exc:  # pragma: no cover - 断言会给出具体错误
                errors.append(exc)

        threads = [threading.Thread(target=authenticate), threading.Thread(target=authenticate)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sum(len(client.login_calls) for client in login_clients), 1)

    def test_concurrent_401_refreshes_once_when_new_token_text_matches_old_token(self):
        clock = [1_000.0]
        old_entry = settings_module._SettingsTokenCacheEntry(
            access_token="settings-token-same-text",
            expires_at=clock[0] + 3_600,
        )
        settings_module._SETTINGS_TOKEN_CACHE["mother@mail.com"] = old_entry
        login_clients = []
        factory_lock = threading.Lock()
        start = threading.Barrier(2)
        errors = []

        def factory():
            client = _LoginClient(
                _Session(),
                access_token="settings-token-same-text",
                expires_at=clock[0] + 3_600,
                login_delay=0.05,
            )
            with factory_lock:
                login_clients.append(client)
            return client

        def refresh():
            try:
                client = MailComSettingsClient(login_client_factory=factory, now=lambda: clock[0])
                client._parent_email = "mother@mail.com"
                client._password = "test-password"
                client._settings_access_token = old_entry.access_token
                client._settings_cache_entry = old_entry
                start.wait(timeout=2)
                client._refresh_settings_token_after_unauthorized(
                    old_entry.access_token,
                    observed_entry=old_entry,
                )
            except Exception as exc:  # pragma: no cover - 断言会给出具体错误
                errors.append(exc)

        threads = [threading.Thread(target=refresh), threading.Thread(target=refresh)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sum(len(client.login_calls) for client in login_clients), 1)

    def test_list_addresses_refreshes_once_after_unauthorized(self):
        clock = [1_000.0]

        class UnauthorizedListSession(_Session):
            def get(self, url, **kwargs):
                self.calls.append(("GET", url, kwargs))
                return _Response(status_code=401)

        sessions = [UnauthorizedListSession(), _Session()]
        login_clients = []

        def factory():
            client = _LoginClient(
                sessions.pop(0),
                access_token=f"settings-token-{len(login_clients)}",
                expires_at=clock[0] + 3_600,
            )
            login_clients.append(client)
            return client

        client = MailComSettingsClient(login_client_factory=factory, now=lambda: clock[0])
        client.authenticate("mother@mail.com", "test-password")
        addresses = client.list_addresses()

        self.assertEqual(len(addresses), 2)
        self.assertEqual(len(login_clients), 2)
        self.assertEqual(len(login_clients[0].session.calls), 1)
        self.assertEqual(len(login_clients[1].session.calls), 1)
        self.assertEqual(
            login_clients[1].session.calls[0][2]["headers"]["Authorization"],
            "Bearer settings-token-1",
        )

    def test_list_addresses_retries_only_once_when_refreshed_token_is_also_unauthorized(self):
        clock = [1_000.0]

        class UnauthorizedListSession(_Session):
            def get(self, url, **kwargs):
                self.calls.append(("GET", url, kwargs))
                return _Response(status_code=401)

        sessions = [UnauthorizedListSession(), UnauthorizedListSession()]
        login_clients = []

        def factory():
            client = _LoginClient(
                sessions.pop(0),
                access_token=f"settings-token-{len(login_clients)}",
                expires_at=clock[0] + 3_600,
            )
            login_clients.append(client)
            return client

        client = MailComSettingsClient(login_client_factory=factory, now=lambda: clock[0])
        client.authenticate("mother@mail.com", "test-password")
        with self.assertRaises(MailComSettingsError) as ctx:
            client.list_addresses()

        self.assertEqual(ctx.exception.error_type, "unauthorized")
        self.assertEqual(len(login_clients), 2)
        self.assertEqual(sum(len(login_client.session.calls) for login_client in login_clients), 2)

    def test_create_401_confirms_existing_alias_without_replaying_post(self):
        clock = [1_000.0]

        class UnauthorizedCreateSession(_Session):
            def post(self, url, **kwargs):
                self.calls.append(("POST", url, kwargs))
                if url.endswith("/emailAddresses"):
                    return _Response(status_code=401)
                raise AssertionError(url)

        first = UnauthorizedCreateSession()
        confirmed = _Session()
        confirmed.addresses.append({"address": "alias@example.com", "state": "ACTIVE", "deletable": True})
        sessions = [first, confirmed]

        def factory():
            return _LoginClient(
                sessions.pop(0),
                access_token=f"settings-token-{len(sessions)}",
                expires_at=clock[0] + 3_600,
            )

        client = MailComSettingsClient(login_client_factory=factory, now=lambda: clock[0])
        client.authenticate("mother@mail.com", "test-password")
        client.create_address("alias@example.com")

        create_calls = [call for call in first.calls if call[0] == "POST" and call[1].endswith("/emailAddresses")]
        self.assertEqual(len(create_calls), 1)
        self.assertFalse(any(call[0] == "POST" for call in confirmed.calls))

    def test_create_401_without_alias_confirmation_does_not_replay_post(self):
        clock = [1_000.0]

        class UnauthorizedCreateSession(_Session):
            def post(self, url, **kwargs):
                self.calls.append(("POST", url, kwargs))
                if url.endswith("/emailAddresses"):
                    return _Response(status_code=401)
                raise AssertionError(url)

        first = UnauthorizedCreateSession()
        second = _Session()
        sessions = [first, second]

        def factory():
            return _LoginClient(
                sessions.pop(0),
                access_token=f"settings-token-{len(sessions)}",
                expires_at=clock[0] + 3_600,
            )

        client = MailComSettingsClient(login_client_factory=factory, now=lambda: clock[0])
        client.authenticate("mother@mail.com", "test-password")
        with self.assertRaises(MailComSettingsConfirmationError):
            client.create_address("alias@example.com")

        create_calls = [call for call in first.calls if call[0] == "POST" and call[1].endswith("/emailAddresses")]
        self.assertEqual(len(create_calls), 1)
        self.assertFalse(any(call[0] == "POST" for call in second.calls))

    def test_delete_401_confirms_absence_without_replaying_post(self):
        clock = [1_000.0]

        class UnauthorizedDeleteSession(_Session):
            def post(self, url, **kwargs):
                self.calls.append(("POST", url, kwargs))
                if "/emailAddressesRemovals/" in url:
                    return _Response(status_code=401)
                raise AssertionError(url)

        first = UnauthorizedDeleteSession()
        second = _Session()
        sessions = [first, second]

        def factory():
            return _LoginClient(
                sessions.pop(0),
                access_token=f"settings-token-{len(sessions)}",
                expires_at=clock[0] + 3_600,
            )

        client = MailComSettingsClient(login_client_factory=factory, now=lambda: clock[0])
        client.authenticate("mother@mail.com", "test-password")
        self.assertTrue(client.delete_address("missing@example.com"))

        delete_calls = [call for call in first.calls if "/emailAddressesRemovals/" in call[1]]
        self.assertEqual(len(delete_calls), 1)
        self.assertFalse(any(call[0] == "POST" for call in second.calls))

    def test_delete_401_with_remaining_alias_does_not_replay_post(self):
        clock = [1_000.0]

        class UnauthorizedDeleteSession(_Session):
            def post(self, url, **kwargs):
                self.calls.append(("POST", url, kwargs))
                if "/emailAddressesRemovals/" in url:
                    return _Response(status_code=401)
                raise AssertionError(url)

        first = UnauthorizedDeleteSession()
        second = _Session()
        second.addresses.append({"address": "alias@example.com", "state": "ACTIVE", "deletable": True})
        sessions = [first, second]

        def factory():
            return _LoginClient(
                sessions.pop(0),
                access_token=f"settings-token-{len(sessions)}",
                expires_at=clock[0] + 3_600,
            )

        client = MailComSettingsClient(login_client_factory=factory, now=lambda: clock[0])
        client.authenticate("mother@mail.com", "test-password")
        with self.assertRaises(MailComSettingsConfirmationError):
            client.delete_address("alias@example.com")

        delete_calls = [call for call in first.calls if "/emailAddressesRemovals/" in call[1]]
        self.assertEqual(len(delete_calls), 1)
        self.assertFalse(any(call[0] == "POST" for call in second.calls))


if __name__ == "__main__":
    unittest.main()
