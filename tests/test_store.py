import sqlite3
from datetime import datetime, timedelta, timezone

from jobfinder.filters import Job
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
