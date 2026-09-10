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
"""
import os
import pathlib
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
os.environ["ALLOWED_ORIGINS"] = "http://localhost:3000,http://localhost:8000"
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
