# -*- coding: utf-8 -*-
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from core import sentinel_runner
from core.registration_geo import build_registration_locale, timezone_offset_for


class StopNow(Exception):
    pass


class _FakeStdin:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, text):
        self.writes.append(text)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _LineIter:
    def __init__(self, lines):
        self._it = iter(lines)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)


class _FakeProc:
    def __init__(self, stdout_lines=(), stderr_lines=()):
        self.stdin = _FakeStdin()
        self.stdout = _LineIter(stdout_lines)
        self.stderr = _LineIter(stderr_lines)
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.waited = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.waited.append(timeout)
        return self.returncode


def _line(obj):
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _requirements_result(flow="username_password_create", p="gAAAAACreq"):
    return _line({"type": "requirements_result", "flow": flow, "p": p})


def _solve_result(flow="username_password_create", token=None, so=None):
    token = token if token is not None else {
        "p": "gAAAAACtok", "c": "challenge", "id": "device", "flow": flow,
    }
    return _line({"type": "solve_result", "flow": flow, "token": token, "so": so})


class SentinelSDKContractTests(unittest.TestCase):
    def _make_sdk(self, **kwargs):
        kwargs.setdefault("flow", "username_password_create")
        kwargs.setdefault("device_id", "device")
        return sentinel_runner.SentinelSDK(**kwargs)



    def test_proof_presence_follows_the_current_challenge_requirement(self):
        for required in (False, True):
            with self.subTest(pow_required=required):
                proc = _FakeProc(stdout_lines=[_requirements_result(), _solve_result(token={
                    "p": None, "c": "challenge", "id": "device",
                    "flow": "username_password_create",
                })])
                with patch.object(sentinel_runner.subprocess, "Popen", return_value=proc), patch.object(
                    sentinel_runner, "_ensure_runner_environment"
                ), self._make_sdk() as sdk:
                    sdk.requirements()
                    challenge = {"token": "challenge", "proofofwork": {"required": required}}
                    if required:
                        with self.assertRaises(sentinel_runner.SentinelSDKError):
                            sdk.solve(challenge)
                    else:
                        token, _ = sdk.solve(challenge)
                        self.assertEqual(json.loads(token)["c"], challenge["token"])

    def test_empty_main_token_is_rejected_even_without_a_pow_requirement(self):
        proc = _FakeProc(stdout_lines=[_requirements_result(), _solve_result(token={})])
        with patch.object(sentinel_runner.subprocess, "Popen", return_value=proc), patch.object(
            sentinel_runner, "_ensure_runner_environment"
        ), self._make_sdk() as sdk:
            sdk.requirements()
            with self.assertRaises(sentinel_runner.SentinelSDKError):
                sdk.solve({"token": "challenge", "proofofwork": {"required": False}})

    def test_solve_before_requirements_fails(self):
        sdk = self._make_sdk()
        with self.assertRaises(sentinel_runner.SentinelSDKError) as raised:
            sdk.solve({"token": "challenge"})
        self.assertEqual(raised.exception.category, "no_requirements")

    def test_requirements_called_twice_fails(self):
        proc = _FakeProc(stdout_lines=[_requirements_result()])
        sdk = self._make_sdk()
        with patch.object(sentinel_runner.subprocess, "Popen", return_value=proc), \
                patch.object(sentinel_runner, "_ensure_runner_environment"):
            sdk.requirements()
            with self.assertRaises(sentinel_runner.SentinelSDKError) as raised:
                sdk.requirements()
        self.assertEqual(raised.exception.category, "reuse")

    def test_stop_requested_propagates_and_closes_process(self):
        proc = _FakeProc(stdout_lines=[])

        def stop():
            raise StopNow()

        sdk = self._make_sdk(check_stop_requested=stop)
        with patch.object(sentinel_runner.subprocess, "Popen", return_value=proc), \
                patch.object(sentinel_runner, "_ensure_runner_environment"):
            with self.assertRaises(StopNow):
                with sdk:
                    sdk.requirements()
        self.assertTrue(proc.terminated or proc.killed)

    def test_close_is_idempotent(self):
        proc = _FakeProc(stdout_lines=[_requirements_result()])
        sdk = self._make_sdk()
        with patch.object(sentinel_runner.subprocess, "Popen", return_value=proc), \
                patch.object(sentinel_runner, "_ensure_runner_environment"):
            sdk.requirements()
            sdk.close()
            sdk.close()
        self.assertTrue(proc.terminated or proc.killed)


@unittest.skipUnless(shutil.which("node"), "Node.js runtime unavailable")
class SentinelDateTimezoneTests(unittest.TestCase):
    def test_vm_dates_use_each_dates_timezone_offset_and_long_name(self):
        script = """
const fs = require('node:fs');
const vm = require('node:vm');
const runner = require('./sentinel/sentinel-runner.js');
const message = JSON.parse(fs.readFileSync(0, 'utf8'));
const {context, clearTimers} = runner.createBrowserContext(runner.buildOptions(message, message.flow));
const result = vm.runInContext(`['2026-01-15T12:00:00Z', '2026-07-15T12:00:00Z'].map(value => {
  const date = new Date(value);
  const parts = new Intl.DateTimeFormat('en-US', {timeZoneName: 'long'}).formatToParts(date);
  return {offset: date.getTimezoneOffset(), text: date.toString(),
    name: parts.find(part => part.type === 'timeZoneName').value};
})`, context);
clearTimers();
process.stdout.write(JSON.stringify(result));
"""
        dates = (datetime(2026, 1, 15, 12, tzinfo=timezone.utc),
                 datetime(2026, 7, 15, 12, tzinfo=timezone.utc))
        for country, zone in (("AU", "Australia/Sydney"), ("CA", "America/Toronto"),
                              ("DE", "Europe/Berlin")):
            with self.subTest(country=country):
                sdk = sentinel_runner.SentinelSDK(flow="username_password_create", device_id="test-device",
                    browser_profile=build_registration_locale(country, zone))
                sdk._timezone_iana = zone
                result = subprocess.run([shutil.which("node"), "-e", script],
                    input=json.dumps(sdk._build_requirements_message()),
                    cwd=Path(__file__).resolve().parents[1], env={**os.environ, "TZ": zone},
                    capture_output=True, text=True, encoding="utf-8", timeout=15, check=True)
                for actual, date in zip(json.loads(result.stdout), dates):
                    self.assertEqual(actual["offset"], -timezone_offset_for(zone, date))
                    self.assertTrue(actual["text"].endswith("(" + actual["name"] + ")"))


if __name__ == "__main__":
    unittest.main()
