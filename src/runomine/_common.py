"""Small request helpers shared by the framework integrations. No classification lives here."""

from __future__ import annotations

import fnmatch
from typing import Callable, Iterable

DEFAULT_SKIP_PATHS: tuple[str, ...] = ("/admin", "/static", "/media")
COUNTRY_HEADERS = ("cf-ipcountry", "cloudfront-viewer-country")


def is_skipped(path: str, patterns: Iterable[str]) -> bool:
    """``/admin`` skips ``/admin`` and ``/admin/...`` (not ``/administration``); ``*`` patterns use fnmatch."""
    for pattern in patterns or ():
        if not pattern:
            continue
        if any(c in pattern for c in "*?["):
            if fnmatch.fnmatchcase(path, pattern):
                return True
            continue
        base = pattern.rstrip("/") or "/"
        if base == "/":
            if path == "/":
                return True
            continue
        if path == base or path.startswith(base + "/"):
            return True
    return False


def is_prefetch(get: Callable[[str], "str | None"]) -> bool:
    """Speculative loads (``Purpose: prefetch``, ``Sec-Purpose: prefetch``, ``X-Moz: prefetch``) aren't visits."""
    for name in ("sec-purpose", "purpose", "x-moz", "x-purpose"):
        value = get(name)
        if value and "prefetch" in value.lower():
            return True
    return False


def first_forwarded(value: str | None) -> str | None:
    """First entry of an ``X-Forwarded-For``-style list, which is the original client."""
    if not value:
        return None
    first = value.split(",")[0].strip()
    return first or None


def country(get: Callable[[str], "str | None"]) -> str | None:
    """ISO 3166-1 alpha-2 from CDN headers; Cloudflare's ``XX`` (unknown) and ``T1`` (Tor) are not countries."""
    for name in COUNTRY_HEADERS:
        value = (get(name) or "").strip().upper()
        if len(value) == 2 and value.isalpha() and value != "XX":
            return value
    return None
