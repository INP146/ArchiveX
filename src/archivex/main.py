from __future__ import annotations

import hashlib
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from archivex.api import create_api_router, create_auth_router
from archivex.config import Settings, get_settings
from archivex.logging import configure_logging
from archivex.media import GalleryDlMediaDownloader
from archivex.queue_health import SystemReadinessProbe
from archivex.session import SessionAccountManager, TwscrapeSessionAccountManager
from archivex.source import PostSource, TwscrapePostSource
from archivex.storage import initialize_storage
from archivex.storage import ArchiveRepository
from archivex.sync import ArchiveSyncService
from archivex.task_center import TaskCenterRepository
from archivex.task_dispatcher import (
    InlineSyncTaskDispatcher,
    SyncTaskDispatcher,
    TaskiqSyncTaskDispatcher,
)

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, post_source: PostSource | None = None,
               session_account_manager: SessionAccountManager | None = None,
               task_dispatcher: SyncTaskDispatcher | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    repository = ArchiveRepository(app_settings.archive_db_path, app_settings.archive_data_dir)
    source = post_source or TwscrapePostSource(app_settings.twscrape_session_path)
    session_accounts = (
        session_account_manager
        or TwscrapeSessionAccountManager(app_settings.twscrape_session_path)
    )
    service = ArchiveSyncService(
        repository,
        source,
        app_settings.archive_initial_post_limit,
        app_settings.archive_incremental_known_post_limit,
        GalleryDlMediaDownloader(timeout_seconds=app_settings.task_media_timeout_seconds),
        app_settings.archive_media_enabled,
        app_settings.archive_media_max_bytes,
    )
    dispatcher = task_dispatcher or (
        TaskiqSyncTaskDispatcher()
        if app_settings.task_queue_enabled
        else InlineSyncTaskDispatcher(service)
    )
    task_center = TaskCenterRepository(
        app_settings.task_lifecycle_db_path,
        app_settings.archive_sync_interval_seconds,
        app_settings.task_crawl_queue_name,
    )
    readiness_probe = SystemReadinessProbe(app_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        configure_logging(app_settings.log_level)
        initialize_storage(
            app_settings.archive_db_path,
            app_settings.archive_data_dir,
            app_settings.twscrape_session_path,
        )
        logger.info(
            "ArchiveX started with %d enabled accounts; task queue %s",
            len(repository.list_enabled_account_ids()),
            "enabled" if app_settings.task_queue_enabled else "disabled",
        )
        yield
        logger.info("ArchiveX stopped")

    app = FastAPI(title="ArchiveX", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=app_settings.web_session_secret or _derived_session_secret(app_settings.web_auth_token),
        session_cookie="archivex_session",
        max_age=60 * 60 * 24 * 30,
        same_site="lax",
        https_only=app_settings.web_cookie_secure,
    )
    app.include_router(create_auth_router(
        app_settings.web_auth_token,
        app_settings.web_auth_display_name,
        app_settings.web_auth_username,
        app_settings.web_auth_avatar_url,
    ))
    app.include_router(create_api_router(
        repository,
        app_settings.web_auth_token,
        source,
        session_accounts,
        dispatcher,
        task_center,
    ))

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", tags=["system"])
    async def ready() -> JSONResponse:
        result = await readiness_probe.check()
        return JSONResponse(
            result,
            status_code=200 if result["status"] == "ready" else 503,
        )

    return app


def _derived_session_secret(auth_token: str) -> str:
    return hashlib.sha256(f"archivex-session:{auth_token}".encode()).hexdigest()


def main() -> None:
    """Development server entry point."""
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "archivex.main:create_app",
        factory=True,
        host=settings.web_host,
        port=settings.web_port,
        reload=False,
    )
