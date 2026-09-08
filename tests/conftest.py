"""Root test configuration: environment defaults that must be in place before
`api.database` is imported by any test module (tests/api has its own conftest
with the same defaults for when that subtree is run on its own)."""
import os

os.environ.setdefault("RATE_LIMIT_ENABLED", "0")
os.environ.setdefault("AUTOBUY_DB_POOL", "null")   # pooled asyncpg conns pin to one event loop
os.environ.setdefault("RESEND_API_KEY", "")        # never send real email from tests
