from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from archivex.source import PostSource
from archivex.storage import ArchiveRepository, PostInput

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountSyncResult:
    username: str
    status: str
    posts_seen: int = 0
    posts_new: int = 0
    error: str | None = None


class ArchiveSyncService:
    """Runs account synchronizations sequentially and makes replay safe via SQLite upserts."""

    def __init__(self, repository: ArchiveRepository, source: PostSource, initial_lookback_days: int,
                 now: Callable[[], datetime] | None = None) -> None:
        self.repository = repository
        self.source = source
        self.initial_lookback_days = initial_lookback_days
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
        cutoff = _sync_cutoff(account.last_sync_at, self.initial_lookback_days, self.now())
        try:
            async for post in self.source.fetch_timeline(account.x_user_id):
                if post.posted_at.astimezone(UTC) < cutoff:
                    break
                posts_seen += 1
                if self.repository.upsert_post(
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
                ):
                    posts_new += 1
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            self.repository.finish_sync_run(
                run_id, posts_seen=posts_seen, posts_new=posts_new, media_new=0,
                status="error", error=message
            )
            self.repository.mark_account_sync_error(account.id, message)
            logger.exception("Synchronization failed for @%s", username)
            return AccountSyncResult(username=username, status="error", posts_seen=posts_seen,
                                     posts_new=posts_new, error=message)

        completed_at = self.now()
        self.repository.finish_sync_run(
            run_id, posts_seen=posts_seen, posts_new=posts_new, media_new=0, status="success"
        )
        self.repository.mark_account_sync_success(account.id, completed_at)
        return AccountSyncResult(username=source_account.username, status="success",
                                 posts_seen=posts_seen, posts_new=posts_new)


def _sync_cutoff(last_sync_at: str | None, initial_lookback_days: int, now: datetime) -> datetime:
    if last_sync_at:
        return datetime.fromisoformat(last_sync_at).astimezone(UTC)
    return now.astimezone(UTC) - timedelta(days=initial_lookback_days)
