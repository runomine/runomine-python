import asyncio

import pytest
from conftest import TOKEN, FakeTransport

from runomine import Client, visitor_hash
from runomine.asgi import RunomineMiddleware, context_from_scope, track, visitor_for

UA = "Mozilla/5.0 test"


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture
def rom(transport):
    client = Client(token=TOKEN, url="https://runomine.test", transport=transport, flush_interval=60, backoff=0.01)
    yield client
    client.shutdown(1)


def sent(rom, transport):
    assert rom.flush(2)
    return transport.events


# --------------------------------------------------------------- raw ASGI


def raw_app(status=200, content_type=b"text/html; charset=utf-8", chunks=(b"<h1>hi</h1>",)):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", content_type)]})
        for i, chunk in enumerate(chunks):
            await send({"type": "http.response.body", "body": chunk, "more_body": i < len(chunks) - 1})

    return app


def http_scope(path="/", method="GET", query=b"", headers=None, client=("203.0.113.9", 5000)):
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "root_path": "",
        "headers": [(b"host", b"Example.com"), (b"user-agent", UA.encode())] + list(headers or []),
        "client": client,
        "server": ("example.com", 443),
    }


def run(app, scope):
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    return messages


def test_records_after_last_body_chunk(rom, transport):
    seen_queue_sizes = []
    inner = raw_app(chunks=(b"<h1>", b"hi", b"</h1>"))

    async def app(scope, receive, send):
        async def spy(message):
            seen_queue_sizes.append(len(rom._queue))
            await send(message)

        await inner(scope, receive, spy)

    middleware = RunomineMiddleware(app, client=rom)
    run(middleware, http_scope(query=b"utm_source=x", headers=[(b"referer", b"https://www.google.com/"), (b"cf-ipcountry", b"ES")]))
    assert seen_queue_sizes == [0, 0, 0, 0]  # nothing queued while the response was still streaming
    [event] = sent(rom, transport)
    assert event["type"] == "pageview"
    assert event["url"] == "https://Example.com/?utm_source=x"
    assert event["referrer"] == "https://www.google.com/"
    assert event["country"] == "ES"
    assert event["visitor"] == visitor_hash(TOKEN, "203.0.113.9", UA, "example.com")


@pytest.mark.parametrize(
    "app,scope",
    [
        (raw_app(), http_scope(method="POST")),
        (raw_app(), http_scope(method="HEAD")),
        (raw_app(status=404), http_scope()),
        (raw_app(status=301), http_scope()),
        (raw_app(content_type=b"application/json"), http_scope()),
        (raw_app(), http_scope(path="/static/app.css")),
        (raw_app(), http_scope(path="/admin")),
        (raw_app(), http_scope(headers=[(b"sec-purpose", b"prefetch")])),
    ],
)
def test_filters(rom, transport, app, scope):
    run(RunomineMiddleware(app, client=rom), scope)
    assert sent(rom, transport) == []


def test_custom_skip_paths(rom, transport):
    middleware = RunomineMiddleware(raw_app(), client=rom, skip_paths=["/docs", "/redoc"])
    run(middleware, http_scope(path="/docs"))
    run(middleware, http_scope(path="/admin"))  # no longer in the list
    assert [e["url"] for e in sent(rom, transport)] == ["https://Example.com/admin"]


def test_non_http_scopes_pass_through(rom):
    called = []

    async def app(scope, receive, send):
        called.append(scope["type"])

    middleware = RunomineMiddleware(app, client=rom)
    asyncio.run(middleware({"type": "lifespan"}, None, None))
    asyncio.run(middleware({"type": "websocket", "path": "/"}, None, None))
    assert called == ["lifespan", "websocket"]


def test_ip_header(rom, transport):
    middleware = RunomineMiddleware(raw_app(), client=rom, ip_header="X-Forwarded-For")
    run(middleware, http_scope(headers=[(b"x-forwarded-for", b"198.51.100.7, 10.0.0.1")], client=("10.0.0.1", 1)))
    [event] = sent(rom, transport)
    assert event["visitor"] == visitor_hash(TOKEN, "198.51.100.7", UA, "example.com")


def test_app_errors_propagate_untouched(rom, transport):
    async def broken(scope, receive, send):
        raise ValueError("app bug")

    with pytest.raises(ValueError):
        run(RunomineMiddleware(broken, client=rom), http_scope())
    assert sent(rom, transport) == []


def test_context_from_scope_without_host_header():
    scope = http_scope()
    scope["headers"] = []
    scope["server"] = ("127.0.0.1", 8000)
    ctx = context_from_scope(scope)
    assert ctx.url == "https://127.0.0.1:8000/"
    assert ctx.ip == "203.0.113.9"


def test_track_with_raw_scope(rom, transport):
    scope = http_scope(path="/checkout/success")
    scope["runomine"] = rom
    track(scope, "purchase", revenue=(4.99, "EUR"), user=42)
    [event] = sent(rom, transport)
    assert event["name"] == "purchase" and event["user"] == "42"
    assert event["url"] == "https://Example.com/checkout/success"
    assert event["visitor"] == visitor_for(scope)


def test_track_without_request_omits_visitor(rom, transport):
    import runomine

    runomine.set_client(rom)
    track(None, "renewal", user="7")
    [event] = sent(rom, transport)
    assert "visitor" not in event and event["user"] == "7"


# -------------------------------------------------------- Starlette/FastAPI

starlette = pytest.importorskip("starlette")
httpx = pytest.importorskip("httpx")

from starlette.applications import Starlette  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402


class User:
    is_authenticated = True
    identity = "7"


def starlette_app(rom):
    async def home(request):
        return HTMLResponse("<h1>home</h1>")

    async def api(request):
        return JSONResponse({"ok": True})

    async def text(request):
        return PlainTextResponse("plain")

    async def signup(request):
        track(request, "signup", props={"plan": "pro"})
        return HTMLResponse("<p>welcome</p>", status_code=201)

    async def purchase(request):
        request.scope["user"] = User()
        track(request, "purchase", revenue={"amount": 9.99, "currency": "EUR"})
        return JSONResponse({"ok": True})

    return Starlette(
        routes=[
            Route("/", home),
            Route("/api", api),
            Route("/text", text),
            Route("/signup", signup, methods=["POST"]),
            Route("/purchase", purchase, methods=["POST"]),
        ],
        middleware=[Middleware(RunomineMiddleware, client=rom, skip_paths=["/docs"])],
    )


def call(app, method, url, **kwargs):
    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            return await http.request(method, url, headers={"user-agent": UA}, **kwargs)

    return asyncio.run(go())


def test_starlette_pageviews_and_filters(rom, transport):
    app = starlette_app(rom)
    assert call(app, "GET", "/?utm_campaign=october").status_code == 200
    assert call(app, "GET", "/api").status_code == 200
    assert call(app, "GET", "/text").status_code == 200
    assert call(app, "GET", "/nope").status_code == 404
    assert call(app, "POST", "/").status_code == 405
    events = sent(rom, transport)
    assert [e["url"] for e in events] == ["http://testserver/?utm_campaign=october"]
    assert events[0]["user_agent"] == UA


def test_starlette_track_helper(rom, transport):
    app = starlette_app(rom)
    assert call(app, "POST", "/signup").status_code == 201
    assert call(app, "POST", "/purchase").status_code == 200
    signup, purchase = sent(rom, transport)
    assert signup["name"] == "signup" and signup["props"] == {"plan": "pro"}
    assert purchase["revenue"] == {"amount": 9.99, "currency": "EUR"}
    assert purchase["user"] == "7"
    assert signup["visitor"] == purchase["visitor"]


def test_fastapi_style_add_middleware(rom, transport):
    async def home(request):
        return HTMLResponse("<h1>home</h1>")

    app = Starlette(routes=[Route("/", home)])
    app.add_middleware(RunomineMiddleware, client=rom)
    call(app, "GET", "/")
    assert len(sent(rom, transport)) == 1


def test_checkout_metadata_from_a_scope_and_a_starlette_request(rom):
    import runomine
    from runomine.asgi import checkout_metadata
    from starlette.requests import Request

    scope = http_scope(path="/checkout", method="POST")
    scope["runomine"] = rom
    expected = visitor_hash(TOKEN, "203.0.113.9", UA, "example.com")
    assert checkout_metadata(scope) == {"runomine_visitor": expected}
    assert checkout_metadata(Request(scope), user=42) == {"runomine_visitor": expected, "runomine_user": "42"}
    scope["user"] = User()
    assert checkout_metadata(Request(scope)) == {"runomine_visitor": expected, "runomine_user": "7"}
    assert runomine.checkout_metadata(Request(scope), "acct_9") == {"runomine_visitor": expected, "runomine_user": "acct_9"}
    assert checkout_metadata(None, user=5) == {"runomine_user": "5"}


def test_checkout_metadata_without_a_token_or_an_ip(rom):
    from runomine.asgi import checkout_metadata

    scope = http_scope(client=None)
    scope["runomine"] = rom
    assert checkout_metadata(scope, user="u1") == {"runomine_user": "u1"}
    scope = http_scope()
    scope["runomine"] = Client(token=None)
    assert checkout_metadata(scope, user="u1") == {"runomine_user": "u1"}
    assert checkout_metadata(scope) == {}
