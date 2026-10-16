"""Postgres storage for the Guardian engine: the worker's side of the schema.

Guardian ran on a raw-SQL SQLite wrapper (`sentinel/db.py`); the engine, the
detectors and their tests speak to it through one contract, `cursor()`, and
read rows by column name. That contract is kept exactly, on psycopg2 against
the platform's Postgres, so the ported engine code and its tests are the
proof that detection did not change.

The dialect boundary lives here, per connection, so no caller has to know:

* **Timestamps** come back as ISO-8601 strings, the shape the engine wrote
  (`datetime.now(timezone.utc).isoformat()`), because five readers
  `datetime.fromisoformat()` them and the digest slices one. The columns are
  timestamptz, so the API side gets real datetimes through SQLAlchemy.
* **jsonb** comes back as the raw JSON text, because the readers
  `json.loads()` it themselves; writers pass `json.dumps()` text and Postgres
  casts the literal.
* **numeric** comes back as Decimal (psycopg2's default); every reader already
  goes through `numeric.to_decimal`.
* **uuid** (`guardian_accounts.user_id`) adapts both ways.
* Every connection runs in UTC.

What changed from SQLite: `?` became `%s` and `cur.lastrowid` became
`RETURNING id` at the five insert sites; `INSERT OR IGNORE` became
`ON CONFLICT DO NOTHING`. The schema is Alembic's (migration 030), not this
module's: there is no `_init_schema` and no ALTER list.

Connections are per thread, like before (the engine runs a worker pool), and
`cursor()` is one transaction: committed on a clean exit, rolled back on an
exception. The SQLite write lock is gone; Postgres serialises writers itself
and `Engine._claim` already serialises per-account work. A connection that
died between polls (a Postgres restart during a deploy) is replaced once.
"""
from __future__ import annotations

import contextlib
import logging
import re
import threading
from contextlib import contextmanager
from typing import Iterator

import psycopg2
import psycopg2.extensions
import psycopg2.extras

logger = logging.getLogger("guardian.db")

# Postgres type OIDs for the casters below (stable since forever).
_OID_TIMESTAMPTZ = 1184
_OID_TIMESTAMP = 1114

psycopg2.extras.register_uuid()


def platform_dsn(url: str | None = None) -> str:
    """The platform's DATABASE_URL as a plain libpq DSN. The API uses the
    asyncpg driver; the worker is synchronous and uses psycopg2, which is
    already in the image (Alembic and the scripts use it)."""
    if url is None:
        from api.config import settings
        url = settings.database_url
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url


def _iso_timestamp(value, cursor):
    """timestamptz/timestamp -> ISO-8601 text, the shape the engine wrote."""
    if value is None:
        return None
    parsed = psycopg2.extensions.PYDATETIMETZ(value, cursor)
    return parsed.isoformat()


_ISO_TIMESTAMP = psycopg2.extensions.new_type((_OID_TIMESTAMPTZ, _OID_TIMESTAMP), "GUARDIAN_ISO_TIMESTAMP", _iso_timestamp)


# Tables whose rows have a generated `id`; a plain INSERT into one of them gets
# `RETURNING id` appended so sqlite3's `cur.lastrowid` keeps working.
_TABLES_WITH_ID = frozenset(f"guardian_{t}" for t in (
    "accounts", "events", "known_addresses", "seen_withdrawals", "balance_snapshots", "permission_snapshots",
    "alerts", "traded_pairs", "seen_fills", "seen_transfers", "transfer_routes", "operator_pages",
    "sms_deliveries", "snoozes", "expected_activity", "config_changes"))
_INSERT_RE = re.compile(r"^\s*INSERT\s+INTO\s+(guardian_[a-z_]+)", re.IGNORECASE)


class _Cursor:
    """A psycopg2 cursor that still answers `lastrowid` after an INSERT.

    Guardian's engine and, above all, its tests read `cur.lastrowid` after
    inserting; psycopg2 has no such thing. A plain INSERT into a table with an
    id gets `RETURNING id` appended and the id is kept here; a statement that
    already says RETURNING is left alone (the caller fetches it). Everything
    else is the real cursor."""

    def __init__(self, cur):
        self._cur = cur
        self.lastrowid = None

    def execute(self, sql, params=None):
        match = _INSERT_RE.match(sql)
        if match and match.group(1).lower() in _TABLES_WITH_ID and "returning" not in sql.lower():
            self._cur.execute(sql.rstrip().rstrip(";") + " RETURNING id", params)
            row = self._cur.fetchone()
            self.lastrowid = row["id"] if row else None
            return
        self._cur.execute(sql, params)
        self.lastrowid = None

    def executemany(self, sql, seq):
        self.lastrowid = None
        return self._cur.executemany(sql, seq)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class Database:
    """Thread-local psycopg2 connections with the `cursor()` contract the
    engine was written against. `dsn` defaults to the platform's
    DATABASE_URL; tests pass their own."""

    CONNECT_TIMEOUT_S = 10

    def __init__(self, dsn: str | None = None):
        self._dsn = platform_dsn(dsn)
        self._local = threading.local()
        self._all_conns: list = []
        self._conns_lock = threading.Lock()
        # Fail at construction, not at the first poll: a worker that cannot
        # reach its database must not start and look healthy.
        self._connection()

    def _new_connection(self):
        conn = psycopg2.connect(self._dsn, connect_timeout=self.CONNECT_TIMEOUT_S,
                                cursor_factory=psycopg2.extras.RealDictCursor)
        psycopg2.extensions.register_type(_ISO_TIMESTAMP, conn)
        psycopg2.extras.register_default_jsonb(conn_or_curs=conn, loads=lambda s: s)
        psycopg2.extras.register_default_json(conn_or_curs=conn, loads=lambda s: s)
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
        conn.commit()
        with self._conns_lock:
            self._all_conns.append(conn)
        return conn

    def _connection(self):
        conn = getattr(self._local, "conn", None)
        if conn is None or conn.closed:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    def _replace_connection(self):
        """After an OperationalError: drop this thread's connection and open a
        fresh one. Once; a database that stays down surfaces as a failed poll,
        which the breaker and the heartbeat report, rather than a hang."""
        old = getattr(self._local, "conn", None)
        if old is not None:
            with contextlib.suppress(Exception):
                old.close()
            with self._conns_lock:
                with contextlib.suppress(ValueError):
                    self._all_conns.remove(old)
        self._local.conn = None
        return self._connection()

    @contextmanager
    def cursor(self) -> Iterator[_Cursor]:
        """A cursor on this thread's connection: one transaction, committed on
        clean exit, rolled back on an exception. Rows are dict-like (`row["col"]`,
        `row.keys()`, `dict(row)`), as the sqlite3.Row readers expect."""
        conn = self._connection()
        try:
            cur = conn.cursor()
        except psycopg2.OperationalError:
            conn = self._replace_connection()
            cur = conn.cursor()
        try:
            yield _Cursor(cur)
            conn.commit()
        except psycopg2.OperationalError:
            # The connection itself is gone (restart, network): nothing to roll
            # back on it. Replace it so the NEXT poll works, then re-raise so
            # THIS one is recorded as a failure.
            with contextlib.suppress(Exception):
                conn.rollback()
            self._replace_connection()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            with contextlib.suppress(Exception):
                cur.close()

    def close(self) -> None:
        """Closes every connection this database handed out, not just the
        caller's, so worker threads do not leak sockets."""
        with self._conns_lock:
            conns, self._all_conns = self._all_conns, []
        for conn in conns:
            with contextlib.suppress(Exception):
                conn.close()
        self._local = threading.local()
