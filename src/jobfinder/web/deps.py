"""Runtime paths and per-request dependencies for the web app."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import Request

from ..store import Store


def default_config_path() -> Path:
    return Path(os.environ.get("JOBFINDER_CONFIG", "config.yaml"))


def default_db_path() -> Path:
    return Path(os.environ.get("JOBFINDER_DB", "jobfinder.db"))


def get_store(request: Request):
    """Short-lived read connection per request; WAL keeps it from blocking
    (or being blocked by) the scheduler thread's writes."""
    store = Store(request.app.state.db_path)
    try:
        yield store
    finally:
        store.close()
