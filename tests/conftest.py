"""Root test configuration. Runs before any test module and before any `api.*` import.

Isolation guarantees:

* Tests run only against a throwaway database whose name ends in ``_test``
  (default ``autobuy_test``) and Redis db **1**. The suite refuses to start
  against anything else, so it can never touch ``autobuy_db`` / db 0 that the
  running API uses. Override the targets with ``TEST_DATABASE_URL`` /
  ``TEST_REDIS_URL``; the guards still apply.
* ``.env`` is never read: ``AUTOBUY_SKIP_DOTENV=1`` disables python-dotenv in
  ``api.database`` / ``alembic/env.py`` and pydantic's ``env_file`` in
  ``api.config``. Every setting the tests rely on is assigned here (plain
  assignment, so nothing exported by the shell leaks in either).
* The test database is created if missing and migrated to Alembic head once
  per session. The only sanctioned way to wipe Redis is ``flush_test_redis()``,
  which re-checks the db index before FLUSHDB.

Where a test lives (documented 2026-09-22, audit Batch B4):

* ``tests/api/`` — HTTP-level tests of one router or service through the app,
  using the ``db`` / ``user`` / ``binance_key`` fixtures and ``auth_headers``
  from ``tests/api/conftest.py``. One file per router or service module.
* ``tests/`` (root) — the purchase pipeline end to end: executor, gateway,
  fill guard, withdrawals, holdings, the /api/v2/purchases flows, and the
  cross-cutting guards (env plumbing, public proxy). These use
  ``tests/conftest_autobuy.py`` (``make_purchase``, ``make_key``, ``auth``)
  and ``tests/fake_gateway.py``. Root-level ``test_bitget_client.py`` /
  ``test_binance_client.py`` etc. test the exchange transports at the repo root.
* This file (autouse fixtures) applies to both: Bitget market data is stubbed,
  fake gateway ids reset, and the isolation guards above hold everywhere.
"""
import os
import pathlib

import pytest
from urllib.parse import urlsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or \
    "postgresql+asyncpg://autobuy:autobuy_password@localhost:5432/autobuy_test"
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL") or "redis://localhost:6379/1"


def database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def redis_db_index(url: str) -> str:
    return urlsplit(url).path.lstrip("/") or "0"


def guard_database_url(url: str) -> None:
    """Refuse any database whose name does not end in ``_test``."""
    name = database_name(url)
    if not name.endswith("_test"):
        raise SystemExit(f"refusing to run tests against database {name!r}: the test database name must "
                         "end with '_test' (set TEST_DATABASE_URL to e.g. .../autobuy_test)")


def guard_redis_url(url: str) -> None:
    """Refuse any Redis database other than db 1."""
    db = redis_db_index(url)
    if db != "1":
        raise SystemExit(f"refusing to run tests against Redis db {db}: tests may only touch db 1 "
                         "(set TEST_REDIS_URL to e.g. redis://localhost:6379/1)")


guard_database_url(TEST_DATABASE_URL)
guard_redis_url(TEST_REDIS_URL)

# Everything below is assigned, not setdefault: neither .env nor the shell may
# steer the suite at a different database, sender or environment.
os.environ["AUTOBUY_SKIP_DOTENV"] = "1"
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["REDIS_URL"] = TEST_REDIS_URL
os.environ["API_ENV"] = "development"
os.environ["REQUIRE_PRODUCTION"] = ""
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["AUTOBUY_DRY_RUN"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "0"       # tests that need limits enable them explicitly
os.environ["AUTOBUY_DB_POOL"] = "null"       # pooled asyncpg conns pin to one event loop
os.environ["SECRET_KEY"] = "test-only-secret-key-" + "0" * 40
os.environ["ENCRYPTION_KEY"] = ""
os.environ["ALLOWED_ORIGINS"] = "http://localhost:5173,http://localhost:8000"
for var in ("RESEND_API_KEY", "RESEND_FROM_EMAIL", "RESEND_REPLY_TO", "SUPPORT_EMAIL", "APP_BASE_URL",
            "APP_NAME", "LOG_RESET_LINKS", "SUMSUB_APP_TOKEN", "SUMSUB_SECRET_KEY", "SUMSUB_WEBHOOK_SECRET",
            "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "FCM_SERVER_KEY", "SLACK_WEBHOOK_URL"):
    os.environ[var] = ""


def _ensure_test_database() -> None:
    """Create the *_test database if missing and migrate it to Alembic head."""
    import psycopg2
    from alembic import command
    from alembic.config import Config

    parts = urlsplit(TEST_DATABASE_URL.replace("+asyncpg", ""))
    name = database_name(TEST_DATABASE_URL)
    admin = psycopg2.connect(host=parts.hostname, port=parts.port or 5432, user=parts.username,
                             password=parts.password, dbname="postgres", connect_timeout=10)
    try:
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute("select 1 from pg_database where datname = %s", (name,))
            if cur.fetchone() is None:
                cur.execute(f'create database "{name}"')
    finally:
        admin.close()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")   # alembic/env.py reads DATABASE_URL (set above)


_ensure_test_database()


def flush_test_redis() -> None:
    """Wipe the test Redis. Refuses unless REDIS_URL still points at db 1."""
    import redis
    url = os.environ["REDIS_URL"]
    guard_redis_url(url)
    redis.Redis.from_url(url).flushdb()


# ------------------------------------------------------------------ Bitget market data stub (no network)

SAMPLE_SYMBOL = {"symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "minTradeAmount": "0",
                 "maxTradeAmount": "900000000000000000000", "takerFeeRate": "0.002", "makerFeeRate": "0.002",
                 "pricePrecision": "2", "quantityPrecision": "6", "quotePrecision": "8", "status": "online",
                 "minTradeUSDT": "1", "maxMarketOrderValue": "1000000"}
SAMPLE_CHAINS = {
    "BTC": [{"chain": "BTC", "needTag": "false", "withdrawable": "true", "rechargeable": "true", "withdrawFee": "0.00003",
             "extraWithdrawFee": "0", "withdrawConfirm": "3", "minWithdrawAmount": "0.0005",
             "browserUrl": "https://www.blockchain.com/explorer/transactions/btc/", "contractAddress": None, "withdrawMinScale": "8"},
            {"chain": "BEP20", "needTag": "false", "withdrawable": "true", "rechargeable": "true", "withdrawFee": "0.00000195",
             "extraWithdrawFee": "0", "withdrawConfirm": "60", "minWithdrawAmount": "0.00012961",
             "browserUrl": "https://bscscan.com/tx/", "contractAddress": "0x7130", "withdrawMinScale": "8"},
            {"chain": "LIGHTNING", "needTag": "false", "withdrawable": "false", "rechargeable": "false", "withdrawFee": "0.00001",
             "extraWithdrawFee": "0", "withdrawConfirm": "6", "minWithdrawAmount": "0.00002", "browserUrl": "", "withdrawMinScale": "8"}],
    "ETH": [{"chain": "ERC20", "needTag": "false", "withdrawable": "true", "rechargeable": "true", "withdrawFee": "0.0008",
             "extraWithdrawFee": "0", "withdrawConfirm": "12", "minWithdrawAmount": "0.001", "browserUrl": "https://etherscan.io/tx/", "withdrawMinScale": "8"},
            {"chain": "ArbitrumOne", "needTag": "false", "withdrawable": "true", "rechargeable": "true", "withdrawFee": "0.0001",
             "extraWithdrawFee": "0", "withdrawConfirm": "12", "minWithdrawAmount": "0.001", "browserUrl": "https://arbiscan.io/tx/", "withdrawMinScale": "8"}],
    "USDT": [{"chain": "ERC20", "needTag": "false", "withdrawable": "true", "rechargeable": "true", "withdrawFee": "0.8",
              "extraWithdrawFee": "0", "withdrawConfirm": "96", "minWithdrawAmount": "10", "browserUrl": "https://etherscan.io/tx/", "withdrawMinScale": "6"},
             {"chain": "TRC20", "needTag": "false", "withdrawable": "true", "rechargeable": "true", "withdrawFee": "1.5",
              "extraWithdrawFee": "0", "withdrawConfirm": "3", "minWithdrawAmount": "10", "browserUrl": "https://tronscan.org/#/transaction/", "withdrawMinScale": "6"},
             {"chain": "SOL", "needTag": "false", "withdrawable": "true", "rechargeable": "true", "withdrawFee": "1",
              "extraWithdrawFee": "0", "withdrawConfirm": "5", "minWithdrawAmount": "10", "browserUrl": "https://solscan.io/tx/", "withdrawMinScale": "6"}],
    "USDC": [{"chain": "ERC20", "needTag": "false", "withdrawable": "true", "rechargeable": "true", "withdrawFee": "0.8",
              "extraWithdrawFee": "0", "withdrawConfirm": "96", "minWithdrawAmount": "10", "browserUrl": "https://etherscan.io/tx/", "withdrawMinScale": "6"}],
    "SOL": [{"chain": "SOL", "needTag": "false", "withdrawable": "true", "rechargeable": "true", "withdrawFee": "0.008",
             "extraWithdrawFee": "0", "withdrawConfirm": "5", "minWithdrawAmount": "0.02", "browserUrl": "https://solscan.io/tx/", "withdrawMinScale": "8"}],
    # tag chains, as live Bitget reported them on 2026-09-16 (needTag true)
    "XRP": [{"chain": "XRP", "needTag": "true", "withdrawable": "true", "rechargeable": "true", "withdrawFee": "0.2",
             "extraWithdrawFee": "0", "withdrawConfirm": "1", "minWithdrawAmount": "5", "browserUrl": "https://xrpscan.com/tx/", "withdrawMinScale": "6"}],
    "XLM": [{"chain": "StellarLumens", "needTag": "true", "withdrawable": "true", "rechargeable": "true", "withdrawFee": "0.02",
             "extraWithdrawFee": "0", "withdrawConfirm": "1", "minWithdrawAmount": "57.7433883", "browserUrl": "https://stellar.expert/explorer/public/tx/", "withdrawMinScale": "7"}],
}
SAMPLE_ASK = {"BTC": 77042.84, "ETH": 4000.0, "SOL": 200.0, "USDT": 1.0, "USDC": 1.0, "XRP": 2.5, "XLM": 0.35}


@pytest.fixture(autouse=True)
def stub_bitget_market(monkeypatch):
    """Tests never call Bitget: symbol/chain fetchers and the live ask are stubbed
    with the shapes captured from the real endpoints, and the hour cache is
    bypassed so every test sees its own stub."""
    from api.services import bitget_market as bm

    def fetch_symbol(symbol):
        coin = symbol[:-4] if symbol.endswith("USDT") else symbol
        if coin not in SAMPLE_CHAINS or coin in ("USDT", "USDC"):   # no USDT/USDT or USDC/USDT spot pair
            return None
        return {**SAMPLE_SYMBOL, "symbol": f"{coin}USDT", "baseCoin": coin,
                "pricePrecision": {"BTC": "2", "ETH": "2", "SOL": "2", "XRP": "4", "XLM": "5"}.get(coin, "2")}

    def fetch_chains(coin):
        return [dict(c) for c in SAMPLE_CHAINS.get(coin.upper(), [])]

    monkeypatch.setattr(bm, "fetch_symbol", fetch_symbol)
    monkeypatch.setattr(bm, "fetch_chains", fetch_chains)
    bm.live_ask_unstubbed = bm.live_ask   # for tests that exercise the real price lookup's error handling
    monkeypatch.setattr(bm, "live_ask", lambda coin: SAMPLE_ASK.get(coin.upper(), 100.0))
    bm.cached_real, bm.cached_sync_real = bm._cached, bm._cached_sync   # tests of the cache policy restore these
    # no cache: each test's stub is authoritative (failures still surface as MarketDataUnavailable, as in production)
    def _cached_sync(key, producer):
        try:
            return producer()
        except Exception as exc:
            raise bm.MarketDataUnavailable(f"Bitget market data unavailable for {key}: {exc}") from exc

    async def _cached(key, producer):
        return _cached_sync(key, producer)
    monkeypatch.setattr(bm, "_cached_sync", _cached_sync)
    monkeypatch.setattr(bm, "_cached", _cached)
    # candles (portfolio chart): no cache, and no network unless a test stubs fetch_page itself
    from api.services import candles

    def _cached_page(key, ttl, produce):
        try:
            return produce()
        except Exception as exc:
            raise bm.MarketDataUnavailable(f"Bitget candles unavailable for {key}: {exc}") from exc

    def fetch_page(symbol, granularity, end_ms, rows=candles.PAGE_ROWS):
        raise RuntimeError("candles.fetch_page is not stubbed in this test")
    monkeypatch.setattr(candles, "_cached_page", _cached_page)
    monkeypatch.setattr(candles, "fetch_page", fetch_page)
    yield


@pytest.fixture(autouse=True)
def _reset_fake_gateway_ids():
    """Exchange ids from the fake gateway restart at ORD-1 / WD-1 for every test."""
    from tests.fake_gateway import FakeGateway
    FakeGateway.reset_ids()
    yield
