# -*- coding: utf-8 -*-
import unittest
from urllib.parse import parse_qs, urlparse

from core.chatgpt_auth import _ensure_authorize_context


class _Session:
    device_id = "did-123"
    auth_session_logging_id = "log-456"


class ChatgptAuthContextTests(unittest.TestCase):
    def test_context_normalization_preserves_authorization_state_and_pkce(self):
        url = (
            "https://auth.openai.com/api/accounts/authorize?"
            "state=opaque%2Bstate%2Fwith%3Dpadding&code_challenge=proof%2Bvalue"
            "&code_challenge_method=S256&redirect_uri=http%3A%2F%2Flocalhost%2Fcallback%3Fx%3D1"
            "&resource=first&resource=second&empty=&device_id=stale#continue"
        )
        before = parse_qs(urlparse(url).query, keep_blank_values=True)
        result = urlparse(_ensure_authorize_context(url, _Session(), "user@example.com"))
        after = parse_qs(result.query, keep_blank_values=True)
        for key in ("state", "code_challenge", "code_challenge_method",
                    "redirect_uri", "resource", "empty"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(result.fragment, "continue")
        self.assertEqual(after["device_id"], [_Session.device_id])

    def test_uninitialized_device_cannot_be_bound_to_authorization(self):
        session = _Session()
        session.device_id = ""
        with self.assertRaises(RuntimeError):
            _ensure_authorize_context(
                "https://auth.openai.com/api/accounts/authorize?state=opaque",
                session, "user@example.com",
            )


if __name__ == "__main__":
    unittest.main()
