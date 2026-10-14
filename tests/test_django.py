import asyncio

import pytest
from conftest import TOKEN, FakeTransport

django = pytest.importorskip("django")

from django.conf import settings  # noqa: E402

if not settings.configured:
    settings.configure(
        DEBUG=False,
        SECRET_KEY="test",
        ALLOWED_HOSTS=["*"],
        ROOT_URLCONF=__name__,
        MIDDLEWARE=["runomine.django.RunomineMiddleware"],
        INSTALLED_APPS=[],
        RUNOMINE_SKIP_PATHS=["/admin", "/healthz", "/internal/*"],
    )
    django.setup()

from django.http import HttpResponse, JsonResponse  # noqa: E402
from django.test import Client as DjangoTestClient  # noqa: E402
from django.test import RequestFactory, override_settings  # noqa: E402
from django.urls import path  # noqa: E402

import runomine  # noqa: E402
import runomine.django as rd  # noqa: E402
from runomine import Client, visitor_hash  # noqa: E402

UA = "Mozilla/5.0 test"


def html(request):
    return HttpResponse("<h1>hi</h1>")


def api(request):
    return JsonResponse({"ok": True})


def missing(request):
    return HttpResponse("<h1>nope</h1>", status=404)


def signup(request):
    rd.track(request, "signup", props={"plan": "pro"})
    return HttpResponse("<p>welcome</p>")


urlpatterns = [
    path("", html),
    path("pricing", html),
    path("admin/", html),
    path("healthz", html),
    path("internal/x", html),
    path("api", api),
    path("missing", missing),
    path("signup", signup),
]


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture(autouse=True)
def rom(transport):
    client = Client(token=TOKEN, url="https://runomine.test", transport=transport, flush_interval=60, backoff=0.01)
    rd.set_client(client)
    yield client
    client.shutdown(1)
    rd.set_client(None)


def sent(rom, transport):
    assert rom.flush(2)
    return transport.events


def test_records_html_get(rom, transport):
    response = DjangoTestClient(HTTP_USER_AGENT=UA).get(
        "/pricing?utm_source=newsletter", HTTP_REFERER="https://www.google.com/", HTTP_CF_IPCOUNTRY="es"
    )
    assert response.status_code == 200
    [event] = sent(rom, transport)
    assert event["type"] == "pageview"
    assert event["url"] == "http://testserver/pricing?utm_source=newsletter"
    assert event["referrer"] == "https://www.google.com/"
    assert event["user_agent"] == UA
    assert event["country"] == "ES"
    assert event["visitor"] == visitor_hash(TOKEN, "127.0.0.1", UA, "testserver")
    assert "user" not in event


@pytest.mark.parametrize(
    "method,url,headers",
    [
        ("post", "/", {}),
        ("head", "/", {}),
        ("get", "/api", {}),
        ("get", "/missing", {}),
        ("get", "/admin/", {}),
        ("get", "/healthz", {}),
        ("get", "/internal/x", {}),
        ("get", "/", {"HTTP_SEC_PURPOSE": "prefetch;prerender"}),
        ("get", "/", {"HTTP_PURPOSE": "prefetch"}),
    ],
)
def test_filters(rom, transport, method, url, headers):
    getattr(DjangoTestClient(), method)(url, **headers)
    assert sent(rom, transport) == []


def test_country_xx_is_dropped(rom, transport):
    DjangoTestClient().get("/", HTTP_CF_IPCOUNTRY="XX")
    [event] = sent(rom, transport)
    assert "country" not in event


def test_ip_header_behind_proxy(rom, transport):
    with override_settings(RUNOMINE_IP_HEADER="X-Forwarded-For"):
        DjangoTestClient(HTTP_USER_AGENT=UA).get("/", HTTP_X_FORWARDED_FOR="198.51.100.7, 10.0.0.1")
    [event] = sent(rom, transport)
    assert event["visitor"] == visitor_hash(TOKEN, "198.51.100.7", UA, "testserver")


def test_track_helper_in_view(rom, transport):
    DjangoTestClient(HTTP_USER_AGENT=UA).get("/signup")
    events = sent(rom, transport)
    assert [e["type"] for e in events] == ["event", "pageview"]
    assert events[0]["name"] == "signup"
    assert events[0]["props"] == {"plan": "pro"}
    assert events[0]["visitor"] == events[1]["visitor"]


class FakeUser:
    is_authenticated = True
    pk = 42


class Anonymous:
    is_authenticated = False
    pk = None


def test_user_from_request_user(rom, transport):
    middleware = rd.RunomineMiddleware(html)
    request = RequestFactory().get("/", HTTP_USER_AGENT=UA)
    request.user = FakeUser()
    middleware(request)
    anon = RequestFactory().get("/")
    anon.user = Anonymous()
    middleware(anon)
    first, second = sent(rom, transport)
    assert first["user"] == "42"
    assert "user" not in second


def test_track_helper_user_and_revenue(rom, transport):
    request = RequestFactory().get("/checkout/success", HTTP_USER_AGENT=UA)
    request.user = FakeUser()
    rd.track(request, "purchase", revenue={"amount": 4.99, "currency": "EUR"}, props={"plan": "rent"})
    rd.track(request, "purchase", user="override")
    first, second = sent(rom, transport)
    assert first["user"] == "42"
    assert first["revenue"] == {"amount": 4.99, "currency": "EUR"}
    assert first["url"] == "http://testserver/checkout/success"
    assert second["user"] == "override"
    assert rd.visitor_for(request) == first["visitor"]


def test_track_without_request_omits_visitor(rom, transport):
    rd.track(None, "renewal", user="7", revenue=(9.99, "EUR"))
    [event] = sent(rom, transport)
    assert "visitor" not in event and event["user"] == "7"


def test_never_breaks_the_request(rom, transport):
    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    rom.pageview = explode
    response = DjangoTestClient().get("/")
    assert response.status_code == 200
    rd.track(object(), "x")  # not a request at all: still no exception


def test_disabled_client_records_nothing(transport):
    rd.set_client(Client(token=None, transport=transport))
    response = DjangoTestClient().get("/")
    assert response.status_code == 200
    assert transport.calls == []


def test_async_middleware(rom, transport):
    async def view(request):
        return HttpResponse("<p>async</p>")

    middleware = rd.RunomineMiddleware(view)
    request = RequestFactory().get("/async", HTTP_USER_AGENT=UA)
    request.user = FakeUser()
    response = asyncio.run(middleware(request))
    assert response.status_code == 200
    [event] = sent(rom, transport)
    assert event["url"] == "http://testserver/async" and event["user"] == "42"


def test_client_from_settings(monkeypatch):
    rd.set_client(None)
    with override_settings(RUNOMINE_TOKEN="rm_live_fromsettings", RUNOMINE_URL="https://rm.example"):
        client = rd.get_client()
    assert client.token == "rm_live_fromsettings"
    assert client.endpoint == "https://rm.example/api/v1/events"
    assert runomine.get_client() is client  # runomine.track uses the same client


def test_checkout_metadata(rom):
    request = RequestFactory().post("/checkout", HTTP_USER_AGENT=UA, REMOTE_ADDR="203.0.113.9")
    request.user = FakeUser()
    expected = visitor_hash(TOKEN, "203.0.113.9", UA, "testserver")
    assert rd.checkout_metadata(request) == {"runomine_visitor": expected, "runomine_user": "42"}
    assert rd.checkout_metadata(request, user=7) == {"runomine_visitor": expected, "runomine_user": "7"}
    assert runomine.checkout_metadata(request) == {"runomine_visitor": expected, "runomine_user": "42"}
    anon = RequestFactory().get("/checkout", HTTP_USER_AGENT=UA, REMOTE_ADDR="203.0.113.9")
    anon.user = Anonymous()
    assert rd.checkout_metadata(anon) == {"runomine_visitor": expected}
    assert rd.checkout_metadata(None, user="7") == {"runomine_user": "7"}
    assert rd.checkout_metadata(object(), user=1) == {"runomine_user": "1"}


def test_checkout_metadata_with_a_disabled_client(transport):
    rd.set_client(Client(token=None, transport=transport))
    request = RequestFactory().get("/checkout", HTTP_USER_AGENT=UA)
    request.user = FakeUser()
    assert rd.checkout_metadata(request) == {"runomine_user": "42"}
