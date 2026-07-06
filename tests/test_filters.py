import re

from jobfinder.config import Filters, SalaryFilter
from jobfinder.filters import (Job, describes_hybrid, evaluate, parse_salary_text,
                               yearly_salary_range)


def _loc(*exprs) -> list[re.Pattern]:
    return [re.compile(e, re.IGNORECASE) for e in exprs]


def _job(**kwargs) -> Job:
    defaults = dict(id="1", title="Platform Engineer", company="Acme")
    defaults.update(kwargs)
    return Job(**defaults)


def _filters(**kwargs) -> Filters:
    return Filters(**kwargs)


class TestTitle:
    def test_include_regex_must_match(self):
        f = _filters(title_include=[re.compile(r"(?i)platform|sre")])
        assert evaluate(_job(title="Senior Platform Engineer"), f)
        assert evaluate(_job(title="Frontend Developer"), f) is None

    def test_exclude_wins_over_include(self):
        f = _filters(
            title_include=[re.compile(r"(?i)engineer")],
            title_exclude=[re.compile(r"(?i)intern")],
        )
        assert evaluate(_job(title="Engineering Intern"), f) is None

    def test_no_patterns_passes_everything(self):
        assert evaluate(_job(title="Anything"), _filters())


class TestEmployer:
    def test_blocklist_is_case_insensitive_whole_word(self):
        f = _filters(employer_exclude=["meta"])
        assert evaluate(_job(company="Meta Platforms, Inc."), f) is None
        assert evaluate(_job(company="META"), f) is None
        assert evaluate(_job(company="Metabase"), f)


class TestLocation:
    def test_allowlist_regex(self):
        f = _filters(locations_allow=_loc("san francisco"))
        assert evaluate(_job(location="San Francisco, CA"), f)
        assert evaluate(_job(location="Austin, TX"), f) is None

    def test_state_abbreviation_pattern(self):
        f = _filters(locations_allow=_loc(r",\s*(wa|or)\b"))
        assert evaluate(_job(location="Portland, OR"), f)
        assert evaluate(_job(location="Kirkland, WA"), f)
        assert evaluate(_job(location="Austin, TX"), f) is None
        assert evaluate(_job(location="Warsaw, Poland"), f) is None

    def test_remote_allowed_via_flag(self):
        f = _filters(locations_allow=_loc("remote"))
        assert evaluate(_job(location=None, is_remote=True), f)
        assert evaluate(_job(location="Austin, TX", is_remote=False), f) is None

    def test_empty_allowlist_passes_everything(self):
        assert evaluate(_job(location="Anywhere"), _filters())


class TestSalary:
    def test_yearly_normalization(self):
        hourly = _job(min_amount=30, max_amount=40, interval="hourly")
        assert yearly_salary_range(hourly) == (62400, 83200)
        monthly = _job(min_amount=10000, interval="monthly")
        assert yearly_salary_range(monthly) == (120000, None)

    def test_min_filter(self):
        f = _filters(salary=SalaryFilter(min=150000))
        assert evaluate(_job(min_amount=140000, max_amount=180000, interval="yearly"), f)
        assert evaluate(_job(min_amount=90000, max_amount=120000, interval="yearly"), f) is None
        assert evaluate(_job(min_amount=30, max_amount=40, interval="hourly"), f) is None

    def test_max_filter(self):
        f = _filters(salary=SalaryFilter(max=200000))
        assert evaluate(_job(min_amount=180000, max_amount=250000, interval="yearly"), f)
        assert evaluate(_job(min_amount=220000, max_amount=300000, interval="yearly"), f) is None

    def test_unlisted_salary_policy(self):
        keep = _filters(salary=SalaryFilter(min=150000, keep_unlisted=True))
        match = evaluate(_job(), keep)
        assert match and match.salary_unlisted
        drop = _filters(salary=SalaryFilter(min=150000, keep_unlisted=False))
        assert evaluate(_job(), drop) is None

    def test_foreign_currency_treated_as_unlisted(self):
        f = _filters(salary=SalaryFilter(min=150000, currency="USD", keep_unlisted=False))
        job = _job(min_amount=200000, max_amount=250000, interval="yearly", currency="CAD")
        assert evaluate(job, f) is None

    def test_no_salary_filter_ignores_amounts(self):
        assert evaluate(_job(min_amount=1, max_amount=2, interval="yearly"), _filters())


class TestParseSalaryText:
    def test_multi_level_usd_ranges(self):
        text = ("The base salary range is 208,000 USD - 333,500 USD for Level 5, "
                "and 256,000 USD - 414,000 USD for Level 6.")
        assert parse_salary_text(text) == (208000, 414000)

    def test_decimal_amounts(self):
        text = ("The projected compensation range for this position is "
                "$61,900.00 to $141,000.00 (annualized USD)")
        assert parse_salary_text(text) == (61900, 141000)

    def test_markdown_escaped_separator(self):
        text = r"The base salary range is 208,000 USD \- 333,500 USD for Level 5\."
        assert parse_salary_text(text) == (208000, 333500)

    def test_dollar_k_range(self):
        assert parse_salary_text("Compensation: $150k-200k plus equity") == (150000, 200000)

    def test_between_and_phrasing(self):
        # linkedin.com/jobs/view/4433913187 — salary only in the description, joined by "and"
        text = ("the pay range for this position at commencement of employment is "
                "expected to be between $104,400 and $171,000/year.")
        assert parse_salary_text(text) == (104400, 171000)

    def test_hourly_range(self):
        assert parse_salary_text("Pay: $75 to $90 per hour") == (75 * 2080, 90 * 2080)

    def test_years_of_experience_not_mistaken_for_pay(self):
        assert parse_salary_text("Requires 3-5 years of experience") is None

    def test_unmarked_numbers_ignored(self):
        assert parse_salary_text("Team of 10-20 engineers, 100-200 servers") is None

    def test_no_text(self):
        assert parse_salary_text("We offer competitive compensation.") is None


class TestDescribesHybrid:
    def test_hybrid_work_arrangements(self):
        for text in [
            "We've adopted a flexible hybrid working environment",
            "This role is hybrid",
            "hybrid schedule with 3 days in the office",
            "you'll spend 2 days per week on-site",
            "expected in the office 3 days a week... 3 days at our office",
        ]:
            assert describes_hybrid(text), text

    def test_hybrid_cloud_is_not_a_work_arrangement(self):
        for text in [
            "experience operating hybrid cloud environments",
            "hybrid on-prem/AWS infrastructure",
            "our office has an on-site gym",
        ]:
            assert not describes_hybrid(text), text
