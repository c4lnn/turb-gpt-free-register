# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from core import db


class MailComRegistrationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patches = [
            patch.dict(os.environ, {"RUNTIME_STORAGE_BACKEND": "json"}),
            patch.object(db, "_MAILCOM_ALIAS_JSON", root / "aliases.json"),
            patch.object(db, "_MAILCOM_EMAIL_JSON", root / "parents.json"),
        ]
        for item in self.patches:
            item.start()
        db.create_mailcom_alias(
            alias_email="alias@example.com",
            parent_email="mother@mail.com",
            local_part="alias",
            domain="example.com",
        )

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_direct_registration_records_alias_start_before_driver_selection(self):
        with patch.object(main._roxy_cfg, "REGISTRATION_DRIVER", "unsupported-driver"):
            with self.assertRaisesRegex(RuntimeError, "不支持的 REGISTRATION_DRIVER"):
                main.run_registration("alias@example.com", "Alice Example")
        alias = db.get_mailcom_alias_internal("alias@example.com")
        self.assertIsNotNone(alias["registration_started_at"])


if __name__ == "__main__":
    unittest.main()
