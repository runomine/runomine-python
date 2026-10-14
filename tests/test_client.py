import json
import re
import threading
import time
import urllib.error
import uuid
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import SAFARI, TOKEN, FakeTransport

import runomine
from runomine import Client, RequestContext, visitor_hash

TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
REQUEST = RequestContext(
    url="https://example.com/pricing?utm_source=newsletter",
    ip="203.0.113.9",
    user_agent=SAFARI,
    host="example.com",
    referrer="https://www.google.com/",
    country="ES",
)


# ------------------------------------------------------------------ config


def test_env_configuration(monkeypatch):
    monkeypatch.setenv("RUNOMINE_TOKEN", TOKEN)
    monkeypatch.setenv("RUNOMINE_URL", "https://rm.example/")
    c = Client()
    assert c.token == TOKEN and c.enabled
    assert c.endpoint == "https://rm.example/api/v1/events"


def test_default_url_and_disabled_without_token():
    c = Client()
    assert not c.enabled
    assert c.endpoint == "https://runomine.com/api/v1/events"
    assert c.track("signup") is None
    assert c.pageview("https://example.com/") is None
    assert c.flush(0.1) is True
    assert c._thread is None  # a disabled client never starts a thread


@pytest.mark.parametrize("raw,expected", [("false", False), ("0", False), ("no", False), ("true", True), ("1", True), ("", True)])
def test_enabled_env(monkeypatch, raw, expected):
    monkeypatch.setenv("RUNOMINE_TOKEN", TOKEN)
    monkeypatch.setenv("RUNOMINE_ENABLED", raw)
    assert Client().enabled is expected


def test_explicit_arguments_win_over_env(monkeypatch):
    monkeypatch.setenv("RUNOMINE_ENABLED", "false")
    assert Client(token=TOKEN, enabled=True).enabled
    assert not Client(token=TOKEN, enabled=False).enabled


def test_enabled_given_as_string_is_parsed():
    assert not Client(token=TOKEN, enabled="false").enabled
    assert not Client(token=TOKEN, enabled="0").enabled
    assert Client(token=TOKEN, enabled="true").enabled
    assert Client(token=TOKEN, enabled="").enabled


def test_module_level_helpers_use_env_client(monkeypatch):
    monkeypatch.setenv("RUNOMINE_ENABLED", "false")
    monkeypatch.setenv("RUNOMINE_TOKEN", TOKEN)
    assert runomine.track("signup") is None
    assert runomine.flush(0.1) is True


# ----------------------------------------------------------------- payload


def test_pageview_payload(client, transport):
    event_id = client.pageview(request=REQUEST, user=42)
    assert client.flush(2)
    call = transport.calls[0]
    assert call["url"] == "https://runomine.test/api/v1/events"
    assert call["headers"]["Authorization"] == "Bearer " + TOKEN
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["headers"]["User-Agent"].startswith("runomine-python/")
    [event] = call["json"]["events"]
    assert set(event) == {"id", "type", "ts", "url", "referrer", "user_agent", "visitor", "country", "user"}
    assert event["id"] == event_id and uuid.UUID(event_id).version == 7
    assert event["type"] == "pageview"
    assert TS.match(event["ts"])
    assert event["url"] == REQUEST.url
    assert event["referrer"] == "https://www.google.com/"
    assert event["user_agent"] == SAFARI
    assert event["visitor"] == visitor_hash(TOKEN, "203.0.113.9", SAFARI, "example.com")
    assert event["country"] == "ES"
    assert event["user"] == "42"
    assert b"203.0.113.9" not in call["body"]  # the IP never leaves the server


def test_pageview_from_keywords_derives_host_from_url(client, transport):
    client.pageview("https://Example.com:8443/a", ip="1.2.3.4", user_agent="UA")
    client.flush(2)
    [event] = transport.events
    assert event["visitor"] == visitor_hash(TOKEN, "1.2.3.4", "UA", "example.com")
    assert "referrer" not in event and "user" not in event and "country" not in event


def test_pageview_props_follow_the_track_rules(client, transport):
    client.pageview(
        "https://example.com/news",
        ip="1.2.3.4",
        user_agent="UA",
        props={"section": "news", "team": 7, "live": False, "gone": None},
    )
    client.pageview("https://example.com/", ip="1.2.3.4", user_agent="UA", props={})
    client.pageview("https://example.com/", ip="1.2.3.4", user_agent="UA", props="not a mapping")
    client.flush(2)
    first, second, third = transport.events
    assert first["props"] == {"section": "news", "team": 7, "live": "false"}
    assert "props" not in second and "props" not in third


def test_samples_go_at_once_to_their_own_endpoint(client, transport):
    import datetime as dt

    assert client.sample({"queue": {"pending": 3}, "healthy": True}, "2026-10-13T18:00:00.000Z") is True
    assert client.sample({"cpu": 0.5}, dt.datetime(2026, 10, 13, 20, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))) is True
    assert client.sample({}) is True
    assert len(client._queue) == 0  # not queued with the events
    assert [c["url"] for c in transport.calls] == ["https://runomine.test/api/v1/samples"] * 3
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer " + TOKEN
    assert transport.calls[0]["json"] == {"samples": [{"ts": "2026-10-13T18:00:00.000Z", "data": {"queue": {"pending": 3}, "healthy": True}}]}
    assert transport.samples[1] == {"ts": "2026-10-13T18:00:00.000Z", "data": {"cpu": 0.5}}
    assert TS.match(transport.samples[2]["ts"]) and transport.samples[2]["data"] == {}
    assert transport.events == []


def test_many_samples_in_calls_of_500_and_failures_are_false():
    transport = FakeTransport([202, 400])
    client = Client(token=TOKEN, url="https://runomine.test", transport=transport, backoff=0.001)
    rows = [{"ts": 1760000000 + i * 60, "data": {"n": i}} for i in range(501)]
    assert client.samples([*rows, "junk", {"ts": 1}, {"data": [1, 2]}]) is False
    assert [len(c["json"]["samples"]) for c in transport.calls] == [500, 1]

    failing = FakeTransport([503, 503, 503])
    assert Client(token=TOKEN, transport=failing, backoff=0.001).sample({"cpu": 1}) is False
    assert len(failing.calls) == 3
    assert Client(token=TOKEN, transport=FakeTransport([503, 202]), backoff=0.001).sample({"cpu": 1}) is True
    assert Client(token=TOKEN, transport=FakeTransport([ConnectionError("down")] * 3), backoff=0.001).sample({"cpu": 1}) is False
    assert client.samples([]) is True
    disabled = FakeTransport()
    assert Client(token=TOKEN, enabled=False, transport=disabled).sample({"cpu": 1}) is False
    assert disabled.calls == []


def test_module_level_sample(monkeypatch):
    transport = FakeTransport()
    runomine.set_client(Client(token=TOKEN, transport=transport))
    assert runomine.sample({"cpu": 1}) is True
    assert runomine.samples([{"data": {"cpu": 2}}]) is True
    assert [s["data"] for s in transport.samples] == [{"cpu": 1}, {"cpu": 2}]


def test_purchase_payload(client, transport):
    client.track(
        "purchase",
        props={"plan": "rent", "film": "the-third-man", "qty": 2, "gift": True, "none": None},
        revenue={"amount": 4.99, "currency": "eur"},
        user=42,
        request=REQUEST,
    )
    client.flush(2)
    [event] = transport.events
    assert set(event) == {"id", "type", "name", "ts", "url", "visitor", "country", "user", "revenue", "props"}
    assert event["type"] == "event" and event["name"] == "purchase"
    assert event["revenue"] == {"amount": 4.99, "currency": "EUR"}
    assert event["props"] == {"plan": "rent", "film": "the-third-man", "qty": 2, "gift": "true"}
    assert event["visitor"] == visitor_hash(TOKEN, "203.0.113.9", SAFARI, "example.com")
    assert event["user"] == "42"
    assert "user_agent" not in event and "ip" not in event


@pytest.mark.parametrize(
    "revenue,expected",
    [
        ((19, "usd"), {"amount": 19, "currency": "USD"}),
        (Decimal("4.99"), {"amount": 4.99, "currency": "EUR"}),
        (9.5, {"amount": 9.5, "currency": "EUR"}),
        ("12.50", {"amount": 12.5, "currency": "EUR"}),
        ({"amount": "nope", "currency": "EUR"}, None),
        (object(), None),
    ],
)
def test_revenue_forms(client, transport, revenue, expected):
    client.track("purchase", revenue=revenue, visitor="abc")
    client.flush(2)
    [event] = transport.events
    assert event.get("revenue") == expected


def test_track_without_request_uses_explicit_visitor_or_omits_it(client, transport):
    client.track("renewal", user="7", visitor="5f9e1bebe31e8bf5", ts=1_700_000_000)
    client.track("renewal", user="7")
    client.track("renewal", user="7", request=12345)  # not a request: same as none
    client.flush(2)
    first, second, third = transport.events
    assert first["visitor"] == "5f9e1bebe31e8bf5"
    assert first["ts"] == "2023-11-14T22:13:20.000Z"
    assert set(second) == {"id", "type", "name", "ts", "user"}  # no placeholder visitor is ever invented
    assert "visitor" not in third


def test_dict_request_context(client, transport):
    client.track("signup", request={"url": "https://example.com/join", "ip": "1.1.1.1", "user_agent": "UA", "user": 9})
    client.flush(2)
    [event] = transport.events
    assert event["url"] == "https://example.com/join"
    assert event["user"] == "9"
    assert event["visitor"] == visitor_hash(TOKEN, "1.1.1.1", "UA", "example.com")


def test_bad_inputs_never_raise(client, transport):
    assert client.track("Bad Name!") is not None  # sent anyway, runomine reports it as rejected
    assert client.track("x", props={"nan": float("nan")}) is not None
    assert client.pageview() is None  # no url: ignored, not raised
    assert client.track("x", request=12345) is not None
    client.flush(2)
    assert len(transport.events) == 3


# ---------------------------------------------------------------- batching


def test_chunks_of_500_exactly(transport):
    c = Client(token=TOKEN, transport=transport)
    c._ensure_worker = lambda: None  # fill the queue without a running sender
    for i in range(1200):
        c.track("tick", props={"i": i}, visitor="v")
    c._drain()
    assert [len(call["json"]["events"]) for call in transport.calls] == [500, 500, 200]
    assert [e["props"]["i"] for e in transport.events] == list(range(1200))


def test_background_sender_never_exceeds_500(client, transport):
    for i in range(1300):
        client.track("tick", visitor="v")
    assert client.flush(5)
    sizes = [len(call["json"]["events"]) for call in transport.calls]
    assert sum(sizes) == 1300 and max(sizes) <= 500 and len(sizes) >= 3
    assert len({e["id"] for e in transport.events}) == 1300


def test_flushes_on_interval_without_explicit_flush(transport):
    c = Client(token=TOKEN, transport=transport, flush_interval=0.05)
    c.track("signup", visitor="v")
    deadline = time.time() + 3
    while not transport.calls and time.time() < deadline:
        time.sleep(0.01)
    assert len(transport.events) == 1
    c.shutdown(1)


def test_queue_is_bounded(transport):
    c = Client(token=TOKEN, transport=transport, max_queue=10)
    c._ensure_worker = lambda: None
    for i in range(25):
        c.track("tick", props={"i": i}, visitor="v")
    assert len(c._queue) == 10
    c._drain()
    assert [e["props"]["i"] for e in transport.events] == list(range(15, 25))  # oldest dropped


def test_shutdown_flushes_and_stops(client, transport):
    client.track("signup", visitor="v")
    client.shutdown(2)
    assert len(transport.events) == 1
    assert not client._thread.is_alive()
    client.shutdown(1)  # idempotent


# ------------------------------------------------------------------- retry


@pytest.mark.parametrize("first", [500, 502, 503, 429, urllib.error.URLError("down"), TimeoutError()])
def test_retries_then_delivers(first):
    transport = FakeTransport([first, 202])
    c = Client(token=TOKEN, transport=transport, backoff=0.01)
    c.track("signup", visitor="v")
    assert c.flush(3)
    assert len(transport.calls) == 2
    assert transport.calls[0]["body"] == transport.calls[1]["body"]  # same ids: idempotent retry
    c.shutdown(1)


def test_retries_twice_then_drops_quietly():
    transport = FakeTransport([503, 503, 503, 503])
    c = Client(token=TOKEN, transport=transport, backoff=0.01)
    c.track("signup", visitor="v")
    assert c.flush(3)  # drained: the batch was given up on, nothing raised
    assert len(transport.calls) == 3
    c.track("later", visitor="v")  # the client keeps working afterwards
    assert c.flush(3)
    assert transport.calls[-1]["json"]["events"][0]["name"] == "later"
    c.shutdown(1)


@pytest.mark.parametrize("status", [400, 401, 413])
def test_client_errors_are_not_retried(status):
    transport = FakeTransport([status])
    c = Client(token=TOKEN, transport=transport, backoff=0.01)
    c.track("signup", visitor="v")
    assert c.flush(3)
    assert len(transport.calls) == 1
    c.shutdown(1)


def test_retry_after_is_honoured_and_capped():
    transport = FakeTransport([429, 202], retry_after="0.2")
    c = Client(token=TOKEN, transport=transport, backoff=0.01)
    c.track("signup", visitor="v")
    started = time.monotonic()
    assert c.flush(3)
    assert time.monotonic() - started >= 0.18
    c.shutdown(1)


def test_shutdown_is_bounded_while_retrying():
    transport = FakeTransport([503] * 10)
    c = Client(token=TOKEN, transport=transport, backoff=5)
    c.track("signup", visitor="v")
    started = time.monotonic()
    c.shutdown(0.3)
    assert time.monotonic() - started < 2


def test_transport_that_explodes_never_raises():
    def boom(*args):
        raise RuntimeError("boom")

    c = Client(token=TOKEN, transport=boom, backoff=0.01)
    assert c.track("signup", visitor="v")
    assert c.flush(3)
    c.shutdown(1)


# --------------------------------------------------------- real HTTP (urllib)


def test_network_failure_never_raises():
    c = Client(token=TOKEN, url="http://127.0.0.1:9", backoff=0.01, timeout=1)  # nothing listens on port 9
    c.pageview("https://example.com/", ip="1.2.3.4", user_agent="UA")
    c.track("signup", visitor="v")
    assert c.flush(5)
    c.shutdown(1)


class _Handler(BaseHTTPRequestHandler):
    statuses: list = []
    received: list = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).received.append((self.path, dict(self.headers), json.loads(body)))
        status = type(self).statuses.pop(0) if type(self).statuses else 202
        payload = json.dumps({"accepted": 1, "rejected": []}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    _Handler.statuses = []
    _Handler.received = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, _Handler
    httpd.shutdown()
    httpd.server_close()


def test_real_http_post(server):
    httpd, handler = server
    handler.statuses = [503]
    c = Client(token=TOKEN, url=f"http://127.0.0.1:{httpd.server_address[1]}", backoff=0.01)
    c.track("purchase", revenue=(4.99, "EUR"), request=REQUEST)
    assert c.flush(5)
    assert len(handler.received) == 2  # 503, then 202
    path, headers, body = handler.received[-1]
    assert path == "/api/v1/events"
    assert headers["Authorization"] == "Bearer " + TOKEN
    assert headers["Content-Type"] == "application/json"
    assert body["events"][0]["revenue"] == {"amount": 4.99, "currency": "EUR"}
    c.shutdown(1)


# -------------------------------------------------------- checkout metadata

VECTOR_VISITOR = "5f9e1bebe31e8bf5"


def test_checkout_metadata_matches_the_contract_vector():
    c = Client(token=TOKEN, transport=FakeTransport())
    ctx = RequestContext(url="https://Example.com:8443/checkout", ip="203.0.113.9", user_agent=SAFARI)
    assert c.checkout_metadata(ctx, day="2026-10-13") == {"runomine_visitor": VECTOR_VISITOR}
    assert c.checkout_metadata(ctx, 42, day="2026-10-13") == {"runomine_visitor": VECTOR_VISITOR, "runomine_user": "42"}
    as_dict = {"ip": "203.0.113.9", "user_agent": SAFARI, "host": "example.com", "user": "acct_9"}
    assert c.checkout_metadata(as_dict, day="2026-10-13") == {"runomine_visitor": VECTOR_VISITOR, "runomine_user": "acct_9"}
    assert c.checkout_metadata(REQUEST)["runomine_visitor"] == visitor_hash(TOKEN, "203.0.113.9", SAFARI, "example.com")


def test_checkout_metadata_leaves_out_what_is_unknown():
    c = Client(token=TOKEN, transport=FakeTransport())
    # No IP (a job, a webhook): no visitor, never a made-up one.
    assert c.checkout_metadata(RequestContext(url="https://example.com/", user_agent=SAFARI), 42) == {"runomine_user": "42"}
    assert c.checkout_metadata(None, 0) == {"runomine_user": "0"}
    assert c.checkout_metadata() == {}
    assert c.checkout_metadata(None, "") == {}
    assert c.checkout_metadata(object(), "u1") == {"runomine_user": "u1"}
    # Disabled or no token: the user still goes, the visitor doesn't.
    assert Client(token=None).checkout_metadata(REQUEST, "u1") == {"runomine_user": "u1"}
    assert Client(token=TOKEN, enabled=False).checkout_metadata(REQUEST) == {}


def test_module_level_checkout_metadata(monkeypatch):
    assert runomine.checkout_metadata(REQUEST, 7) == {"runomine_user": "7"}  # no token in the environment
    monkeypatch.setenv("RUNOMINE_TOKEN", TOKEN)
    runomine.set_client(None)
    expected = visitor_hash(TOKEN, "203.0.113.9", SAFARI, "example.com")
    assert runomine.checkout_metadata(REQUEST, 7) == {"runomine_visitor": expected, "runomine_user": "7"}
    assert runomine.checkout_metadata(None) == {}
    runomine.shutdown(1)
