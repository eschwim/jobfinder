"""One poll cycle: scrape, filter, dedupe, notify — shared by CLI and scheduler."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import AppConfig
from .filters import Job, Match, evaluate, location_matches
from .notify import Notifier, NotifyError
from .scraper import fetch_jobs, linkedin_says_remote
from .store import Store

log = logging.getLogger("jobfinder")


@dataclass
class RunResult:
    started_at: datetime
    finished_at: datetime | None = None
    match_count: int = 0
    site_totals: dict[str, int] = field(default_factory=dict)
    error: str | None = None


def _needs_remote_verification(job: Job, cfg) -> bool:
    """A match is worth two extra LinkedIn requests only when its remoteness
    rests solely on the search facet AND it would fail the location filter
    without it. A Seattle-tagged hybrid job is still a match; a facet-tagged
    "remote" job in Austin is only a match if it's actually remote."""
    return (job.site == "linkedin"
            and job.remote_by_facet
            and bool(cfg.filters.locations_allow)
            and not location_matches(job, cfg.filters))


def run_once(cfg: AppConfig, store: Store, notifier: Notifier,
             dry_run: bool = False) -> RunResult:
    """Run one poll cycle. Notification failures land in RunResult.error
    rather than raising, so a scheduler loop survives them; scrape errors are
    already isolated per-site inside fetch_jobs."""
    result = RunResult(started_at=datetime.now(timezone.utc))
    run_id = None if dry_run else store.record_run_start()
    matches: list[Match] = []

    try:
        for search in cfg.searches:
            jobs, counts = fetch_jobs(search)
            for site, count in counts.items():
                result.site_totals[site] = result.site_totals.get(site, 0) + count
            for job in jobs:
                if store.is_seen(job):
                    # Record repost sightings too, so an actively reposted role
                    # keeps its repost window fresh instead of re-alerting
                    # every window's end.
                    if not dry_run:
                        store.mark_seen(job)
                    continue
                match = evaluate(job, cfg.filters)
                if match and _needs_remote_verification(job, cfg):
                    if linkedin_says_remote(job) is False:
                        log.info("dropping %s @ %s (%s): linkedin lists it as "
                                 "hybrid/on-site, not remote",
                                 job.title, job.company, job.location)
                        job.is_remote = False
                        match = evaluate(job, cfg.filters)
                if match:
                    if not dry_run:
                        store.mark_matched(match)
                    matches.append(match)

        result.match_count = len(matches)
        log.info("%d new matching job(s) this run", result.match_count)
        notifier.alert_matches(matches, cfg.notify.digest_threshold)

        for site, total in result.site_totals.items():
            streak = store.record_site_count(site, total)
            if cfg.notify.health_alerts and streak == cfg.notify.empty_runs_before_alert:
                log.warning("%s empty for %d consecutive runs", site, streak)
                notifier.health_alert(site, streak)
    except NotifyError as exc:
        log.error("%s", exc)
        result.error = str(exc)
    finally:
        result.finished_at = datetime.now(timezone.utc)
        if run_id is not None:
            store.record_run_end(run_id, result.match_count, result.error)
    return result
