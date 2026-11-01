"""Direct-network policy for automatic email-provider HTTP requests."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_DIRECT_EMAIL_REQUESTS: ContextVar[bool] = ContextVar("direct_email_requests", default=False)


def direct_email_requests_enabled() -> bool:
    return _DIRECT_EMAIL_REQUESTS.get()


@contextmanager
def direct_email_requests(enabled: bool = True) -> Iterator[None]:
    """Temporarily bypass environment proxies for email-provider transports only."""
    token = _DIRECT_EMAIL_REQUESTS.set(bool(enabled) or _DIRECT_EMAIL_REQUESTS.get())
    try:
        yield
    finally:
        _DIRECT_EMAIL_REQUESTS.reset(token)


def requests_session():
    import requests

    session = requests.Session()
    if direct_email_requests_enabled():
        session.trust_env = False
    return session


def requests_request(method: str, url: str, **kwargs):
    import requests

    if not direct_email_requests_enabled():
        return requests.request(method, url, **kwargs)
    with requests_session() as session:
        return session.request(method, url, **kwargs)


def configure_curl_session(session):
    if direct_email_requests_enabled():
        session.trust_env = False
    return session
