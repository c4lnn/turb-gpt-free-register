# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import patch

from core.browser_data_saver import BrowserDataSaver
from core.browser_traffic import SeleniumTrafficTracker


def _performance_event(method, params):
    return {
        "message": json.dumps({"message": {"method": method, "params": params}}),
    }


class _SeleniumDriver:
    def __init__(self, entries):
        self.entries = list(entries)
        self.cdp_commands = []

    def execute_cdp_cmd(self, command, params):
        self.cdp_commands.append((command, params))
        return {}

    def get_log(self, name):
        entries, self.entries = self.entries, []
        return entries

    def execute_script(self, script):
        return {}


class _CoverageSeleniumDriver(_SeleniumDriver):
    def execute_cdp_cmd(self, command, params):
        self.cdp_commands.append((command, params))
        if command == "Profiler.takePreciseCoverage":
            return {
                "result": [
                    {
                        "scriptId": "10",
                        "url": "https://chatgpt.com/cdn/assets/used.js?token=secret",
                        "functions": [
                            {"functionName": "", "ranges": [{"startOffset": 0, "endOffset": 80, "count": 1}]},
                            {"functionName": "neverCalled", "ranges": [{"startOffset": 90, "endOffset": 140, "count": 0}]},
                        ],
                    },
                    {
                        "scriptId": "11",
                        "url": "https://chatgpt.com/cdn/assets/unused.js",
                        "functions": [
                            {"functionName": "unused", "ranges": [{"startOffset": 0, "endOffset": 60, "count": 0}]},
                        ],
                    },
                    {
                        "scriptId": "12",
                        "url": "data:text/javascript,inline-secret",
                        "functions": [
                            {"functionName": "inline", "ranges": [{"startOffset": 0, "endOffset": 10, "count": 0}]},
                        ],
                    },
                ],
            }


class BrowserTrafficTests(unittest.TestCase):
    def test_selenium_parses_network_events_and_websocket_frames(self):
        entries = [
            _performance_event(
                "Network.requestWillBeSent",
                {
                    "requestId": "1",
                    "request": {
                        "method": "POST",
                        "url": "https://example.test/register",
                        "headers": {"content-type": "application/json"},
                        "postData": "{}",
                    },
                },
            ),
            _performance_event(
                "Network.responseReceived",
                {
                    "requestId": "1",
                    "response": {"status": 200, "statusText": "OK", "headers": {"content-type": "application/json"}},
                },
            ),
            _performance_event(
                "Network.loadingFinished",
                {"requestId": "1", "encodedDataLength": 20},
            ),
            _performance_event(
                "Network.webSocketCreated",
                {"requestId": "ws-1"},
            ),
            _performance_event(
                "Network.webSocketFrameSent",
                {"requestId": "ws-1", "response": {"payloadData": "xy"}},
            ),
            _performance_event(
                "Network.webSocketFrameReceived",
                {"requestId": "ws-1", "response": {"payloadData": "z"}},
            ),
        ]
        driver = _SeleniumDriver(entries)
        tracker = SeleniumTrafficTracker(driver)
        result = tracker.stop()

        self.assertEqual(driver.cdp_commands[0][0], "Network.enable")
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["completed_request_count"], 1)
        self.assertEqual(result["websocket_count"], 1)
        self.assertEqual(result["websocket_upload_bytes"], 2)
        self.assertEqual(result["websocket_download_bytes"], 1)
        self.assertGreater(result["http_upload_bytes"], 0)
        self.assertGreaterEqual(result["http_download_bytes"], 20)

    def test_selenium_does_not_count_cdp_blocked_request_as_network_bytes(self):
        entries = [
            _performance_event(
                "Network.requestWillBeSent",
                {
                    "requestId": "blocked-1",
                    "type": "Image",
                    "request": {
                        "method": "GET",
                        "url": "https://cdn.example/avatar.png",
                        "headers": {},
                    },
                },
            ),
            _performance_event(
                "Network.loadingFailed",
                {"requestId": "blocked-1", "blockedReason": "inspector"},
            ),
        ]
        driver = _SeleniumDriver(entries)
        with patch("core.browser_data_saver._cfg.BROWSER_DATA_SAVER_MODE", True), patch(
            "core.browser_data_saver._cfg.BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES", ["image"]
        ):
            saver = BrowserDataSaver(label="test")
            saver.install_selenium(driver)
            tracker = SeleniumTrafficTracker(driver)
            tracker.attach_data_saver(saver)
            result = tracker.stop()

        self.assertEqual(result["http_upload_bytes"], 0)
        self.assertEqual(result["http_download_bytes"], 0)
        self.assertEqual(result["failed_request_count"], 0)
        self.assertEqual(result["data_saver_blocked_count"], 1)

    def test_selenium_logs_status_cache_failure_and_unfinished_details(self):
        entries = [
            _performance_event(
                "Network.requestWillBeSent",
                {
                    "requestId": "ok",
                    "type": "Script",
                    "request": {"method": "GET", "url": "https://example.test/app.js", "headers": {}},
                },
            ),
            _performance_event(
                "Network.responseReceived",
                {
                    "requestId": "ok",
                    "response": {"status": 200, "statusText": "OK", "headers": {"content-type": "text/javascript"}, "mimeType": "text/javascript"},
                },
            ),
            _performance_event("Network.loadingFinished", {"requestId": "ok", "encodedDataLength": 40}),
            _performance_event(
                "Network.requestWillBeSent",
                {
                    "requestId": "cached",
                    "type": "Image",
                    "request": {"method": "GET", "url": "https://cdn.example/cached.png", "headers": {}},
                },
            ),
            _performance_event(
                "Network.responseReceived",
                {
                    "requestId": "cached",
                    "response": {"status": 200, "statusText": "OK", "headers": {}, "fromMemoryCache": True},
                },
            ),
            _performance_event(
                "Network.requestWillBeSent",
                {
                    "requestId": "failed",
                    "type": "Fetch",
                    "request": {"method": "POST", "url": "https://example.test/api?secret=hidden", "headers": {}, "postData": "{}"},
                },
            ),
            _performance_event(
                "Network.responseReceived",
                {
                    "requestId": "failed",
                    "response": {"status": 500, "statusText": "Error", "headers": {}},
                },
            ),
            _performance_event(
                "Network.loadingFailed",
                {"requestId": "failed", "errorText": "net::ERR_FAILED"},
            ),
            _performance_event(
                "Network.requestWillBeSent",
                {
                    "requestId": "unfinished",
                    "type": "XHR",
                    "request": {"method": "GET", "url": "https://example.test/pending", "headers": {}},
                },
            ),
            _performance_event("Network.dataReceived", {"requestId": "unfinished", "encodedDataLength": 7}),
        ]
        driver = _SeleniumDriver(entries)
        with patch("core.browser_traffic._browser_cfg.BROWSER_TRAFFIC_DETAIL_LOG", True):
            tracker = SeleniumTrafficTracker(driver, label="detail")
            with self.assertLogs("core.browser_traffic", level="INFO") as captured:
                result = tracker.stop()

        detail_lines = [line for line in captured.output if "[资源明细]" in line]
        self.assertEqual(len(detail_lines), 4)
        joined = "\n".join(detail_lines)
        self.assertIn("status=200", joined)
        self.assertIn("cache=hit", joined)
        self.assertIn("failed=1", joined)
        self.assertIn("unfinished=1", joined)
        self.assertIn("secret=<redacted>", joined)
        self.assertNotIn("hidden", joined)
        self.assertEqual(result["detail_recorded_count"], 4)

    def test_selenium_records_precise_js_coverage_and_unexecuted_candidates(self):
        driver = _CoverageSeleniumDriver([])
        with patch("core.browser_traffic._browser_cfg.BROWSER_JS_COVERAGE_LOG", True), patch(
            "core.browser_traffic._browser_cfg.BROWSER_JS_COVERAGE_MAX_ENTRIES", 20
        ):
            tracker = SeleniumTrafficTracker(driver, label="coverage")
            with self.assertLogs("core.browser_traffic", level="INFO") as captured:
                result = tracker.stop()

        coverage = result["js_coverage"]
        self.assertTrue(coverage["enabled"])
        self.assertTrue(coverage["supported"])
        self.assertTrue(coverage["collected"])
        self.assertEqual(coverage["script_count"], 3)
        self.assertEqual(coverage["executed_script_count"], 1)
        self.assertEqual(coverage["candidate_script_count"], 1)
        self.assertEqual(coverage["function_count"], 4)
        self.assertEqual(coverage["executed_function_count"], 1)
        self.assertEqual(coverage["candidate_scripts"], ["https://chatgpt.com/cdn/assets/unused.js"])
        self.assertIn("Profiler.startPreciseCoverage", [command for command, _ in driver.cdp_commands])
        self.assertIn("Profiler.takePreciseCoverage", [command for command, _ in driver.cdp_commands])
        joined = "\n".join(captured.output)
        self.assertIn("[JS执行汇总]", joined)
        self.assertIn("[JS执行]", joined)
        self.assertIn("function=<anonymous>", joined)
        self.assertIn("[JS候选]", joined)
        self.assertIn("unused.js", joined)
        self.assertIn("token=<redacted>", joined)
        self.assertNotIn("secret", joined)

if __name__ == "__main__":
    unittest.main()
