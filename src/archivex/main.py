from __future__ import annotations

import hashlib
import logging
import secrets
from contextlib import AsyncExitStack, asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from archivex.api import create_api_router, create_auth_router
from archivex.config import Settings, get_settings
from archivex.logging import configure_logging
from archivex.media import GalleryDlMediaDownloader
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
        app_settings.task_dashboard_db_path,
        app_settings.archive_sync_interval_seconds,
        app_settings.task_crawl_queue_name,
    )
    task_dashboard = None
    if app_settings.task_queue_enabled and app_settings.task_dashboard_enabled:
        from archivex.task_dashboard import create_task_dashboard

        task_dashboard = create_task_dashboard(app_settings)

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
        logger.info(
            "ArchiveX started with %d enabled accounts; task queue %s",
            len(repository.list_enabled_account_ids()),
            "enabled" if app_settings.task_queue_enabled else "disabled",
        )
        async with AsyncExitStack() as stack:
            if task_dashboard is not None:
                dashboard_app = task_dashboard.application
                await stack.enter_async_context(
                    dashboard_app.router.lifespan_context(dashboard_app)
                )
            elif app_settings.task_queue_enabled:
                from archivex.tasks import broker

                await broker.startup()
                stack.push_async_callback(broker.shutdown)
            yield
        logger.info("ArchiveX stopped")

    app = FastAPI(title="ArchiveX", version="0.1.0", lifespan=lifespan)
    if task_dashboard is not None:
        from archivex.tasks import dashboard_api_token

        app.add_middleware(
            DashboardAccessMiddleware,
            dashboard_path=app_settings.task_dashboard_path,
            dashboard_api_token=dashboard_api_token(app_settings),
        )
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
    if task_dashboard is not None:
        app.mount(app_settings.task_dashboard_path, task_dashboard.application)

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


class DashboardAccessMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, dashboard_path: str, dashboard_api_token: str) -> None:
        super().__init__(app)
        self.dashboard_path = dashboard_path.rstrip("/")
        self.dashboard_api_token = dashboard_api_token

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path != self.dashboard_path and not path.startswith(self.dashboard_path + "/"):
            return await call_next(request)
        dashboard_token = request.headers.get("access-token")
        if dashboard_token and secrets.compare_digest(dashboard_token, self.dashboard_api_token):
            return await call_next(request)
        if request.method == "GET":
            return RedirectResponse("/tasks", status_code=303)
        return JSONResponse({"detail": "internal endpoint"}, status_code=401)
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
