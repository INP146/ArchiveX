import asyncio
import json
import sqlite3
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from archivex.api import _post_response
from archivex.config import Settings
from archivex.main import create_app
from archivex.post_model import parse_post, UserSnapshot
from archivex.storage import ArchiveRepository, PostInput, initialize_storage
from archivex.sync import ArchiveSyncService
from archivex.task_center import (
    TaskCenterRepository,
    TASK_MEDIA_ID_LABEL,
    TASK_PARENT_ID_LABEL,
    TASK_OBSERVED_ACCOUNT_ID_LABEL,
)
from archivex import tasks
from archivex.task_dispatcher import InlineSyncTaskDispatcher
from archivex.media import DownloadResult


def tweet(identifier, author="1", text="body", media=None, **extra):
    return {
        "id": str(identifier),
        "user": {"id": author, "username": f"user{author}", "displayname": f"User {author}"},
        "rawContent": text,
        "date": "2026-09-19T12:00:00+00:00",
        "url": f"https://x.com/user{author}/status/{identifier}",
        "media": {"photos": [{"url": url} for url in media or []]},
        **extra,
    }


@pytest.fixture
def repo(tmp_path):
    database, archive = tmp_path / "archive.sqlite3", tmp_path / "archive"
    initialize_storage(database, archive, tmp_path / "sessions")
    r = ArchiveRepository(database, archive)
    r.upsert_account("1", "user1", "User 1")
    return r


def counts(repo):
    with sqlite3.connect(repo.database_path) as c:
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        return {
            t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in (
                "posts",
                "reposts",
                "media",
                "x_users",
                "observed_accounts",
                "archive_post_observations",
                "archive_repost_observations",
            )
        }


def test_parser_keeps_media_ownership_and_type_priority():
    origin = tweet("10", "2", "origin", ["inner"])
    quoted = tweet("11", "1", "comment", ["outer"], quotedTweet=origin, inReplyToTweetId="8")
    item = parse_post(quoted)
    assert item.post_type == "quote"
    assert item.text == "comment" and item.referenced.text == "origin"
    assert [m.source_url for m in item.own_media] == ["outer"]
    assert [m.source_url for m in item.referenced.own_media] == ["inner"]
    event = parse_post(tweet("12", retweetedTweet=quoted, quotedTweet=origin, inReplyToTweetId="8"))
    assert event.post_type == "repost" and event.own_media == ()
    assert event.referenced.post_type == "quote"
    with pytest.raises(ValueError, match="origin ID"):
        parse_post(tweet("13"), post_type="repost")


def test_shared_origin_self_repost_and_later_observed_author(repo):
    origin = tweet("10", "2", "shared", ["image"])
    for identifier in ["20", "21"]:
        result = repo.ingest_item(parse_post(tweet(identifier, retweetedTweet=origin)), "1")
        assert result.observation_new
    before = counts(repo)
    assert before["posts"] == 1 and before["reposts"] == 2 and before["media"] == 1
    assert before["observed_accounts"] == 1 and before["archive_post_observations"] == 0
    assert repo.list_enabled_account_ids() == ["1"]
    assert [i.tweet_id for i in repo.list_posts(observed_account_x_user_id="1")] == ["21", "20"]
    assert len(repo.media_ids_to_download("1")) == 1
    replay = repo.ingest_item(parse_post(tweet("21", retweetedTweet=origin)), "1")
    assert (
        not replay.observation_new
        and replay.media_new == replay.referenced_new == replay.reposts_new == 0
    )
    assert counts(repo) == before
    repo.upsert_account("2", "user2")
    added = repo.ingest_item(parse_post(origin), "2")
    assert added.observation_new and added.media_new == 0
    own_event = repo.ingest_item(parse_post(tweet("22", "2", retweetedTweet=origin)), "2")
    assert own_event.reposts_new == 1
    assert repo.get_post("10").capture_source == "timeline"
    assert counts(repo)["posts"] == 1 and counts(repo)["reposts"] == 3
    assert repo.get_account_details("1")["post_count"] == 2
    assert repo.get_account_details("2")["post_count"] == 2


def test_unknown_reference_can_be_hydrated_without_losing_complete_content(repo):
    item = parse_post(
        tweet("20", inReplyToTweetId="10", inReplyToUser={"id": "2", "username": "user2"})
    )
    assert item.referenced.raw_payload is None and item.referenced.availability == "unknown"
    repo.ingest_item(item, "1")
    placeholder = repo.get_post("10")
    assert placeholder.author_x_user_id == "2" and placeholder.text == ""
    assert not (repo.archive_data_dir / "posts/10/post.json").exists()
    repo.ensure_x_user(UserSnapshot("2", "renamed"))
    repo.upsert_post(
        PostInput(
            "10",
            "2",
            "original",
            "complete",
            datetime.now(UTC),
            "https://x.com/i/status/10",
            tweet("10", "2", "complete"),
        )
    )
    repo.ingest_item(item, "1")
    assert repo.get_post("10").text == "complete"
    assert (
        json.loads((repo.archive_data_dir / "posts/10/post.json").read_text())["rawContent"]
        == "complete"
    )


def test_observations_are_independent_and_transaction_rejects_wrong_author(repo):
    repo.upsert_post(
        PostInput(
            "10", "2", "original", "external", datetime.now(UTC), "https://x.com/i/status/10", {}
        )
    )
    repo.upsert_account("3", "user3")
    repo.observe_post("1", "10")
    repo.observe_post("3", "10")
    assert len(repo.list_posts()) == 1
    assert repo.list_posts(observed_account_x_user_id="3")[0].observed_account_x_user_id == "3"
    state = counts(repo)
    with pytest.raises(ValueError, match="source returned"):
        repo.ingest_item(parse_post(tweet("20", "2")), "1")
    assert counts(repo) == state
    raw = (repo.archive_data_dir / "posts/10/post.json").read_bytes()
    with pytest.raises(ValueError, match="conflicting author"):
        repo.ingest_item(parse_post(tweet("10", "1", quotedTweet=tweet("50", "4"))), "1")
    assert (
        counts(repo) == state and (repo.archive_data_dir / "posts/10/post.json").read_bytes() == raw
    )


def test_timeline_api_uses_event_time_origin_metrics_and_quote_cards(repo):
    origin = tweet(
        "10", "2", "needle origin", ["inner"], date="2026-09-18T00:00:00+00:00", likeCount=7
    )
    quoted = tweet("20", text="needle comment", media=["outer"], quotedTweet=origin, likeCount=11)
    repo.ingest_item(parse_post(quoted), "1")
    repo.ingest_item(
        parse_post(
            tweet("30", retweetedTweet=origin, date="2026-09-20T00:00:00+00:00", likeCount=999)
        ),
        "1",
    )
    repo.ingest_item(
        parse_post(tweet("31", retweetedTweet=origin, date="2026-09-21T00:00:00+00:00")), "1"
    )
    assert [p.tweet_id for p in repo.list_posts(limit=2)] == ["31", "30"]
    assert [p.tweet_id for p in repo.list_posts(limit=2, offset=2)] == ["20"]
    assert [p.tweet_id for p in repo.list_posts(query="needle")] == ["20"]
    assert len(repo.list_posts(query="needle", search_origin=True)) == 3
    quote_response = _post_response(repo.get_post("20"), repo)
    assert (
        quote_response["text"] == "needle comment"
        and quote_response["reference"]["text"] == "needle origin"
    )
    assert quote_response["like_count"] == 11 and quote_response["reference"]["like_count"] == 7
    assert quote_response["media"][0]["id"] != quote_response["reference"]["media"][0]["id"]
    event_response = _post_response(repo.get_post("30"), repo)
    assert event_response["origin"]["tweet_id"] == "10" and event_response["like_count"] == 7
    assert event_response["metrics_source_tweet_id"] == "10"
    assert event_response["permalink"].endswith("/30") and event_response["origin"][
        "permalink"
    ].endswith("/10")
    settings = Settings(
        _env_file=None,
        archive_db_path=repo.database_path,
        archive_data_dir=repo.archive_data_dir,
        twscrape_session_path=repo.database_path.parent / "sessions",
        task_queue_enabled=False,
        web_auth_token="test",
    )
    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": "Bearer test"}
        legacy = client.get("/api/posts?account_x_user_id=1", headers=headers)
        new = client.get("/api/posts?observed_account_x_user_id=1", headers=headers)
        assert legacy.json() == new.json()
        assert (
            client.get(
                "/api/posts?account_x_user_id=1&observed_account_x_user_id=2", headers=headers
            ).status_code
            == 422
        )
        assert client.get("/api/posts/30", headers=headers).json()["origin"]["tweet_id"] == "10"
        assert (
            len(client.get("/api/posts?q=needle&search_origin=true", headers=headers).json()) == 3
        )


def test_media_filter_only_follows_visible_quote_references(repo):
    origin = tweet("10", "2", media=["origin-image"])
    quote = tweet("20", quotedTweet=origin)
    reply = tweet("30", inReplyToTweetId="10", inReplyToUser={"id": "2", "username": "user2"})
    for payload in (
        quote,
        tweet("21", quotedTweet=quote),
        reply,
        tweet("31", media=["reply-image"], inReplyToTweetId="10"),
        tweet("40", quotedTweet=reply),
        tweet("50", retweetedTweet=reply),
        tweet("51", retweetedTweet=quote),
    ):
        repo.ingest_item(parse_post(payload), "1")

    with_media = {
        p.tweet_id for p in repo.list_posts(observed_account_x_user_id="1", has_media=True)
    }
    without_media = {
        p.tweet_id for p in repo.list_posts(observed_account_x_user_id="1", has_media=False)
    }
    assert with_media == {"20", "21", "31", "51"}
    assert without_media == {"30", "40", "50"}


def test_reply_username_fallback_is_local_to_each_post(repo):
    for identifier, target, username in (("20", "10", "alice"), ("21", "11", "bob")):
        reply = tweet(
            identifier, inReplyToTweetId=target, inReplyToUser=None, inReplyToScreenName=username
        )
        repo.ingest_item(parse_post(reply), "1")
        response = _post_response(repo.get_post(identifier), repo)
        assert response["reply_to_username"] == username
        assert response["reference"]["author"]["x_user_id"] == "unknown"
        assert response["reference"]["author"]["username"] is None
    assert _post_response(repo.get_post("20"), repo)["reply_to_username"] == "alice"
    repo.ingest_item(parse_post(tweet("30", retweetedTweet=reply)), "1")
    response = _post_response(repo.get_post("30"), repo)
    assert response["origin"]["reply_to_username"] == response["reply_to_username"] == "bob"
    assert repo.get_x_user("unknown")["username"] is None


def test_reply_prefers_known_author_username_over_payload_fallback(repo):
    repo.upsert_account("2", "current_name")
    reply = tweet(
        "20", inReplyToTweetId="10", inReplyToUser={"id": "2", "username": "old_name"},
        inReplyToScreenName="old_name",
    )
    repo.ingest_item(parse_post(reply), "1")
    assert _post_response(repo.get_post("20"), repo)["reply_to_username"] == "current_name"


def test_external_media_tasks_keep_parent_observer_and_can_complete_and_retry(repo, monkeypatch):
    origin = tweet("10", "2", media=["https://image.test/one.jpg"])
    repo.ingest_item(parse_post(tweet("20", retweetedTweet=origin)), "1")
    media_id = repo.media_ids_to_download("1")[0]
    lifecycle = TaskCenterRepository(repo.database_path, 3600, "crawl")
    parent, child = str(uuid.uuid4()), str(uuid.uuid4())
    lifecycle.record_queued(
        parent, "archivex.sync_account", "crawl", ["1"], {}, {TASK_OBSERVED_ACCOUNT_ID_LABEL: "1"}
    )
    lifecycle.record_queued(
        child,
        "archivex.download_media",
        "media",
        [media_id],
        {},
        {TASK_MEDIA_ID_LABEL: media_id, TASK_PARENT_ID_LABEL: parent},
    )
    task = lifecycle.get_task(child)
    assert task["observed_account_x_user_id"] == "1"
    assert task["context"]["post_author"]["x_user_id"] == "2"
    assert task["context"]["media"]["owner_tweet_id"] == "10"
    assert repo.get_account("2") is None

    class Downloader:
        fail = True

        def download(self, url, directory, max_bytes):
            if self.fail:
                raise RuntimeError("temporary failure")
            path = directory / "image.jpg"
            path.write_bytes(b"image")
            return DownloadResult(path, "checksum")

    downloader = Downloader()
    service = ArchiveSyncService(repo, None, 10, 10, media_downloader=downloader)
    dispatcher = InlineSyncTaskDispatcher(service)
    with pytest.raises(RuntimeError):
        asyncio.run(dispatcher.enqueue_media_download(media_id))
    assert repo.failed_media_post_ids("1") == ["10"]
    downloader.fail = False
    asyncio.run(dispatcher.enqueue_media_download(media_id))
    assert repo.get_media_record(media_id).download_status == "completed"
    assert repo.media_ids_to_download("1") == []


def test_sync_replay_counts_observations_and_only_queues_canonical_media(repo, monkeypatch):
    origin = tweet("10", "2", media=["image"])
    items = [
        parse_post(tweet("20", retweetedTweet=origin)),
        parse_post(tweet("21", retweetedTweet=origin)),
    ]

    class Source:
        async def fetch_timeline(self, x_user_id):
            for item in items:
                yield item

    service = ArchiveSyncService(repo, Source(), -1, -1)
    first = asyncio.run(service.sync_account("1"))
    assert (
        first.posts_seen,
        first.posts_new,
        first.reposts_new,
        first.referenced_new,
        first.media_new,
    ) == (2, 2, 2, 1, 1)
    second = asyncio.run(service.sync_account("1"))
    assert (second.posts_new, second.reposts_new, second.referenced_new, second.media_new) == (
        0,
        0,
        0,
        0,
    )
    queued = []

    async def enqueue(media_id, **kwargs):
        queued.append(media_id)
        from archivex.task_dispatcher import TaskSubmission

        return TaskSubmission("task", "queued", False)

    async def extend(*args):
        pass

    monkeypatch.setattr(tasks, "_repository", lambda: repo)
    monkeypatch.setattr(tasks, "enqueue_media_download", enqueue)
    monkeypatch.setattr(tasks, "_extend_execution_lock", extend)
    monkeypatch.setattr(tasks.settings, "archive_media_enabled", True)
    assert asyncio.run(tasks._enqueue_account_media("1", "lock", "task")) == (1, 0)
    assert len(queued) == 1
