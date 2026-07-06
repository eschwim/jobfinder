"""SQLite persistence: jobs already alerted on, and per-site health counters."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .filters import Job

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_jobs (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    company     TEXT,
    site        TEXT,
    url         TEXT,
    first_seen  TEXT,
    fingerprint TEXT
);
CREATE INDEX IF NOT EXISTS seen_jobs_fingerprint ON seen_jobs (fingerprint);
CREATE TABLE IF NOT EXISTS site_health (
    site              TEXT PRIMARY KEY,
    consecutive_empty INTEGER NOT NULL DEFAULT 0
);
"""


def fingerprint(title: str, company: str) -> str | None:
    """Normalized company+title key for spotting reposts of the same role.

    Boards (LinkedIn especially) repost the same job under a fresh id, often
    tagged to a different metro area, so id/url dedupe alone re-alerts on it.
    """
    if not (title.strip() and company.strip()):
        return None
    return re.sub(r"[^a-z0-9]+", " ", f"{company} {title}".lower()).strip()


class Store:
    def __init__(self, path: Path | str, repost_window_days: int = 60):
        self._conn = sqlite3.connect(path)
        self._migrate(self._conn)
        self._conn.executescript(_SCHEMA)
        self.repost_window_days = repost_window_days

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add the fingerprint column to a pre-existing DB and backfill it."""
        cols = [row[1] for row in conn.execute("PRAGMA table_info(seen_jobs)")]
        if not cols or "fingerprint" in cols:
            return
        conn.execute("ALTER TABLE seen_jobs ADD COLUMN fingerprint TEXT")
        for job_id, title, company in conn.execute(
                "SELECT id, title, company FROM seen_jobs").fetchall():
            conn.execute("UPDATE seen_jobs SET fingerprint = ? WHERE id = ?",
                         (fingerprint(title or "", company or ""), job_id))
        conn.commit()

    def is_seen(self, job: Job) -> bool:
        # A fingerprint match only counts within the repost window, so a role
        # reopened after months of silence alerts again. Id matches are
        # permanent: the same posting is never news twice.
        if self.repost_window_days <= 0:
            row = self._conn.execute(
                "SELECT 1 FROM seen_jobs WHERE id = ?", (job.id,)).fetchone()
            return row is not None
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.repost_window_days)
                  ).isoformat(timespec="seconds")
        # "fingerprint = NULL" never matches in SQL, so a job with no usable
        # fingerprint falls back to id-only dedupe.
        row = self._conn.execute(
            "SELECT 1 FROM seen_jobs WHERE id = ? OR (fingerprint = ? AND first_seen >= ?)",
            (job.id, fingerprint(job.title, job.company), cutoff),
        ).fetchone()
        return row is not None

    def mark_seen(self, job: Job) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO seen_jobs (id, title, company, site, url, first_seen, fingerprint)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job.id, job.title, job.company, job.site, job.url,
             datetime.now(timezone.utc).isoformat(timespec="seconds"),
             fingerprint(job.title, job.company)),
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
