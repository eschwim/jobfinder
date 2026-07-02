from jobfinder.scraper import _row_to_job


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


class TestSalaryEnrichment:
    def test_salary_parsed_from_description(self):
        job = _row_to_job(_row(description="Pay: $200,000 - $250,000 per year"))
        assert (job.min_amount, job.max_amount) == (200000, 250000)
        assert job.salary_source == "description"

    def test_structured_salary_not_overwritten(self):
        job = _row_to_job(_row(min_amount=180000, max_amount=220000,
                               description="Pay: $1 - $2"))
        assert (job.min_amount, job.max_amount) == (180000, 220000)
