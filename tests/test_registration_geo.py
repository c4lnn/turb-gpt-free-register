# -*- coding: utf-8 -*-
"""注册地域画像纯校验层：跨国映射、IANA 别名、DST、未知国家与相容性。"""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch
from core import registration_geo

from core.registration_geo import (
    RegistrationGeoError,
    CATEGORY_GEO_INCOMPLETE,
    CATEGORY_GEO_MISMATCH,
    COUNTRY_DEFAULT_TIMEZONE,
    COUNTRY_TIMEZONES,
    build_registration_locale,
    is_known_country,
    resolve_timezone,
    timezone_display_name,
    timezone_offset_for,
)


class RegistrationGeoCountryMapTests(unittest.TestCase):
    def test_every_known_country_builds_complete_locale_without_observation(self):
        # 全部受支持国家都能在无外部时区观测时，凭国家默认映射生成完整画像。
        for country in COUNTRY_TIMEZONES:
            with self.subTest(country=country):
                locale = build_registration_locale(country)
                # HTTP 首选语言与 SDK 必须一致；日期偏移来自有效 IANA 时区而非宿主。
                accepted = [part.split(";", 1)[0] for part in locale["accept_language"].split(",")]
                self.assertEqual(locale["navigator_languages"][0], accepted[0])
                self.assertEqual(locale["navigator_language"], accepted[0])
                zone = ZoneInfo(locale["timezone_iana"])
                expected = datetime.now(zone).utcoffset().total_seconds() // 60
                self.assertEqual(locale["timezone_offset_minutes"], expected)


class RegistrationGeoResolutionTests(unittest.TestCase):
    def test_unknown_country_is_not_known_and_rejected(self):
        self.assertFalse(is_known_country("AQ"))
        with self.assertRaises(RegistrationGeoError) as ctx:
            build_registration_locale("AQ")
        self.assertEqual(ctx.exception.category, CATEGORY_GEO_INCOMPLETE)

    def test_incomplete_or_cross_country_default_mapping_cannot_pass_country_gate(self):
        for mapping, value in ((registration_geo._COUNTRY_LANGUAGE_TAG, ""),
                               (COUNTRY_DEFAULT_TIMEZONE, ""),
                               (COUNTRY_DEFAULT_TIMEZONE, "Europe/London")):
            with self.subTest(value=value), patch.dict(mapping, {"JP": value}):
                self.assertFalse(is_known_country("JP"))
                with self.assertRaises(RegistrationGeoError) as raised:
                    build_registration_locale("JP")
                self.assertEqual(raised.exception.category, CATEGORY_GEO_INCOMPLETE)

    def test_invalid_explicit_timezone_is_rejected_not_replaced(self):
        with self.assertRaises(RegistrationGeoError) as raised:
            resolve_timezone("SG", "Not/AZone")
        self.assertEqual(raised.exception.category, CATEGORY_GEO_INCOMPLETE)
        with self.assertRaises(RegistrationGeoError) as raised:
            build_registration_locale("SG", "Not/AZone")
        self.assertEqual(raised.exception.category, CATEGORY_GEO_INCOMPLETE)

    def test_incompatible_timezone_for_country_reports_mismatch(self):
        with self.assertRaises(RegistrationGeoError) as ctx:
            resolve_timezone("JP", "Europe/London")
        self.assertEqual(ctx.exception.category, CATEGORY_GEO_MISMATCH)

    def test_iana_alias_is_preserved_and_compatible(self):
        # 合法别名 Asia/Saigon 保留原拼写，但按 VN 判定相容。
        self.assertEqual(resolve_timezone("VN", "Asia/Saigon"), "Asia/Saigon")
        locale = build_registration_locale("VN", "Asia/Saigon")
        self.assertEqual(locale["timezone_iana"], "Asia/Saigon")
        self.assertEqual(locale["timezone_name"], "Indochina Time")
        self.assertEqual(locale["timezone_offset_minutes"], 420)



class RegistrationGeoCrossCountryLocaleTests(unittest.TestCase):
    def test_au_locale_uses_au_language_and_timezone(self):
        locale = build_registration_locale("AU")
        self.assertEqual(locale["navigator_language"], "en-AU")
        self.assertEqual(locale["navigator_languages"][0], "en-AU")
        self.assertTrue(locale["accept_language"].startswith("en-AU"))
        self.assertEqual(locale["timezone_iana"], "Australia/Sydney")
        self.assertNotEqual(locale["timezone_iana"], "Europe/London")

    def test_ca_locale_uses_ca_language_and_timezone(self):
        locale = build_registration_locale("CA")
        self.assertEqual(locale["navigator_language"], "en-CA")
        self.assertEqual(locale["navigator_languages"][0], "en-CA")
        self.assertTrue(locale["accept_language"].startswith("en-CA"))
        self.assertEqual(locale["timezone_iana"], "America/Toronto")
        self.assertNotEqual(locale["timezone_iana"], "America/Los_Angeles")



class RegistrationGeoDstTests(unittest.TestCase):
    def test_us_pacific_offset_and_name_follow_date(self):
        summer = datetime(2026, 7, 1, tzinfo=ZoneInfo("America/Los_Angeles"))
        winter = datetime(2026, 1, 15, tzinfo=ZoneInfo("America/Los_Angeles"))
        self.assertEqual(timezone_offset_for("America/Los_Angeles", summer), -420)
        self.assertEqual(timezone_offset_for("America/Los_Angeles", winter), -480)
        self.assertEqual(timezone_display_name("America/Los_Angeles", summer), "Pacific Daylight Time")
        self.assertEqual(timezone_display_name("America/Los_Angeles", winter), "Pacific Standard Time")

    def test_london_name_switches_between_gmt_and_bst(self):
        summer = datetime(2026, 7, 1, tzinfo=ZoneInfo("Europe/London"))
        winter = datetime(2026, 1, 15, tzinfo=ZoneInfo("Europe/London"))
        self.assertEqual(timezone_offset_for("Europe/London", summer), 60)
        self.assertEqual(timezone_offset_for("Europe/London", winter), 0)
        self.assertEqual(timezone_display_name("Europe/London", summer), "British Summer Time")
        self.assertEqual(timezone_display_name("Europe/London", winter), "Greenwich Mean Time")



if __name__ == "__main__":
    unittest.main()
