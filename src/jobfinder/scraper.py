"""Thin wrapper around jobspy.scrape_jobs with per-site error isolation."""

from __future__ import annotations

import logging

import pandas as pd
from jobspy import scrape_jobs

from .config import SearchSpec
from .filters import Job, parse_salary_text

log = logging.getLogger(__name__)


def _clean(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _row_to_job(row: dict) -> Job:
    url = _clean(row.get("job_url")) or ""
    min_amount = _clean(row.get("min_amount"))
    max_amount = _clean(row.get("max_amount"))
    title = str(_clean(row.get("title")) or "")
    location = _clean(row.get("location"))
    job = Job(
        id=str(_clean(row.get("id")) or url),
        title=title,
        company=str(_clean(row.get("company")) or ""),
        site=str(_clean(row.get("site")) or ""),
        url=url,
        location=location,
        # JobSpy's is_remote flag is a text heuristic that trips on phrases like
        # "remote sensing" in fetched descriptions — trust only title/location.
        is_remote="remote" in f"{title} {location or ''}".lower(),
        min_amount=float(min_amount) if min_amount is not None else None,
        max_amount=float(max_amount) if max_amount is not None else None,
        interval=_clean(row.get("interval")),
        currency=_clean(row.get("currency")),
        salary_source=_clean(row.get("salary_source")),
        date_posted=str(_clean(row.get("date_posted")) or "") or None,
    )
    if job.min_amount is None and job.max_amount is None:
        description = _clean(row.get("description"))
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
            verbose=0,
        )
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
        rows = df.to_dict("records") if df is not None else []
        counts[site] = counts.get(site, 0) + len(rows)
        jobs.extend(_row_to_job(row) for row in rows)
        log.info("search %r: %s returned %d jobs", search.name, site, len(rows))
    return jobs, counts
