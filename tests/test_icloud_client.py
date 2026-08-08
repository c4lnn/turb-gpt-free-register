import unittest
from unittest.mock import patch

from core import icloud_client


class ICloudPollAddressTests(unittest.TestCase):
    def test_normalizes_icloud_split_address(self):
        self.assertEqual(
            icloud_client._normalize_poll_address("xxx+aaa@icloud.com"),
            "xxx@icloud.com",
        )

    def test_keeps_regular_icloud_address(self):
        self.assertEqual(
            icloud_client._normalize_poll_address("xxx@icloud.com"),
            "xxx@icloud.com",
        )

    def test_normalizes_case_insensitive_icloud_domain(self):
        self.assertEqual(
            icloud_client._normalize_poll_address("xxx+tag@iCloud.COM"),
            "xxx@iCloud.COM",
        )

    def test_keeps_non_icloud_split_address(self):
        address = "xxx+aaa@privaterelay.appleid.com"
        self.assertEqual(icloud_client._normalize_poll_address(address), address)

    @patch("core.qqmail_client.fetch_latest_otp", return_value="123456")
    def test_delegates_normalized_address_and_preserves_poll_arguments(self, fetch_otp):
        result = icloud_client.fetch_latest_otp(
            "xxx+aaa@icloud.com",
            after_ts=100.0,
            max_wait=20,
            poll_interval=2,
            settle_seconds=4,
        )

        self.assertEqual(result, "123456")
        fetch_otp.assert_called_once_with(
            "xxx@icloud.com",
            after_ts=100.0,
            max_wait=20,
            poll_interval=2,
            settle_seconds=4,
        )


if __name__ == "__main__":
    unittest.main()
