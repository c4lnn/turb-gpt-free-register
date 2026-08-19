# -*- coding: utf-8 -*-
import tempfile
import unittest
from base64 import b64encode
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import mailcom_latest_mail_demo as demo


class _Response:
    def __init__(self, status_code, *, text="", url="", headers=None, payload=None):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = headers or {}
        self.history = []
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeSession:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url == demo.LOGIN_PAGE_URL:
            return _Response(
                200,
                text=(
                    '<form action="https://search.mail.com/web"><input name="q"></form>'
                    '<form action="https://login.mail.com/login">'
                    '<input name="service" value="mailint">'
                    '<input name="statistics" value="fresh">'
                    '<input name="username"><input name="password"></form>'
                ),
            )
        if "/halogin" in url:
            return _Response(200, url="https://navigator-lxa.mail.com/?sid=SID-123")
        if "/login?" in url:
            return _Response(200, url=url)
        if "/mailheader/" in url:
            return _Response(
                200,
                payload={
                    "mailHeader": {
                        "from": "Target <sender@example.com>",
                        "subject": "最新主题",
                        "date": "2026-08-19T01:02:03Z",
                    }
                },
            )
        raise AssertionError(url)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url == demo.LOGIN_URL:
            return _Response(
                303,
                headers={"Location": "https://navigator-lxa.mail.com/login?edition=US&ott=OTT"},
            )
        if url == demo.OAUTH_URL:
            return _Response(200, payload={"access_token": "ACCESS", "scope": "mail_mailbox_r"})
        if "/Mailbox/Mail" in url:
            return _Response(
                200,
                payload={
                    "totalCount": 1,
                    "mailListElements": [
                        {"type": "ad", "rawData": {}},
                        {
                            "type": "mail",
                            "rawData": {
                                "attribute": {"mailIdentifier": "123"},
                                "mailHeader": {"from": "Target <sender@example.com>"},
                            },
                        },
                    ],
                },
            )
        if "/Body/html" in url:
            return _Response(200, text="<p>Hello &amp; welcome</p><script>ignored()</script><br>World")
        raise AssertionError(url)

    def close(self):
        pass


class MailComLatestMailDemoTests(unittest.TestCase):
    def test_read_credentials_requires_exactly_three_nonempty_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mail_test.txt"
            path.write_text("account@mail.com\npassword\nsender@example.com\n", encoding="utf-8")
            credentials = demo._read_credentials(path)
            self.assertEqual(credentials.account, "account@mail.com")
            self.assertEqual(credentials.sender, "sender@example.com")

            path.write_text("account@mail.com\npassword\n", encoding="utf-8")
            with self.assertRaisesRegex(demo.MailComDemoError, "三行"):
                demo._read_credentials(path)

    def test_login_and_fetch_uses_mailbox_read_token(self):
        session = _FakeSession()
        client = demo.MailComClient(session=session)

        client.login("account@mail.com", "password")
        result = client.fetch_latest("sender@example.com")

        self.assertEqual(client.sid, "SID-123")
        self.assertEqual(result.mail_id, "123")
        self.assertEqual(result.subject, "最新主题")
        self.assertIn("Hello & welcome", result.body)
        self.assertNotIn("ignored", result.body)

        login_call = next(call for call in session.calls if call[0] == "POST" and call[1] == demo.LOGIN_URL)
        self.assertEqual(login_call[2]["data"]["username"], "account@mail.com")
        self.assertEqual(login_call[2]["data"]["password"], "password")

        halogin_call = next(call for call in session.calls if call[0] == "GET" and "/halogin" in call[1])
        query = parse_qs(urlsplit(halogin_call[1]).query)
        self.assertEqual(query["auth_time"], ["1"])

        token_call = next(call for call in session.calls if call[0] == "POST" and call[1] == demo.OAUTH_URL)
        self.assertEqual(token_call[2]["data"]["scope"], "mail_mailbox_r")
        expected_basic = b64encode(
            f"{demo.OAUTH_CLIENT_ID}:{demo.OAUTH_PUBLIC_SECRET}".encode("utf-8")
        ).decode("ascii")
        self.assertEqual(token_call[2]["headers"]["Authorization"], f"Basic {expected_basic}")

        body_call = next(call for call in session.calls if call[0] == "POST" and "/Body/html" in call[1])
        self.assertEqual(body_call[2]["data"], {"access_token": "ACCESS"})


if __name__ == "__main__":
    unittest.main()
