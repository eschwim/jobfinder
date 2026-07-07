"""SQLite persistence: jobs already alerted on, and per-site health counters."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .filters import Job, Match

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_jobs (
    id              TEXT PRIMARY KEY,
    title           TEXT,
    company         TEXT,
    site            TEXT,
    url             TEXT,
    first_seen      TEXT,
    fingerprint     TEXT,
    matched         INTEGER NOT NULL DEFAULT 0,
    matched_at      TEXT,
    location        TEXT,
    is_remote       INTEGER,
    min_amount      REAL,
    max_amount      REAL,
    salary_interval TEXT,
    currency        TEXT,
    salary_source   TEXT,
    salary_unlisted INTEGER,
    date_posted     TEXT
);
CREATE INDEX IF NOT EXISTS seen_jobs_fingerprint ON seen_jobs (fingerprint);
CREATE INDEX IF NOT EXISTS seen_jobs_matched_at ON seen_jobs (matched_at) WHERE matched = 1;
CREATE TABLE IF NOT EXISTS site_health (
    site              TEXT PRIMARY KEY,
    consecutive_empty INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    matches     INTEGER,
    error       TEXT
);
"""

# Columns added after the initial release, applied to pre-existing DBs with
# ALTER TABLE. SQLite can't add a column with a non-constant default via
# ALTER, so `matched` gets its DEFAULT 0 here too (constant, allowed).
_SEEN_JOBS_UPGRADES = [
    ("fingerprint", "TEXT"),
    ("matched", "INTEGER NOT NULL DEFAULT 0"),
    ("matched_at", "TEXT"),
    ("location", "TEXT"),
    ("is_remote", "INTEGER"),
    ("min_amount", "REAL"),
    ("max_amount", "REAL"),
    ("salary_interval", "TEXT"),
    ("currency", "TEXT"),
    ("salary_source", "TEXT"),
    ("salary_unlisted", "INTEGER"),
    ("date_posted", "TEXT"),
]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        # check_same_thread=False: FastAPI resolves sync dependencies in a
        # threadpool, so a per-request Store may be created and used on
        # different threads. Each Store is still used by one request at a time.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Readers (web requests) shouldn't block while a poll cycle writes.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate(self._conn)
        self._conn.executescript(_SCHEMA)
        self.repost_window_days = repost_window_days

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns introduced since a pre-existing DB was created."""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(seen_jobs)")}
        if not cols:
            return  # fresh DB; _SCHEMA creates everything
        backfill_fingerprint = "fingerprint" not in cols
        for name, decl in _SEEN_JOBS_UPGRADES:
            if name not in cols:
                conn.execute(f"ALTER TABLE seen_jobs ADD COLUMN {name} {decl}")
        if backfill_fingerprint:
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
             _utcnow(), fingerprint(job.title, job.company)),
        )
        self._conn.commit()

    def mark_matched(self, match: Match) -> None:
        """Record an alerted match with the details the web UI displays."""
        job = match.job
        now = _utcnow()
        self._conn.execute(
            "INSERT INTO seen_jobs (id, title, company, site, url, first_seen, fingerprint,"
            " matched, matched_at, location, is_remote, min_amount, max_amount,"
            " salary_interval, currency, salary_source, salary_unlisted, date_posted)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " matched = 1, matched_at = excluded.matched_at,"
            " location = excluded.location, is_remote = excluded.is_remote,"
            " min_amount = excluded.min_amount, max_amount = excluded.max_amount,"
            " salary_interval = excluded.salary_interval, currency = excluded.currency,"
            " salary_source = excluded.salary_source,"
            " salary_unlisted = excluded.salary_unlisted,"
            " date_posted = excluded.date_posted",
            (job.id, job.title, job.company, job.site, job.url, now,
             fingerprint(job.title, job.company), now, job.location,
             int(job.is_remote), job.min_amount, job.max_amount, job.interval,
             job.currency, job.salary_source, int(match.salary_unlisted),
             job.date_posted),
        )
        self._conn.commit()

    def recent_matches(self, since: datetime | None = None) -> list[sqlite3.Row]:
        """Matched jobs, newest first; `since` bounds matched_at (None = all)."""
        if since is None:
            return self._conn.execute(
                "SELECT * FROM seen_jobs WHERE matched = 1"
                " ORDER BY matched_at DESC").fetchall()
        return self._conn.execute(
            "SELECT * FROM seen_jobs WHERE matched = 1 AND matched_at >= ?"
            " ORDER BY matched_at DESC",
            (since.astimezone(timezone.utc).isoformat(timespec="seconds"),),
        ).fetchall()

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

    def site_health(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT site, consecutive_empty FROM site_health ORDER BY site").fetchall()

    def record_run_start(self) -> int:
        cur = self._conn.execute(
            "INSERT INTO runs (started_at) VALUES (?)", (_utcnow(),))
        self._conn.commit()
        return cur.lastrowid

    def record_run_end(self, run_id: int, matches: int, error: str | None = None) -> None:
        self._conn.execute(
            "UPDATE runs SET finished_at = ?, matches = ?, error = ? WHERE id = ?",
            (_utcnow(), matches, error, run_id))
        self._conn.commit()

    def last_run(self) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()

    def _empty_streak(self, site: str) -> int:
        row = self._conn.execute(
            "SELECT consecutive_empty FROM site_health WHERE site = ?", (site,)
        ).fetchone()
        return row[0] if row else 0

    def close(self) -> None:
        self._conn.close()
