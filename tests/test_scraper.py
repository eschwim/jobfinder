import pandas as pd

from jobfinder import scraper
from jobfinder.config import SearchSpec
from jobfinder.scraper import _row_to_job, fetch_jobs


def _row(**kwargs) -> dict:
    row = {"id": "1", "title": "Systems Engineer", "company": "Acme",
           "job_url": "https://example.com/1", "location": "Chantilly, VA"}
    row.update(kwargs)
    return row


class TestIsRemote:
    def test_site_flag_alone_is_not_trusted(self):
        # JobSpy sets is_remote from description text ("remote sensing", "remote
        # sites"), which must not mark an on-site job as remote.
        assert _row_to_job(_row(is_remote=True)).is_remote is False

    def test_remote_in_location(self):
        assert _row_to_job(_row(location="United States (Remote)")).is_remote is True

    def test_remote_in_title(self):
        assert _row_to_job(_row(title="Platform Engineer - Remote")).is_remote is True

    def test_remote_search_facet_is_trusted(self):
        # Jobs from an is_remote search were filtered by the board's structured
        # remote facet, so they count as remote even when tagged "United States".
        job = _row_to_job(_row(location="United States"), assume_remote=True)
        assert job.is_remote is True

    def test_hybrid_description_overrides_facet(self):
        # LinkedIn's remote facet sometimes returns hybrid postings
        # (e.g. linkedin.com/jobs/view/4435019581, "Hybrid" in Brooklyn NY).
        job = _row_to_job(
            _row(location="Brooklyn, NY",
                 description="We've adopted a flexible hybrid working environment "
                             "(2-3 days a week in the office depending on the role)."),
            assume_remote=True)
        assert job.is_remote is False

    def test_hybrid_cloud_description_does_not_override_facet(self):
        job = _row_to_job(
            _row(location="United States",
                 description="You will operate our hybrid cloud environment across "
                             "AWS and on-prem datacenters."),
            assume_remote=True)
        assert job.is_remote is True

    def test_remote_in_title_beats_hybrid_description(self):
        job = _row_to_job(
            _row(title="Platform Engineer (Remote)",
                 description="Some teams follow a hybrid schedule."),
            assume_remote=True)
        assert job.is_remote is True


class TestSalaryEnrichment:
    def test_salary_parsed_from_description(self):
        job = _row_to_job(_row(description="Pay: $200,000 - $250,000 per year"))
        assert (job.min_amount, job.max_amount) == (200000, 250000)
        assert job.salary_source == "description"

    def test_structured_salary_not_overwritten(self):
        job = _row_to_job(_row(min_amount=180000, max_amount=220000,
                               description="Pay: $1 - $2"))
        assert (job.min_amount, job.max_amount) == (180000, 220000)


class TestDescriptionRetry:
    # JobSpy swallows LinkedIn detail-fetch failures (timeout/429/signup wall)
    # and returns no description, hiding a listed salary
    # (e.g. linkedin.com/jobs/view/4414690402, "Salary: $220,000-$225,000").

    def _search(self) -> SearchSpec:
        return SearchSpec(name="t", sites=["linkedin"], search_term="sre",
                          fetch_descriptions=True)

    def test_missing_description_refetched(self, monkeypatch):
        rows = [_row(description=None, job_url="https://www.linkedin.com/jobs/view/1")]
        monkeypatch.setattr(scraper, "scrape_jobs", lambda **kw: pd.DataFrame(rows))
        monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
        monkeypatch.setattr(scraper, "_fetch_linkedin_description",
                            lambda url: "Salary: $220,000-$225,000")
        jobs, _ = fetch_jobs(self._search())
        assert (jobs[0].min_amount, jobs[0].max_amount) == (220000, 225000)
        assert jobs[0].salary_source == "description"

    def test_present_description_not_refetched(self, monkeypatch):
        rows = [_row(description="Pay: $200,000 - $250,000 per year")]
        monkeypatch.setattr(scraper, "scrape_jobs", lambda **kw: pd.DataFrame(rows))
        monkeypatch.setattr(scraper, "_fetch_linkedin_description",
                            lambda url: (_ for _ in ()).throw(AssertionError("refetched")))
        jobs, _ = fetch_jobs(self._search())
        assert (jobs[0].min_amount, jobs[0].max_amount) == (200000, 250000)

    def test_retry_failure_leaves_job_unlisted(self, monkeypatch):
        rows = [_row(description=None, job_url="https://www.linkedin.com/jobs/view/1")]
        monkeypatch.setattr(scraper, "scrape_jobs", lambda **kw: pd.DataFrame(rows))
        monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
        monkeypatch.setattr(scraper, "_fetch_linkedin_description", lambda url: None)
        jobs, _ = fetch_jobs(self._search())
        assert (jobs[0].min_amount, jobs[0].max_amount) == (None, None)
