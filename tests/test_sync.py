import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
import sqlite3

import pytest

from archivex.media import DownloadResult
from archivex.source import SourceAccount, SourceMedia, SourcePost
from archivex.storage import ArchiveRepository, PostInput, initialize_storage
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


class CloseTrackingTimeline:
    def __init__(self, posts):
        self.posts = iter(posts)
        self.close_called = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.posts)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def aclose(self) -> None:
        self.close_called = True


class CloseTrackingSource(FakeSource):
    def __init__(self, accounts, posts):
        super().__init__(accounts, posts)
        self.last_timeline = None

    def fetch_timeline(self, x_user_id: str) -> AsyncIterator[SourcePost]:
        self.calls.append(("fetch", x_user_id))
        self.last_timeline = CloseTrackingTimeline(self.posts.get(x_user_id, []))
        return self.last_timeline


class FakeDownloader:
    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail
        self.calls = []

    def download(self, source_url: str, target_dir: Path, max_bytes: int) -> DownloadResult:
        self.calls.append((source_url, max_bytes))
        if self.should_fail:
            raise RuntimeError("media server unavailable")
        path = target_dir / "media-01.jpg"
        path.write_bytes(b"image")
        return DownloadResult(path, "a" * 64)


def _post(tweet_id: str, date: datetime, text: str = "post") -> SourcePost:
    return SourcePost(tweet_id, "1", "first", "original", text, date,
                      f"https://x.com/first/status/{tweet_id}", {"id": tweet_id})


def _service(tmp_path, source, initial_post_limit=1, incremental_known_post_limit=1,
             downloader=None):
    database_path = tmp_path / "archive.sqlite3"
    archive_path = tmp_path / "archive"
    initialize_storage(database_path, archive_path, tmp_path / "sessions")
    repository = ArchiveRepository(database_path, archive_path)
    for account in {item.x_user_id: item for item in source.accounts.values()}.values():
        repository.upsert_account(account.x_user_id, account.username, account.display_name)
    return ArchiveSyncService(repository, source, initial_post_limit,
                              incremental_known_post_limit, media_downloader=downloader,
                              now=lambda: datetime(2026, 8, 5, tzinfo=UTC))


def test_initial_and_incremental_sync_are_idempotent(tmp_path) -> None:
    account = SourceAccount("1", "first", "First")
    source = FakeSource({"first": account}, {"1": [_post("2", datetime(2026, 8, 5, tzinfo=UTC)),
                                                       _post("1", datetime(2026, 6, 1, tzinfo=UTC))]})
    service = _service(tmp_path, source)

    first = asyncio.run(service.sync_account("1"))
    second = asyncio.run(service.sync_account("1"))

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

    results = asyncio.run(service.sync_accounts(["1", "2"]))

    assert [result.status for result in results] == ["error", "success"]
    assert results[1].posts_new == 1


def test_unlimited_initial_sync_imports_all_posts(tmp_path) -> None:
    account = SourceAccount("1", "first", "First")
    source = FakeSource({"first": account}, {"1": [
        _post("2", datetime(2026, 8, 5, tzinfo=UTC)),
        _post("1", datetime(2020, 1, 1, tzinfo=UTC)),
    ]})
    service = _service(tmp_path, source, initial_post_limit=-1)

    result = asyncio.run(service.sync_account("1"))

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
    asyncio.run(service.sync_account("1"))
    source.posts["1"] = [
        _post("4", datetime(2026, 8, 4, tzinfo=UTC)),
        _post("3", datetime(2026, 8, 3, tzinfo=UTC)),
        _post("5", datetime(2026, 8, 2, 12, tzinfo=UTC)),
        _post("2", datetime(2026, 8, 2, tzinfo=UTC)),
        _post("1", datetime(2026, 8, 1, tzinfo=UTC)),
        _post("0", datetime(2026, 7, 31, tzinfo=UTC)),
    ]

    result = asyncio.run(service.sync_account("1"))

    assert (result.posts_seen, result.posts_new) == (5, 2)


def test_incremental_limit_closes_timeline_before_sync_returns(tmp_path) -> None:
    account = SourceAccount("1", "first", "First")
    source = CloseTrackingSource({"first": account}, {"1": [
        _post("2", datetime(2026, 8, 2, tzinfo=UTC)),
        _post("1", datetime(2026, 8, 1, tzinfo=UTC)),
    ]})
    service = _service(tmp_path, source, initial_post_limit=-1,
                       incremental_known_post_limit=1)

    async def run_syncs() -> None:
        await service.sync_account("1")
        result = await service.sync_account("1")
        assert result.posts_seen == 1
        assert source.last_timeline.close_called is True

    asyncio.run(run_syncs())


def test_interrupted_initial_sync_ignores_incremental_known_post_limit(tmp_path) -> None:
    account = SourceAccount("1", "first", "First")
    source = FakeSource({"first": account}, {"1": [
        _post("4", datetime(2026, 8, 4, tzinfo=UTC)),
        _post("3", datetime(2026, 8, 3, tzinfo=UTC)),
        _post("2", datetime(2026, 8, 2, tzinfo=UTC)),
        _post("1", datetime(2026, 8, 1, tzinfo=UTC)),
    ]})
    service = _service(tmp_path, source, initial_post_limit=-1,
                       incremental_known_post_limit=2)
    for post in source.posts["1"][:3]:
        service.repository.upsert_post(PostInput(
            post.tweet_id, post.x_user_id, post.post_type, post.text, post.posted_at,
            post.permalink, post.raw_payload,
        ))

    result = asyncio.run(service.sync_account("1"))

    assert (result.status, result.posts_seen, result.posts_new) == ("success", 4, 1)
    assert service.repository.get_post("1") is not None


def test_cancelled_sync_run_is_marked_interrupted(tmp_path) -> None:
    account = SourceAccount("1", "first", "First")

    class BlockingSource(FakeSource):
        def __init__(self):
            super().__init__({"first": account}, {})
            self.started = asyncio.Event()

        async def fetch_timeline(self, x_user_id: str) -> AsyncIterator[SourcePost]:
            self.started.set()
            await asyncio.Event().wait()
            if False:
                yield

    source = BlockingSource()
    service = _service(tmp_path, source, initial_post_limit=-1)

    async def cancel_sync() -> None:
        task = asyncio.create_task(service.sync_account("1"))
        await source.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_sync())

    run = service.repository.list_sync_runs(account_x_user_id="1")[0]
    assert run.status == "interrupted"
    assert run.finished_at is not None
    assert run.error == "synchronization cancelled"
    assert service.repository.get_account("1").last_sync_at is None


def test_new_media_is_downloaded_and_completed_media_is_not_downloaded_again(tmp_path) -> None:
    account = SourceAccount("1", "first", "First")
    post = SourcePost("2", "1", "first", "original", "post", datetime(2026, 8, 5, tzinfo=UTC),
                      "https://x.com/first/status/2", {"id": "2"},
                      (SourceMedia("image", "https://pbs.twimg.com/media/example.jpg"),))
    source = FakeSource({"first": account}, {"1": [post]})
    downloader = FakeDownloader()
    service = _service(tmp_path, source, downloader=downloader)

    first = asyncio.run(service.sync_account("1"))
    second = asyncio.run(service.sync_account("1"))

    assert first.media_new == 1
    assert second.media_new == 0
    assert downloader.calls == [("https://pbs.twimg.com/media/example.jpg", 0)]
    with sqlite3.connect(tmp_path / "archive.sqlite3") as connection:
        status, local_path, sha256 = connection.execute(
            "SELECT download_status, local_path, sha256 FROM media"
        ).fetchone()
    assert status == "completed"
    assert local_path.endswith("media-01.jpg")
    assert sha256 == "a" * 64


def test_failed_media_download_is_recorded_without_failing_post_sync(tmp_path) -> None:
    account = SourceAccount("1", "first", "First")
    post = SourcePost("2", "1", "first", "original", "post", datetime(2026, 8, 5, tzinfo=UTC),
                      "https://x.com/first/status/2", {"id": "2"},
                      (SourceMedia("image", "https://pbs.twimg.com/media/example.jpg"),))
    service = _service(tmp_path, FakeSource({"first": account}, {"1": [post]}),
                       downloader=FakeDownloader(should_fail=True))

    result = asyncio.run(service.sync_account("1"))

    assert result.status == "success"
    with sqlite3.connect(tmp_path / "archive.sqlite3") as connection:
        status, error = connection.execute("SELECT download_status, error FROM media").fetchone()
    assert status == "failed"
    assert error == "media server unavailable"


def test_failed_media_is_retried_when_post_is_absent_from_later_timeline(tmp_path) -> None:
    account = SourceAccount("1", "first", "First")
    post = SourcePost("2", "1", "first", "original", "post", datetime(2026, 8, 5, tzinfo=UTC),
                      "https://x.com/first/status/2", {"id": "2"},
                      (SourceMedia("image", "https://pbs.twimg.com/media/example.jpg"),))
    source = FakeSource({"first": account}, {"1": [post]})
    downloader = FakeDownloader(should_fail=True)
    service = _service(tmp_path, source, downloader=downloader)
    asyncio.run(service.sync_account("1"))

    source.posts["1"] = []
    downloader.should_fail = False
    result = asyncio.run(service.sync_account("1"))

    assert result.status == "success"
    assert len(downloader.calls) == 2
    with sqlite3.connect(tmp_path / "archive.sqlite3") as connection:
        status, error = connection.execute(
            "SELECT download_status, error FROM media"
        ).fetchone()
    assert status == "completed"
    assert error is None


def test_existing_posts_are_backfilled_from_their_raw_payload(tmp_path) -> None:
    account = SourceAccount("1", "first", "First")
    source = FakeSource({"first": account}, {"1": []})
    downloader = FakeDownloader()
    service = _service(tmp_path, source, downloader=downloader)
    archived_account = service.repository.upsert_account("1", "first", "First")
    service.repository.upsert_post(PostInput(
        "2", archived_account.x_user_id, "repost", "post", datetime(2026, 8, 5, tzinfo=UTC),
        "https://x.com/first/status/2", {
            "media": {},
            "retweetedTweet": {
                "media": {"photos": [{"url": "https://pbs.twimg.com/media/example.jpg"}],
                          "videos": [], "animated": []}
            },
        },
    ))

    result = asyncio.run(service.sync_account("1"))

    assert result.media_new == 1
    assert downloader.calls == [("https://pbs.twimg.com/media/example.jpg", 0)]


def test_sync_uses_stable_x_user_id_after_username_changes_owner(tmp_path) -> None:
    original = SourceAccount("1", "alice", "Original")
    replacement = SourceAccount("2", "alice", "Replacement")
    renamed_post = SourcePost(
        "10", "1", "alice_new", "original", "renamed", datetime(2026, 8, 5, tzinfo=UTC),
        "https://x.com/alice_new/status/10",
        {"id": "10", "user": {"username": "alice_new", "displayname": "Original"}},
    )
    historical_post = SourcePost(
        "9", "1", "alice", "original", "old", datetime(2026, 8, 4, tzinfo=UTC),
        "https://x.com/alice/status/9", {"id": "9"},
    )
    source = FakeSource({"alice": replacement}, {"1": [renamed_post, historical_post]})
    service = _service(tmp_path, source, initial_post_limit=-1)
    service.repository.upsert_account(original.x_user_id, original.username, original.display_name)

    result = asyncio.run(service.sync_account("1"))

    assert result.status == "success"
    assert source.calls == [("fetch", "1")]
    assert service.repository.get_account("1").current_username == "alice_new"
    assert service.repository.get_account("2").current_username == "alice"


def test_sync_rejects_posts_from_a_different_x_user_id(tmp_path) -> None:
    account = SourceAccount("1", "alice", "Alice")
    wrong_post = SourcePost(
        "10", "2", "alice", "original", "wrong", datetime(2026, 8, 5, tzinfo=UTC),
        "https://x.com/alice/status/10", {"id": "10"},
    )
    service = _service(tmp_path, FakeSource({"alice": account}, {"1": [wrong_post]}))

    result = asyncio.run(service.sync_account("1"))

    assert result.status == "error"
    assert "source returned X user 2" in result.error
    assert service.repository.get_post("10") is None
