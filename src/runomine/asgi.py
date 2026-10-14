"""Pure ASGI integration for Starlette, FastAPI and any ASGI app. Imports no framework.

    from fastapi import FastAPI, Request
    from runomine.asgi import RunomineMiddleware, track

    app = FastAPI()
    app.add_middleware(RunomineMiddleware, skip_paths=["/docs", "/redoc"])

    @app.post("/signup")
    async def signup(request: Request):
        ...
        track(request, "signup", user=new_user.id)
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Iterable, Mapping, MutableMapping
from urllib.parse import quote

from . import client as _core
from ._common import DEFAULT_SKIP_PATHS, country, first_forwarded, is_prefetch, is_skipped
from .client import Client, RequestContext

__all__ = ["RunomineMiddleware", "track", "visitor_for", "checkout_metadata", "context_from_scope"]

logger = logging.getLogger("runomine")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
SCOPE_KEY = "runomine"
OPTIONS_KEY = "runomine.options"


def _headers(scope: Scope) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in scope.get("headers") or ():
        key = name.decode("latin-1").lower() if isinstance(name, bytes) else str(name).lower()
        val = value.decode("latin-1") if isinstance(value, bytes) else str(value)
        out[key] = out[key] + "," + val if key in out else val
    return out


def _user_from_scope(scope: Scope) -> str | None:
    """Starlette's AuthenticationMiddleware puts a user with ``is_authenticated`` and ``identity`` in the scope."""
    try:
        account = scope.get("user")
        if account is None or not getattr(account, "is_authenticated", False):
            return None
        for attr in ("identity", "id", "pk"):
            value = getattr(account, attr, None)
            if value is not None and value != "":
                return str(value)
    except Exception:
        pass
    return None


def context_from_scope(
    scope: Scope,
    ip_header: str | None = None,
    get_user: Callable[[Scope], Any] | None = None,
) -> RequestContext:
    """What runomine needs from an HTTP scope. The IP only feeds the visitor hash."""
    headers = _headers(scope)
    server = scope.get("server") or None
    host = headers.get("host")
    if not host and server:
        host = f"{server[0]}:{server[1]}" if server[1] not in (80, 443, None) else str(server[0])
    host = host or ""
    raw_path = scope.get("raw_path")
    if isinstance(raw_path, (bytes, bytearray)) and raw_path:
        path = raw_path.decode("latin-1")
    else:
        path = quote(str(scope.get("root_path", "")) + str(scope.get("path", "/")), safe="/%:@!$&'()*+,;=-._~")
    query = scope.get("query_string") or b""
    if isinstance(query, (bytes, bytearray)):
        query = query.decode("latin-1")
    url = f"{scope.get('scheme', 'http')}://{host}{path}" + (f"?{query}" if query else "")

    ip = first_forwarded(headers.get(ip_header.lower())) if ip_header else None
    if not ip:
        client = scope.get("client")
        ip = client[0] if client else ""

    user = None
    if get_user is not None:
        try:
            value = get_user(scope)
            user = str(value) if value is not None and value != "" else None
        except Exception:
            user = None
    else:
        user = _user_from_scope(scope)

    return RequestContext(
        url=url,
        ip=ip,
        user_agent=headers.get("user-agent", ""),
        host=host,
        referrer=headers.get("referer") or None,
        country=country(headers.get),
        user=user,
    )


class RunomineMiddleware:
    """Pure ASGI middleware: records a pageview for ``GET`` answered ``200 text/html``, after the last body chunk.

    Options: ``client`` (or ``token``/``url``/``enabled`` to build one; default: the process-wide client from the
    environment), ``skip_paths`` (prefixes or fnmatch patterns), ``ip_header`` (e.g. ``"x-forwarded-for"`` behind
    a proxy), ``get_user`` (``scope -> user id``; default: Starlette's ``scope["user"].identity``) and
    ``get_props`` (``scope -> props`` for the page view; default: ``request.state.runomine_props`` in Starlette/FastAPI,
    i.e. ``scope["state"]["runomine_props"]``).
    """

    def __init__(
        self,
        app: Callable[[Scope, Receive, Send], Awaitable[None]],
        client: Client | None = None,
        *,
        token: str | None = None,
        url: str | None = None,
        enabled: bool | None = None,
        skip_paths: Iterable[str] = DEFAULT_SKIP_PATHS,
        ip_header: str | None = None,
        get_user: Callable[[Scope], Any] | None = None,
        get_props: Callable[[Scope], Any] | None = None,
    ) -> None:
        self.app = app
        if client is None and (token is not None or url is not None or enabled is not None):
            client = Client(token=token, url=url, enabled=enabled)
        self._client = client
        self.skip_paths = tuple(skip_paths or ())
        self.ip_header = ip_header
        self.get_user = get_user
        self.get_props = get_props

    @property
    def client(self) -> Client:
        return self._client if self._client is not None else _core.get_client()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        try:
            client = self.client
            scope[SCOPE_KEY] = client
            scope[OPTIONS_KEY] = (self.ip_header, self.get_user)
            wanted = (
                client.enabled
                and scope.get("method") == "GET"
                and not is_skipped(str(scope.get("path", "")), self.skip_paths)
                and not is_prefetch(_headers(scope).get)
            )
        except Exception:
            wanted = False
        if not wanted:
            await self.app(scope, receive, send)
            return

        state = {"status": None, "html": False, "done": False}

        async def send_wrapper(message: Message) -> None:
            if message.get("type") == "http.response.start":
                state["status"] = message.get("status")
                for name, value in message.get("headers") or ():
                    if bytes(name).lower() == b"content-type":
                        state["html"] = bytes(value).lower().startswith(b"text/html")
            await send(message)
            if (
                message.get("type") == "http.response.body"
                and not message.get("more_body", False)
                and not state["done"]
            ):
                state["done"] = True
                if state["status"] == 200 and state["html"]:
                    self._record(scope, client)

        await self.app(scope, receive, send_wrapper)

    def _record(self, scope: Scope, client: Client) -> None:
        try:
            client.pageview(request=context_from_scope(scope, self.ip_header, self.get_user), props=self._props(scope))
        except Exception:
            logger.debug("runomine: pageview failed", exc_info=True)


    def _props(self, scope: Scope) -> Any:
        try:
            if self.get_props is not None:
                return self.get_props(scope)
            state = scope.get("state")
            return state.get("runomine_props") if isinstance(state, Mapping) else None
        except Exception:
            logger.debug("runomine: get_props failed", exc_info=True)
            return None


def _scope_of(request: Any) -> Scope | None:
    if request is None:
        return None
    if isinstance(request, MutableMapping) and "type" in request:
        return request
    return getattr(request, "scope", None)


def _client_for(scope: Scope | None) -> Client:
    found = scope.get(SCOPE_KEY) if scope is not None else None
    return found if isinstance(found, Client) else _core.get_client()


def _context(scope: Scope, ip_header: str | None) -> RequestContext:
    options = scope.get(OPTIONS_KEY) or (None, None)
    return context_from_scope(scope, ip_header or options[0], options[1])


def visitor_for(request: Any, ip_header: str | None = None) -> str | None:
    """Today's visitor hash for this request (a Starlette ``Request`` or a raw scope)."""
    try:
        scope = _scope_of(request)
        ctx = _context(scope, ip_header)
        return _client_for(scope).visitor(ctx.ip, ctx.user_agent, ctx.host)
    except Exception:
        return None


def checkout_metadata(request: Any, user: Any = None, *, ip_header: str | None = None) -> dict[str, str]:
    """``{"runomine_visitor": ..., "runomine_user": ...}`` for your payment provider's checkout, only the keys with a
    value (a Starlette ``Request`` or a raw scope). ``user`` defaults to the scope's authenticated user. No visitor
    without a token or the request's IP. Never raises."""
    scope = None
    ctx = None
    try:
        scope = _scope_of(request)
        ctx = _context(scope, ip_header) if scope is not None else None
    except Exception:
        ctx = None
    try:
        return _client_for(scope).checkout_metadata(ctx, user)
    except Exception:
        return {} if user is None or user == "" else {"runomine_user": str(user)}


def track(
    request: Any,
    name: str,
    props: Any = None,
    revenue: Any = None,
    user: Any = None,
    *,
    ip_header: str | None = None,
    **kwargs: Any,
) -> str | None:
    """Track a business event for this request (Starlette/FastAPI ``Request`` or a raw scope). Never raises.

    Uses the client the middleware put in the scope, else the process-wide client.
    """
    try:
        scope = _scope_of(request)
        ctx = _context(scope, ip_header) if scope is not None else None
        return _client_for(scope).track(name, props, revenue, user, ctx, **kwargs)
    except Exception:
        logger.debug("runomine: asgi track failed", exc_info=True)
        return None
