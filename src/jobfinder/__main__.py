"""Run one poll cycle: scrape, filter, dedupe, notify."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .filters import Match, evaluate
from .notify import Notifier, NotifyError, build_channels
from .scraper import fetch_jobs
from .store import Store

log = logging.getLogger("jobfinder")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobfinder")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--db", type=Path, default=Path("jobfinder.db"))
    parser.add_argument("--dry-run", action="store_true",
                        help="print notifications instead of sending them")
    parser.add_argument("--test-notify", action="store_true",
                        help="send a test message through each configured channel, then exit")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    try:
        channels = [] if args.dry_run else build_channels(cfg)
    except NotifyError as exc:
        log.error("%s", exc)
        return 2
    notifier = Notifier(channels, dry_run=args.dry_run)
    log.info("notification channels: %s%s", ", ".join(cfg.notify.channels),
             " (dry-run)" if args.dry_run else "")

    if args.test_notify:
        try:
            notifier.test()
        except NotifyError as exc:
            log.error("%s", exc)
            return 1
        log.info("test notification sent")
        return 0

    store = Store(args.db, repost_window_days=cfg.repost_window_days)
    matches: list[Match] = []
    site_totals: dict[str, int] = {}

    try:
        for search in cfg.searches:
            jobs, counts = fetch_jobs(search)
            for site, count in counts.items():
                site_totals[site] = site_totals.get(site, 0) + count
            for job in jobs:
                if store.is_seen(job):
                    # Record repost sightings too, so an actively reposted role
                    # keeps its repost window fresh instead of re-alerting
                    # every window's end.
                    if not args.dry_run:
                        store.mark_seen(job)
                    continue
                match = evaluate(job, cfg.filters)
                if match:
                    if not args.dry_run:
                        store.mark_seen(job)
                    matches.append(match)

        log.info("%d new matching job(s) this run", len(matches))
        notifier.alert_matches(matches, cfg.notify.digest_threshold)

        for site, total in site_totals.items():
            streak = store.record_site_count(site, total)
            if cfg.notify.health_alerts and streak == cfg.notify.empty_runs_before_alert:
                log.warning("%s empty for %d consecutive runs", site, streak)
                notifier.health_alert(site, streak)
    except NotifyError as exc:
        log.error("%s", exc)
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
