"""FastAPI application: web UI + in-process poll scheduler."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .deps import default_config_path, default_db_path
from .routes import router
from .scheduler import PollScheduler


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if app.state.scheduler is None:
        app.state.scheduler = PollScheduler(app.state.config_path, app.state.db_path)
    await app.state.scheduler.start()
    try:
        yield
    finally:
        await app.state.scheduler.stop()


def create_app(config_path: Path | None = None, db_path: Path | None = None,
               scheduler=None) -> FastAPI:
    """Build the app; tests pass explicit paths and a stub scheduler."""
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    app = FastAPI(title="jobfinder", lifespan=_lifespan)
    app.state.config_path = config_path or default_config_path()
    app.state.db_path = db_path or default_db_path()
    app.state.scheduler = scheduler
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")),
              name="static")
    return app


app = create_app()
