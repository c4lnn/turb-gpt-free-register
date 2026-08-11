import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import requests

from config import roxybrowser as roxy_config
from core import roxy_codex_oauth
from core import roxy_registration
from core import roxybrowser_client as roxy_client


class _Response:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload if payload is not None else {"code": 0}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload


class RoxyProfileCreationSerializationTests(unittest.TestCase):
    def setUp(self):
        roxy_client._NEXT_CREATE_AT = 0.0

    @staticmethod
    def _client(handler):
        client = roxy_client.RoxyBrowserClient(api_base="http://roxy.test")
        client.http.request = MagicMock(side_effect=handler)
        return client

    def test_create_requests_from_multiple_clients_are_serialized(self):
        state_lock = threading.Lock()
        start = threading.Barrier(5)
        active = 0
        max_active = 0
        sequence = 0

        def handler(*_args, **_kwargs):
            nonlocal active, max_active, sequence
            with state_lock:
                active += 1
                max_active = max(max_active, active)
                sequence += 1
                profile_id = f"profile-{sequence}"
            threading.Event().wait(0.02)
            with state_lock:
                active -= 1
            return _Response({"code": 0, "data": {"dirId": profile_id}})

        clients = [self._client(handler) for _ in range(4)]

        def create(index):
            start.wait()
            if index == 0:
                result = clients[index].request("POST", "/browser/create", json_body={})
                return result["data"]["dirId"]
            return clients[index].create_profile()

        with patch.object(roxy_config, "ROXY_CREATE_INTERVAL", 0.0), ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(create, index) for index in range(4)]
            start.wait()
            profile_ids = [future.result(timeout=2) for future in futures]

        self.assertEqual(max_active, 1)
        self.assertEqual(len(set(profile_ids)), 4)

    def test_create_interval_uses_default_and_custom_values(self):
        for interval in (0.5, 0.2):
            with self.subTest(interval=interval):
                roxy_client._NEXT_CREATE_AT = 0.0
                client = self._client(lambda *_args, **_kwargs: _Response())
                with patch.object(roxy_config, "ROXY_CREATE_INTERVAL", interval), patch.object(
                    roxy_client.time, "monotonic", side_effect=[0.0, 0.0, 0.0, interval]
                ), patch.object(roxy_client.time, "sleep") as sleep:
                    client.request("POST", "/browser/create", json_body={})
                    client.request("POST", "/browser/create", json_body={})
                sleep.assert_called_once_with(interval)

    def test_registration_and_codex_share_create_scheduler(self):
        state_lock = threading.Lock()
        start = threading.Barrier(3)
        active = 0
        max_active = 0
        sequence = 0

        def request_handler(_method, url, **_kwargs):
            nonlocal active, max_active, sequence
            if not url.endswith("/browser/create"):
                payload = {"code": 0, "data": {"debuggerAddress": "127.0.0.1:9222"}}
                return _Response(payload)
            with state_lock:
                active += 1
                max_active = max(max_active, active)
                sequence += 1
                profile_id = f"workflow-{sequence}"
            threading.Event().wait(0.02)
            with state_lock:
                active -= 1
            return _Response({"code": 0, "data": {"dirId": profile_id}})

        def run_after_barrier(callable_):
            start.wait()
            try:
                return callable_()
            except RuntimeError as exc:
                self.assertEqual(str(exc), "stop after profile creation")
                return None

        workflows = (
            lambda: roxy_registration.run_roxy_registration("reg@example.com", "Reg User", "1990-01-01"),
            lambda: roxy_codex_oauth.run_roxy_codex_oauth("codex@example.com", force=True),
        )
        with patch.object(roxy_config, "ROXY_CREATE_INTERVAL", 0.0), patch.object(
            roxy_config, "ROXY_PROFILE_ID", ""
        ), patch.object(roxy_client.requests.Session, "request", side_effect=request_handler), patch.object(
            roxy_registration, "_build_driver", side_effect=RuntimeError("stop after profile creation")
        ), patch.object(
            roxy_codex_oauth, "_build_driver", side_effect=RuntimeError("stop after profile creation")
        ), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run_after_barrier, workflow) for workflow in workflows]
            start.wait()
            results = [future.result(timeout=2) for future in futures]

        self.assertEqual(max_active, 1)
        self.assertEqual(len(results), 2)

    def test_create_failures_release_lock_and_are_not_retried(self):
        failures = (
            _Response({"code": 1, "msg": "正在创建中，请稍等"}),
            _Response({"code": 500}, status_code=500, text="server error"),
            requests.Timeout("create timed out"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                roxy_client._NEXT_CREATE_AT = 0.0
                outcomes = [failure, _Response({"code": 0, "data": {"dirId": "next"}})]

                def handler(*_args, **_kwargs):
                    outcome = outcomes.pop(0)
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome

                client = self._client(handler)
                with patch.object(roxy_config, "ROXY_CREATE_INTERVAL", 0.0):
                    with self.assertRaises(Exception):
                        client.request("POST", "/browser/create", json_body={})
                    result = client.request("POST", "/browser/create", json_body={})

                self.assertEqual(result["data"]["dirId"], "next")
                self.assertEqual(client.http.request.call_count, 2)

    def test_non_create_request_does_not_wait_for_create_lock(self):
        create_entered = threading.Event()
        release_create = threading.Event()
        open_completed = threading.Event()

        def handler(_method, url, **_kwargs):
            if url.endswith("/browser/create"):
                create_entered.set()
                release_create.wait(1)
                return _Response({"code": 0})
            open_completed.set()
            return _Response({"code": 0})

        client = self._client(handler)
        with patch.object(roxy_config, "ROXY_CREATE_INTERVAL", 0.0), ThreadPoolExecutor(max_workers=2) as executor:
            create_future = executor.submit(client.request, "POST", "/browser/create", json_body={})
            self.assertTrue(create_entered.wait(1))
            open_future = executor.submit(client.request, "POST", "/browser/open", json_body={})
            self.assertTrue(open_completed.wait(0.2))
            release_create.set()
            create_future.result(timeout=1)
            open_future.result(timeout=1)

    def test_codex_reuse_existing_profile_does_not_create_client(self):
        opened = MagicMock(profile_id="existing", raw={})
        driver = MagicMock()
        with patch.object(roxy_codex_oauth, "RoxyBrowserClient") as client_class, patch(
            "core.codex_oauth._codex_auth_url_source", return_value="unsupported-test-source"
        ):
            result = roxy_codex_oauth._run_roxy_codex_oauth_once(
                "user@example.com",
                force=True,
                existing_driver=driver,
                existing_opened=opened,
                reuse_existing_profile=True,
                clear_existing_state=False,
            )

        self.assertFalse(result["ok"])
        client_class.assert_not_called()


if __name__ == "__main__":
    unittest.main()
