"""CLI entry point: run one poll cycle and exit."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .notify import Notifier, NotifyError, build_channels
from .runner import run_once
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
    try:
        result = run_once(cfg, store, notifier, dry_run=args.dry_run)
    finally:
        store.close()
    return 1 if result.error else 0


if __name__ == "__main__":
    sys.exit(main())
