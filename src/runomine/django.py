"""Django integration.

    # settings.py
    MIDDLEWARE = [..., "runomine.django.RunomineMiddleware"]
    RUNOMINE_TOKEN = env("RUNOMINE_TOKEN")             # optional: falls back to the environment

    # anywhere with a request
    from runomine.django import track
    track(request, "signup")

Settings (each falls back to the environment variable of the same name where one exists):
``RUNOMINE_TOKEN``, ``RUNOMINE_URL``, ``RUNOMINE_ENABLED``, ``RUNOMINE_SKIP_PATHS`` (default
``("/admin", "/static", "/media")``) and ``RUNOMINE_IP_HEADER`` (e.g. ``"X-Forwarded-For"`` or ``"CF-Connecting-IP"``
when behind a proxy; default ``REMOTE_ADDR``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import client as _core
from ._common import DEFAULT_SKIP_PATHS, country, first_forwarded, is_prefetch, is_skipped
from .client import Client, RequestContext

__all__ = [
    "RunomineMiddleware",
    "track",
    "visitor_for",
    "checkout_metadata",
    "context_from_request",
    "get_client",
    "set_client",
]

logger = logging.getLogger("runomine")

_client: Client | None = None
_UNSET = object()


def _setting(name: str, default: Any = None) -> Any:
    try:
        from django.conf import settings

        return getattr(settings, name, default)
    except Exception:
        return default


def get_client() -> Client:
    """The client configured from Django settings (falling back to the environment). One per process."""
    global _client
    if _client is None:
        token, url, enabled = (_setting(n, _UNSET) for n in ("RUNOMINE_TOKEN", "RUNOMINE_URL", "RUNOMINE_ENABLED"))
        if token is _UNSET and url is _UNSET and enabled is _UNSET:
            _client = _core.get_client()
        else:
            _client = Client(
                token=None if token is _UNSET else token,
                url=None if url is _UNSET else url,
                enabled=None if enabled is _UNSET else enabled,
            )
            _core.set_client(_client)
    return _client


def set_client(client: Client | None) -> None:
    """Use this client for the middleware, ``track`` and ``runomine.track``."""
    global _client
    _client = client
    _core.set_client(client)


def _meta_key(header: str) -> str:
    key = header.strip().upper().replace("-", "_")
    if key in ("REMOTE_ADDR", "CONTENT_TYPE", "CONTENT_LENGTH") or key.startswith("HTTP_"):
        return key
    return "HTTP_" + key


def _header(request: Any, name: str) -> str | None:
    return request.META.get(_meta_key(name))


def context_from_request(request: Any, ip_header: str | None = None) -> RequestContext:
    """What runomine needs from a Django ``HttpRequest``. The IP only feeds the visitor hash."""
    meta = request.META
    if ip_header is None:
        ip_header = _setting("RUNOMINE_IP_HEADER")
    ip = first_forwarded(meta.get(_meta_key(ip_header))) if ip_header else None
    ip = ip or meta.get("REMOTE_ADDR") or ""
    try:
        host = request.get_host()
    except Exception:
        host = meta.get("HTTP_HOST") or meta.get("SERVER_NAME") or ""
    try:
        url = request.build_absolute_uri()
    except Exception:
        url = "http://" + host + request.get_full_path()
    user = None
    try:
        account = getattr(request, "user", None)
        if account is not None and getattr(account, "is_authenticated", False):
            pk = getattr(account, "pk", None)
            user = str(pk) if pk is not None else None
    except Exception:
        user = None
    return RequestContext(
        url=url,
        ip=ip,
        user_agent=meta.get("HTTP_USER_AGENT", ""),
        host=host,
        referrer=meta.get("HTTP_REFERER") or None,
        country=country(lambda name: _header(request, name)),
        user=user,
    )


def visitor_for(request: Any) -> str | None:
    """Today's visitor hash for this request: store it (e.g. in checkout metadata) to attribute webhooks later."""
    try:
        ctx = context_from_request(request)
        return get_client().visitor(ctx.ip, ctx.user_agent, ctx.host)
    except Exception:
        return None


def checkout_metadata(request: Any, user: Any = None) -> dict[str, str]:
    """``{"runomine_visitor": ..., "runomine_user": ...}`` for your payment provider's checkout, only the keys with a
    value. ``user`` defaults to ``request.user.pk``. No visitor without a token or the request's IP. Never raises."""
    try:
        ctx = context_from_request(request) if request is not None else None
    except Exception:
        ctx = None
    try:
        return get_client().checkout_metadata(ctx, user)
    except Exception:
        return {} if user is None or user == "" else {"runomine_user": str(user)}


def track(request: Any, name: str, props: Any = None, revenue: Any = None, user: Any = None, **kwargs: Any) -> str | None:
    """Track a business event for this request. ``user`` defaults to ``request.user.pk``. Never raises."""
    try:
        ctx = context_from_request(request) if request is not None else None
        return get_client().track(name, props, revenue, user, ctx, **kwargs)
    except Exception:
        logger.debug("runomine: django track failed", exc_info=True)
        return None


try:  # asgiref >= 3.6 (Django 4.2+)
    from asgiref.sync import iscoroutinefunction as _iscoroutinefunction
    from asgiref.sync import markcoroutinefunction as _markcoroutinefunction
except Exception:  # pragma: no cover
    _iscoroutinefunction = asyncio.iscoroutinefunction
    _markcoroutinefunction = None


class RunomineMiddleware:
    """Records a pageview for ``GET`` requests answered ``200 text/html``, outside ``RUNOMINE_SKIP_PATHS``.

    The pageview is queued once the view has produced its response; sending happens on a background thread,
    so the response is never delayed by runomine and nothing here can raise into the request.
    """

    sync_capable = True
    async_capable = True

    def __init__(self, get_response: Any) -> None:
        self.get_response = get_response
        self.skip_paths = tuple(_setting("RUNOMINE_SKIP_PATHS", DEFAULT_SKIP_PATHS) or ())
        self.ip_header = _setting("RUNOMINE_IP_HEADER")
        self._is_async = _iscoroutinefunction(get_response)
        if self._is_async and _markcoroutinefunction is not None:
            _markcoroutinefunction(self)

    def __call__(self, request: Any) -> Any:
        if self._is_async:
            return self.__acall__(request)
        response = self.get_response(request)
        if self._wants(request, response):
            self._record(request)
        return response

    async def __acall__(self, request: Any) -> Any:
        response = await self.get_response(request)
        if self._wants(request, response):
            try:
                from asgiref.sync import sync_to_async

                # request.user may hit the database, which Django forbids in async code.
                await sync_to_async(self._record, thread_sensitive=True)(request)
            except Exception:
                logger.debug("runomine: async record failed", exc_info=True)
        return response

    def _wants(self, request: Any, response: Any) -> bool:
        try:
            if request.method != "GET" or getattr(response, "status_code", None) != 200:
                return False
            if not str(response.get("Content-Type", "")).lower().startswith("text/html"):
                return False
            if is_skipped(request.path, self.skip_paths):
                return False
            if is_prefetch(lambda name: _header(request, name)):
                return False
            return get_client().enabled
        except Exception:
            return False

    def _record(self, request: Any) -> None:
        try:
            get_client().pageview(request=context_from_request(request, self.ip_header))
        except Exception:
            logger.debug("runomine: pageview failed", exc_info=True)
