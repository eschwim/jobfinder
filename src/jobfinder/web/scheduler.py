"""In-process poll scheduler: one background task, sequential runs.

Runs the (blocking) poll cycle in a worker thread so the event loop stays
responsive, then sleeps on an interruptible Event. Because the loop is
strictly run-then-sleep, overlapping runs are impossible; "Run now" and
config saves just cut the sleep short.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import AppConfig, ConfigError, load_config
from ..digest import DigestError, DigestResult, send_digest
from ..notify import Notifier, NotifyError, build_channels
from ..runner import RunResult, run_once
from ..store import Store

log = logging.getLogger("jobfinder.scheduler")


class PollScheduler:
    def __init__(self, config_path: Path, db_path: Path):
        self.config_path = config_path
        self.db_path = db_path
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._run_requested = False
        self.running = False
        self.last_result: RunResult | None = None
        self.next_run_at: datetime | None = None
        # Set when config is missing/invalid or notification channels can't be
        # built; shown on the status page.
        self.config_error: str | None = None
        self.notify_error: str | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="jobfinder-poll")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def trigger_now(self) -> None:
        self._run_requested = True
        self._wake.set()

    def reschedule(self) -> None:
        """Config changed: re-read it and recompute the next run time."""
        self._wake.set()

    def _load_config(self) -> AppConfig | None:
        try:
            cfg = load_config(self.config_path)
        except ConfigError as exc:
            self.config_error = str(exc)
            return None
        self.config_error = None
        return cfg

    def _run_sync(self, cfg: AppConfig) -> RunResult:
        try:
            channels = build_channels(cfg)
            health_channels = build_channels(cfg, cfg.notify.resolved_health_channels())
            self.notify_error = None
        except NotifyError as exc:
            # Missing credentials shouldn't stop polling: matches still land
            # in the DB for the web UI, alerts resume once secrets are set.
            log.warning("notifications disabled this run: %s", exc)
            self.notify_error = str(exc)
            channels = []
            health_channels = []
        store = Store(self.db_path, repost_window_days=cfg.repost_window_days)
        try:
            return run_once(cfg, store, Notifier(channels, health_channels))
        finally:
            store.close()

    async def _wait(self, seconds: float | None) -> None:
        """Sleep until woken or (if seconds is not None) the timeout passes."""
        self._wake.clear()
        if self._run_requested:
            return
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _loop(self) -> None:
        while True:
            cfg = self._load_config()
            if cfg is None:
                log.warning("scheduler idle: %s", self.config_error)
                self.next_run_at = None
                await self._wait(None)  # a config save or Run now wakes us
                continue

            self._run_requested = False
            self.running = True
            try:
                self.last_result = await asyncio.to_thread(self._run_sync, cfg)
            except Exception:
                log.exception("poll cycle crashed")
            finally:
                self.running = False

            await self._sleep_until_due(datetime.now(timezone.utc))

    async def _sleep_until_due(self, last_run: datetime) -> None:
        """Sleep out the poll interval. A reschedule wake loops to re-read the
        interval from config; a Run now wake (or the timeout) ends the sleep."""
        while not self._run_requested:
            cfg = self._load_config()
            minutes = cfg.poll_interval_minutes if cfg else 60
            due = last_run + timedelta(minutes=minutes)
            self.next_run_at = due
            remaining = (due - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                return
            await self._wait(remaining)


class DigestScheduler:
    """Fires the daily resume-matched digest at digest.time (local time).

    Same shape as PollScheduler: one background task, interruptible sleeps,
    config re-read on every wake so UI edits apply without a restart. A
    "Send digest now" trigger works even while digest.enabled is false, so
    the feature can be tested before being switched on."""

    def __init__(self, config_path: Path, db_path: Path):
        self.config_path = config_path
        self.db_path = db_path
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._run_requested = False
        self.running = False
        self.last_result: DigestResult | None = None
        self.last_error: str | None = None
        self.next_run_at: datetime | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="jobfinder-digest")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def trigger_now(self) -> None:
        self._run_requested = True
        self._wake.set()

    def reschedule(self) -> None:
        self._wake.set()

    def _load_config(self) -> AppConfig | None:
        try:
            return load_config(self.config_path)
        except ConfigError:
            return None

    @staticmethod
    def _next_fire(cfg: AppConfig) -> datetime:
        """The next local-time occurrence of digest.time."""
        hour, minute = (int(part) for part in cfg.digest.time.split(":"))
        now = datetime.now().astimezone()
        fire = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if fire <= now:
            fire += timedelta(days=1)
        return fire

    def _run_sync(self, cfg: AppConfig) -> DigestResult | None:
        store = Store(self.db_path, repost_window_days=cfg.repost_window_days)
        try:
            try:
                result = send_digest(cfg, store, self.config_path.resolve().parent)
                self.last_error = None
                return result
            except DigestError as exc:
                log.warning("digest skipped: %s", exc)
                self.last_error = str(exc)
                store.record_digest(0, 0, 0, 0, error=str(exc))
                return None
        finally:
            store.close()

    async def _wait(self, seconds: float | None) -> None:
        self._wake.clear()
        if self._run_requested:
            return
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _loop(self) -> None:
        while True:
            cfg = self._load_config()
            if not self._run_requested:
                if cfg is None or not cfg.digest.enabled:
                    self.next_run_at = None
                    await self._wait(None)  # config save or Send now wakes us
                    continue
                due = self._next_fire(cfg)
                self.next_run_at = due
                remaining = (due - datetime.now().astimezone()).total_seconds()
                if remaining > 0:
                    await self._wait(remaining)
                    if not self._run_requested and datetime.now().astimezone() < due:
                        continue  # reschedule wake: recompute from fresh config
            self._run_requested = False
            if cfg is None:
                continue
            self.running = True
            try:
                self.last_result = await asyncio.to_thread(self._run_sync, cfg)
            except Exception:
                log.exception("digest cycle crashed")
            finally:
                self.running = False
