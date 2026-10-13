"""api/models/guardian.py and migration 030 describe the same tables.

The worker writes through raw SQL and the API reads through the ORM; the
only thing keeping them honest is that both match the migrated schema. Any
column, type, index or constraint that drifts shows up here as a diff."""
import os

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from api.database import Base
from guardian.db import platform_dsn


def test_orm_mirror_matches_the_migrated_guardian_tables():
    engine = create_engine(platform_dsn(os.environ["DATABASE_URL"]).replace("postgresql://", "postgresql+psycopg2://", 1))
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"compare_type": True, "compare_server_default": False})
            diffs = compare_metadata(ctx, Base.metadata)
    finally:
        engine.dispose()
    guardian_diffs = [d for d in diffs if "guardian_" in repr(d)]
    assert guardian_diffs == [], f"ORM and migration 030 disagree: {guardian_diffs}"


def test_every_guardian_table_is_mirrored():
    from api.models import guardian as models
    expected = {
        "guardian_accounts", "guardian_events", "guardian_known_addresses", "guardian_seen_withdrawals",
        "guardian_balance_snapshots", "guardian_permission_snapshots", "guardian_poll_state", "guardian_alerts",
        "guardian_traded_pairs", "guardian_seen_fills", "guardian_seen_transfers", "guardian_transfer_routes",
        "guardian_operator_pages", "guardian_fee_baseline", "guardian_sms_deliveries", "guardian_snoozes",
        "guardian_expected_activity", "guardian_config_changes", "guardian_alert_triage",
    }
    mirrored = {t.name for t in Base.metadata.sorted_tables if t.name.startswith("guardian_")}
    assert mirrored == expected
    assert models.GuardianAccount.__tablename__ == "guardian_accounts"
