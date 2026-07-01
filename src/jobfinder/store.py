"""SQLite persistence: jobs already alerted on, and per-site health counters."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .filters import Job

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_jobs (
    id         TEXT PRIMARY KEY,
    title      TEXT,
    company    TEXT,
    site       TEXT,
    url        TEXT,
    first_seen TEXT
);
CREATE TABLE IF NOT EXISTS site_health (
    site              TEXT PRIMARY KEY,
    consecutive_empty INTEGER NOT NULL DEFAULT 0
);
"""


class Store:
    def __init__(self, path: Path | str):
        self._conn = sqlite3.connect(path)
        self._conn.executescript(_SCHEMA)

    def is_seen(self, job_id: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM seen_jobs WHERE id = ?", (job_id,)).fetchone()
        return row is not None

    def mark_seen(self, job: Job) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO seen_jobs (id, title, company, site, url, first_seen)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (job.id, job.title, job.company, job.site, job.url,
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        self._conn.commit()

    def record_site_count(self, site: str, count: int) -> int:
        """Record a run's result count for a site; return its consecutive empty-run streak."""
        streak = 0 if count > 0 else self._empty_streak(site) + 1
        self._conn.execute(
            "INSERT INTO site_health (site, consecutive_empty) VALUES (?, ?)"
            " ON CONFLICT(site) DO UPDATE SET consecutive_empty = excluded.consecutive_empty",
            (site, streak),
        )
        self._conn.commit()
        return streak

    def _empty_streak(self, site: str) -> int:
        row = self._conn.execute(
            "SELECT consecutive_empty FROM site_health WHERE site = ?", (site,)
        ).fetchone()
        return row[0] if row else 0

    def close(self) -> None:
        self._conn.close()
