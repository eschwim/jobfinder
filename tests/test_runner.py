import re
from types import SimpleNamespace

import jobfinder.runner as runner
from jobfinder.config import Filters, parse_config
from jobfinder.filters import Job
from jobfinder.notify import Notifier
from jobfinder.runner import _needs_remote_verification, run_once
from jobfinder.store import Store


def _cfg(*location_exprs: str) -> SimpleNamespace:
    return SimpleNamespace(filters=Filters(
        locations_allow=[re.compile(e, re.IGNORECASE) for e in location_exprs]))


def _job(**kwargs) -> Job:
    defaults = dict(id="li-1", title="Platform Engineer", company="Acme",
                    site="linkedin", location="Austin, TX", remote_by_facet=True)
    defaults.update(kwargs)
    return Job(**defaults)


class TestNeedsRemoteVerification:
    def test_facet_remote_job_outside_allowed_locations(self):
        assert _needs_remote_verification(_job(), _cfg("seattle", "remote"))

    def test_allowed_location_needs_no_verification(self):
        # A Seattle-tagged job matches even if it turns out hybrid.
        job = _job(location="Seattle, WA")
        assert not _needs_remote_verification(job, _cfg("seattle", "remote"))

    def test_text_backed_remoteness_is_trusted(self):
        job = _job(location="United States (Remote)", remote_by_facet=False)
        assert not _needs_remote_verification(job, _cfg("seattle", "remote"))

    def test_non_linkedin_job_skipped(self):
        assert not _needs_remote_verification(_job(site="indeed"), _cfg("remote"))

    def test_no_location_filter_means_no_verification(self):
        assert not _needs_remote_verification(_job(), _cfg())


def _app_cfg(**overrides):
    raw = {
        "searches": [{"name": "test", "sites": ["indeed"],
                      "search_term": "platform engineer"}],
        "filters": {"title_include": ["(?i)platform"]},
        "notify": {"channels": ["pushover"]},
    }
    raw.update(overrides)
    return parse_config(raw)


class TestRunOnce:
    def _run(self, monkeypatch, jobs, cfg=None, store=None, dry_run=False,
             counts=None):
        cfg = cfg or _app_cfg()
        store = store or Store(":memory:")
        monkeypatch.setattr(runner, "fetch_jobs",
                            lambda search: (jobs, counts or {"indeed": len(jobs)}))
        notifier = Notifier([], dry_run=True)
        return run_once(cfg, store, notifier, dry_run=dry_run), store

    def test_match_is_persisted_with_details(self, monkeypatch):
        job = _job(site="indeed", location="Oakland, CA", remote_by_facet=False,
                   min_amount=180000, max_amount=220000, interval="yearly")
        result, store = self._run(monkeypatch, [job])
        assert result.match_count == 1
        assert result.error is None
        row = store.recent_matches()[0]
        assert row["title"] == "Platform Engineer"
        assert row["min_amount"] == 180000
        assert row["matched"] == 1

    def test_non_matching_title_filtered_out(self, monkeypatch):
        job = _job(site="indeed", title="Accountant", remote_by_facet=False)
        result, store = self._run(monkeypatch, [job])
        assert result.match_count == 0
        assert store.recent_matches() == []

    def test_seen_job_skipped_but_remarked(self, monkeypatch):
        job = _job(site="indeed", remote_by_facet=False)
        store = Store(":memory:")
        store.mark_seen(job)
        result, store = self._run(monkeypatch, [job], store=store)
        assert result.match_count == 0

    def test_dry_run_persists_nothing(self, monkeypatch):
        job = _job(site="indeed", remote_by_facet=False)
        result, store = self._run(monkeypatch, [job], dry_run=True)
        assert result.match_count == 1
        assert store.recent_matches() == []
        assert store.last_run() is None

    def test_run_recorded_in_runs_table(self, monkeypatch):
        result, store = self._run(monkeypatch, [])
        row = store.last_run()
        assert row["matches"] == 0
        assert row["error"] is None
        assert row["finished_at"] is not None
        assert result.site_totals == {"indeed": 0}

    def test_facet_remote_dropped_when_linkedin_disagrees(self, monkeypatch):
        cfg = _app_cfg(filters={"title_include": ["(?i)platform"],
                                "locations_allow": ["remote"]})
        job = _job(location="Austin, TX", is_remote=True, remote_by_facet=True)
        monkeypatch.setattr(runner, "linkedin_says_remote", lambda j: False)
        result, store = self._run(monkeypatch, [job], cfg=cfg)
        assert result.match_count == 0
        assert store.recent_matches() == []
