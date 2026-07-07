"""SQLite data layer. One table, opened per-operation — boring on purpose.

Connections are short-lived (open, do one thing, close). At this service's
scale that is far cheaper than managing a pool, and it sidesteps every
threading question. WAL mode keeps readers from blocking the odd write.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS slugs (
    name            TEXT PRIMARY KEY,
    campaign_id     TEXT,                -- NULL for admin-reserved rows
    destination_url TEXT,                -- set only for admin-reserved custom targets
    status          TEXT NOT NULL CHECK (status IN ('active', 'reserved', 'tombstoned')),
    created_at      TEXT NOT NULL,
    created_by      TEXT,
    notes           TEXT
);
"""


class SlugExists(Exception):
    """The name is already occupied (any status)."""


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            with conn:  # one transaction per operation
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def get(self, name: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM slugs WHERE name = ?", (name,)).fetchone()

    def create_active(self, name: str, campaign_id: str) -> None:
        """Register a campaign slug. Raises SlugExists on any collision,
        including races: the PRIMARY KEY is the arbiter, not a prior SELECT."""
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO slugs (name, campaign_id, status, created_at)"
                    " VALUES (?, ?, 'active', ?)",
                    (name, campaign_id, self._now()),
                )
        except sqlite3.IntegrityError:
            raise SlugExists(name) from None

    def create_reserved(
        self, name: str, destination_url: str, created_by: str | None, notes: str | None
    ) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO slugs (name, destination_url, status, created_at,"
                    " created_by, notes) VALUES (?, ?, 'reserved', ?, ?, ?)",
                    (name, destination_url, self._now(), created_by, notes),
                )
        except sqlite3.IntegrityError:
            raise SlugExists(name) from None

    def tombstone(self, name: str, notes: str | None) -> bool:
        """Mark a slug tombstoned, keeping the row so the name stays occupied.
        Returns False if no such slug exists."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE slugs SET status = 'tombstoned',"
                " notes = COALESCE(?, notes) WHERE name = ?",
                (notes, name),
            )
            return cur.rowcount > 0

    def list_all(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM slugs ORDER BY created_at").fetchall()
