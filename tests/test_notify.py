from jobfinder.filters import Job, Match
from jobfinder.notify import email_bodies, location_str, salary_str


def _match(**kwargs) -> Match:
    defaults = dict(id="1", title="Platform Engineer", company="Acme",
                    url="https://example.com/job/1", location="Seattle, WA")
    defaults.update(kwargs)
    return Match(Job(**defaults))


class TestEmailBodies:
    def test_links_in_both_bodies(self):
        text, html_body = email_bodies([_match()])
        assert "https://example.com/job/1" in text
        assert 'href="https://example.com/job/1"' in html_body

    def test_html_is_escaped(self):
        text, html_body = email_bodies(
            [_match(title="SRE <Staff>", company="A&B Corp")]
        )
        assert "SRE <Staff>" in text
        assert "SRE &lt;Staff&gt;" in html_body
        assert "A&amp;B Corp" in html_body

    def test_salary_included(self):
        m = _match(min_amount=200000, max_amount=250000, interval="yearly", currency="USD")
        text, html_body = email_bodies([m])
        assert "200,000–250,000 USD/yr" in text
        assert "200,000–250,000 USD/yr" in html_body


class TestSalaryStr:
    def test_description_source_flagged(self):
        job = Job(id="1", title="t", company="c", min_amount=208000, max_amount=414000,
                  interval="yearly", currency="USD", salary_source="description")
        assert salary_str(job, unlisted=False) == "208,000–414,000 USD/yr (description)"

    def test_unlisted(self):
        assert salary_str(Job(id="1", title="t", company="c"), unlisted=True) == "salary unlisted"


class TestLocationStr:
    def test_remote_job_labeled_remote_not_city(self):
        job = Job(id="1", title="T", company="C", location="New York, NY", is_remote=True)
        assert location_str(job) == "Remote"

    def test_onsite_job_shows_city(self):
        job = Job(id="1", title="T", company="C", location="Seattle, WA")
        assert location_str(job) == "Seattle, WA"
