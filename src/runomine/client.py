"""The framework-agnostic runomine client: builds events, queues them, sends them in batches.

Contract (docs/contract.md, "Libraries"): the queue is in memory, flushes every few seconds and at exit, at most
500 events per call, retries 5xx/429 with backoff and then gives up quietly. The app is never blocked and nothing
here ever raises because of runomine.
"""

from __future__ import annotations

import atexit
import collections
import datetime as _dt
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
import weakref
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from ._version import __version__
from ._uuid7 import uuid7
from .visitor import visitor_hash

__all__ = [
    "Client",
    "RequestContext",
    "get_client",
    "set_client",
    "DEFAULT_URL",
    "MAX_BATCH",
]

logger = logging.getLogger("runomine")
logger.addHandler(logging.NullHandler())

DEFAULT_URL = "https://runomine.com"
MAX_BATCH = 500
EVENTS_PATH = "/api/v1/events"
SAMPLES_PATH = "/api/v1/samples"
_NAME_RE = re.compile(r"^[a-z0-9_.:-]{1,64}$")
_FALSE = {"0", "false", "no", "off"}

# transport(url, body, headers, timeout) -> (status, retry_after_header_or_None).
# Raising means a network failure, which is retried like a 5xx.
Transport = Callable[[str, bytes, Mapping[str, str], float], "tuple[int, str | None]"]


@dataclass
class RequestContext:
    """What a library knows about the current request. The IP is only used for the visitor hash."""

    url: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    host: str | None = None
    referrer: str | None = None
    country: str | None = None
    user: str | None = None
    visitor: str | None = None

    @classmethod
    def coerce(cls, value: Any) -> "RequestContext | None":
        if value is None or isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            fields = cls.__dataclass_fields__
            return cls(**{k: v for k, v in value.items() if k in fields})
        return None


def urllib_transport(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> "tuple[int, str | None]":
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            return response.status, None
    except urllib.error.HTTPError as error:
        retry_after = error.headers.get("Retry-After") if error.headers is not None else None
        try:
            error.close()
        except Exception:
            pass
        return error.code, retry_after


def _env_enabled(raw: str | None) -> bool | None:
    if raw is None or raw.strip() == "":
        return None
    return raw.strip().lower() not in _FALSE


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _iso(ts: Any) -> str:
    if ts is None:
        return _now_iso()
    if isinstance(ts, _dt.datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        return ts.astimezone(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return _iso(_dt.datetime.fromtimestamp(ts, _dt.timezone.utc))
    return str(ts)


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (float, Decimal)):
        number = float(value)
        return number if number == number and number not in (float("inf"), float("-inf")) else None
    if isinstance(value, str):
        try:
            return _number(Decimal(value.strip()))
        except Exception:
            return None
    return None


def _revenue(value: Any) -> dict | None:
    """Accept {"amount", "currency"}, (amount, currency) or a bare amount (EUR)."""
    if value is None:
        return None
    amount: Any
    currency: Any = "EUR"
    if isinstance(value, Mapping):
        amount, currency = value.get("amount"), value.get("currency", "EUR")
    elif isinstance(value, (tuple, list)) and len(value) == 2:
        amount, currency = value
    else:
        amount = value
    number = _number(amount)
    if number is None or not isinstance(currency, str) or not currency.strip():
        logger.warning("runomine: ignoring invalid revenue %r", value)
        return None
    return {"amount": number, "currency": currency.strip().upper()}


def _props(value: Any) -> dict | None:
    if not value or not isinstance(value, Mapping):
        return None
    out: dict[str, str | int | float] = {}
    for key, item in value.items():
        if item is None:
            continue
        if isinstance(item, bool):
            out[str(key)] = "true" if item else "false"
        elif isinstance(item, (int, str)):
            out[str(key)] = item
        elif isinstance(item, (float, Decimal)):
            number = _number(item)
            out[str(key)] = number if number is not None else str(item)
        else:
            out[str(key)] = str(item)
    return out or None


class Client:
    """Sends pageviews and events to runomine.

    Reads ``RUNOMINE_TOKEN``, ``RUNOMINE_URL`` and ``RUNOMINE_ENABLED`` from the environment unless given.
    Without a token it is disabled and every call is a cheap no-op.
    """

    def __init__(
        self,
        token: str | None = None,
        url: str | None = None,
        enabled: bool | None = None,
        *,
        flush_interval: float = 2.0,
        timeout: float = 5.0,
        max_retries: int = 2,
        backoff: float = 0.5,
        max_queue: int = 10_000,
        shutdown_timeout: float = 3.0,
        transport: Transport | None = None,
    ) -> None:
        self.token = token if token is not None else os.environ.get("RUNOMINE_TOKEN") or None
        self.url = (url or os.environ.get("RUNOMINE_URL") or DEFAULT_URL).rstrip("/")
        if enabled is None:
            enabled = _env_enabled(os.environ.get("RUNOMINE_ENABLED"))
        elif isinstance(enabled, str):  # e.g. a Django setting copied from os.environ
            enabled = _env_enabled(enabled)
        self.enabled = bool(self.token) and (True if enabled is None else bool(enabled))
        if not self.token:
            logger.debug("runomine: no RUNOMINE_TOKEN, the client is disabled")

        self.endpoint = self.url + EVENTS_PATH
        self.samples_endpoint = self.url + SAMPLES_PATH
        self.flush_interval = flush_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.max_queue = max_queue
        self.shutdown_timeout = shutdown_timeout
        self._transport: Transport = transport or urllib_transport
        self._warned_401 = False
        self._atexit_registered = False
        self._init_state()

        if hasattr(os, "register_at_fork"):
            ref = weakref.ref(self)

            def _after_fork_in_child() -> None:
                client = ref()
                if client is not None:
                    client._init_state()

            os.register_at_fork(after_in_child=_after_fork_in_child)

    # ------------------------------------------------------------------ state

    def _init_state(self) -> None:
        """(Re)create the queue and worker state. Also runs in a forked child: its parent owns what was queued."""
        self._pid = os.getpid()
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._queue: collections.deque[str] = collections.deque(maxlen=self.max_queue)
        self._inflight = 0
        self._wake = threading.Event()
        self._abort = threading.Event()
        self._stopping = False
        self._thread: threading.Thread | None = None

    def _ensure_worker(self) -> None:
        if self._pid != os.getpid():
            self._init_state()
        if self._thread is not None and self._thread.is_alive():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._abort.clear()
            self._thread = threading.Thread(target=self._run, name="runomine-sender", daemon=True)
            self._thread.start()
        if not self._atexit_registered:
            self._atexit_registered = True
            ref = weakref.ref(self)

            def _at_exit() -> None:
                client = ref()
                if client is not None:
                    client.shutdown(client.shutdown_timeout)

            atexit.register(_at_exit)

    # ------------------------------------------------------------- public API

    def visitor(self, ip: str | None, user_agent: str | None, host: str | None, day: Any = None) -> str:
        """The contract visitor hash with this client's token."""
        return visitor_hash(self.token or "", ip, user_agent, host, day)

    def checkout_metadata(
        self,
        request: RequestContext | Mapping | None = None,
        user: Any = None,
        *,
        day: Any = None,
    ) -> dict[str, str]:
        """What to pass to your payment provider when creating a checkout (Stripe ``metadata``, Paddle
        ``custom_data``, ...), so runomine can attribute the sale.

        ``runomine_visitor`` is the request's visitor hash (left out when the client is disabled or the request has
        no IP) and ``runomine_user`` is ``user``, else the request's user, as a string. Only keys with a value.
        Never raises.
        """
        metadata: dict[str, str] = {}
        ctx = None
        try:
            ctx = RequestContext.coerce(request)
            if self.enabled and ctx is not None and ctx.ip:
                host = ctx.host or (urlsplit(ctx.url).hostname if ctx.url else None)
                metadata["runomine_visitor"] = self.visitor(ctx.ip, ctx.user_agent, host, day)
        except Exception:
            logger.debug("runomine: checkout_metadata failed", exc_info=True)
        try:
            if user is None or user == "":
                user = ctx.user if ctx is not None else None
            if user is not None and str(user) != "":
                metadata["runomine_user"] = str(user)
        except Exception:
            pass
        return metadata

    def pageview(
        self,
        url: str | None = None,
        *,
        ip: str | None = None,
        user_agent: str | None = None,
        referrer: str | None = None,
        country: str | None = None,
        user: Any = None,
        host: str | None = None,
        visitor: str | None = None,
        ts: Any = None,
        request: RequestContext | Mapping | None = None,
        props: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Queue a pageview. Returns the event id, or None when disabled or on error. Never raises.

        ``props`` follow ``track``'s rules (flat, up to 20 keys, string or number values); runomine stores them as the
        page view's dims, each key a breakdown and a filter in Traffic (``area`` feeds the Areas breakdown).
        """
        if not self.enabled:
            return None
        try:
            ctx = RequestContext.coerce(request) or RequestContext()
            url = url or ctx.url
            if not url:
                logger.warning("runomine: pageview without url ignored")
                return None
            ip = ip if ip is not None else ctx.ip
            user_agent = user_agent if user_agent is not None else ctx.user_agent
            host = host or ctx.host or urlsplit(url).hostname
            visitor = visitor or ctx.visitor or self.visitor(ip, user_agent, host)
            event: dict[str, Any] = {
                "id": uuid7(),
                "type": "pageview",
                "ts": _iso(ts),
                "url": url,
                "user_agent": user_agent or "",
                "visitor": visitor,
            }
            referrer = referrer if referrer is not None else ctx.referrer
            if referrer:
                event["referrer"] = referrer
            country = country or ctx.country
            if country:
                event["country"] = country
            user = user if user is not None else ctx.user
            if user is not None and user != "":
                event["user"] = str(user)
            properties = _props(props)
            if properties:
                event["props"] = properties
            return self._enqueue(event)
        except Exception:
            logger.debug("runomine: pageview failed", exc_info=True)
            return None

    def track(
        self,
        name: str,
        props: Mapping[str, Any] | None = None,
        revenue: Any = None,
        user: Any = None,
        request: RequestContext | Mapping | None = None,
        *,
        url: str | None = None,
        visitor: str | None = None,
        country: str | None = None,
        ts: Any = None,
    ) -> str | None:
        """Queue a business event (``signup``, ``purchase``...). Returns the event id or None. Never raises.

        ``revenue`` is ``{"amount": 4.99, "currency": "EUR"}``, ``(4.99, "EUR")`` or a bare amount in EUR.
        With a request the visitor is hashed from it. Without a request or an explicit ``visitor`` (webhooks,
        jobs) the event carries no ``visitor`` at all and runomine attributes it through ``user``; to keep the
        browser's visitor, capture it with ``visitor_for(request)`` and pass ``visitor=`` later.
        """
        if not self.enabled:
            return None
        try:
            name = str(name)
            if not _NAME_RE.match(name):
                logger.warning("runomine: event name %r does not match [a-z0-9_.:-]{1,64}; runomine will reject it", name)
            given = RequestContext.coerce(request)
            ctx = given or RequestContext()
            if not visitor and given is not None:
                visitor = ctx.visitor or self.visitor(ctx.ip, ctx.user_agent, ctx.host or (urlsplit(ctx.url).hostname if ctx.url else None))
            event: dict[str, Any] = {"id": uuid7(), "type": "event", "name": name, "ts": _iso(ts)}
            if visitor:
                event["visitor"] = visitor
            url = url or ctx.url
            if url:
                event["url"] = url
            country = country or ctx.country
            if country:
                event["country"] = country
            user = user if user is not None else ctx.user
            if user is not None and user != "":
                event["user"] = str(user)
            money = _revenue(revenue)
            if money:
                event["revenue"] = money
            properties = _props(props)
            if properties:
                event["props"] = properties
            return self._enqueue(event)
        except Exception:
            logger.debug("runomine: track failed", exc_info=True)
            return None

    def sample(self, data: Mapping[str, Any], ts: Any = None) -> bool:
        """Send one sample of the app's own ops numbers (queue depth, CPU, people on the site right now) to
        ``POST /api/v1/samples``, for runomine's Live page. Never raises.

        Nested mappings become dotted keys there (``queue.pending``), booleans 0/1, and anything that isn't a number is
        dropped. Sent at once in the calling thread, not queued, with the same retries as events; one sample per
        second (the same ``ts`` again replaces it), any age. True when runomine took it, False when disabled or failed.
        """
        return self.samples([{"data": data, "ts": ts}])

    def samples(self, samples: Any) -> bool:
        """Send several samples, each ``{"data": {...}, "ts": ...}`` (a catch-up after an outage), in calls of at most
        500. True when runomine took every call. Never raises."""
        if not self.enabled:
            return False
        try:
            rows = [
                {"ts": _iso(item.get("ts")), "data": dict(item["data"])}
                for item in samples
                if isinstance(item, Mapping) and isinstance(item.get("data"), Mapping)
            ]
            ok = True
            for start in range(0, len(rows), MAX_BATCH):
                chunk = rows[start : start + MAX_BATCH]
                try:
                    body = json.dumps({"samples": chunk}, separators=(",", ":"), default=str, allow_nan=False, ensure_ascii=False)
                except Exception:
                    logger.warning("runomine: dropping samples that cannot be serialised", exc_info=True)
                    ok = False
                    continue
                ok = self._post(self.samples_endpoint, body.encode("utf-8"), len(chunk)) and ok
            return ok
        except Exception:
            logger.debug("runomine: samples failed", exc_info=True)
            return False

    def flush(self, timeout: float | None = 5.0) -> bool:
        """Send everything queued now. Waits up to ``timeout`` seconds; True when the queue drained."""
        try:
            if self._pid != os.getpid():
                self._init_state()
            with self._lock:
                if not self._queue and not self._inflight:
                    return True
            self._ensure_worker()
            self._wake.set()
            with self._idle:
                return self._idle.wait_for(lambda: not self._queue and not self._inflight, timeout)
        except Exception:
            logger.debug("runomine: flush failed", exc_info=True)
            return False

    def shutdown(self, timeout: float | None = None) -> None:
        """Flush (bounded by ``timeout``), then stop the sender thread. Safe to call more than once."""
        try:
            timeout = self.shutdown_timeout if timeout is None else timeout
            self.flush(timeout)
            thread = self._thread
            self._stopping = True
            self._abort.set()
            self._wake.set()
            if thread is not None and thread is not threading.current_thread():
                thread.join(0.5)
        except Exception:
            logger.debug("runomine: shutdown failed", exc_info=True)

    close = shutdown

    # --------------------------------------------------------------- internals

    def _enqueue(self, event: dict) -> str | None:
        try:
            line = json.dumps(event, separators=(",", ":"), default=str, allow_nan=False, ensure_ascii=False)
        except Exception:
            logger.warning("runomine: dropping an event that cannot be serialised", exc_info=True)
            return None
        self._ensure_worker()
        with self._lock:
            if len(self._queue) == self._queue.maxlen:
                logger.debug("runomine: queue full, dropping the oldest event")
            self._queue.append(line)
            size = len(self._queue)
        if size >= MAX_BATCH:
            self._wake.set()
        return event["id"]

    def _run(self) -> None:
        while True:
            self._wake.wait(self.flush_interval)
            self._wake.clear()
            try:
                self._drain()
            except Exception:  # pragma: no cover - the worker must survive anything
                logger.debug("runomine: sender error", exc_info=True)
                with self._idle:
                    self._inflight = 0
                    self._idle.notify_all()
            if self._stopping:
                return

    def _drain(self) -> None:
        while True:
            with self._idle:
                batch = [self._queue.popleft() for _ in range(min(MAX_BATCH, len(self._queue)))]
                self._inflight = len(batch)
                if not batch:
                    self._idle.notify_all()
                    return
            try:
                self._send(batch)
            finally:
                with self._idle:
                    self._inflight = 0
                    self._idle.notify_all()

    def _send(self, batch: list[str]) -> bool:
        return self._post(self.endpoint, ('{"events":[' + ",".join(batch) + "]}").encode("utf-8"), len(batch))

    def _post(self, url: str, body: bytes, count: int) -> bool:
        headers = {
            "Authorization": "Bearer " + (self.token or ""),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "runomine-python/" + __version__,
        }
        for attempt in range(self.max_retries + 1):
            retry_after = None
            try:
                status, retry_after = self._transport(url, body, headers, self.timeout)
            except Exception as error:
                status = None
                logger.debug("runomine: network error: %s", error)
            if status is not None and 200 <= status < 300:
                return True
            if status is not None and status != 429 and status < 500:
                if status == 401 and not self._warned_401:
                    self._warned_401 = True
                    logger.warning("runomine: 401 from %s, check RUNOMINE_TOKEN", url)
                else:
                    logger.debug("runomine: %s, dropping %d items", status, count)
                return False
            if attempt == self.max_retries:
                break
            delay = self.backoff * (2**attempt)
            if retry_after:
                try:
                    delay = max(delay, min(float(retry_after), 10.0))
                except ValueError:
                    pass
            if self._abort.wait(delay):
                break
        logger.debug("runomine: giving up on %d items", count)
        return False


_default: Client | None = None
_default_lock = threading.Lock()


def get_client() -> Client:
    """The process-wide client, created from the environment on first use."""
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = Client()
    return _default


def set_client(client: Client | None) -> None:
    """Replace the process-wide client (used by the Django integration and by tests)."""
    global _default
    _default = client
