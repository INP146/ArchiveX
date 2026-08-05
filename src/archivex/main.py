from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from archivex.config import Settings, get_settings
from archivex.logging import configure_logging
from archivex.storage import initialize_storage

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        configure_logging(app_settings.log_level)
        initialize_storage(
            app_settings.archive_db_path,
            app_settings.archive_data_dir,
            app_settings.twscrape_session_path,
        )
        logger.info("ArchiveX started with %d configured accounts", len(app_settings.archive_accounts))
        yield
        logger.info("ArchiveX stopped")

    app = FastAPI(title="ArchiveX", version="0.1.0", lifespan=lifespan)

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app

