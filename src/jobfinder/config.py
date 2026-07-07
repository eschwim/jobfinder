"""Load and validate config.yaml plus Pushover credentials from the environment."""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(Exception):
    pass


@dataclass
class SearchSpec:
    name: str
    sites: list[str]
    search_term: str
    location: str | None = None
    hours_old: int = 4
    results_wanted: int = 50
    country_indeed: str = "USA"
    google_search_term: str | None = None
    fetch_descriptions: bool = False
    is_remote: bool = False


@dataclass
class SalaryFilter:
    min: float | None = None
    max: float | None = None
    currency: str = "USD"
    keep_unlisted: bool = True


@dataclass
class Filters:
    title_include: list[re.Pattern] = field(default_factory=list)
    title_exclude: list[re.Pattern] = field(default_factory=list)
    employer_exclude: list[str] = field(default_factory=list)
    locations_allow: list[re.Pattern] = field(default_factory=list)
    # On-site/hybrid jobs in these locations are rejected even if the
    # allowlist would accept them; fully remote jobs are exempt.
    locations_deny: list[re.Pattern] = field(default_factory=list)
    salary: SalaryFilter = field(default_factory=SalaryFilter)


KNOWN_CHANNELS = {"pushover", "email"}


@dataclass
class NotifyConfig:
    channels: list[str] = field(default_factory=lambda: ["pushover"])
    digest_threshold: int = 5
    health_alerts: bool = True
    empty_runs_before_alert: int = 3


@dataclass
class AppConfig:
    searches: list[SearchSpec]
    filters: Filters
    notify: NotifyConfig
    pushover_token: str | None
    pushover_user: str | None
    # Suppress reposts of an already-alerted role (same company+title) for this
    # many days after its last sighting; 0 disables repost detection.
    repost_window_days: int = 60
    # Minutes between poll cycles when running the web app's scheduler.
    poll_interval_minutes: int = 120
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    email_from: str | None = None
    email_to: str | None = None


def _compile_patterns(raw: list[str], where: str, flags: int = 0) -> list[re.Pattern]:
    patterns = []
    for expr in raw:
        try:
            patterns.append(re.compile(expr, flags))
        except re.error as exc:
            raise ConfigError(f"invalid regex in {where}: {expr!r} ({exc})") from exc
    return patterns


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a KEY=VALUE .env file, without overriding existing vars."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_raw_config(path: Path) -> dict:
    """The parsed-but-unvalidated YAML dict — the format the config UI edits."""
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"config file is not a YAML mapping: {path}")
    return raw


def load_config(path: Path) -> AppConfig:
    raw = load_raw_config(path)
    _load_dotenv(path.resolve().parent / ".env")
    return parse_config(raw)


def save_config(path: Path, raw: dict) -> None:
    """Validate a raw config dict and atomically write it as YAML.

    Any comments in the existing file are lost — the dict is the source.
    """
    parse_config(raw)  # raises ConfigError before anything touches disk
    fd, tmp = tempfile.mkstemp(dir=str(path.resolve().parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            yaml.safe_dump(raw, fh, sort_keys=False, allow_unicode=True)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def parse_config(raw: dict) -> AppConfig:
    searches_raw = raw.get("searches") or []
    if not searches_raw:
        raise ConfigError("config must define at least one entry under 'searches'")
    searches = []
    for i, entry in enumerate(searches_raw):
        try:
            searches.append(SearchSpec(**entry))
        except TypeError as exc:
            raise ConfigError(f"searches[{i}] is invalid: {exc}") from exc
        if not searches[-1].sites:
            raise ConfigError(f"searches[{i}] ({searches[-1].name}) lists no sites")

    filters_raw = raw.get("filters") or {}
    salary_raw = filters_raw.get("salary") or {}
    try:
        salary = SalaryFilter(**salary_raw)
    except TypeError as exc:
        raise ConfigError(f"filters.salary is invalid: {exc}") from exc
    filters = Filters(
        title_include=_compile_patterns(filters_raw.get("title_include") or [], "filters.title_include"),
        title_exclude=_compile_patterns(filters_raw.get("title_exclude") or [], "filters.title_exclude"),
        employer_exclude=[s.lower() for s in filters_raw.get("employer_exclude") or []],
        locations_allow=_compile_patterns(
            filters_raw.get("locations_allow") or [], "filters.locations_allow", re.IGNORECASE
        ),
        locations_deny=_compile_patterns(
            filters_raw.get("locations_deny") or [], "filters.locations_deny", re.IGNORECASE
        ),
        salary=salary,
    )

    try:
        notify = NotifyConfig(**(raw.get("notify") or {}))
    except TypeError as exc:
        raise ConfigError(f"notify section is invalid: {exc}") from exc
    unknown = set(notify.channels) - KNOWN_CHANNELS
    if unknown:
        raise ConfigError(f"unknown notify.channels: {sorted(unknown)} "
                          f"(known: {sorted(KNOWN_CHANNELS)})")
    if not notify.channels:
        raise ConfigError("notify.channels must list at least one channel")

    repost_window_days = raw.get("repost_window_days", 60)
    if not isinstance(repost_window_days, int) or isinstance(repost_window_days, bool) \
            or repost_window_days < 0:
        raise ConfigError("repost_window_days must be a non-negative integer")

    poll_interval_minutes = raw.get("poll_interval_minutes", 120)
    if not isinstance(poll_interval_minutes, int) or isinstance(poll_interval_minutes, bool) \
            or poll_interval_minutes < 1:
        raise ConfigError("poll_interval_minutes must be a positive integer")

    return AppConfig(
        searches=searches,
        filters=filters,
        notify=notify,
        repost_window_days=repost_window_days,
        poll_interval_minutes=poll_interval_minutes,
        pushover_token=os.environ.get("PUSHOVER_TOKEN"),
        pushover_user=os.environ.get("PUSHOVER_USER"),
        smtp_host=os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(os.environ.get("SMTP_PORT", "587")),
        smtp_user=os.environ.get("SMTP_USER"),
        smtp_password=os.environ.get("SMTP_PASSWORD"),
        email_from=os.environ.get("EMAIL_FROM"),
        email_to=os.environ.get("EMAIL_TO"),
    )
