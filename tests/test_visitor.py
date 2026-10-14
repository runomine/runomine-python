import datetime as dt
import uuid

from conftest import SAFARI, TOKEN

from runomine import daily_salt, uuid7, visitor_hash
from runomine.visitor import normalize_host

SALT = "fd5d804a4a60e713ada2bcef694204ccfece37b96dfff60153236f075014f883"
VISITOR = "5f9e1bebe31e8bf5"


def test_contract_vector_salt():
    assert daily_salt(TOKEN, "2026-10-13") == SALT


def test_contract_vector_visitor():
    assert visitor_hash(TOKEN, "203.0.113.9", SAFARI, "example.com", "2026-10-13") == VISITOR


def test_day_accepts_date_and_aware_datetime():
    assert visitor_hash(TOKEN, "203.0.113.9", SAFARI, "example.com", dt.date(2026, 10, 13)) == VISITOR
    madrid_late = dt.datetime(2026, 10, 14, 1, 30, tzinfo=dt.timezone(dt.timedelta(hours=2)))  # 23:30 UTC on the 13th
    assert visitor_hash(TOKEN, "203.0.113.9", SAFARI, "example.com", madrid_late) == VISITOR


def test_host_is_lowercased_without_port():
    assert visitor_hash(TOKEN, "203.0.113.9", SAFARI, "Example.COM:443", "2026-10-13") == VISITOR
    assert normalize_host("[::1]:8000") == "::1"
    assert normalize_host("::1") == "::1"
    assert normalize_host(None) == ""


def test_changes_with_day_token_and_inputs():
    other_day = visitor_hash(TOKEN, "203.0.113.9", SAFARI, "example.com", "2026-10-14")
    other_token = visitor_hash(TOKEN + "x", "203.0.113.9", SAFARI, "example.com", "2026-10-13")
    other_host = visitor_hash(TOKEN, "203.0.113.9", SAFARI, "example.org", "2026-10-13")
    assert len({VISITOR, other_day, other_token, other_host}) == 4


def test_default_day_is_today_utc():
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    assert visitor_hash(TOKEN, "1.2.3.4", "ua", "h") == visitor_hash(TOKEN, "1.2.3.4", "ua", "h", today)


def test_uuid7_shape_and_order():
    ids = [uuid7() for _ in range(2000)]
    assert len(set(ids)) == 2000
    assert ids == sorted(ids)
    for value in ids[:50]:
        parsed = uuid.UUID(value)
        assert parsed.version == 7
        assert parsed.variant == uuid.RFC_4122
    ms = int(uuid.UUID(ids[0]).hex[:12], 16)
    now = dt.datetime.now(dt.timezone.utc).timestamp() * 1000
    assert abs(now - ms) < 60_000
