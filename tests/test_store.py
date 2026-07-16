import sqlite3
from datetime import datetime, timedelta, timezone

from jobfinder.filters import Job, Match
from jobfinder.store import Store, fingerprint


def _backdate(store: Store, job_id: str, days: int) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    store._conn.execute("UPDATE seen_jobs SET first_seen = ? WHERE id = ?", (stamp, job_id))
    store._conn.commit()


def _job(**kwargs) -> Job:
    defaults = dict(id="li-1", title="Site Reliability Engineer", company="Acme",
                    site="linkedin", url="https://www.linkedin.com/jobs/view/1")
    defaults.update(kwargs)
    return Job(**defaults)


class TestFingerprint:
    def test_normalizes_case_and_punctuation(self):
        assert fingerprint("SRE - Platform (Remote)", "Acme, Inc.") == \
            fingerprint("sre platform remote", "acme inc")

    def test_blank_fields_have_no_fingerprint(self):
        assert fingerprint("SRE", "") is None
        assert fingerprint("", "Acme") is None


class TestRepostDedupe:
    # LinkedIn reposts the same role under a fresh job id, often tagged to a
    # different metro; id-only dedupe re-alerts on every repost.

    def test_repost_with_new_id_is_seen(self):
        store = Store(":memory:")
        store.mark_seen(_job(id="li-1"))
        assert store.is_seen(_job(id="li-2", location="Seattle, WA"))

    def test_same_title_different_company_not_seen(self):
        store = Store(":memory:")
        store.mark_seen(_job(id="li-1"))
        assert not store.is_seen(_job(id="li-2", company="Globex"))

    def test_jobs_without_company_fall_back_to_id_dedupe(self):
        store = Store(":memory:")
        store.mark_seen(_job(id="li-1", company=""))
        assert store.is_seen(_job(id="li-1", company=""))
        assert not store.is_seen(_job(id="li-2", company="", title="Other Role"))


class TestRepostWindow:
    def test_repost_outside_window_alerts_again(self):
        store = Store(":memory:", repost_window_days=60)
        store.mark_seen(_job(id="li-1"))
        _backdate(store, "li-1", days=61)
        assert store.is_seen(_job(id="li-1"))  # id dedupe never expires
        assert not store.is_seen(_job(id="li-2"))

    def test_repost_inside_window_suppressed(self):
        store = Store(":memory:", repost_window_days=60)
        store.mark_seen(_job(id="li-1"))
        _backdate(store, "li-1", days=59)
        assert store.is_seen(_job(id="li-2"))

    def test_fresh_sighting_extends_window(self):
        # An actively reposted role stays suppressed: each sighting is recorded,
        # so the window is measured from the last repost, not the first alert.
        store = Store(":memory:", repost_window_days=60)
        store.mark_seen(_job(id="li-1"))
        store.mark_seen(_job(id="li-2"))  # repost sighting, recorded not alerted
        _backdate(store, "li-1", days=90)  # original alert has aged out
        _backdate(store, "li-2", days=2)
        assert store.is_seen(_job(id="li-3"))  # still within li-2's window

    def test_zero_window_disables_repost_detection(self):
        store = Store(":memory:", repost_window_days=0)
        store.mark_seen(_job(id="li-1"))
        assert store.is_seen(_job(id="li-1"))
        assert not store.is_seen(_job(id="li-2"))


class TestMigration:
    def test_old_db_gains_backfilled_fingerprints(self, tmp_path):
        path = tmp_path / "old.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE seen_jobs (id TEXT PRIMARY KEY, title TEXT,"
                     " company TEXT, site TEXT, url TEXT, first_seen TEXT)")
        conn.execute("INSERT INTO seen_jobs VALUES ('li-1', 'Site Reliability Engineer',"
                     " 'Acme', 'linkedin', 'u', '2026-07-01T00:00:00+00:00')")
        conn.commit()
        conn.close()

        store = Store(path)
        assert store.is_seen(_job(id="li-99"))  # repost of the pre-migration row

    def test_old_db_gains_match_columns_and_keeps_rows(self, tmp_path):
        path = tmp_path / "old.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE seen_jobs (id TEXT PRIMARY KEY, title TEXT,"
                     " company TEXT, site TEXT, url TEXT, first_seen TEXT, fingerprint TEXT)")
        conn.execute("INSERT INTO seen_jobs VALUES ('li-1', 'SRE', 'Acme', 'linkedin',"
                     " 'u', '2026-07-01T00:00:00+00:00', 'acme sre')")
        conn.commit()
        conn.close()

        store = Store(path)
        assert store.recent_matches() == []  # legacy rows aren't matches
        row = store._conn.execute("SELECT matched, matched_at FROM seen_jobs"
                                  " WHERE id = 'li-1'").fetchone()
        assert row["matched"] == 0 and row["matched_at"] is None
        store.mark_matched(Match(_job(id="li-2")))
        assert [r["id"] for r in store.recent_matches()] == ["li-2"]


class TestMatches:
    def test_mark_matched_records_details(self):
        store = Store(":memory:")
        job = _job(id="li-1", location="Oakland, CA", is_remote=True,
                   min_amount=150.0, max_amount=180.0, interval="hourly",
                   currency="USD", salary_source="description",
                   date_posted="2026-07-06")
        store.mark_matched(Match(job, salary_unlisted=False))
        row = store.recent_matches()[0]
        assert row["location"] == "Oakland, CA"
        assert row["is_remote"] == 1
        assert (row["min_amount"], row["max_amount"]) == (150.0, 180.0)
        assert row["salary_interval"] == "hourly"
        assert row["salary_unlisted"] == 0
        assert row["date_posted"] == "2026-07-06"
        assert row["matched_at"] is not None and row["first_seen"] is not None

    def test_mark_matched_upgrades_seen_row(self):
        # A row inserted by mark_seen (e.g. an earlier repost sighting) gains
        # match details without losing its original first_seen.
        store = Store(":memory:")
        store.mark_seen(_job(id="li-1"))
        first_seen = store._conn.execute(
            "SELECT first_seen FROM seen_jobs WHERE id = 'li-1'").fetchone()[0]
        store.mark_matched(Match(_job(id="li-1", location="Remote")))
        row = store.recent_matches()[0]
        assert row["matched"] == 1
        assert row["location"] == "Remote"
        assert row["first_seen"] == first_seen

    def test_recent_matches_window(self):
        store = Store(":memory:")
        store.mark_matched(Match(_job(id="old", title="Old Role")))
        store.mark_matched(Match(_job(id="new", title="New Role", company="Globex")))
        stamp = (datetime.now(timezone.utc) - timedelta(hours=30)
                 ).isoformat(timespec="seconds")
        store._conn.execute(
            "UPDATE seen_jobs SET matched_at = ? WHERE id = 'old'", (stamp,))
        store._conn.commit()

        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        assert [r["id"] for r in store.recent_matches(since=cutoff)] == ["new"]
        assert [r["id"] for r in store.recent_matches()] == ["new", "old"]

    def test_matches_sorted_newest_first(self):
        store = Store(":memory:")
        for i, hours_ago in [(1, 5), (2, 1), (3, 10)]:
            store.mark_matched(Match(_job(id=f"li-{i}", title=f"Role {i}",
                                          company=f"Co{i}")))
            stamp = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)
                     ).isoformat(timespec="seconds")
            store._conn.execute(
                "UPDATE seen_jobs SET matched_at = ? WHERE id = ?", (stamp, f"li-{i}"))
        store._conn.commit()
        assert [r["id"] for r in store.recent_matches()] == ["li-2", "li-1", "li-3"]


class TestRuns:
    def test_run_bookkeeping(self):
        store = Store(":memory:")
        assert store.last_run() is None
        run_id = store.record_run_start()
        row = store.last_run()
        assert row["id"] == run_id and row["finished_at"] is None
        store.record_run_end(run_id, matches=3)
        row = store.last_run()
        assert row["matches"] == 3 and row["error"] is None
        assert row["finished_at"] is not None

    def test_run_error_recorded(self):
        store = Store(":memory:")
        run_id = store.record_run_start()
        store.record_run_end(run_id, matches=0, error="boom")
        assert store.last_run()["error"] == "boom"


class TestTailoredResumes:
    def test_lifecycle_pending_to_done(self):
        store = Store(":memory:")
        rid = store.create_tailored("li-1")
        row = store.get_tailored(rid)
        assert row["status"] == "pending" and row["job_id"] == "li-1"
        store.finish_tailored(rid, markdown="# Tailored")
        row = store.get_tailored(rid)
        assert row["status"] == "done"
        assert row["markdown"] == "# Tailored"
        assert row["finished_at"] is not None

    def test_error_recorded(self):
        store = Store(":memory:")
        rid = store.create_tailored("li-1")
        store.finish_tailored(rid, error="boom")
        row = store.get_tailored(rid)
        assert row["status"] == "error" and row["error"] == "boom"

    def test_latest_is_newest_row(self):
        store = Store(":memory:")
        first = store.create_tailored("li-1")
        store.finish_tailored(first, markdown="v1")
        second = store.create_tailored("li-1")
        assert store.latest_tailored("li-1")["id"] == second

    def test_latest_map_one_row_per_job(self):
        store = Store(":memory:")
        store.create_tailored("li-1")
        newest_1 = store.create_tailored("li-1")
        newest_2 = store.create_tailored("li-2")
        result = store.latest_tailored_map(["li-1", "li-2", "li-3"])
        assert result["li-1"]["id"] == newest_1
        assert result["li-2"]["id"] == newest_2
        assert "li-3" not in result
        assert store.latest_tailored_map([]) == {}

    def test_fail_pending_flips_only_pending(self):
        store = Store(":memory:")
        done = store.create_tailored("li-1")
        store.finish_tailored(done, markdown="ok")
        pending = store.create_tailored("li-2")
        assert store.fail_pending_tailored("interrupted") == 1
        assert store.get_tailored(done)["status"] == "done"
        row = store.get_tailored(pending)
        assert row["status"] == "error" and row["error"] == "interrupted"


class TestSiteHealth:
    def test_site_health_lists_streaks(self):
        store = Store(":memory:")
        store.record_site_count("indeed", 5)
        store.record_site_count("linkedin", 0)
        store.record_site_count("linkedin", 0)
        health = {r["site"]: r["consecutive_empty"] for r in store.site_health()}
        assert health == {"indeed": 0, "linkedin": 2}
