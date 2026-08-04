# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from core import qqmail_client


class QQMailClientTests(unittest.TestCase):
    @patch("core.qqmail_client.imaplib.IMAP4_SSL")
    def test_connect_imap_sets_socket_timeout(self, imap_ssl):
        mail = Mock()
        imap_ssl.return_value = mail

        with (
            patch.object(qqmail_client._email_cfg, "QQ_IMAP_SERVER", "imap.test"),
            patch.object(qqmail_client._email_cfg, "QQ_IMAP_PORT", 993),
            patch.object(qqmail_client._email_cfg, "QQ_IMAP_TIMEOUT", 12),
            patch.object(qqmail_client._email_cfg, "QQ_EMAIL", "owner@example.com"),
            patch.object(qqmail_client._email_cfg, "QQ_IMAP_PASSWORD", "secret"),
        ):
            result = qqmail_client._connect_imap()

        self.assertIs(result, mail)
        imap_ssl.assert_called_once_with("imap.test", 993, timeout=12)
        mail.login.assert_called_once_with("owner@example.com", "secret")
        mail.select.assert_called_once_with("INBOX")


if __name__ == "__main__":
    unittest.main()
