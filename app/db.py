"""SQLite data layer. One table, opened per-operation — boring on purpose.

Connections are short-lived (open, do one thing, close). At this service's
scale that is far cheaper than managing a pool, and it sidesteps every
threading question. WAL mode keeps readers from blocking the odd write.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS slugs (
    name            TEXT PRIMARY KEY,
    forge           TEXT,                -- which forge repo_id belongs to (e.g. 'github'); NULL for admin-reserved rows
    repo_id         INTEGER,             -- forge-native numeric repo id; NULL for admin-reserved rows
    destination_url TEXT,                -- set only for admin-reserved custom targets
    status          TEXT NOT NULL CHECK (status IN ('pending', 'active', 'reserved', 'tombstoned')),
    claim_token     TEXT,                -- set only while pending; the right to activate the name
    expires_at      TEXT,                -- set only while pending; after it the claim occupies nothing
    created_at      TEXT NOT NULL,
    created_by      TEXT,
    notes           TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def in_minutes_iso(minutes: int) -> str:
    """A timestamp `minutes` from now, in the same format as now_iso() — the two
    are compared lexicographically, which holds while the format is identical."""
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(
        timespec="seconds"
    )


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
        return now_iso()

    def get(self, name: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM slugs WHERE name = ?", (name,)).fetchone()

    def drop_expired_claim(self, name: str) -> bool:
        """Remove the name's claim if it has run out, so the write that follows
        sees a free name. Call before anything that needs to occupy a name."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM slugs WHERE name = ? AND status = 'pending'"
                " AND expires_at < ?",
                (name, self._now()),
            )
            return cur.rowcount > 0

    def create_pending(self, name: str, claim_token: str, expires_at: str) -> None:
        """Hold a name for whoever presents `claim_token`, until `expires_at`.
        Raises SlugExists if the name is occupied — the PRIMARY KEY is the
        arbiter, so two simultaneous claims cannot both succeed."""
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO slugs (name, status, claim_token, expires_at,"
                    " created_at) VALUES (?, 'pending', ?, ?, ?)",
                    (name, claim_token, expires_at, self._now()),
                )
        except sqlite3.IntegrityError:
            raise SlugExists(name) from None

    def activate(self, name: str, forge: str, repo_id: int, claim_token: str | None) -> bool:
        """Turn this name's claim into the live campaign it was held for. Returns
        False if the name has no claim matching `claim_token`, which includes a
        claim that ran out and was taken over by someone else."""
        if not claim_token:
            return False
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE slugs SET status = 'active', forge = ?, repo_id = ?,"
                " claim_token = NULL, expires_at = NULL"
                " WHERE name = ? AND status = 'pending' AND claim_token = ?",
                (forge, repo_id, name, claim_token),
            )
            return cur.rowcount > 0

    def release(self, name: str, claim_token: str) -> bool:
        """Give up a claim, freeing the name. Returns False if the name is not
        held under this token."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM slugs WHERE name = ? AND status = 'pending'"
                " AND claim_token = ?",
                (name, claim_token),
            )
            return cur.rowcount > 0

    def create_active(self, name: str, forge: str, repo_id: int) -> None:
        """Register a campaign slug against its (forge, repo_id) — the forge
        qualifies the id so a GitHub id never collides with a GitLab one. Raises
        SlugExists on any collision, including races: the PRIMARY KEY is the
        arbiter, not a prior SELECT."""
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO slugs (name, forge, repo_id, status, created_at)"
                    " VALUES (?, ?, ?, 'active', ?)",
                    (name, forge, repo_id, self._now()),
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
