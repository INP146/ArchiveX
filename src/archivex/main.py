from __future__ import annotations

import logging
import asyncio
import hashlib
from contextlib import asynccontextmanager
from contextlib import suppress
from typing import AsyncIterator

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from archivex.api import create_api_router, create_auth_router
from archivex.config import Settings, get_settings
from archivex.logging import configure_logging
from archivex.media import GalleryDlMediaDownloader
from archivex.session import SessionAccountManager, TwscrapeSessionAccountManager
from archivex.source import PostSource, TwscrapePostSource
from archivex.storage import initialize_storage
from archivex.storage import ArchiveRepository
from archivex.sync import ArchiveSyncService

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, post_source: PostSource | None = None,
               session_account_manager: SessionAccountManager | None = None) -> FastAPI:
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
        GalleryDlMediaDownloader(),
        app_settings.archive_media_enabled,
        app_settings.archive_media_max_bytes,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        configure_logging(app_settings.log_level)
        initialize_storage(
            app_settings.archive_db_path,
            app_settings.archive_data_dir,
            app_settings.twscrape_session_path,
        )
        interrupted_runs = repository.interrupt_running_sync_runs()
        if interrupted_runs:
            logger.warning(
                "Marked %d unfinished synchronization run(s) as interrupted",
                interrupted_runs,
            )
        stop_sync = asyncio.Event()
        sync_task = asyncio.create_task(
            _sync_loop(service, repository, app_settings.archive_sync_interval_seconds, stop_sync),
            name="archive-sync-loop",
        )
        logger.info(
            "ArchiveX started with %d enabled accounts",
            len(repository.list_enabled_account_ids()),
        )
        try:
            yield
        finally:
            stop_sync.set()
            sync_task.cancel()
            with suppress(asyncio.CancelledError):
                await sync_task
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
        repository, app_settings.web_auth_token, source, service, session_accounts
    ))

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _derived_session_secret(auth_token: str) -> str:
    return hashlib.sha256(f"archivex-session:{auth_token}".encode()).hexdigest()


async def _sync_loop(service: ArchiveSyncService, repository: ArchiveRepository,
                     interval_seconds: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        results = await service.sync_accounts(repository.list_enabled_account_ids())
        for result in results:
            if result.status == "error":
                # Source errors can contain request details, so keep logs account-scoped.
                logger.error("Synchronization failed for @%s", result.username)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


def main() -> None:
    """Development server entry point."""
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "archivex.main:create_app",
        factory=True,
        host=settings.web_host,
        port=settings.web_port,
        reload=True,
    )
