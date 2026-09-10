import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

# Make "api.*" importable when alembic is run from the project root
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if os.getenv("AUTOBUY_SKIP_DOTENV") != "1":   # tests own DATABASE_URL and skip .env
    load_dotenv()

from api.database import Base  # noqa: E402
import api.models  # noqa: F401,E402  (registers all tables on Base.metadata)

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """DATABASE_URL from the environment, swapped to a sync driver for Alembic."""
    url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://autobuy:autobuy_password@localhost:5432/autobuy_db",
    )
    return url.replace("+asyncpg", "+psycopg2")


def process_revision_directives(context, revision, directives):
    """Skip generating an empty migration when autogenerate finds no changes."""
    if config.cmd_opts is not None and getattr(config.cmd_opts, "autogenerate", False):
        script = directives[0]
        if script.upgrade_ops.is_empty():
            directives[:] = []
            print("No schema changes detected; no revision file created.")


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            process_revision_directives=process_revision_directives,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
