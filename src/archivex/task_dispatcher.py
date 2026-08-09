from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Protocol

from archivex.sync import ArchiveSyncService


@dataclass(frozen=True)
class TaskSubmission:
    task_id: str
    state: str
    duplicate: bool

    def as_dict(self) -> dict[str, str | bool]:
        return asdict(self)


class SyncTaskDispatcher(Protocol):
    async def enqueue_account_sync(
        self,
        x_user_id: str,
        trigger: str = "manual",
        retry_of: str | None = None,
    ) -> TaskSubmission: ...

    async def enqueue_media_download(
        self,
        media_id: str,
        retry_of: str | None = None,
    ) -> TaskSubmission: ...


class InlineSyncTaskDispatcher:
    """Test/development fallback used only when the distributed queue is disabled."""

    def __init__(self, service: ArchiveSyncService) -> None:
        self.service = service

    async def enqueue_account_sync(
        self,
        x_user_id: str,
        trigger: str = "manual",
        retry_of: str | None = None,
    ) -> TaskSubmission:
        result = await self.service.sync_account(x_user_id)
        return TaskSubmission(
            task_id=f"inline:{x_user_id}",
            state=result.status,
            duplicate=False,
        )

    async def enqueue_media_download(
        self,
        media_id: str,
        retry_of: str | None = None,
    ) -> TaskSubmission:
        media = self.service.repository.get_media_record(media_id)
        if media is None:
            return TaskSubmission(f"inline:media:{media_id}", "not_found", True)
        if media.download_status == "completed":
            return TaskSubmission(f"inline:media:{media_id}", "completed", True)
        if self.service.media_downloader is None:
            raise RuntimeError("media downloader is disabled")

        try:
            result = await asyncio.to_thread(
                self.service.media_downloader.download,
                media.source_url,
                self.service.repository.post_directory(media.tweet_id),
                self.service.media_max_bytes,
            )
            self.service.repository.complete_media(media.id, result.local_path, result.sha256)
        except Exception as exc:
            self.service.repository.fail_media(
                media.id, str(exc) or exc.__class__.__name__
            )
            raise
        return TaskSubmission(f"inline:media:{media_id}", "completed", False)


class TaskiqSyncTaskDispatcher:
    async def enqueue_account_sync(
        self,
        x_user_id: str,
        trigger: str = "manual",
        retry_of: str | None = None,
    ) -> TaskSubmission:
        from archivex.tasks import enqueue_account_sync

        return await enqueue_account_sync(x_user_id, trigger, retry_of)

    async def enqueue_media_download(
        self,
        media_id: str,
        retry_of: str | None = None,
    ) -> TaskSubmission:
        from archivex.tasks import enqueue_media_download

        return await enqueue_media_download(media_id, retry_of)
