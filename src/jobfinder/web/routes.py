"""All web routes: job views, config forms, status, and actions."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import (
    KNOWN_CHANNELS,
    ConfigError,
    load_config,
    load_raw_config,
    save_config,
)
from ..filters import Job, yearly_salary_range
from ..notify import Notifier, NotifyError, build_channels, location_str
from ..store import Store
from .deps import get_store

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Job-view lookback windows: label -> hours (None = everything).
WINDOWS: dict[str, int | None] = {"24h": 24, "3d": 72, "7d": 168, "30d": 720, "all": None}

KNOWN_SITES = ["indeed", "linkedin", "google", "zip_recruiter", "glassdoor"]

SECRET_VARS = ["PUSHOVER_TOKEN", "PUSHOVER_USER", "SMTP_USER", "SMTP_PASSWORD",
               "EMAIL_FROM", "EMAIL_TO"]


def _rel_time(iso: str | None) -> str:
    if not iso:
        return ""
    then = datetime.fromisoformat(iso)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - then
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)}m ago"
    if seconds < 129600:
        return f"{round(seconds / 3600)}h ago"
    return f"{round(seconds / 86400)}d ago"


templates.env.filters["rel_time"] = _rel_time


def _row_display(row: sqlite3.Row) -> dict:
    """Render-ready dict for one matched job, reusing the alert formatters."""
    job = Job(id=row["id"], title=row["title"] or "", company=row["company"] or "",
              site=row["site"] or "", url=row["url"] or "",
              location=row["location"], is_remote=bool(row["is_remote"]),
              min_amount=row["min_amount"], max_amount=row["max_amount"],
              interval=row["salary_interval"], currency=row["currency"],
              salary_source=row["salary_source"], date_posted=row["date_posted"])
    salary_min, salary_max = yearly_salary_range(job)
    return {
        "title": job.title, "company": job.company, "url": job.url,
        "site": job.site, "location": location_str(job),
        "salary_min": salary_min, "salary_max": salary_max,
        "salary_min_str": _salary_amount_str(salary_min, job.currency),
        "salary_max_str": _salary_amount_str(salary_max, job.currency),
        "salary_source": job.salary_source,
        "matched_at": row["matched_at"], "date_posted": row["date_posted"],
    }


def _salary_amount_str(amount: float | None, currency: str | None) -> str:
    if amount is None:
        return "—"
    text = f"{amount:,.0f}"
    if currency and currency.upper() != "USD":
        text += f" {currency.upper()}"
    return text


# Column key -> function extracting a sortable value from a display dict.
# None-valued cells always sort to the end regardless of direction.
SORT_KEYS = {
    "title": lambda j: j["title"].lower(),
    "company": lambda j: j["company"].lower(),
    "location": lambda j: j["location"].lower(),
    "min": lambda j: j["salary_min"],
    "max": lambda j: j["salary_max"],
    "site": lambda j: j["site"].lower(),
    "posted": lambda j: j["date_posted"],
    "matched": lambda j: j["matched_at"],
}


def _sort_jobs(jobs: list[dict], sort: str, direction: str) -> list[dict]:
    key = SORT_KEYS[sort]
    present = [j for j in jobs if key(j) is not None]
    missing = [j for j in jobs if key(j) is None]
    present.sort(key=key, reverse=(direction == "desc"))
    return present + missing


def _jobs_context(request: Request, store: Store, window: str,
                  sort: str, direction: str) -> dict:
    if window not in WINDOWS:
        window = "24h"
    if sort not in SORT_KEYS:
        sort = "matched"
    if direction not in ("asc", "desc"):
        direction = "desc"
    hours = WINDOWS[window]
    since = None if hours is None else datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = store.recent_matches(since=since)
    jobs = _sort_jobs([_row_display(r) for r in rows], sort, direction)
    return {"jobs": jobs, "window": window, "windows": list(WINDOWS),
            "sort": sort, "dir": direction}


@router.get("/")
async def index():
    return RedirectResponse("/jobs")


@router.get("/jobs")
async def jobs_page(request: Request, window: str = "24h",
                    sort: str = "matched", dir: str = "desc",
                    store: Store = Depends(get_store)):
    return templates.TemplateResponse(request, "jobs.html",
                                      _jobs_context(request, store, window, sort, dir))


@router.get("/jobs/table")
async def jobs_table(request: Request, window: str = "24h",
                     sort: str = "matched", dir: str = "desc",
                     store: Store = Depends(get_store)):
    return templates.TemplateResponse(request, "_jobs_table.html",
                                      _jobs_context(request, store, window, sort, dir))


# --- config -----------------------------------------------------------------

def _raw_config(request: Request) -> dict:
    try:
        return load_raw_config(request.app.state.config_path)
    except ConfigError:
        # First run: no config file yet; the forms start from a skeleton.
        return {}


def _config_page(request: Request, raw: dict, error: str | None = None,
                 saved: bool = False, status_code: int = 200):
    return templates.TemplateResponse(request, "config.html", {
        "raw": raw, "error": error, "saved": saved,
        "known_sites": KNOWN_SITES, "known_channels": sorted(KNOWN_CHANNELS),
        "searches": raw.get("searches") or [],
        "filters": raw.get("filters") or {},
        "salary": (raw.get("filters") or {}).get("salary") or {},
        "notify": raw.get("notify") or {},
        "repost_window_days": raw.get("repost_window_days", 60),
        "poll_interval_minutes": raw.get("poll_interval_minutes", 120),
    }, status_code=status_code)


def _save(request: Request, raw: dict):
    """Persist an edited raw config; on success nudge the scheduler and
    redirect (POST-redirect-GET), on validation failure re-render with the
    error and leave the file untouched."""
    try:
        save_config(request.app.state.config_path, raw)
    except ConfigError as exc:
        return _config_page(request, raw, error=str(exc), status_code=422)
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.reschedule()
    return RedirectResponse("/config?saved=1", status_code=303)


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _int_or_none(value: str) -> int | None:
    value = value.strip()
    return int(value) if value else None


def _search_from_form(form) -> dict:
    entry: dict = {
        "name": form.get("name", "").strip(),
        "sites": form.getlist("sites"),
        "search_term": form.get("search_term", "").strip(),
    }
    if form.get("location", "").strip():
        entry["location"] = form.get("location").strip()
    if form.get("google_search_term", "").strip():
        entry["google_search_term"] = form.get("google_search_term").strip()
    for key in ("hours_old", "results_wanted"):
        value = _int_or_none(form.get(key, ""))
        if value is not None:
            entry[key] = value
    if form.get("country_indeed", "").strip():
        entry["country_indeed"] = form.get("country_indeed").strip()
    if form.get("fetch_descriptions"):
        entry["fetch_descriptions"] = True
    if form.get("is_remote"):
        entry["is_remote"] = True
    return entry


@router.get("/config")
async def config_page(request: Request, saved: bool = False):
    return _config_page(request, _raw_config(request), saved=saved)


@router.post("/config/searches")
async def add_search(request: Request):
    raw = _raw_config(request)
    form = await request.form()
    raw.setdefault("searches", []).append(_search_from_form(form))
    return _save(request, raw)


@router.post("/config/searches/{index}")
async def edit_search(request: Request, index: int):
    raw = _raw_config(request)
    searches = raw.get("searches") or []
    if not 0 <= index < len(searches):
        return RedirectResponse("/config", status_code=303)
    form = await request.form()
    searches[index] = _search_from_form(form)
    return _save(request, raw)


@router.post("/config/searches/{index}/delete")
async def delete_search(request: Request, index: int):
    raw = _raw_config(request)
    searches = raw.get("searches") or []
    if 0 <= index < len(searches):
        del searches[index]
        return _save(request, raw)
    return RedirectResponse("/config", status_code=303)


@router.post("/config/filters")
async def save_filters(request: Request):
    raw = _raw_config(request)
    form = await request.form()
    filters: dict = {}
    for key in ("title_include", "title_exclude", "employer_exclude",
                "locations_allow", "locations_deny"):
        lines = _lines(form.get(key, ""))
        if lines:
            filters[key] = lines
    salary: dict = {}
    for key in ("min", "max"):
        value = _int_or_none(form.get(f"salary_{key}", ""))
        if value is not None:
            salary[key] = value
    if form.get("salary_currency", "").strip():
        salary["currency"] = form.get("salary_currency").strip()
    salary["keep_unlisted"] = bool(form.get("keep_unlisted"))
    if salary:
        filters["salary"] = salary
    raw["filters"] = filters
    return _save(request, raw)


@router.post("/config/notify")
async def save_notify(request: Request):
    raw = _raw_config(request)
    form = await request.form()
    notify = raw.get("notify") or {}
    notify["channels"] = form.getlist("channels")
    threshold = _int_or_none(form.get("digest_threshold", ""))
    if threshold is not None:
        notify["digest_threshold"] = threshold
    notify["health_alerts"] = bool(form.get("health_alerts"))
    empty_runs = _int_or_none(form.get("empty_runs_before_alert", ""))
    if empty_runs is not None:
        notify["empty_runs_before_alert"] = empty_runs
    raw["notify"] = notify
    return _save(request, raw)


@router.post("/config/general")
async def save_general(request: Request):
    raw = _raw_config(request)
    form = await request.form()
    for key in ("repost_window_days", "poll_interval_minutes"):
        value = _int_or_none(form.get(key, ""))
        if value is not None:
            raw[key] = value
    return _save(request, raw)


# --- status and actions -----------------------------------------------------

def _status_context(request: Request, store: Store) -> dict:
    scheduler = getattr(request.app.state, "scheduler", None)
    last_run = store.last_run()
    return {
        "scheduler": scheduler,
        "last_run": last_run,
        "site_health": store.site_health(),
        "secrets": {name: bool(os.environ.get(name)) for name in SECRET_VARS},
        "now": datetime.now(timezone.utc),
    }


@router.get("/status")
async def status_page(request: Request, store: Store = Depends(get_store)):
    return templates.TemplateResponse(request, "status.html",
                                      _status_context(request, store))


@router.get("/status/panel")
async def status_panel(request: Request, store: Store = Depends(get_store)):
    return templates.TemplateResponse(request, "_status_panel.html",
                                      _status_context(request, store))


@router.post("/run")
async def run_now(request: Request, store: Store = Depends(get_store)):
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.trigger_now()
    return templates.TemplateResponse(request, "_status_panel.html",
                                      _status_context(request, store))


@router.post("/test-notify")
async def test_notify(request: Request, store: Store = Depends(get_store)):
    context = _status_context(request, store)
    try:
        cfg = load_config(request.app.state.config_path)
        notifier = Notifier(build_channels(cfg))
        await asyncio.to_thread(notifier.test)
        context["test_notify_result"] = "test notification sent"
    except (ConfigError, NotifyError) as exc:
        context["test_notify_result"] = f"failed: {exc}"
    return templates.TemplateResponse(request, "_status_panel.html", context)


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}
