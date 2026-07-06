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

    def test_interval_suffix_on_each_amount(self):
        text = "expected to be between $133,200/year and $219,600/year. However,"
        assert parse_salary_text(text) == (133200, 219600)

    def test_usd_prefix_and_yr_suffix(self):
        text = "Target salary range: USD $105,230.00/Yr. - USD $111,000.00/Yr."
        assert parse_salary_text(text) == (105230, 111000)

    def test_hourly_suffix_without_spaces(self):
        assert parse_salary_text("estimated to be $55.00/hr-$60.00/hr") == \
            (55 * 2080, 60 * 2080)

    def test_k_suffix_qualifies_both_endpoints(self):
        assert parse_salary_text("Salary will be in the $100-115k range plus bonus") == \
            (100000, 115000)

    def test_labeled_min_max_pair(self):
        text = "office. Minimum Salary: $120,000 Maximum Salary: $175,000 The minimum"
        assert parse_salary_text(text) == (120000, 175000)

    def test_labeled_min_max_rate_annually(self):
        text = "rolling basis. Minimum Rate $100,000 Annually Maximum Rate $245,000 Annually"
        assert parse_salary_text(text) == (100000, 245000)

    def test_labeled_pair_reversed_word_order(self):
        text = "Salary Type : Annual Salary Salary Min : $ 85000 Salary Max : $ 95000"
        assert parse_salary_text(text) == (85000, 95000)

    def test_labeled_range_start_end(self):
        text = "Compensation Range Pay Range - Start: $118,960.00 Pay Range - End $178,440.00"
        assert parse_salary_text(text) == (118960, 178440)

    def test_labeled_pair_without_salary_context_ignored(self):
        assert parse_salary_text("min order $25,000 max discount $40,000 on fleet") is None

    def test_labeled_min_midpoint(self):
        text = "Salary/Wage Info Grade: S5 Minimum: $98,877 Midpoint: $123,596"
        assert parse_salary_text(text) == (98877, 123596)

    def test_adjacent_amounts_without_separator(self):
        text = "Employment Type: Full-Time Compensation: $300,000 $450,000 Base + Equity"
        assert parse_salary_text(text) == (300000, 450000)

    def test_thousands_qualify_both_endpoints(self):
        assert parse_salary_text("Salary Range: $125-$135,000 annually") == (125000, 135000)

    def test_point_salary(self):
        assert parse_salary_text("Competitive annual salary of $90,000. Remote.") == \
            (90000, 90000)
        assert parse_salary_text("Compensation $110,000 + 5% annual bonus") == \
            (110000, 110000)

    def test_point_salary_needs_adjacent_keyword(self):
        # Benefit amounts must not read as pay even with "per year" nearby.
        assert parse_salary_text("laptop $5,000 per year for professional development, "
                                 "plus $600 per year for tech") is None

    def test_hourly_word_near_yearly_amounts_ignored(self):
        # Boilerplate like "Annual Base Salary Range Or Hourly Base Pay Range"
        # must not multiply five-figure amounts by 2080.
        text = "Annual Base Salary Range Or Hourly Base Pay Range $70,400.00 - $128,379.99"
        assert parse_salary_text(text) == (70400, 128379.99)

    def test_hourly_range_with_leading_hourly_label(self):
        text = "The Hourly pay range for this role is $29.67 - $32.96 for Illinois"
        assert parse_salary_text(text) == (29.67 * 2080, 32.96 * 2080)

    def test_week_in_context_does_not_poison_point_salary(self):
        text = "(On-Site – 5 Days/Week) Employment Type: Full-Time Compensation: $300,000+Base"
        assert parse_salary_text(text) == (300000, 300000)

    def test_up_to_gives_max_only(self):
        text = "Level 1 – Subject Matter Expert Salary: Up to $245,000.00 per year"
        assert parse_salary_text(text) == (None, 245000)

    def test_at_least_gives_min_only(self):
        text = "travel expenses. Salary: at least $139,556 per year. Job Location:"
        assert parse_salary_text(text) == (139556, None)

    def test_starting_at_gives_min_only(self):
        assert parse_salary_text("Competitive salary starting at $130,000 Health,") == \
            (130000, None)
        assert parse_salary_text("Pay Range: Starting from $140,000 What's the Job?") == \
            (140000, None)

    def test_one_sided_needs_pay_context(self):
        assert parse_salary_text("tuition assistance up to $25,000 for degree programs") is None
        assert parse_salary_text("Referral Bonus Program offering up to $50,000") is None

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
