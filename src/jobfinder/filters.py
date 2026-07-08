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
    # True when is_remote comes only from the board's remote search facet, with
    # no "remote" in the title/location to back it up. LinkedIn's facet
    # sometimes returns hybrid postings, so this remoteness is verifiable.
    remote_by_facet: bool = False
    min_amount: float | None = None
    max_amount: float | None = None
    interval: str | None = None
    currency: str | None = None
    salary_source: str | None = None
    date_posted: str | None = None
    # Full description text when the search fetched it; persisted for matches
    # so the daily digest can compare requirements against the resume.
    description: str | None = None


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


def location_matches(job: Job, filters: Filters) -> bool:
    """The tagged location itself matches the allowlist (remote flags aside)."""
    return any(p.search(job.location or "") for p in filters.locations_allow)


def _location_ok(job: Job, filters: Filters) -> bool:
    # Denylist beats the allowlist for on-site/hybrid jobs, but a fully remote
    # job is fine wherever the posting happens to be tagged — remote postings
    # are often tagged with a hiring-hub city.
    if (not job.is_remote
            and any(p.search(job.location or "") for p in filters.locations_deny)):
        return False
    if not filters.locations_allow:
        return True
    if job.is_remote and any(p.search("remote") for p in filters.locations_allow):
        return True
    return location_matches(job, filters)


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


# Phrases that describe a hybrid/in-office work arrangement. Deliberately narrow:
# "hybrid" alone would trip on "hybrid cloud", which is everywhere in infra jobs.
_HYBRID_HINTS = re.compile(
    r"hybrid\s+(?:work(?:ing|place)?|role|schedule|model|position|arrangement|environment|setup)"
    r"|(?:role|position|schedule|arrangement)\s+(?:is|will\s+be)\s+hybrid"
    r"|days?\s*(?:(?:a|per)\s+week|/\s*week)?\s+(?:in|at)\s+(?:the\s+|our\s+)?(?:office|person\b)"
    r"|days?\s*(?:(?:a|per)\s+week|/\s*week)?\s+on-?site",
    re.IGNORECASE,
)


def describes_hybrid(text: str) -> bool:
    """True if free text describes a hybrid/in-office arrangement (not "hybrid cloud")."""
    return bool(_HYBRID_HINTS.search(text.replace("\\", "")))


# "$208,000 - $333,500", "208,000 USD - 333,500 USD", "$61,900.00 to $141,000.00",
# "$150k-200k", "$75 to $90 per hour", "between $104,400 and $171,000/year",
# "USD $105,230.00/Yr. - USD $111,000.00/Yr.", "$55.00/hr-$60.00/hr", "$100-115k"
_AMOUNT = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?\s*[kK]\b|\d+(?:\.\d+)?"
_INTERVAL_HINT = (r"/\s*(?:yr|year|hr|hour|mo|month|wk|week)\b\.?"
                  r"|per\s+(?:year|hour|month|week)|annual(?:ly)?|hourly")


def _amount_side(n: int) -> str:
    """One side of a salary range: [USD] [$] amount [USD] [/yr | per year | ...]."""
    return (rf"(?P<cur{n}>USD\s*)?(?P<dol{n}>\$\s*)?(?P<amt{n}>{_AMOUNT})"
            rf"\s*(?P<cur{n}b>USD\b)?\s*(?P<ivl{n}>{_INTERVAL_HINT})?")


# The bare-whitespace separator ("$300,000 $450,000 Base") only pairs directly
# adjacent amounts, so unrelated dollar figures in running text don't combine.
_SALARY_RANGE = re.compile(
    _amount_side(1) + r"(?:\s*(?:-|–|—|\bto\b|\band\b)\s*|\s+)" + _amount_side(2),
    re.IGNORECASE,
)

# Ranges written as labeled endpoints with no separator between them:
# "Minimum Salary: $120,000 Maximum Salary: $175,000",
# "Minimum Rate $100,000 Annually Maximum Rate $245,000 Annually",
# "Salary Min : $ 85000 Salary Max : $ 95000",
# "Pay Range - Start: $118,960.00 Pay Range - End $178,440.00",
# "Minimum: $98,877 Midpoint: $123,596"
_LABELED_PAIR = re.compile(
    rf"\b(?:min(?:imum)?|start(?:ing)?)\b[^$\d]{{0,30}}\$\s*(?P<amt1>{_AMOUNT})"
    rf"[^$]{{0,45}}?\b(?:max(?:imum)?|end|mid(?:point)?)\b[^$\d]{{0,30}}\$\s*(?P<amt2>{_AMOUNT})",
    re.IGNORECASE,
)

# A single stated salary: "Compensation $110,000 + 5% bonus", "annual salary of
# $90,000", "Salary: $115,000". Deliberately requires the keyword right before
# the amount so benefit figures ("$5,000 per year for professional
# development") don't qualify.
_POINT_SALARY = re.compile(
    rf"\b(?:salary|compensation|pay)\b\s*[:\-–]?\s*(?:is\s+|of\s+)?\$\s*(?P<amt>{_AMOUNT})",
    re.IGNORECASE,
)

# One-sided salary bounds: "Salary: Up to $245,000.00 per year" (max only),
# "at least $139,556 per year", "salary starting at $130,000", "Pay Range:
# Starting from $140,000", "begins at $160,000" (min only).
_ONE_SIDED = re.compile(
    rf"\b(?P<kind>up\s+to|at\s+least|starting\s+(?:at|from)|begins?\s+at)"
    rf"\s+\$\s*(?P<amt>{_AMOUNT})",
    re.IGNORECASE,
)

_SALARY_WORDS = re.compile(r"salar|\bpay\b|\brate\b|compensat|wage", re.IGNORECASE)


def _to_number(text: str) -> float:
    text = text.replace(",", "").strip()
    if text[-1] in "kK":
        return float(text[:-1]) * 1000
    return float(text)


def _interval_factor(hint: str, lo: float) -> float | None:
    """Yearly multiplier for a pay interval named in `hint`, or None if the
    amounts are too small to be yearly and no interval is named."""
    if re.search(r"hour|/\s*hr\b", hint):
        return 2080.0
    if re.search(r"month|/\s*mo\b", hint):
        return 12.0
    if re.search(r"week|/\s*wk\b", hint):
        return 52.0
    if re.search(r"year|/\s*yr\b|annual", hint):
        return 1.0
    # Small numbers with no interval marker are too ambiguous to assume yearly.
    return 1.0 if lo >= 1000 else None


def parse_salary_text(text: str) -> tuple[float | None, float | None] | None:
    """Extract a yearly USD salary range from free text (min across all ranges, max across all).

    Only amounts marked with $ or USD count, so "3-5 years experience" is never
    mistaken for pay. One side may be None when the text only bounds the salary
    from one direction ("up to $245,000"). Returns None if nothing plausible is
    found.
    """
    # JobSpy renders descriptions as markdown with escaped punctuation ("\-", "\.").
    text = text.replace("\\", "")
    lows, highs = [], []

    def consider(lo: float | None, hi: float | None, hint: str) -> None:
        ref = lo if lo is not None else hi
        factor = _interval_factor(hint.lower(), ref)
        if factor is None:
            return
        if factor > 1 and ref * factor > 2_000_000 and ref >= 10_000:
            # An interval word near yearly amounts ("Annual Salary Range Or
            # Hourly Pay Range $70,400 - ...", "5 Days/Week ... $300,000"):
            # when the guess is implausible but the plain reading isn't, the
            # amounts are yearly.
            factor = 1.0
        lo = lo * factor if lo is not None else None
        hi = hi * factor if hi is not None else None
        for side in (lo, hi):
            if side is not None and not (10_000 <= side <= 2_000_000):
                return
        if lo is not None and hi is not None and lo > hi:
            return
        lows.append(lo)
        highs.append(hi)

    for m in _SALARY_RANGE.finditer(text):
        if not any(m.group(g) for g in ("cur1", "dol1", "cur1b", "cur2", "dol2", "cur2b")):
            continue
        a1, a2 = m.group("amt1"), m.group("amt2")
        lo, hi = _to_number(a1), _to_number(a2)
        # "$100-115k": the k qualifies both endpoints.
        if a2.strip()[-1] in "kK" and a1.strip()[-1] not in "kK" and lo < 1000:
            lo *= 1000
        # "$125-$135,000 annually": the thousands qualify both endpoints.
        elif lo < 1000 <= hi and "," in a2 and lo * 1000 <= hi:
            lo *= 1000
        hint = (m.group("ivl1") or m.group("ivl2")
                or text[max(0, m.start() - 45):m.end() + 30])
        consider(lo, hi, hint)

    for m in _LABELED_PAIR.finditer(text):
        context = text[max(0, m.start() - 20):m.end() + 20]
        if _SALARY_WORDS.search(context):
            consider(_to_number(m.group("amt1")), _to_number(m.group("amt2")), context)

    for m in _POINT_SALARY.finditer(text):
        amount = _to_number(m.group("amt"))
        consider(amount, amount, text[max(0, m.start() - 45):m.end() + 30])

    for m in _ONE_SIDED.finditer(text):
        # Only near pay language: "save up to $2,000 on commuter benefits" and
        # "tuition assistance up to $25,000" must not read as salary bounds.
        if not _SALARY_WORDS.search(text[max(0, m.start() - 45):m.start()]):
            continue
        amount = _to_number(m.group("amt"))
        hint = text[max(0, m.start() - 45):m.end() + 30]
        if m.group("kind").lower().startswith("up"):
            consider(None, amount, hint)
        else:
            consider(amount, None, hint)

    if not lows:
        return None
    lo_vals = [x for x in lows if x is not None]
    hi_vals = [x for x in highs if x is not None]
    return (min(lo_vals) if lo_vals else None,
            max(hi_vals) if hi_vals else None)


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
