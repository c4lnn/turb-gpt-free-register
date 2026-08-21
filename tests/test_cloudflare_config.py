# -*- coding: utf-8 -*-
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from config import email, env_loader
from webui.config_editor import EDITABLE_FIELDS


class CloudflareConfigTests(unittest.TestCase):
    def test_email_config_declares_random_subdomain_defaults(self):
        source = Path(email.__file__).read_text(encoding="utf-8")
        self.assertIn("CLOUDFLARE_RANDOM_SUBDOMAIN_ENABLED = False", source)
        self.assertIn("CLOUDFLARE_RANDOM_SUBDOMAIN_LENGTH = 6", source)
        self.assertIn('CLOUDFLARE_RANDOM_SUBDOMAIN_SUFFIX = ""', source)
        self.assertIn("'CLOUDFLARE_RANDOM_SUBDOMAIN_ENABLED': 'bool'", source)
        self.assertIn("'CLOUDFLARE_RANDOM_SUBDOMAIN_LENGTH': 'int'", source)
        self.assertIn("'CLOUDFLARE_RANDOM_SUBDOMAIN_SUFFIX': 'str'", source)

    def test_environment_overrides_random_subdomain_values(self):
        namespace = {
            "CLOUDFLARE_RANDOM_SUBDOMAIN_ENABLED": False,
            "CLOUDFLARE_RANDOM_SUBDOMAIN_LENGTH": 6,
            "CLOUDFLARE_RANDOM_SUBDOMAIN_SUFFIX": "",
        }
        schema = {
            "CLOUDFLARE_RANDOM_SUBDOMAIN_ENABLED": "bool",
            "CLOUDFLARE_RANDOM_SUBDOMAIN_LENGTH": "int",
            "CLOUDFLARE_RANDOM_SUBDOMAIN_SUFFIX": "str",
        }
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        try:
            with patch.dict(os.environ, {
                "CLOUDFLARE_RANDOM_SUBDOMAIN_ENABLED": "true",
                "CLOUDFLARE_RANDOM_SUBDOMAIN_LENGTH": "12",
                "CLOUDFLARE_RANDOM_SUBDOMAIN_SUFFIX": "mail",
            }, clear=True):
                env_loader.apply_env_overrides(namespace, schema)
        finally:
            env_loader._LOADED = old_loaded

        self.assertTrue(namespace["CLOUDFLARE_RANDOM_SUBDOMAIN_ENABLED"])
        self.assertEqual(namespace["CLOUDFLARE_RANDOM_SUBDOMAIN_LENGTH"], 12)
        self.assertEqual(namespace["CLOUDFLARE_RANDOM_SUBDOMAIN_SUFFIX"], "mail")

    def test_webui_exposes_random_subdomain_fields(self):
        fields = {item["key"]: item for item in EDITABLE_FIELDS}
        self.assertEqual(fields["CLOUDFLARE_RANDOM_SUBDOMAIN_ENABLED"]["type"], "bool")
        self.assertEqual(fields["CLOUDFLARE_RANDOM_SUBDOMAIN_LENGTH"]["type"], "int")
        self.assertEqual(fields["CLOUDFLARE_RANDOM_SUBDOMAIN_SUFFIX"]["type"], "str")


if __name__ == "__main__":
    unittest.main()
