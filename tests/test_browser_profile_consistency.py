# -*- coding: utf-8 -*-
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from config import browser
from core import sentinel_runner
from core.sentinel import generate_fingerprint_data
from core.session import BrowserSession


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = PROJECT_ROOT / "sentinel" / "sentinel-runner.js"
NODE_EXECUTABLE = shutil.which("node") or shutil.which("node.exe")


class BrowserProfileVersionTests(unittest.TestCase):
    def test_openai_browser_version_fields_are_chrome146(self):
        profile = browser.build_browser_environment({}, base_profile={})

        self.assertEqual(browser.CHROME_MAJOR, "146")
        self.assertEqual(browser.CHROME_FULL_VERSION, "146.0.0.0")
        self.assertEqual(browser.IMPERSONATE, "chrome146")
        self.assertEqual(profile["chrome_major"], "146")
        self.assertEqual(profile["chrome_full_version"], "146.0.0.0")
        self.assertIn("Chrome/146.0.0.0", profile["user_agent"])
        self.assertIn('"Google Chrome";v="146"', profile["sec_ch_ua"])
        self.assertIn('"Chromium";v="146"', profile["sec_ch_ua"])
        self.assertIn('"Google Chrome";v="146.0.0.0"', profile["sec_ch_ua_full_version_list"])
        self.assertEqual(browser.validate_browser_profile(profile), [])

class BrowserLocaleFallbackTests(unittest.TestCase):
    def _profile(self, geo):
        return browser.build_browser_environment(geo, base_profile={})

    def test_unknown_country_does_not_partially_apply_geo_timezone(self):
        profile = self._profile({"country": "ID", "timezone": "Asia/Jakarta"})
        default = browser.BROWSER_LOCALE_PROFILES[browser._default_locale_profile_key()]

        self.assertEqual(profile["locale_profile"], browser._default_locale_profile_key())
        for key in ("navigator_language", "navigator_languages", "accept_language", "timezone_iana", "timezone_offset_minutes", "timezone_name"):
            self.assertEqual(profile[key], default[key])
        self.assertNotEqual(profile["timezone_iana"], "Asia/Jakarta")

    def test_missing_country_does_not_partially_apply_geo_timezone(self):
        profile = self._profile({"timezone": "Asia/Jakarta"})
        default = browser.BROWSER_LOCALE_PROFILES[browser._default_locale_profile_key()]

        self.assertEqual(profile["locale_profile"], browser._default_locale_profile_key())
        self.assertEqual(profile["navigator_language"], default["navigator_language"])
        self.assertEqual(profile["timezone_iana"], default["timezone_iana"])
        self.assertEqual(profile["timezone_offset_minutes"], default["timezone_offset_minutes"])
        self.assertEqual(profile["timezone_name"], default["timezone_name"])

    def test_known_country_with_valid_timezone_updates_timezone_as_a_unit(self):
        profile = self._profile({"country": "JP", "timezone": "Asia/Tokyo"})

        self.assertEqual(profile["locale_profile"], "jp")
        self.assertEqual(profile["navigator_language"], "ja-JP")
        self.assertEqual(profile["timezone_iana"], "Asia/Tokyo")
        self.assertEqual(profile["timezone_offset_minutes"], 540)
        self.assertEqual(profile["timezone_name"], "Japan Standard Time")

    def test_known_country_with_invalid_timezone_keeps_profile_timezone(self):
        profile = self._profile({"country": "CN", "timezone": "Not/AZone"})

        self.assertEqual(profile["locale_profile"], "cn")
        self.assertEqual(profile["navigator_language"], "zh-CN")
        self.assertEqual(profile["timezone_iana"], "Asia/Shanghai")
        self.assertEqual(profile["timezone_offset_minutes"], 480)
        self.assertEqual(profile["timezone_name"], "China Standard Time")

    def test_geoip_disabled_uses_default_profile_as_a_unit(self):
        default = browser.BROWSER_LOCALE_PROFILES[browser._default_locale_profile_key()]
        with patch.object(browser, "AUTO_BROWSER_LOCALE_FROM_IP", False):
            profile = self._profile({"country": "US", "timezone": "America/New_York"})

        self.assertEqual(profile["locale_profile"], browser._default_locale_profile_key())
        for key in ("navigator_language", "accept_language", "timezone_iana", "timezone_offset_minutes", "timezone_name"):
            self.assertEqual(profile[key], default[key])

    def test_invalid_default_profile_falls_back_to_jp_as_a_unit(self):
        with patch.object(browser, "BROWSER_LOCALE_PROFILE", "unknown-profile"):
            profile = self._profile({"country": "ID", "timezone": "Asia/Jakarta"})

        fallback = browser.BROWSER_LOCALE_PROFILES["jp"]
        self.assertEqual(profile["locale_profile"], "jp")
        self.assertEqual(profile["navigator_language"], fallback["navigator_language"])
        self.assertEqual(profile["timezone_iana"], fallback["timezone_iana"])
        self.assertEqual(profile["timezone_offset_minutes"], fallback["timezone_offset_minutes"])
        self.assertEqual(profile["timezone_name"], fallback["timezone_name"])


class BrowserProfileCrossLayerTests(unittest.TestCase):
    def test_python_sentinel_and_http_headers_use_same_profile(self):
        profile = browser.build_browser_environment({"country": "CN", "timezone": "Asia/Shanghai"}, base_profile={})
        fingerprint = generate_fingerprint_data("test-device-id", profile=profile)

        session = BrowserSession.__new__(BrowserSession)
        session.browser_profile = profile
        headers = session._get_common_headers()

        self.assertEqual(fingerprint[4], profile["user_agent"])
        self.assertEqual(fingerprint[7], profile["navigator_language"])
        self.assertEqual(fingerprint[8], ",".join(profile["navigator_languages"]))
        self.assertIn("GMT+0800", fingerprint[1])
        self.assertIn(profile["timezone_name"], fingerprint[1])
        self.assertEqual(headers["User-Agent"], profile["user_agent"])
        self.assertEqual(headers["accept-language"], profile["accept_language"])
        self.assertEqual(headers["sec-ch-ua"], profile["sec_ch_ua"])
        self.assertEqual(headers["sec-ch-ua-platform"], profile["sec_ch_ua_platform"])

    def test_runner_receives_profile_version_and_locale_parameters(self):
        profile = browser.build_browser_environment({"country": "CN", "timezone": "Asia/Shanghai"}, base_profile={})
        fake_process = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"p": "p", "c": "c", "id": "device", "flow": "authorize_continue"}),
            stderr="",
        )

        with patch.object(sentinel_runner, "_ensure_runner_environment"), patch.object(
            sentinel_runner.subprocess, "run", return_value=fake_process
        ) as run:
            result = sentinel_runner.generate_sentinel_token(
                {},
                "authorize_continue",
                "device",
                browser_profile=profile,
                cookie="oai-did=device",
            )

        self.assertEqual(json.loads(result)["flow"], "authorize_continue")
        command = run.call_args.args[0]

        def command_value(name):
            return command[command.index(name) + 1]

        self.assertEqual(command_value("--chrome-major"), profile["chrome_major"])
        self.assertEqual(command_value("--chrome-full-version"), profile["chrome_full_version"])
        self.assertEqual(command_value("--user-agent"), profile["user_agent"])
        self.assertEqual(command_value("--language"), profile["navigator_language"])
        self.assertEqual(command_value("--time-zone"), profile["timezone_iana"])
        self.assertEqual(command_value("--timezone-name"), profile["timezone_name"])
        self.assertEqual(command_value("--timezone-offset-minutes"), str(profile["timezone_offset_minutes"]))
        self.assertIn('"Google Chrome";v="146"', command_value("--sec-ch-ua"))
        self.assertEqual(run.call_args.kwargs["env"]["TZ"], profile["timezone_iana"])

    @unittest.skipUnless(NODE_EXECUTABLE, "未找到 Node.js，跳过 Sentinel Runner 默认值测试")
    def test_node_runner_defaults_are_chrome146(self):
        script = (
            "const r=require(process.argv[1]);"
            "console.log(JSON.stringify({major:r.DEFAULT_CHROME_MAJOR,full:r.DEFAULT_CHROME_FULL_VERSION,"
            "ua:r.DEFAULT_USER_AGENT,sec:r.DEFAULT_SEC_CH_UA,fullList:r.DEFAULT_SEC_CH_UA_FULL_VERSION_LIST}));"
        )
        completed = subprocess.run(
            [NODE_EXECUTABLE, "-e", script, str(RUNNER_PATH)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        defaults = json.loads(completed.stdout)

        self.assertEqual(defaults["major"], "146")
        self.assertEqual(defaults["full"], "146.0.0.0")
        self.assertIn("Chrome/146.0.0.0", defaults["ua"])
        self.assertIn('"Google Chrome";v="146"', defaults["sec"])
        self.assertIn('"Chromium";v="146.0.0.0"', defaults["fullList"])


if __name__ == "__main__":
    unittest.main()
