"""The suite can only ever run against a *_test database and Redis db 1, and never reads .env."""
import os

import pytest
from sqlalchemy import text

import tests.conftest as root_conftest
from api.config import settings
from api.database import engine


def test_database_is_the_throwaway_test_database():
    assert engine.url.database.endswith("_test"), engine.url.database
    assert engine.url.database != "autobuy_db"
    assert settings.database_url.endswith("/" + engine.url.database)


async def test_redis_is_db_1():
    from api.services.redis_client import get_redis
    r = get_redis()
    assert str(r.connection_pool.connection_kwargs.get("db")) == "1"
    assert root_conftest.redis_db_index(os.environ["REDIS_URL"]) == "1"


@pytest.mark.parametrize("url", ["postgresql+asyncpg://u:p@localhost/autobuy_db", "postgresql+asyncpg://u:p@localhost/autobuy",
                                 "postgresql+asyncpg://u:p@localhost/production_test_backup"])
def test_guard_refuses_non_test_databases(url):
    with pytest.raises(SystemExit, match="must end with '_test'"):
        root_conftest.guard_database_url(url)


@pytest.mark.parametrize("url", ["redis://localhost:6379/0", "redis://localhost:6379", "redis://localhost:6379/2"])
def test_guard_refuses_other_redis_dbs(url):
    with pytest.raises(SystemExit, match="only touch db 1"):
        root_conftest.guard_redis_url(url)


def test_flush_helper_refuses_when_redis_url_is_not_db_1(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    with pytest.raises(SystemExit):
        root_conftest.flush_test_redis()


def test_dotenv_is_not_read_by_the_suite():
    """.env on a dev box carries real Resend/Sumsub values; none may reach the tests."""
    assert os.environ.get("AUTOBUY_SKIP_DOTENV") == "1"
    assert settings.resend_from_email == "" and settings.resend_api_key == "" and settings.sumsub_app_token == ""
    assert settings.api_env == "development" and settings.scheduler_enabled is False


async def test_test_database_is_migrated_to_head():
    async with engine.connect() as conn:
        version = (await conn.execute(text("select version_num from alembic_version"))).scalar()
        tables = {r[0] for r in await conn.execute(text(
            "select table_name from information_schema.tables where table_schema='public'"))}
    assert version and {"users", "purchases", "wallets", "purchase_transactions"} <= tables
