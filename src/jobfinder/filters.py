"""Pure filtering logic over normalized job records."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Filters

# Multipliers to normalize a salary interval to a yearly amount.
INTERVAL_TO_YEARLY = {
    "yearly": 1.0,
    "monthly": 12.0,
    "weekly": 52.0,
    "daily": 260.0,
    "hourly": 2080.0,
}


@dataclass
class Job:
    id: str
    title: str
    company: str
    site: str = ""
    url: str = ""
    location: str | None = None
    is_remote: bool = False
    min_amount: float | None = None
    max_amount: float | None = None
    interval: str | None = None
    currency: str | None = None
    salary_source: str | None = None
    date_posted: str | None = None


@dataclass
class Match:
    job: Job
    salary_unlisted: bool = False


def yearly_salary_range(job: Job) -> tuple[float | None, float | None]:
    factor = INTERVAL_TO_YEARLY.get((job.interval or "yearly").lower(), 1.0)
    lo = job.min_amount * factor if job.min_amount else None
    hi = job.max_amount * factor if job.max_amount else None
    return lo, hi


def _title_ok(job: Job, filters: Filters) -> bool:
    title = job.title or ""
    if filters.title_include and not any(p.search(title) for p in filters.title_include):
        return False
    return not any(p.search(title) for p in filters.title_exclude)


def _employer_ok(job: Job, filters: Filters) -> bool:
    # Whole-word match so blocking "Meta" catches "Meta Platforms" but not "Metabase".
    company = (job.company or "").lower()
    return not any(
        re.search(rf"\b{re.escape(blocked)}\b", company)
        for blocked in filters.employer_exclude
    )


def _location_ok(job: Job, filters: Filters) -> bool:
    if not filters.locations_allow:
        return True
    if job.is_remote and any(p.search("remote") for p in filters.locations_allow):
        return True
    location = job.location or ""
    return any(p.search(location) for p in filters.locations_allow)


def _salary_state(job: Job, filters: Filters) -> str:
    """Return 'ok', 'unlisted', or 'out_of_range'."""
    wanted = filters.salary
    if wanted.min is None and wanted.max is None:
        return "ok"
    lo, hi = yearly_salary_range(job)
    if lo is None and hi is None:
        return "unlisted"
    # A range in a different currency can't be compared against the filter.
    if job.currency and job.currency.upper() != wanted.currency.upper():
        return "unlisted"
    if wanted.min is not None and (hi or lo) < wanted.min:
        return "out_of_range"
    if wanted.max is not None and (lo or hi) > wanted.max:
        return "out_of_range"
    return "ok"


# "$208,000 - $333,500", "208,000 USD - 333,500 USD", "$61,900.00 to $141,000.00",
# "$150k-200k", "$75 to $90 per hour"
_AMOUNT = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?\s*[kK]\b|\d+(?:\.\d+)?"
_SALARY_RANGE = re.compile(
    rf"(\$?)\s*({_AMOUNT})\s*(USD)?"
    r"\s*(?:-|–|—|\bto\b)\s*"
    rf"(\$?)\s*({_AMOUNT})\s*(USD)?"
)


def _to_number(text: str) -> float:
    text = text.replace(",", "").strip()
    if text[-1] in "kK":
        return float(text[:-1]) * 1000
    return float(text)


def parse_salary_text(text: str) -> tuple[float, float] | None:
    """Extract a yearly USD salary range from free text (min across all ranges, max across all).

    Only ranges marked with $ or USD count, so "3-5 years experience" is never
    mistaken for pay. Returns None if nothing plausible is found.
    """
    # JobSpy renders descriptions as markdown with escaped punctuation ("\-", "\.").
    text = text.replace("\\", "")
    lows, highs = [], []
    for m in _SALARY_RANGE.finditer(text):
        if not (m.group(1) or m.group(3) or m.group(4) or m.group(6)):
            continue
        lo, hi = _to_number(m.group(2)), _to_number(m.group(5))
        if lo > hi:
            continue
        context = text[max(0, m.start() - 30):m.end() + 30].lower()
        if "hour" in context:
            lo, hi = lo * 2080, hi * 2080
        elif "month" in context:
            lo, hi = lo * 12, hi * 12
        elif "week" in context:
            lo, hi = lo * 52, hi * 52
        elif lo < 1000:
            continue  # small numbers with no interval marker are too ambiguous
        if not (10_000 <= lo <= 2_000_000 and hi <= 2_000_000):
            continue
        lows.append(lo)
        highs.append(hi)
    if not lows:
        return None
    return min(lows), max(highs)


def evaluate(job: Job, filters: Filters) -> Match | None:
    """Return a Match if the job passes all filters, else None."""
    if not (_title_ok(job, filters) and _employer_ok(job, filters) and _location_ok(job, filters)):
        return None
    state = _salary_state(job, filters)
    if state == "out_of_range":
        return None
    if state == "unlisted":
        return Match(job, salary_unlisted=True) if filters.salary.keep_unlisted else None
    return Match(job)
