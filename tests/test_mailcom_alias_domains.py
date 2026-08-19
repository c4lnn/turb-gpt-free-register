# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path

from core.mailcom_alias_domains import (
    EXPECTED_DOMAIN_COUNT,
    MailComAliasDomainError,
    generate_alias_local_part,
    load_alias_domains,
    normalize_alias_local_part,
)


class MailComAliasDomainsTests(unittest.TestCase):
    def test_versioned_directory_contains_expected_unique_domains(self):
        domains = load_alias_domains()
        self.assertEqual(len(domains), EXPECTED_DOMAIN_COUNT)
        self.assertEqual(len(set(domains)), EXPECTED_DOMAIN_COUNT)
        self.assertTrue(all(domain == domain.lower() and "." in domain for domain in domains))

    def test_invalid_or_duplicate_directory_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "domains.json"
            path.write_text(json.dumps(["example.com"] * EXPECTED_DOMAIN_COUNT), encoding="utf-8")
            with self.assertRaisesRegex(MailComAliasDomainError, "重复"):
                load_alias_domains(path)

    def test_local_part_reuses_name_and_is_normalized(self):
        local = generate_alias_local_part(
            name_factory=lambda: " Alice Example ",
            suffix_factory=lambda: "ABC123",
        )
        self.assertEqual(local, "aliceexampleabc123")
        self.assertEqual(normalize_alias_local_part(" A--B "), "a--b")
        with self.assertRaises(MailComAliasDomainError):
            normalize_alias_local_part("**")


if __name__ == "__main__":
    unittest.main()
