# Changelog

## 0.2.0

- Page views take `props`, with the same rules as events (flat, up to 20 keys, string or number values):
  `pageview(..., props={...})`, `request.runomine_props` in a Django view, `request.state.runomine_props` (or the
  `get_props` option) with the ASGI middleware. runomine stores them as the page view's dims.
- Samples, an app's own ops numbers for runomine's Live page: `Client.sample(data, ts=None)` and
  `Client.samples([...])` (also `runomine.sample()` / `runomine.samples()`), sent at once to `POST /api/v1/samples`.
  They return `True` when runomine took the call.

## 0.1.0

- First release: client (pageviews, events with props and revenue, background batching, retries, the contract's
  visitor hash, checkout metadata), Django middleware and helpers, pure ASGI middleware (FastAPI/Starlette).
