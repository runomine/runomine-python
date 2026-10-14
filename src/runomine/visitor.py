"""The visitor hash, identical in every runomine library and in runomine itself.

    day     = the current UTC date, "YYYY-MM-DD"
    salt    = hex(HMAC-SHA256(key = token, message = "runomine:" + day))
    visitor = first 16 hex chars of hex(SHA256(salt + "|" + ip + "|" + user_agent + "|" + host))
    host    = the request's hostname, lowercase, without port
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac

__all__ = ["visitor_hash", "daily_salt", "normalize_host", "utc_day"]


def utc_day(day: str | _dt.date | _dt.datetime | None = None) -> str:
    """Return ``day`` as ``YYYY-MM-DD``; ``None`` means today in UTC."""
    if day is None:
        return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    if isinstance(day, _dt.datetime):
        if day.tzinfo is not None:
            day = day.astimezone(_dt.timezone.utc)
        return day.strftime("%Y-%m-%d")
    if isinstance(day, _dt.date):
        return day.strftime("%Y-%m-%d")
    return str(day)


def normalize_host(host: str | None) -> str:
    """Lowercase hostname without port. Accepts ``Example.com:8000`` and ``[::1]:8000``."""
    if not host:
        return ""
    host = str(host).strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end != -1 else host[1:]
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host


def daily_salt(token: str, day: str | _dt.date | _dt.datetime | None = None) -> str:
    """hex(HMAC-SHA256(key=token, message="runomine:" + day))."""
    message = ("runomine:" + utc_day(day)).encode("utf-8")
    return hmac.new(str(token).encode("utf-8"), message, hashlib.sha256).hexdigest()


def visitor_hash(
    token: str,
    ip: str | None,
    user_agent: str | None,
    host: str | None,
    day: str | _dt.date | _dt.datetime | None = None,
) -> str:
    """The 16-hex-char visitor id for this person, on this site, on this UTC day.

    The IP is only an input to the hash; it is never sent anywhere.
    """
    salt = daily_salt(token, day)
    material = "|".join([salt, ip or "", user_agent or "", normalize_host(host)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
