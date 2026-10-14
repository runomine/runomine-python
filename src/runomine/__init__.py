"""runomine: cookieless analytics that counts money, not just visits.

    import runomine

    runomine.track("signup", user=user.id, visitor=visitor)          # process-wide client from the environment
    client = runomine.Client(token="rm_live_...")                      # or your own client

Framework integrations live in ``runomine.django`` and ``runomine.asgi`` and are only imported on demand.
"""

from __future__ import annotations

from typing import Any

from ._uuid7 import uuid7
from ._version import __version__
from .client import DEFAULT_URL, MAX_BATCH, Client, RequestContext, get_client, set_client
from .visitor import daily_salt, visitor_hash

__all__ = [
    "__version__",
    "Client",
    "RequestContext",
    "visitor_hash",
    "daily_salt",
    "uuid7",
    "get_client",
    "set_client",
    "track",
    "pageview",
    "sample",
    "samples",
    "checkout_metadata",
    "flush",
    "shutdown",
    "DEFAULT_URL",
    "MAX_BATCH",
]


def track(name: str, props: Any = None, revenue: Any = None, user: Any = None, request: Any = None, **kwargs: Any) -> str | None:
    """``get_client().track(...)``: a business event from anywhere in the app. Never raises."""
    try:
        return get_client().track(name, props, revenue, user, request, **kwargs)
    except Exception:
        return None


def checkout_metadata(request: Any = None, user: Any = None) -> dict[str, str]:
    """``{"runomine_visitor": ..., "runomine_user": ...}`` to pass to your payment provider when creating a checkout,
    only the keys with a value. ``request`` is a Django ``HttpRequest``, a Starlette/FastAPI ``Request`` (or an ASGI
    scope) or a ``RequestContext``; ``user`` is your user id (default: the request's user). Never raises."""
    try:
        if request is not None and hasattr(request, "META"):
            from .django import checkout_metadata as for_django

            return for_django(request, user)
        if request is not None and not isinstance(request, RequestContext):
            from .asgi import _scope_of
            from .asgi import checkout_metadata as for_asgi

            if _scope_of(request) is not None:
                return for_asgi(request, user)
        return get_client().checkout_metadata(request, user)
    except Exception:
        return {} if user is None or user == "" else {"runomine_user": str(user)}


def pageview(url: str | None = None, **kwargs: Any) -> str | None:
    """``get_client().pageview(...)``. Never raises."""
    try:
        return get_client().pageview(url, **kwargs)
    except Exception:
        return None


def sample(data: Any, ts: Any = None) -> bool:
    """``get_client().sample(...)``: the app's own ops numbers, sent at once to runomine's Live page. Never raises."""
    try:
        return get_client().sample(data, ts)
    except Exception:
        return False


def samples(items: Any) -> bool:
    """``get_client().samples(...)``: several ``{"data": ..., "ts": ...}``, in calls of at most 500. Never raises."""
    try:
        return get_client().samples(items)
    except Exception:
        return False


def flush(timeout: float | None = 5.0) -> bool:
    """Send what the process-wide client has queued, waiting up to ``timeout`` seconds."""
    try:
        return get_client().flush(timeout)
    except Exception:
        return False


def shutdown(timeout: float | None = None) -> None:
    """Flush and stop the process-wide client. Also runs automatically at exit."""
    try:
        get_client().shutdown(timeout)
    except Exception:
        pass
