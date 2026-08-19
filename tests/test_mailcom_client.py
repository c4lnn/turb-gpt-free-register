# -*- coding: utf-8 -*-
import threading
import unittest

from core.mailcom_client import MailComClient, MailComError, MailComInvalidTokenError, MailComProtocolError


class _Response:
    def __init__(self, status_code=200, *, payload=None, text="", headers=None, url=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}
        self.url = url
        self.history = []

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _AtOnlySession:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if "/Mailbox/Mail" in url:
            return _Response(payload={
                "totalCount": 1,
                "mailListElements": [{
                    "type": "mail",
                    "rawData": {
                        "attribute": {"mailIdentifier": "message-1", "internalDate": 2_000_000_000},
                        "mailHeader": {"from": "OpenAI <noreply@openai.com>", "subject": "Your code"},
                    },
                }],
            })
        if "/Body/html" in url:
            return _Response(text="<style>ignored</style><p>Your verification code is 123456</p>")
        raise AssertionError(url)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if "/mailheader/" in url:
            return _Response(payload={"mailHeader": {"from": "OpenAI <noreply@openai.com>", "subject": "Your code"}})
        raise AssertionError(url)


class _LoginSession(_AtOnlySession):
    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if "premiumlogin" in url:
            return _Response(text=(
                '<form action="https://login.mail.com/login">'
                '<input name="service" value="mailint"><input name="username"><input name="password">'
                '</form>'
            ))
        if "/login?" in url:
            return _Response(url=url)
        if "/halogin" in url:
            return _Response(url="https://navigator-lxa.mail.com/mail?sid=SID-1")
        if "/mail_settings" in url:
            return _Response(text=(
                '<script>const root="https://mailset-root.mail.com?navsid=SID-1&iac_appname=mailset-root&iac_token=one";</script>'
            ))
        if url.startswith("https://mailset-root.mail.com"):
            return _Response(text="<html></html>")
        return super().get(url, **kwargs)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url == "https://login.mail.com/login":
            return _Response(303, headers={"Location": "https://navigator-lxa.mail.com/login?ott=one"})
        if "oauth2/token" in url:
            scope = str(kwargs.get("data", {}).get("scope") or "")
            return _Response(payload={
                "access_token": "settings-at" if "webmailer_setting_r" in scope else "fresh-at",
                "expires_in": 3600,
                "scope": scope,
            })
        return super().post(url, **kwargs)


class MailComClientTests(unittest.TestCase):
    def test_cold_start_uses_only_bearer_at_without_login_or_sid(self):
        session = _AtOnlySession()
        client = MailComClient(session=session, access_token="persisted-at")

        result = client.fetch_latest_otp(after_ts=0, max_wait=0, settle_seconds=0)

        self.assertEqual(result, "123456")
        self.assertEqual(client.sid, "")
        self.assertFalse(any("premiumlogin" in url or "oauth2/token" in url for _, url, _ in session.calls))
        list_call = next(call for call in session.calls if "/Mailbox/Mail" in call[1])
        self.assertEqual(list_call[2]["headers"]["Authorization"], "Bearer persisted-at")
        self.assertNotIn("Cookie", list_call[2]["headers"])

        body_call = next(call for call in session.calls if "/Body/html" in call[1])
        body_headers = body_call[2]["headers"]
        self.assertEqual(body_call[2]["data"], {"access_token": "persisted-at"})
        self.assertEqual(body_headers["Accept"], "text/html,application/xhtml+xml")
        self.assertEqual(body_headers["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(body_headers["Origin"], "https://webmailer.mail.com")
        self.assertEqual(body_headers["Referer"], "https://webmailer.mail.com/")
        self.assertNotIn("Authorization", body_headers)
        self.assertNotIn("X-Request-ID", body_headers)
        self.assertNotIn("X-UI-App", body_headers)

    def test_confirmed_invalid_token_becomes_typed_error(self):
        class InvalidSession(_AtOnlySession):
            def post(self, url, **kwargs):
                if "/Mailbox/Mail" in url:
                    return _Response(401, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
                return super().post(url, **kwargs)

        with self.assertRaises(MailComInvalidTokenError):
            MailComClient(session=InvalidSession(), access_token="bad").list_page()

    def test_login_dynamic_form_and_token_exchange_validate_expiry(self):
        session = _LoginSession()
        client = MailComClient(session=session, now=lambda: 100.0)

        token = client.authenticate("account@mail.com", "password")

        self.assertEqual(client.sid, "SID-1")
        self.assertEqual(token.access_token, "fresh-at")
        self.assertEqual(token.expires_at, 3700.0)
        login_call = next(call for call in session.calls if call[0] == "POST" and call[1] == "https://login.mail.com/login")
        self.assertEqual(login_call[2]["data"]["username"], "account@mail.com")
        self.assertEqual(login_call[2]["data"]["password"], "password")
        token_call = next(call for call in session.calls if "oauth2/token" in call[1])
        self.assertEqual(token_call[2]["data"]["scope"], "mail_mailbox_r")

    def test_token_without_expiry_fails_fast_without_response_leak(self):
        class MissingExpiry(_LoginSession):
            def post(self, url, **kwargs):
                if "oauth2/token" in url:
                    return _Response(payload={"access_token": "fresh-at"})
                return super().post(url, **kwargs)

        with self.assertRaisesRegex(MailComProtocolError, "expires_in"):
            MailComClient(session=MissingExpiry()).authenticate("account@mail.com", "password")

    def test_settings_bootstrap_uses_settings_root_and_scoped_bearer_token(self):
        session = _LoginSession()
        client = MailComClient(session=session)
        client.login("account@mail.com", "password")

        token = client.bootstrap_settings_session()

        self.assertEqual(token.access_token, "settings-at")
        navigation_call = next(call for call in session.calls if "/mail_settings" in call[1])
        self.assertEqual(navigation_call[2]["params"], {"sid": "SID-1"})
        root_call = next(call for call in session.calls if call[1].startswith("https://mailset-root.mail.com"))
        self.assertIn("navsid=SID-1", root_call[1])
        token_call = [call for call in session.calls if "oauth2/token" in call[1]][-1]
        self.assertEqual(token_call[2]["data"]["scope"], "mail_mailbox_w webmailer_setting_r webmailer_setting_w mail_confix_w")
        self.assertEqual(token_call[2]["headers"]["Origin"], "https://mailset-root.mail.com")
        self.assertTrue(token_call[2]["headers"]["Authorization"].startswith("Basic "))

    def test_settings_bootstrap_logs_stages_without_sensitive_values(self):
        session = _LoginSession()
        client = MailComClient(session=session)
        client.login("account@mail.com", "test-password")

        with self.assertLogs("core.mailcom_client", level="INFO") as logs:
            client.bootstrap_settings_session()

        output = "\n".join(logs.output)
        self.assertIn("stage=settings_navigation", output)
        self.assertIn("stage=settings_root", output)
        self.assertIn("stage=settings_oauth_token", output)
        self.assertNotIn("SID-1", output)
        self.assertNotIn("settings-at", output)
        self.assertNotIn("test-password", output)

    def test_settings_bootstrap_rejects_untrusted_root_url(self):
        class InvalidRootSession(_LoginSession):
            def get(self, url, **kwargs):
                if "/mail_settings" in url:
                    self.calls.append(("GET", url, kwargs))
                    return _Response(text="https://example.invalid/?navsid=SID-1&iac_appname=x&iac_token=y")
                return super().get(url, **kwargs)

        client = MailComClient(session=InvalidRootSession())
        client.login("account@mail.com", "password")
        with self.assertRaisesRegex(MailComProtocolError, "mailset-root"):
            client.bootstrap_settings_session()

    def test_sender_matching_scans_pages_and_selects_latest_internal_date(self):
        class Paged(_AtOnlySession):
            def post(self, url, **kwargs):
                if "/Mailbox/Mail" not in url:
                    return super().post(url, **kwargs)
                offset = int(kwargs.get("params", {}).get("offset", 0))
                stamp = 10 if offset == 0 else 20
                mail_id = "older" if offset == 0 else "newer"
                return _Response(payload={
                    "totalCount": 2,
                    "mailListElements": [{
                        "type": "mail",
                        "rawData": {
                            "attribute": {"mailIdentifier": mail_id, "internalDate": stamp},
                            "mailHeader": {"from": "Target <sender@example.com>", "subject": "code"},
                        },
                    }],
                })

        item = MailComClient(session=Paged(), access_token="at").find_latest(
            "sender@example.com", page_size=1, max_pages=3
        )
        self.assertEqual(item["rawData"]["attribute"]["mailIdentifier"], "newer")

    def test_alias_recipient_matching_reads_full_header_before_body_and_fails_closed(self):
        class SharedInbox(_AtOnlySession):
            def post(self, url, **kwargs):
                if "/Mailbox/Mail" in url:
                    return _Response(payload={
                        "totalCount": 2,
                        "mailListElements": [
                            {
                                "type": "mail",
                                "rawData": {
                                    "attribute": {"mailIdentifier": "other", "internalDate": 200},
                                    "mailHeader": {"from": "OpenAI <noreply@openai.com>", "subject": "Your code"},
                                },
                            },
                            {
                                "type": "mail",
                                "rawData": {
                                    "attribute": {"mailIdentifier": "target", "internalDate": 100},
                                    "mailHeader": {"from": "OpenAI <noreply@openai.com>", "subject": "Your code"},
                                },
                            },
                        ],
                    })
                if "/Body/html" in url:
                    self.calls.append(("POST", url, kwargs))
                    return _Response(text="<p>code 123456</p>")
                return super().post(url, **kwargs)

            def get(self, url, **kwargs):
                self.calls.append(("GET", url, kwargs))
                if "/mailheader/other" in url:
                    return _Response(payload={"mailHeader": {
                        "from": "OpenAI <noreply@openai.com>", "subject": "Your code",
                        "to": "Other <other@example.com>",
                    }})
                if "/mailheader/target" in url:
                    return _Response(payload={"mailHeader": {
                        "from": "OpenAI <noreply@openai.com>", "subject": "Your code",
                        "to": [{"address": "TARGET@example.com"}],
                    }})
                raise AssertionError(url)

        session = SharedInbox()
        otp = MailComClient(session=session, access_token="at").fetch_latest_otp(
            after_ts=10, recipient="target@example.com", max_wait=0, settle_seconds=0
        )
        self.assertEqual(otp, "123456")
        body_calls = [call for call in session.calls if "/Body/html" in call[1]]
        self.assertEqual(len(body_calls), 1)
        self.assertIn("/target/Body/html", body_calls[0][1])

    def test_alias_recipient_missing_or_other_alias_never_reads_body(self):
        class NoRecipient(_AtOnlySession):
            def get(self, url, **kwargs):
                self.calls.append(("GET", url, kwargs))
                return _Response(payload={"mailHeader": {
                    "from": "OpenAI <noreply@openai.com>", "subject": "Your code",
                }})

        session = NoRecipient()
        with self.assertRaisesRegex(Exception, "超时"):
            MailComClient(session=session, access_token="at").fetch_latest_otp(
                after_ts=0, recipient="target@example.com", max_wait=0, settle_seconds=0
            )
        self.assertFalse(any("/Body/html" in call[1] for call in session.calls))

    def test_alias_tasks_sharing_one_inbox_keep_candidates_and_otp_isolated(self):
        class SharedInbox(_AtOnlySession):
            def post(self, url, **kwargs):
                self.calls.append(("POST", url, kwargs))
                if "/Mailbox/Mail" in url:
                    return _Response(payload={
                        "totalCount": 2,
                        "mailListElements": [
                            {
                                "type": "mail",
                                "rawData": {
                                    "attribute": {"mailIdentifier": "alpha", "internalDate": 100},
                                    "mailHeader": {"from": "OpenAI <noreply@openai.com>", "subject": "Your code"},
                                },
                            },
                            {
                                "type": "mail",
                                "rawData": {
                                    "attribute": {"mailIdentifier": "beta", "internalDate": 101},
                                    "mailHeader": {"from": "OpenAI <noreply@openai.com>", "subject": "Your code"},
                                },
                            },
                        ],
                    })
                if "/Body/html" in url:
                    code = "222222" if "/beta/" in url else "111111"
                    return _Response(text=f"<p>Your verification code is {code}</p>")
                raise AssertionError(url)

            def get(self, url, **kwargs):
                self.calls.append(("GET", url, kwargs))
                if "/mailheader/alpha" in url:
                    target = "alpha@example.com"
                elif "/mailheader/beta" in url:
                    target = "beta@example.com"
                else:
                    raise AssertionError(url)
                return _Response(payload={"mailHeader": {
                    "from": "OpenAI <noreply@openai.com>",
                    "subject": "Your code",
                    "to": f"Alias <{target}>",
                }})

        session = SharedInbox()
        client = MailComClient(session=session, access_token="at")
        results = {}
        errors = []

        def read(alias):
            try:
                results[alias] = client.fetch_latest_otp(
                    after_ts=0,
                    recipient=alias,
                    max_wait=0,
                    settle_seconds=0,
                )
            except Exception as exc:  # pragma: no cover - assertion below
                errors.append(exc)

        threads = [threading.Thread(target=read, args=("alpha@example.com",)), threading.Thread(target=read, args=("beta@example.com",))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        self.assertEqual(errors, [])
        self.assertEqual(results, {"alpha@example.com": "111111", "beta@example.com": "222222"})
        body_calls = [call for call in session.calls if "/Body/html" in call[1]]
        self.assertEqual(sorted(call[1].split("/Mail/", 1)[-1].split("/Body", 1)[0] for call in body_calls), ["alpha", "beta"])

    def test_alias_time_window_excludes_old_message_and_invalid_otp_is_not_returned(self):
        class WindowSession(_AtOnlySession):
            def post(self, url, **kwargs):
                self.calls.append(("POST", url, kwargs))
                if "/Mailbox/Mail" in url:
                    return _Response(payload={
                        "totalCount": 2,
                        "mailListElements": [
                            {"type": "mail", "rawData": {"attribute": {"mailIdentifier": "new", "internalDate": 110}, "mailHeader": {"from": "OpenAI <noreply@openai.com>", "subject": "Your code"}}},
                            {"type": "mail", "rawData": {"attribute": {"mailIdentifier": "old", "internalDate": 90}, "mailHeader": {"from": "OpenAI <noreply@openai.com>", "subject": "Your code"}}},
                        ],
                    })
                if "/Body/html" in url:
                    return _Response(text="<p>Your verification code is 1234567</p>")
                raise AssertionError(url)

            def get(self, url, **kwargs):
                self.calls.append(("GET", url, kwargs))
                if "/mailheader/new" in url:
                    return _Response(payload={"mailHeader": {"from": "OpenAI <noreply@openai.com>", "subject": "Your code", "to": "target@example.com"}})
                if "/mailheader/old" in url:
                    raise AssertionError("旧邮件不应读取完整邮件头")
                raise AssertionError(url)

        session = WindowSession()
        with self.assertRaisesRegex(MailComError, "超时") as ctx:
            MailComClient(session=session, access_token="at").fetch_latest_otp(
                after_ts=100,
                recipient="target@example.com",
                max_wait=0,
                settle_seconds=0,
            )
        self.assertEqual(ctx.exception.error_type, "timeout")
        self.assertTrue(any("/new/Body/html" in call[1] for call in session.calls))


if __name__ == "__main__":
    unittest.main()
