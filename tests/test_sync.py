import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
import sqlite3

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
    return ArchiveSyncService(ArchiveRepository(database_path, archive_path), source,
                              initial_post_limit, incremental_known_post_limit,
                              media_downloader=downloader,
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


def test_new_media_is_downloaded_and_completed_media_is_not_downloaded_again(tmp_path) -> None:
    account = SourceAccount("1", "first", "First")
    post = SourcePost("2", "1", "first", "original", "post", datetime(2026, 8, 5, tzinfo=UTC),
                      "https://x.com/first/status/2", {"id": "2"},
                      (SourceMedia("image", "https://pbs.twimg.com/media/example.jpg"),))
    source = FakeSource({"first": account}, {"1": [post]})
    downloader = FakeDownloader()
    service = _service(tmp_path, source, downloader=downloader)

    first = asyncio.run(service.sync_account("first"))
    second = asyncio.run(service.sync_account("first"))

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

    result = asyncio.run(service.sync_account("first"))

    assert result.status == "success"
    with sqlite3.connect(tmp_path / "archive.sqlite3") as connection:
        status, error = connection.execute("SELECT download_status, error FROM media").fetchone()
    assert status == "failed"
    assert error == "media server unavailable"


def test_existing_posts_are_backfilled_from_their_raw_payload(tmp_path) -> None:
    account = SourceAccount("1", "first", "First")
    source = FakeSource({"first": account}, {"1": []})
    downloader = FakeDownloader()
    service = _service(tmp_path, source, downloader=downloader)
    archived_account = service.repository.upsert_account("1", "first", "First")
    service.repository.upsert_post(PostInput(
        "2", archived_account.id, "first", "original", "post", datetime(2026, 8, 5, tzinfo=UTC),
        "https://x.com/first/status/2", {
            "media": {"photos": [{"url": "https://pbs.twimg.com/media/example.jpg"}],
                      "videos": [], "animated": []}
        },
    ))

    result = asyncio.run(service.sync_account("first"))

    assert result.media_new == 1
    assert downloader.calls == [("https://pbs.twimg.com/media/example.jpg", 0)]
