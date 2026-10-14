# runomine for Python

Cookieless analytics that counts money, not just visits. This library sends pageviews and business events
(`signup`, `purchase` with revenue...) from your backend to [runomine](https://runomine.com).

- Python 3.10+, **standard library only** at runtime.
- Django middleware, pure ASGI middleware (FastAPI, Starlette...) and a framework-agnostic client.
- Never blocks a request and never raises: events go into an in-memory queue that a background thread sends in
  batches.

## Install

From PyPI (once published):

```sh
pip install runomine
```

Until then, from this repository (public, no token needed):

```sh
pip install "git+https://github.com/runomine/runomine-python.git"

# pin a branch, tag or commit with @ref
pip install "git+https://github.com/runomine/runomine-python.git@main"

# or from a local clone
pip install /path/to/runomine-python
```

In `requirements.txt`: `runomine @ git+https://github.com/runomine/runomine-python.git@main`. With uv:
`uv add "runomine @ git+https://github.com/runomine/runomine-python.git"`; with Poetry:
`poetry add git+https://github.com/runomine/runomine-python.git`.

## Configure

| Environment variable | Default | |
|---|---|---|
| `RUNOMINE_TOKEN` | (required) | The site's server token, `rm_live_...`. Without it the library is a silent no-op. |
| `RUNOMINE_URL` | `https://runomine.com` | Where events are sent (`/api/v1/events` is appended). |
| `RUNOMINE_ENABLED` | `true` when the token is set | `false`/`0`/`no`/`off` turns it off (e.g. in tests or staging). |

## Django

```python
# settings.py
MIDDLEWARE = [
    # ...
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "runomine.django.RunomineMiddleware",  # after auth, so it can see request.user
]

# All optional: each falls back to the environment variable of the same name.
RUNOMINE_TOKEN = os.environ.get("RUNOMINE_TOKEN")
RUNOMINE_SKIP_PATHS = ["/admin", "/static", "/media", "/healthz"]  # default: /admin, /static, /media
RUNOMINE_IP_HEADER = "X-Forwarded-For"  # behind a proxy/load balancer; "CF-Connecting-IP" behind Cloudflare
```

The middleware records a pageview for every `GET` answered `200` with `text/html`, except skipped paths and
browser prefetches. It takes the IP from `REMOTE_ADDR` (or the first entry of `RUNOMINE_IP_HEADER`), the country
from `CF-IPCountry`/`CloudFront-Viewer-Country`, and the user from `request.user.pk` when authenticated. It works
with sync and async (ASGI) Django.

`RUNOMINE_SKIP_PATHS` entries are path prefixes by segment (`/admin` skips `/admin` and `/admin/...`, not
`/administration`) or `fnmatch` patterns when they contain `*`, `?` or `[`.

Only set `RUNOMINE_IP_HEADER` when a proxy you control sets that header; otherwise visitors could spoof it.

### Events

```python
from runomine.django import track

def signup(request):
    user = create_user(...)
    track(request, "signup", user=user.pk, props={"plan": "free"})
    ...

def checkout_success(request):
    order = ...
    track(request, "purchase",
          revenue={"amount": order.total, "currency": "EUR"},   # major units: 4.99, not 499
          props={"plan": order.plan, "film": order.film.slug})
    ...
```

`user` defaults to `request.user.pk`.

## FastAPI / Starlette (any ASGI app)

```python
from fastapi import FastAPI, Request
from runomine.asgi import RunomineMiddleware, track

app = FastAPI()
app.add_middleware(
    RunomineMiddleware,
    skip_paths=["/docs", "/redoc", "/static"],  # FastAPI's docs pages are HTML too
    ip_header="X-Forwarded-For",                # only behind a proxy you control
    # token="rm_live_...",                      # default: RUNOMINE_TOKEN
    # get_user=lambda scope: scope["session"].get("user_id"),
)

@app.post("/signup")
async def signup(request: Request):
    user = await create_user(...)
    track(request, "signup", user=user.id)
    ...

@app.post("/checkout/complete")
async def complete(request: Request):
    track(request, "purchase", revenue=(19.0, "EUR"), user=current_user.id, props={"plan": "pro"})
    ...
```

The middleware is pure ASGI (no Starlette import). It wraps `send` to see the status and content type and only
records once the last body chunk has gone out, so the response is never delayed. The user defaults to Starlette's
`scope["user"].identity` when `AuthenticationMiddleware` is in place; pass `get_user` otherwise.

## Flask (or anything else)

There is no Flask module; the client takes a `RequestContext`:

```python
from flask import Flask, request
from runomine import Client, RequestContext

rom = Client()  # reads RUNOMINE_TOKEN
app = Flask(__name__)

def rom_context():
    return RequestContext(url=request.url, ip=request.remote_addr, user_agent=request.user_agent.string,
                          host=request.host, referrer=request.referrer, country=request.headers.get("CF-IPCountry"))

@app.after_request
def rom_pageview(response):
    if request.method == "GET" and response.status_code == 200 and response.mimetype == "text/html":
        rom.pageview(request=rom_context())
    return response

# in a view: rom.track("signup", user=user.id, request=rom_context())
```

## Events without a request (webhooks, jobs)

With a request, the event's `visitor` is hashed from it. With no request behind it (a Stripe webhook, a queued
job), the event is sent **without** a `visitor` and runomine attributes it through `user`; the library never
invents a placeholder visitor. You can call `runomine.track("renewal", user=user_id)` as is.

To keep the browser's visitor anyway, capture it while you still have the request and pass it along:

```python
from runomine.django import visitor_for   # or runomine.asgi.visitor_for
import runomine

# in the view
export_report.delay(report.pk, user_id=request.user.pk, visitor=visitor_for(request))

# in the job
runomine.track("report_exported", user=user_id, visitor=visitor)
```

`user` is what links an event to that person's earlier visits. Pageviews always carry a `visitor`. For payments,
see the next section: runomine reads them from your payment provider.

## Revenue from your payment provider

runomine takes revenue from your payment provider, not from prices in your code: connect Stripe, Paddle, Lemon
Squeezy, Shopify or PayPal in the site's settings → **Integrations**, and runomine reads every sale, renewal and
refund from it. To attribute a sale to the visit and the person behind it, pass two identifiers when you create the
checkout; runomine reads them back from the provider.

```python
from runomine.django import checkout_metadata   # or runomine.asgi, or runomine.checkout_metadata for either

checkout_metadata(request)            # {"runomine_visitor": "5f9e1bebe31e8bf5", "runomine_user": "42"}
checkout_metadata(request, team.pk)   # your own id instead of request.user.pk

# Flask or anything else: the client takes a RequestContext
rom.checkout_metadata(rom_context(), user=current_user.id)
```

`runomine_visitor` is the request's visitor hash and `runomine_user` your user id, as a string. Only keys with a
value are returned: without a token or the request's IP there is no visitor (never a made-up one), and with nobody
logged in there is no user.

Once a provider is connected, **don't also send `revenue`** with `track("purchase", ...)`: it would be counted twice.
Send the event without revenue, or not at all.

**Stripe Checkout**: `client_reference_id` is used as the user when `runomine_user` is missing. For subscriptions,
also put the metadata on the subscription so renewals carry the user.

```python
metadata = checkout_metadata(request)

session = stripe.checkout.Session.create(
    mode="subscription",
    line_items=[{"price": "price_123", "quantity": 1}],
    success_url="https://example.com/billing/done",
    client_reference_id=str(request.user.pk),
    metadata=metadata,
    subscription_data={"metadata": metadata},  # renewals
)

# Without Checkout, on the PaymentIntent:
stripe.PaymentIntent.create(amount=499, currency="eur", metadata=metadata)
```

**Paddle Billing**: render the values into the page and pass them as `customData` to Paddle.js, or as `custom_data`
when you create the transaction through the API.

```html
{{ rom_metadata|json_script:"rom-metadata" }}  {# rom_metadata = checkout_metadata(request), from the view #}
<script>
  Paddle.Checkout.open({
    items: [{ priceId: "pri_123", quantity: 1 }],
    customData: JSON.parse(document.getElementById("rom-metadata").textContent),
  });
</script>
```

**Lemon Squeezy**: `checkout_data.custom` when you create the checkout through the API, or URL parameters on a
checkout link (`?checkout[custom][runomine_visitor]=...&checkout[custom][runomine_user]=...`).

```python
"attributes": {"checkout_data": {"custom": checkout_metadata(request)}},
```

**Shopify** (headless, Storefront API cart): cart attributes, which become the order's note attributes.

```python
attributes = [{"key": k, "value": v} for k, v in checkout_metadata(request).items()]
# cartCreate(input: { lines: [...], attributes: $attributes })
```

**PayPal Orders v2**: `custom_id` on the purchase unit, as a query string (127 characters at most).

```python
from urllib.parse import urlencode

"purchase_units": [{
    "amount": {"currency_code": "EUR", "value": "4.99"},
    "custom_id": urlencode(checkout_metadata(request)),
}],
```

## The client

```python
import runomine

client = runomine.Client(token="rm_live_...", url=None, enabled=None)   # None means "from the environment"
client.pageview("https://example.com/pricing", ip=ip, user_agent=ua, referrer=ref, country="ES", user="42")
client.track("purchase", props={"plan": "rent"}, revenue=(4.99, "EUR"), user="42", request=ctx)
client.flush(timeout=5)   # send now; True when the queue drained
client.shutdown()         # flush and stop; also runs automatically at exit

runomine.track(...)                 # the same on a process-wide client (the one Django configured, or from env)
runomine.visitor_hash(token, ip, user_agent, host, day=None)   # the contract hash, exposed and tested
```

`revenue` can be `{"amount": 4.99, "currency": "EUR"}`, `(4.99, "EUR")` or a bare amount (EUR). `props` is a
flat dict of up to 20 string or number values. Event names match `[a-z0-9_.:-]{1,64}`.

Sending: events are queued in memory (bounded at 10,000; the oldest are dropped first) and posted to
`/api/v1/events` every 2 seconds, as soon as 500 are waiting, and at process exit, at most 500 per request.
`5xx`, `429` and network errors are retried twice with backoff (honouring `Retry-After`), then the batch is dropped
quietly. Every event has a UUID v7 `id`, so a retried batch is stored once. Errors are logged on the `runomine`
logger, never raised. The sender is restarted after a fork (gunicorn, uWSGI).

## Privacy

- No cookies, no local storage, nothing is set in the browser.
- The visitor's IP **never leaves your server**. It is only an input to the visitor hash:
  `visitor = sha256(hmac_sha256(token, "runomine:" + utc_day) + "|" + ip + "|" + user_agent + "|" + host)[:16]`.
  The salt changes every UTC day and per site, so a person counts once per day and site and cannot be followed
  across days or sites.
- The user agent is sent with pageviews so runomine can drop bots and count device families; it is then discarded.
- No source, bot or device classification happens in the library; that lives in runomine, once.

## Development

```sh
pip install -e '.[test]'
pytest -q

# or in a clean container
docker run --rm -v "$PWD":/src:ro python:3.12 sh /src/scripts/docker-test.sh
```

## License

MIT. See [LICENSE](LICENSE).
