import pandas as pd

from jobfinder import scraper
from jobfinder.config import SearchSpec
from jobfinder.filters import Job
from jobfinder.scraper import _row_to_job, fetch_jobs, linkedin_says_remote


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

    def test_facet_only_remoteness_is_flagged_verifiable(self):
        assert _row_to_job(_row(location="United States"),
                           assume_remote=True).remote_by_facet is True
        # "remote" in the location backs the facet up; no verification needed.
        assert _row_to_job(_row(location="United States (Remote)"),
                           assume_remote=True).remote_by_facet is False
        assert _row_to_job(_row()).remote_by_facet is False


class TestLinkedinSaysRemote:
    # LinkedIn's remote facet sometimes returns hybrid postings, and the guest
    # job page has no workplace-type field (e.g. linkedin.com/jobs/view/4378701120,
    # hybrid in Austin, alerted as Remote). The guest search endpoint knows the
    # workplace type but nondeterministically ignores f_WT, so the posting must
    # appear under exactly one of remote (f_WT=2) / on-site+hybrid (f_WT=1,3)
    # for a verdict to count.

    ID = "4378701120"

    def _job(self) -> Job:
        return Job(id=f"li-{self.ID}", title="Staff ML Infrastructure Engineer (Compute)",
                   company="General Motors", site="linkedin", remote_by_facet=True)

    def _patch(self, monkeypatch, responses: list[dict]):
        """Each dict maps f_wt -> id-set for one attempt's pair of queries."""
        monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
        rounds = iter(responses)
        current = {}

        def fake(keywords, f_wt=None):
            nonlocal current
            if f_wt == "2":
                current = next(rounds)
            return current[f_wt]

        monkeypatch.setattr(scraper, "_guest_search_ids", fake)

    def test_only_in_onsite_facet_means_hybrid(self, monkeypatch):
        self._patch(monkeypatch, [{"2": {"999"}, "1,3": {self.ID, "999"}}])
        assert linkedin_says_remote(self._job()) is False

    def test_only_in_remote_facet_confirms_remote(self, monkeypatch):
        self._patch(monkeypatch, [{"2": {self.ID}, "1,3": set()}])
        assert linkedin_says_remote(self._job()) is True

    def test_facet_ignored_response_is_retried(self, monkeypatch):
        # First round: server ignored f_WT and returned the same generic set
        # for both queries; only the consistent second round yields a verdict.
        generic = {self.ID, "111", "222"}
        self._patch(monkeypatch, [{"2": generic, "1,3": generic},
                                  {"2": set(), "1,3": {self.ID}}])
        assert linkedin_says_remote(self._job(), attempts=2) is False

    def test_unfindable_posting_gives_no_verdict(self, monkeypatch):
        self._patch(monkeypatch, [{"2": {"999"}, "1,3": {"999"}}] * 3)
        assert linkedin_says_remote(self._job()) is None

    def test_request_failure_gives_no_verdict(self, monkeypatch):
        self._patch(monkeypatch, [{"2": None, "1,3": None}] * 3)
        assert linkedin_says_remote(self._job()) is None
        self._patch(monkeypatch, [{"2": {self.ID}, "1,3": None}] * 3)
        assert linkedin_says_remote(self._job()) is None


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
