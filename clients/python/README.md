# autobuy-client

Type-safe Python client for the **AutoBuy Crypto API**. Types (`TypedDict`s)
and resource methods are generated from the API's OpenAPI spec
(`clients/openapi.json`); the HTTP layer handles bearer auth and transparent
JWT refresh. Sync and asyncio clients share the same surface.

- Built on `httpx`; Python ≥ 3.9, ships `py.typed`
- `AutoBuyClient` (blocking) and `AsyncAutoBuyClient` (`async/await`)
- Automatic refresh on 401 (single-flight in the async client), `on_tokens_changed` hook for persistence
- Typed errors: `ApiError`, `AuthenticationError`, `RateLimitError` (`retry_after`), `ValidationError` (`errors`)

## Installation

```bash
pip install autobuy-client
# from this repo (until published):
pip install ./clients/python
# or with Poetry inside clients/python:
poetry install
```

## Usage

```python
from autobuy_client import AutoBuyClient, ApiError, RateLimitError

client = AutoBuyClient("http://localhost:8000",
                       on_tokens_changed=lambda tokens: save_somewhere(tokens))

# 1. Register + login
client.register("me@example.com", "Str0ng!Passw0rd")
client.login("me@example.com", "Str0ng!Passw0rd")

# 2. Profile
me = client.me()                                   # UserProfile (TypedDict)

# 3. Schedule a recurring buy (requires verified KYC + a verified exchange key)
purchase = client.purchases.create_purchase({
    "crypto": "BTC",
    "amount_usdt": 50,
    "frequency": "weekly",                         # daily | weekly | biweekly | monthly
})

# 4. History + dashboard
page  = client.transactions.list_transactions(limit=20, offset=0)
stats = client.dashboard.quick_stats()

# 5. Errors
try:
    client.purchases.get_purchase("does-not-exist")
except RateLimitError as e:
    print("retry in", e.retry_after, "s")
except ApiError as e:
    print(e.status, e.detail)

client.logout()
```

### asyncio

```python
from autobuy_client import AsyncAutoBuyClient

async with AsyncAutoBuyClient("http://localhost:8000") as client:
    await client.login("me@example.com", "Str0ng!Passw0rd")
    stats = await client.dashboard.quick_stats()
```

Restore a session with `AutoBuyClient(url, tokens=saved)` or `client.set_tokens(saved)`.

### Resource groups

`client.auth`, `client.user`, `client.exchange`, `client.purchases`,
`client.transactions`, `client.dashboard`, `client.analytics`, `client.reports`,
`client.integrations`, `client.notifications`, `client.advanced`, `client.kyc`,
`client.admin` — one method per endpoint (snake_case of the API handler name),
see `autobuy_client/api.py`. Request bodies are plain dicts typed as the
matching `TypedDict` from `autobuy_client.types`.

Anything else: `client.transport.request(method, path, body=..., query=..., auth=True)`.

## Development

```bash
pip install -e ".[dev]"     # or: poetry install
pytest                      # mock transport, no network
```

Regenerate `autobuy_client/types.py` and `autobuy_client/api.py` after an API change:

```bash
python clients/generate.py --url http://localhost:8000/openapi.json
```
