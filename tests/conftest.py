import json
import threading

import pytest

import runomine
from runomine import Client

TOKEN = "rm_live_test_0123456789abcdef"
SAFARI = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15"


class FakeTransport:
    """Records every POST. ``responses`` is a list of status codes or exceptions, consumed in order (then 202)."""

    def __init__(self, responses=None, retry_after=None):
        self.responses = list(responses or [])
        self.retry_after = retry_after
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, url, body, headers, timeout):
        with self.lock:
            self.calls.append({"url": url, "body": body, "json": json.loads(body), "headers": dict(headers)})
            outcome = self.responses.pop(0) if self.responses else 202
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome, self.retry_after

    @property
    def events(self):
        return [event for call in self.calls for event in call["json"]["events"]]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("RUNOMINE_TOKEN", "RUNOMINE_URL", "RUNOMINE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    runomine.set_client(None)
    yield
    runomine.set_client(None)


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture
def client(transport):
    c = Client(token=TOKEN, url="https://runomine.test", transport=transport, flush_interval=60, backoff=0.01)
    yield c
    c.shutdown(1)
