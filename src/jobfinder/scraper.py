"""Thin wrapper around jobspy.scrape_jobs with per-site error isolation."""

from __future__ import annotations

import logging
import re
import time
from datetime import date, timedelta

import pandas as pd
import requests
from bs4 import BeautifulSoup
from jobspy import scrape_jobs

from .config import SearchSpec
from .filters import Job, describes_hybrid, parse_salary_text

log = logging.getLogger(__name__)

_BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:124.0) "
               "Gecko/20100101 Firefox/124.0")


def _fetch_linkedin_description(url: str) -> str | None:
    """Fetch a LinkedIn job page's description text directly.

    JobSpy's per-job detail fetch swallows timeouts, 429s, and signup-wall
    redirects and silently returns no description, which would make the job's
    salary look unlisted. Guest access to the job page usually still works,
    so retry once ourselves.
    """
    try:
        resp = requests.get(url, headers={"User-Agent": _BROWSER_UA},
                            timeout=10, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    if "linkedin.com/signup" in resp.url:
        return None
    div = BeautifulSoup(resp.text, "html.parser").find(
        "div", class_=lambda x: x and "show-more-less-html__markup" in x
    )
    return div.get_text(" ", strip=True) if div else None


_POSTED_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"
_REL_DATE = re.compile(r"(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago", re.I)
_REL_DAYS = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}


def _relative_to_iso(text: str, today: date | None = None) -> str | None:
    """Turn LinkedIn's 'posted N units ago' label into an ISO date string."""
    today = today or date.today()
    low = text.strip().lower()
    if not low:
        return None
    if "just now" in low or "today" in low or "hour" in low or "minute" in low:
        return today.isoformat()
    if "yesterday" in low:
        return (today - timedelta(days=1)).isoformat()
    m = _REL_DATE.search(low)
    if not m:
        return None
    return (today - timedelta(days=_REL_DAYS[m.group(2)] * int(m.group(1)))).isoformat()


def linkedin_posted_date(job: Job) -> str | None:
    """Scrape LinkedIn's 'posted N ago' label into an ISO date.

    JobSpy leaves date_posted empty on hours_old-filtered LinkedIn searches, so
    backfill it from the guest job-posting page (which still shows the relative
    posting time). hours_old keeps matched jobs recent, so the relative label
    resolves to the right day.
    """
    job_id = re.sub(r"\D", "", job.id or "")
    if not job_id:
        return None
    try:
        resp = requests.get(_POSTED_URL.format(job_id),
                            headers={"User-Agent": _BROWSER_UA}, timeout=10)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    span = BeautifulSoup(resp.text, "html.parser").find(
        class_=lambda c: c and "posted-time-ago__text" in c)
    return _relative_to_iso(span.get_text(strip=True)) if span else None


_GUEST_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"


def _guest_search_ids(keywords: str, f_wt: str | None = None) -> set[str] | None:
    """Job-posting ids returned by LinkedIn's guest search, or None on error."""
    params = {"keywords": keywords}
    if f_wt:
        params["f_WT"] = f_wt
    try:
        resp = requests.get(_GUEST_SEARCH, params=params,
                            headers={"User-Agent": _BROWSER_UA}, timeout=10)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    return set(re.findall(r"urn:li:jobPosting:(\d+)", resp.text))


def linkedin_says_remote(job: Job, attempts: int = 3) -> bool | None:
    """Cross-check a facet-returned "remote" job against LinkedIn's own
    workplace-type data.

    The remote search facet (f_WT=2) sometimes returns hybrid postings, and
    the guest job page carries no workplace-type field to catch them. The
    guest *search* endpoint knows the truth, but nondeterministically ignores
    f_WT and serves generic results instead — so a single query proves
    nothing. Query the exact posting under the remote facet AND its
    complement (on-site+hybrid, f_WT=1,3): an honored pair puts the job in
    exactly one; a facet-ignored response puts it in both, which reads as
    inconsistent and is retried. Returns True (remote), False
    (hybrid/on-site), or None (inconclusive after all attempts).
    """
    job_id = re.sub(r"\D", "", job.id)
    if not job_id or not job.title:
        return None
    keywords = f'"{job.title}" {job.company}'.strip()
    for attempt in range(attempts):
        if attempt:
            time.sleep(2)
        remote = _guest_search_ids(keywords, f_wt="2")
        time.sleep(1)
        onsite = _guest_search_ids(keywords, f_wt="1,3")
        if remote is None or onsite is None:
            continue
        in_remote, in_onsite = job_id in remote, job_id in onsite
        if in_remote != in_onsite:
            return in_remote
        # In both: a facet was ignored. In neither: the search can't see the
        # posting. Either way this round proves nothing.
    return None


def _clean(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _row_to_job(row: dict, assume_remote: bool = False) -> Job:
    url = _clean(row.get("job_url")) or ""
    min_amount = _clean(row.get("min_amount"))
    max_amount = _clean(row.get("max_amount"))
    title = str(_clean(row.get("title")) or "")
    location = _clean(row.get("location"))
    description = _clean(row.get("description"))
    text_remote = "remote" in f"{title} {location or ''}".lower()
    # LinkedIn's remote facet (f_WT=2) sometimes returns hybrid postings; when the
    # fetched description clearly describes a hybrid arrangement, the facet loses.
    if assume_remote and not text_remote and description and describes_hybrid(str(description)):
        assume_remote = False
    job = Job(
        id=str(_clean(row.get("id")) or url),
        title=title,
        company=str(_clean(row.get("company")) or ""),
        site=str(_clean(row.get("site")) or ""),
        url=url,
        location=location,
        # JobSpy's is_remote flag is a text heuristic that trips on phrases like
        # "remote sensing" in fetched descriptions — trust only title/location,
        # or the search itself having filtered on the board's remote facet.
        is_remote=assume_remote or text_remote,
        remote_by_facet=assume_remote and not text_remote,
        min_amount=float(min_amount) if min_amount is not None else None,
        max_amount=float(max_amount) if max_amount is not None else None,
        interval=_clean(row.get("interval")),
        currency=_clean(row.get("currency")),
        salary_source=_clean(row.get("salary_source")),
        date_posted=str(_clean(row.get("date_posted")) or "") or None,
        description=str(description) if description else None,
    )
    if job.min_amount is None and job.max_amount is None:
        parsed = parse_salary_text(str(description)) if description else None
        if parsed:
            job.min_amount, job.max_amount = parsed
            job.interval = "yearly"
            job.currency = "USD"
            job.salary_source = "description"
    return job


def fetch_jobs(search: SearchSpec) -> tuple[list[Job], dict[str, int]]:
    """Scrape each site in the search separately so one broken board doesn't sink the run.

    Returns the jobs found plus a per-site result count (errors count as 0).
    """
    jobs: list[Job] = []
    counts: dict[str, int] = {}
    for site in search.sites:
        kwargs = dict(
            site_name=site,
            search_term=search.search_term,
            location=search.location,
            results_wanted=search.results_wanted,
            hours_old=search.hours_old,
            country_indeed=search.country_indeed,
            linkedin_fetch_description=search.fetch_descriptions,
            is_remote=search.is_remote,
            verbose=0,
        )
        if search.is_remote and site == "indeed" and search.hours_old:
            # Indeed ignores the is_remote facet when hours_old is set, so the
            # search would silently return on-site jobs marked remote.
            log.warning("search %r: indeed drops is_remote when hours_old is set; "
                        "results may include non-remote jobs", search.name)
        if site == "google":
            # Google's scraper matches only on this literal query string, not the structured params.
            kwargs["google_search_term"] = search.google_search_term or (
                f"{search.search_term} jobs near {search.location} since yesterday"
                if search.location else f"{search.search_term} jobs since yesterday"
            )
        try:
            df = scrape_jobs(**kwargs)
        except Exception:
            log.exception("search %r: %s scrape failed", search.name, site)
            counts[site] = counts.get(site, 0)
            continue
        # Trust the remote facet only where the board actually applied it:
        # google matches on the query string alone, and indeed drops the facet
        # when hours_old is set.
        facet_applied = search.is_remote and site not in ("google",) and not (
            site == "indeed" and search.hours_old
        )
        rows = df.to_dict("records") if df is not None else []
        if site == "linkedin" and search.fetch_descriptions:
            missing = [r for r in rows
                       if _clean(r.get("description")) is None and _clean(r.get("job_url"))]
            for row in missing:
                time.sleep(1)  # be gentle; jobspy already hit LinkedIn hard this run
                row["description"] = _fetch_linkedin_description(str(row["job_url"]))
            if missing:
                recovered = sum(1 for r in missing if r["description"])
                log.info("search %r: retried %d missing linkedin descriptions, recovered %d",
                         search.name, len(missing), recovered)
        counts[site] = counts.get(site, 0) + len(rows)
        jobs.extend(_row_to_job(row, assume_remote=facet_applied) for row in rows)
        log.info("search %r: %s returned %d jobs", search.name, site, len(rows))
    return jobs, counts
