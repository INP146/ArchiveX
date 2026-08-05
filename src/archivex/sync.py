from __future__ import annotations

import logging
import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from archivex.media import MediaDownloader
from archivex.source import PostSource, SourceMedia, media_from_payload
from archivex.storage import ArchiveRepository, MediaInput, PostInput

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountSyncResult:
    username: str
    status: str
    posts_seen: int = 0
    posts_new: int = 0
    media_new: int = 0
    error: str | None = None


class ArchiveSyncService:
    """Runs account synchronizations sequentially and makes replay safe via SQLite upserts."""

    def __init__(self, repository: ArchiveRepository, source: PostSource, initial_post_limit: int,
                 incremental_known_post_limit: int,
                 media_downloader: MediaDownloader | None = None, media_enabled: bool = True,
                 media_max_bytes: int = 0,
                 now: Callable[[], datetime] | None = None) -> None:
        self.repository = repository
        self.source = source
        self.initial_post_limit = initial_post_limit
        self.incremental_known_post_limit = incremental_known_post_limit
        self.media_downloader = media_downloader
        self.media_enabled = media_enabled
        self.media_max_bytes = media_max_bytes
        self.now = now or (lambda: datetime.now(UTC))

    async def sync_accounts(self, usernames: Iterable[str]) -> list[AccountSyncResult]:
        results: list[AccountSyncResult] = []
        for username in usernames:
            results.append(await self.sync_account(username))
        return results

    async def sync_account(self, username: str) -> AccountSyncResult:
        try:
            source_account = await self.source.resolve_account(username)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            logger.exception("Account resolution failed for @%s", username)
            return AccountSyncResult(username=username, status="error", error=message)
        if source_account is None:
            return AccountSyncResult(username=username, status="error", error="account not found")

        account = self.repository.upsert_account(
            source_account.x_user_id, source_account.username, source_account.display_name
        )
        run_id = self.repository.start_sync_run(account.id)
        posts_seen = 0
        posts_new = 0
        media_new = 0
        is_initial_sync = account.last_sync_at is None
        consecutive_known_posts = 0
        try:
            media_new += await self._backfill_media()
            async for post in self.source.fetch_timeline(account.x_user_id):
                if is_initial_sync and 0 <= self.initial_post_limit <= posts_seen:
                    break
                posts_seen += 1
                is_new = self.repository.upsert_post(
                    PostInput(
                        tweet_id=post.tweet_id,
                        account_id=account.id,
                        username=post.username,
                        post_type=post.post_type,
                        text=post.text,
                        posted_at=post.posted_at,
                        permalink=post.permalink,
                        raw_payload=post.raw_payload,
                    )
                )
                if is_new:
                    posts_new += 1
                    consecutive_known_posts = 0
                elif not is_initial_sync:
                    consecutive_known_posts += 1
                media_new += self._persist_post_media(post.tweet_id, post.media)
                if self.media_enabled and self.media_downloader is not None:
                    await self._download_post_media(post.tweet_id)
                if (
                    not is_initial_sync
                    and self.incremental_known_post_limit != -1
                    and consecutive_known_posts >= self.incremental_known_post_limit
                ):
                    break
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            self.repository.finish_sync_run(
                run_id, posts_seen=posts_seen, posts_new=posts_new, media_new=media_new,
                status="error", error=message
            )
            self.repository.mark_account_sync_error(account.id, message)
            logger.exception("Synchronization failed for @%s", username)
            return AccountSyncResult(username=username, status="error", posts_seen=posts_seen,
                                     posts_new=posts_new, error=message)

        completed_at = self.now()
        self.repository.finish_sync_run(
            run_id, posts_seen=posts_seen, posts_new=posts_new, media_new=media_new, status="success"
        )
        self.repository.mark_account_sync_success(account.id, completed_at)
        return AccountSyncResult(username=source_account.username, status="success",
                                 posts_seen=posts_seen, posts_new=posts_new, media_new=media_new)

    async def _download_post_media(self, tweet_id: str) -> None:
        target_dir = self.repository.post_directory(tweet_id)
        for media in self.repository.media_to_download(tweet_id):
            try:
                result = await asyncio.to_thread(
                    self.media_downloader.download, media.source_url, target_dir, self.media_max_bytes
                )
                self.repository.complete_media(media.id, result.local_path, result.sha256)
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
                self.repository.fail_media(media.id, message)
                logger.warning("Media download failed for archived post %s", tweet_id)

    async def _backfill_media(self) -> int:
        media_new = 0
        for tweet_id, raw_payload in self.repository.unscanned_post_media():
            media_new += self._persist_post_media(tweet_id, media_from_payload(raw_payload))
            if self.media_enabled and self.media_downloader is not None:
                await self._download_post_media(tweet_id)
        return media_new

    def _persist_post_media(self, tweet_id: str, media_items: tuple[SourceMedia, ...]) -> int:
        media_new = sum(
            self.repository.create_media_if_missing(
                MediaInput(tweet_id, media.media_type, media.source_url)
            )
            for media in media_items
        )
        self.repository.mark_post_media_scanned(tweet_id)
        return media_new
