# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import browser


class BrowserLocaleFallbackTests(unittest.TestCase):
    def _profile(self, geo):
        return browser.build_browser_environment(geo, base_profile={})

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
        default = browser._LOCALE
        with patch.object(browser, "AUTO_BROWSER_LOCALE_FROM_IP", False):
            profile = self._profile({"country": "US", "timezone": "America/New_York"})

        self.assertEqual(profile["locale_profile"], browser.BROWSER_LOCALE_PROFILE)
        for key in ("navigator_language", "accept_language", "timezone_iana", "timezone_offset_minutes", "timezone_name"):
            self.assertEqual(profile[key], default[key])

    def test_invalid_default_profile_falls_back_to_jp_as_a_unit(self):
        with patch.object(browser, "BROWSER_LOCALE_PROFILE", "unknown-profile"):
            profile = self._profile({"country": "ID", "timezone": "Not/AZone"})

        fallback = browser.BROWSER_LOCALE_PROFILES["jp"]
        # 无效默认画像回退到 jp；未知国家语言按 geo 动态匹配，无效时区保留 jp 的时区字段。
        self.assertEqual(profile["locale_profile"], "geo:id")
        self.assertEqual(profile["navigator_language"], "id-ID")
        self.assertEqual(profile["timezone_iana"], fallback["timezone_iana"])
        self.assertEqual(profile["timezone_offset_minutes"], fallback["timezone_offset_minutes"])
        self.assertEqual(profile["timezone_name"], fallback["timezone_name"])


if __name__ == "__main__":
    unittest.main()
