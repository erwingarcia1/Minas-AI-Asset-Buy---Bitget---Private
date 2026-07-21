"""Durable SQLite journal for the meme-buy pipeline.

Its own database file under ``data/`` — deliberately NOT the webapp's
registry.db, so a webapp migration, wipe, or restore can never reset the
rolling daily spend cap.  sqlite3 is stdlib; no new dependency.

Two tables:
  meme_buy_runs    one row per attempted buy, keyed by a UNIQUE client_oid so a
                   crash-restart can look the run up instead of resending.
  meme_buy_events  the before/after journal: two rows per stage, written before
                   the stage acts and after it returns.

Spend accounting: a run counts against the rolling 24h cap from the moment
``mark_committed`` is called — which happens *before* the first money-moving
request, not after it succeeds.  A request that times out may still have been
executed by the exchange, so it must count.  Dry-run rows never count.

Bitget wallet API referenced by the fields stored here:
  https://www.bitget.com/api-doc/spot/account/Wallet-Transfer
  https://www.bitget.com/api-doc/spot/account/Wallet-Withdrawal
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

DEFAULT_DB_PATH = os.path.join("data", "meme_buy.sqlite3")

DAY_SECONDS = 24 * 60 * 60

# Columns `advance`/`complete` are allowed to write.  A whitelist, so a typo in
# a caller kwarg raises instead of silently vanishing.
_UPDATABLE = (
    "stage",
    "chain",
    "token",
    "usdt_amount",
    "transfer_client_oid",
    "withdraw_client_oid",
    "transfer_id",
    "withdraw_id",
    "withdraw_tx_hash",
    "swap_tx_hash",
    "gate_verdict",
    "status",
    "error",
    "committed_at",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meme_buy_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_oid          TEXT    NOT NULL UNIQUE,
    created_at          REAL    NOT NULL,
    created_at_iso      TEXT    NOT NULL,
    updated_at          REAL    NOT NULL,
    committed_at        REAL,
    dry_run             INTEGER NOT NULL DEFAULT 1,
    stage               INTEGER NOT NULL DEFAULT 0,
    chain               TEXT,
    token               TEXT,
    usdt_amount         REAL,
    transfer_client_oid TEXT,
    withdraw_client_oid TEXT,
    transfer_id         TEXT,
    withdraw_id         TEXT,
    withdraw_tx_hash    TEXT,
    swap_tx_hash        TEXT,
    gate_verdict        TEXT,
    status              TEXT    NOT NULL DEFAULT 'pending',
    error               TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_committed ON meme_buy_runs (committed_at);

CREATE TABLE IF NOT EXISTS meme_buy_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   INTEGER NOT NULL,
    ts       REAL    NOT NULL,
    ts_iso   TEXT    NOT NULL,
    stage    INTEGER NOT NULL,
    phase    TEXT    NOT NULL,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run ON meme_buy_events (run_id);
"""


class StateError(Exception):
    """Journal failure.  Callers treat this as fail-closed: do not spend."""


class DuplicateClientOid(StateError):
    """A run with this client_oid already exists — resume, never resend."""


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class MemeBuyState:
    """Thin, synchronous journal.  One short-lived connection per operation."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH, clock=time.time) -> None:
        self.db_path = db_path
        self._clock = clock
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ---------- internal ----------

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")  # a lost journal write == a lost oid
            yield conn
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise StateError(str(exc)) from exc
        finally:
            conn.close()

    def _update(self, run_id: int, **fields: Any) -> None:
        unknown = [k for k in fields if k not in _UPDATABLE]
        if unknown:
            raise StateError("not updatable: %s" % ", ".join(sorted(unknown)))
        now = self._clock()
        sets = ", ".join("%s = ?" % k for k in fields)
        params = list(fields.values()) + [now, run_id]
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE meme_buy_runs SET %s, updated_at = ? WHERE id = ?" % sets,
                params,
            )
            if cur.rowcount != 1:
                raise StateError("run %r not found" % (run_id,))

    # ---------- public: lifecycle ----------

    def begin_run(
        self,
        client_oid: str,
        chain: str,
        token: str,
        usdt_amount: float,
        dry_run: bool = True,
    ) -> int:
        """Insert the run row.  Raises DuplicateClientOid if it already exists."""
        if not client_oid:
            raise StateError("client_oid is required")
        now = self._clock()
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "INSERT INTO meme_buy_runs (client_oid, created_at, created_at_iso,"
                    " updated_at, dry_run, stage, chain, token, usdt_amount, status)"
                    " VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, 'pending')",
                    (
                        client_oid,
                        now,
                        _iso(now),
                        now,
                        1 if dry_run else 0,
                        chain,
                        token,
                        float(usdt_amount),
                    ),
                )
                return int(cur.lastrowid)
        except StateError as exc:
            if "UNIQUE" in str(exc).upper():
                raise DuplicateClientOid(client_oid) from exc
            raise

    def journal(
        self, run_id: int, stage: int, phase: str, detail: Any = None
    ) -> None:
        """Append a before/after event.  `phase` is 'begin' or 'end'."""
        now = self._clock()
        blob = None if detail is None else json.dumps(detail, default=str)[:8000]
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO meme_buy_events (run_id, ts, ts_iso, stage, phase, detail)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, now, _iso(now), int(stage), str(phase), blob),
            )

    def advance(self, run_id: int, stage: int, **fields: Any) -> None:
        """Move to `stage`, persist any produced ids, journal the 'end' event."""
        fields.setdefault("status", "running")
        self._update(run_id, stage=int(stage), **fields)
        self.journal(run_id, stage, "end", fields or None)

    def mark_committed(self, run_id: int) -> None:
        """Record that a money-moving request is about to be sent.

        Called BEFORE the request.  From here on the amount counts against the
        daily cap even if the response never arrives.
        """
        self._update(run_id, committed_at=self._clock())

    def fail(
        self,
        run_id: int,
        error: str,
        stage: Optional[int] = None,
        status: str = "failed",
    ) -> None:
        fields: Dict[str, Any] = {"status": str(status), "error": str(error)[:2000]}
        if stage is not None:
            fields["stage"] = int(stage)
        self._update(run_id, **fields)
        self.journal(run_id, stage if stage is not None else -1, "fail", str(error))

    def complete(self, run_id: int, stage: int, **fields: Any) -> None:
        fields.setdefault("status", "completed")
        self._update(run_id, stage=int(stage), **fields)
        self.journal(run_id, stage, "complete", fields)

    # ---------- public: queries ----------

    def spent_last_24h(self, now: Optional[float] = None) -> float:
        """USDT committed to live spends in the rolling 24h window.

        Survives restarts because it is a query over the durable journal, not
        an in-memory counter.  Dry-run rows are excluded: they moved no money.
        """
        cutoff = (self._clock() if now is None else now) - DAY_SECONDS
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(usdt_amount), 0) AS total FROM meme_buy_runs"
                " WHERE dry_run = 0 AND committed_at IS NOT NULL AND committed_at >= ?",
                (cutoff,),
            ).fetchone()
        return float(row["total"] or 0.0)

    def find_run_by_client_oid(self, client_oid: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM meme_buy_runs WHERE client_oid = ?", (client_oid,)
            ).fetchone()
        return dict(row) if row else None

    def get_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM meme_buy_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return dict(row) if row else None

    def events(self, run_id: int) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM meme_buy_events WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [dict(r) for r in rows]
