"""FastAPI application: web UI + in-process poll scheduler."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..store import Store
from .deps import default_config_path, default_db_path
from .routes import router
from .scheduler import DigestScheduler, PollScheduler


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # A pending tailored-resume row can't have a live task after a restart —
    # sweep them into retryable error rows before serving.
    store = Store(app.state.db_path)
    try:
        swept = store.fail_pending_tailored("interrupted by restart")
        if swept:
            logging.getLogger("jobfinder.web").warning(
                "marked %d interrupted resume generation(s) as failed", swept)
    finally:
        store.close()

    if app.state.scheduler is None:
        app.state.scheduler = PollScheduler(app.state.config_path, app.state.db_path)
    if app.state.digest_scheduler is None:
        app.state.digest_scheduler = DigestScheduler(app.state.config_path,
                                                     app.state.db_path)
    await app.state.scheduler.start()
    await app.state.digest_scheduler.start()
    try:
        yield
    finally:
        await app.state.scheduler.stop()
        await app.state.digest_scheduler.stop()


def create_app(config_path: Path | None = None, db_path: Path | None = None,
               scheduler=None, digest_scheduler=None) -> FastAPI:
    """Build the app; tests pass explicit paths and stub schedulers."""
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    app = FastAPI(title="jobfinder", lifespan=_lifespan)
    app.state.config_path = config_path or default_config_path()
    app.state.db_path = db_path or default_db_path()
    app.state.scheduler = scheduler
    app.state.digest_scheduler = digest_scheduler
    app.state.tailor_tasks = set()  # keeps in-flight generation tasks alive
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")),
              name="static")
    return app


app = create_app()
