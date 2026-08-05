import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from archivex.source import SourceAccount, SourcePost
from archivex.storage import ArchiveRepository, initialize_storage
from archivex.sync import ArchiveSyncService


class FakeSource:
    def __init__(self, accounts, posts, failures=()):
        self.accounts = accounts
        self.posts = posts
        self.failures = set(failures)
        self.calls = []

    async def resolve_account(self, username: str):
        self.calls.append(("resolve", username))
        return self.accounts.get(username)

    async def fetch_timeline(self, x_user_id: str) -> AsyncIterator[SourcePost]:
        self.calls.append(("fetch", x_user_id))
        if x_user_id in self.failures:
            raise RuntimeError("temporary X error")
        for post in self.posts.get(x_user_id, []):
            yield post


def _post(tweet_id: str, date: datetime, text: str = "post") -> SourcePost:
    return SourcePost(tweet_id, "1", "first", "original", text, date,
                      f"https://x.com/first/status/{tweet_id}", {"id": tweet_id})


def _service(tmp_path, source, initial_post_limit=1, incremental_known_post_limit=1):
    database_path = tmp_path / "archive.sqlite3"
    archive_path = tmp_path / "archive"
    initialize_storage(database_path, archive_path, tmp_path / "sessions")
    return ArchiveSyncService(ArchiveRepository(database_path, archive_path), source,
                              initial_post_limit, incremental_known_post_limit,
                              now=lambda: datetime(2026, 8, 5, tzinfo=UTC))


def test_initial_and_incremental_sync_are_idempotent(tmp_path) -> None:
    account = SourceAccount("1", "first", "First")
    source = FakeSource({"first": account}, {"1": [_post("2", datetime(2026, 8, 5, tzinfo=UTC)),
                                                       _post("1", datetime(2026, 6, 1, tzinfo=UTC))]})
    service = _service(tmp_path, source)

    first = asyncio.run(service.sync_account("first"))
    second = asyncio.run(service.sync_account("first"))

    assert (first.status, first.posts_seen, first.posts_new) == ("success", 1, 1)
    assert (second.status, second.posts_seen, second.posts_new) == ("success", 1, 0)


def test_account_failures_do_not_stop_later_accounts(tmp_path) -> None:
    first = SourceAccount("1", "first", "First")
    second = SourceAccount("2", "second", "Second")
    source = FakeSource({"first": first, "second": second}, {"2": [
        SourcePost("20", "2", "second", "reply", "reply", datetime(2026, 8, 4, tzinfo=UTC),
                   "https://x.com/second/status/20", {"id": "20"})
    ]}, failures={"1"})
    service = _service(tmp_path, source)

    results = asyncio.run(service.sync_accounts(["first", "second"]))

    assert [result.status for result in results] == ["error", "success"]
    assert results[1].posts_new == 1


def test_unlimited_initial_sync_imports_all_posts(tmp_path) -> None:
    account = SourceAccount("1", "first", "First")
    source = FakeSource({"first": account}, {"1": [
        _post("2", datetime(2026, 8, 5, tzinfo=UTC)),
        _post("1", datetime(2020, 1, 1, tzinfo=UTC)),
    ]})
    service = _service(tmp_path, source, initial_post_limit=-1)

    result = asyncio.run(service.sync_account("first"))

    assert (result.status, result.posts_seen, result.posts_new) == ("success", 2, 2)


def test_incremental_sync_stops_after_configured_consecutive_known_posts(tmp_path) -> None:
    account = SourceAccount("1", "first", "First")
    source = FakeSource({"first": account}, {"1": [
        _post("3", datetime(2026, 8, 3, tzinfo=UTC)),
        _post("2", datetime(2026, 8, 2, tzinfo=UTC)),
        _post("1", datetime(2026, 8, 1, tzinfo=UTC)),
    ]})
    service = _service(tmp_path, source, initial_post_limit=-1,
                       incremental_known_post_limit=2)
    asyncio.run(service.sync_account("first"))
    source.posts["1"] = [
        _post("4", datetime(2026, 8, 4, tzinfo=UTC)),
        _post("3", datetime(2026, 8, 3, tzinfo=UTC)),
        _post("5", datetime(2026, 8, 2, 12, tzinfo=UTC)),
        _post("2", datetime(2026, 8, 2, tzinfo=UTC)),
        _post("1", datetime(2026, 8, 1, tzinfo=UTC)),
        _post("0", datetime(2026, 7, 31, tzinfo=UTC)),
    ]

    result = asyncio.run(service.sync_account("first"))

    assert (result.posts_seen, result.posts_new) == (5, 2)
