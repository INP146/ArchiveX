from __future__ import annotations

import logging
import asyncio
from collections.abc import Callable, Iterable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime

from archivex.media import MediaDownloader
from archivex.source import PostSource, SourceMedia, media_from_payload
from archivex.storage import ArchiveRepository, MediaInput, PostInput

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountSyncResult:
    x_user_id: str
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
        self._account_locks: dict[tuple[int, str], asyncio.Lock] = {}

    async def sync_accounts(self, x_user_ids: Iterable[str]) -> list[AccountSyncResult]:
        results: list[AccountSyncResult] = []
        for x_user_id in x_user_ids:
            results.append(await self.sync_account(x_user_id))
        return results

    async def sync_account(self, x_user_id: str) -> AccountSyncResult:
        loop_key = (id(asyncio.get_running_loop()), x_user_id)
        lock = self._account_locks.setdefault(loop_key, asyncio.Lock())
        async with lock:
            return await self._sync_account(x_user_id)

    async def _sync_account(self, x_user_id: str) -> AccountSyncResult:
        account = self.repository.get_account(x_user_id)
        if account is None:
            return AccountSyncResult(
                x_user_id=x_user_id, username=x_user_id, status="error", error="account not found"
            )

        username = account.current_username or x_user_id
        run_id = self.repository.start_sync_run(x_user_id)
        posts_seen = 0
        posts_new = 0
        media_new = 0
        is_initial_sync = account.last_sync_at is None
        consecutive_known_posts = 0
        identity_observed = False
        try:
            if self.media_enabled and self.media_downloader is not None:
                await self._retry_failed_media(x_user_id)
            media_new += await self._backfill_media()
            async with aclosing(self.source.fetch_timeline(x_user_id)) as timeline:
                async for post in timeline:
                    if is_initial_sync and 0 <= self.initial_post_limit <= posts_seen:
                        break
                    if post.x_user_id != x_user_id:
                        raise ValueError(
                            f"source returned X user {post.x_user_id} while synchronizing {x_user_id}"
                        )
                    posts_seen += 1
                    if not identity_observed:
                        self.repository.observe_account_identity(
                            x_user_id, post.username, _display_name(post.raw_payload)
                        )
                        identity_observed = True
                    is_new = self.repository.upsert_post(
                        PostInput(
                            tweet_id=post.tweet_id,
                            account_x_user_id=x_user_id,
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
        except asyncio.CancelledError:
            self.repository.finish_sync_run(
                run_id, posts_seen=posts_seen, posts_new=posts_new, media_new=media_new,
                status="interrupted", error="synchronization cancelled"
            )
            raise
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            self.repository.finish_sync_run(
                run_id, posts_seen=posts_seen, posts_new=posts_new, media_new=media_new,
                status="error", error=message
            )
            self.repository.mark_account_sync_error(x_user_id, message)
            logger.exception("Synchronization failed for X user %s", x_user_id)
            return AccountSyncResult(x_user_id=x_user_id, username=username, status="error",
                                     posts_seen=posts_seen, posts_new=posts_new, error=message)

        completed_at = self.now()
        self.repository.finish_sync_run(
            run_id, posts_seen=posts_seen, posts_new=posts_new, media_new=media_new, status="success"
        )
        self.repository.mark_account_sync_success(x_user_id, completed_at)
        current = self.repository.get_account(x_user_id)
        return AccountSyncResult(x_user_id=x_user_id,
                                 username=(current.current_username
                                           if current and current.current_username else username),
                                 status="success",
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

    async def _retry_failed_media(self, account_x_user_id: str) -> None:
        for tweet_id in self.repository.failed_media_post_ids(account_x_user_id):
            await self._download_post_media(tweet_id)

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


def _display_name(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    user = payload.get("user")
    if not isinstance(user, dict):
        return None
    value = user.get("displayname") or user.get("displayName")
    return value if isinstance(value, str) and value else None
