import asyncio
import sqlite3
import uuid
from datetime import UTC, datetime
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from twscrape.queue_client import GqlFeaturesOutdatedError
from twscrape.accounts_pool import NoAccountError

from archivex import tasks
from archivex.main import create_app
from archivex.post_model import parse_post
from archivex.source import TwscrapeResponseError
from archivex.storage import ArchiveRepository, initialize_storage
from archivex.sync import ArchiveSyncService
from archivex.task_center import TaskCenterRepository, TASK_FETCH_TWEET_ID_LABEL, TASK_OBSERVED_ACCOUNT_ID_LABEL
from archivex.task_dispatcher import TaskSubmission
from test_source import ConversationApi, CloseTrackingTimeline, _conversation_source, _page, _tweet_result
from test_api import FakeSource, FakeSessionAccountManager, FakeTaskDispatcher, _settings
from test_tasks import FakeRedis, FakeTask


def repository_at(tmp_path):
    database = tmp_path / "archive.sqlite3"
    archive = tmp_path / "archive"
    initialize_storage(database, archive, tmp_path / "sessions")
    repository = ArchiveRepository(database, archive)
    repository.upsert_account("1", "user1")
    return repository


def service_at(repository, api):
    source = _conversation_source(api)
    source.repository = repository
    return ArchiveSyncService(repository, source, -1, 1, media_enabled=False)


def test_second_sync_reuses_complete_snapshot_without_detail_requests(tmp_path):
    repository = repository_at(tmp_path)
    api = ConversationApi([_page([_tweet_result()], ["10", "11"])],
                          {"11": _tweet_result("11", reply_to="10")})
    first = asyncio.run(service_at(repository, api).sync_account("1"))
    assert first.status == "success" and api.detail_calls == ["11"]
    second_api = ConversationApi([_page([_tweet_result()], ["10", "11"])], {})
    second = asyncio.run(service_at(repository, second_api).sync_account("1"))
    assert second_api.detail_calls == []
    assert second.posts_new == 0
    assert repository.get_post("11").text == "post 11"


@pytest.mark.parametrize("broken", ["placeholder", "missing_file", "bad_json", "bad_encoding"])
def test_id_or_damaged_file_is_not_a_complete_snapshot(tmp_path, broken):
    repository = repository_at(tmp_path)
    api = ConversationApi([_page([_tweet_result()], ["10", "11"])],
                          {"11": _tweet_result("11", reply_to="10")})
    if broken == "placeholder":
        repository.ingest_item(parse_post({"id": "12", "user": {"id": "1"},
                                           "rawContent": "reply", "inReplyToTweetId": "11"}), "1")
    else:
        asyncio.run(service_at(repository, api).sync_account("1"))
        path = repository.post_directory("11") / "post.json"
        if broken == "missing_file":
            path.unlink()
        elif broken == "bad_encoding":
            path.write_bytes(b"\xff")
        else:
            path.write_text("invalid JSON")
        api.timeline = CloseTrackingTimeline([_page([_tweet_result()], ["10", "11"])])
        api.detail_calls.clear()
    assert repository.cached_post_snapshot("11") is None
    result = asyncio.run(service_at(repository, api).sync_account("1"))
    assert result.status == "success"
    assert api.detail_calls == ["11"]
    assert repository.cached_post_snapshot("11") is not None


def test_cached_embedded_content_still_gains_timeline_observation(tmp_path):
    repository = repository_at(tmp_path)
    embedded = {"id": "11", "user": {"id": "1", "username": "user1"}, "rawContent": "embedded"}
    quote = parse_post({"id": "12", "user": {"id": "1", "username": "user1"},
                        "rawContent": "quote", "quotedTweet": embedded})
    repository.ingest_item(quote, "1")
    api = ConversationApi([_page([_tweet_result()], ["10", "11"])], {})
    result = asyncio.run(service_at(repository, api).sync_account("1"))
    assert api.detail_calls == []
    assert result.posts_new == 2  # visible root + newly observed cached content
    assert any(post.tweet_id == "11" for post in repository.list_posts(observed_account_x_user_id="1"))


def test_one_failed_detail_does_not_block_page_and_survives_new_source(tmp_path):
    repository = repository_at(tmp_path)
    api = ConversationApi([_page([_tweet_result()], ["10", "11", "12"])],
                          {"11": None, "12": _tweet_result("12", reply_to="10")})
    result = asyncio.run(service_at(repository, api).sync_account("1"))
    assert result.status == "partial" and result.posts_pending == 1
    assert repository.get_post("10") and repository.get_post("12")
    assert repository.post_fetch("1", "11")["status"] == "pending"
    assert repository.list_sync_runs()[0].status == "partial"
    # A new process/source sees durable gaps even if incremental pagination
    # never returns that old conversation again.
    new_repository = ArchiveRepository(repository.database_path, repository.archive_data_dir)
    retry_api = ConversationApi([], {"11": _tweet_result("11", reply_to="10")})
    recovered = asyncio.run(service_at(new_repository, retry_api).recover_post("1", "11"))
    assert recovered["status"] == "success"
    assert repository.post_fetch_counts("1")["pending"] == 0
    assert repository.get_post("11").availability == "available"


def test_failed_detail_is_not_repeated_by_each_timeline_scan(tmp_path):
    repository = repository_at(tmp_path)
    api = ConversationApi([_page([_tweet_result()], ["10", "11"])], {"11": None})
    asyncio.run(service_at(repository, api).sync_account("1"))
    api2 = ConversationApi([_page([_tweet_result()], ["10", "11"])], {})
    result = asyncio.run(service_at(repository, api2).sync_account("1"))
    assert result.status == "partial"
    assert api2.detail_calls == []
    assert repository.post_fetch("1", "11")["attempt_count"] == 1


def test_locked_detail_endpoint_does_not_block_available_timeline_pages(tmp_path):
    repository = repository_at(tmp_path)
    api = ConversationApi([
        _page([_tweet_result()], ["10", "11", "12"]),
        _page([_tweet_result("20")], ["20", "21"]),
    ], {"11": NoAccountError("No account available")})
    class Pool:
        async def get_all(self):
            return [SimpleNamespace(active=True, locks={"TweetDetail": datetime.now(UTC) + timedelta(minutes=15)})]
    api.pool = Pool()
    result = asyncio.run(service_at(repository, api).sync_account("1"))
    assert result.status == "partial" and result.posts_pending == 3
    assert repository.get_post("10") and repository.get_post("20")
    assert api.detail_calls == ["11"]
    assert "TweetDetail" in repository.post_fetch("1", "11")["last_error"]


def test_explicit_unavailable_stops_automatic_requests_but_keeps_pending_unknown(tmp_path):
    repository = repository_at(tmp_path)
    class Api(ConversationApi):
        async def tweet_details_raw(self, tweet_id):
            if tweet_id == 11:
                self.detail_calls.append("11")
                return SimpleNamespace(json=lambda: {"errors": [{"code": 144, "message": "No status found"}]})
            return await super().tweet_details_raw(tweet_id)
    api = Api([_page([_tweet_result()], ["10", "11", "12"])], {"12": None})
    result = asyncio.run(service_at(repository, api).sync_account("1"))
    assert (result.posts_pending, result.posts_unavailable) == (1, 1)
    assert repository.get_post("10") is not None
    api2 = ConversationApi([_page([_tweet_result()], ["10", "11", "12"])], {})
    asyncio.run(service_at(repository, api2).sync_account("1"))
    assert api2.detail_calls == []
    assert [row["tweet_id"] for row in repository.post_fetches("1")] == ["12"]


def empty_focal_response(tweet_id="11", *, terminated=True, errors=None):
    instructions = [{"type": "TimelineAddEntries", "entries": [{
        "entryId": f"tweet-{tweet_id}", "content": {"itemContent": {
            "itemType": "TimelineTweet", "tweet_results": {},
        }},
    }]}]
    if terminated:
        instructions.extend([{"type": "TimelineTerminateTimeline", "direction": "Top"},
                             {"type": "TimelineTerminateTimeline", "direction": "Bottom"}])
    payload = {"data": {"threaded_conversation_with_injections_v2": {"instructions": instructions}}}
    if errors:
        payload["errors"] = errors
    return payload


def test_completed_empty_focal_response_is_unavailable_and_does_not_keep_sync_partial(tmp_path):
    repository = repository_at(tmp_path)
    class Api(ConversationApi):
        async def tweet_details_raw(self, tweet_id):
            self.detail_calls.append(str(tweet_id))
            return SimpleNamespace(json=lambda: empty_focal_response(str(tweet_id)))
    api = Api([_page([_tweet_result()], ["10", "11"])], {})
    result = asyncio.run(service_at(repository, api).sync_account("1"))
    assert result.status == "success" and result.posts_unavailable == 1
    assert result.posts_pending == result.posts_failed == 0
    assert repository.post_fetch("1", "11")["status"] == "unavailable"
    assert repository.post_fetches("1") == []


@pytest.mark.parametrize("payload", [
    empty_focal_response(terminated=False),
    empty_focal_response(errors=[{"code": -1, "message": "LoadShed"}]),
    empty_focal_response("12"),
])
def test_incomplete_or_errored_empty_response_is_not_classified_as_unavailable(payload):
    import archivex.source as source_module
    assert not source_module._focal_tweet_unavailable(payload, "11")


def test_final_recovery_failure_exits_pending_queue_until_manual_retry(monkeypatch, tmp_path):
    repository = repository_at(tmp_path)
    api = ConversationApi([], {"11": None})
    source = service_at(repository, api).source
    redis = FakeRedis()
    monkeypatch.setattr(tasks, "_redis", lambda: redis)
    monkeypatch.setattr(tasks, "_repository", lambda: repository)
    monkeypatch.setattr(tasks, "TwscrapePostSource", lambda *args, **kwargs: source)
    context = SimpleNamespace(message=SimpleNamespace(task_id=str(uuid.uuid4()),
        labels={"_retries": 4, "max_retries": 5}))
    with pytest.raises(TwscrapeResponseError):
        asyncio.run(tasks.fetch_post_detail_task.original_func("1", "11", context=context))
    assert repository.post_fetch_counts("1") == {"pending": 0, "unavailable": 0, "failed": 1}
    assert repository.post_fetches("1") == []
    assert tasks._post_fetch_lock_key("1", "11") not in redis.values
    assert asyncio.run(service_at(repository, api).recover_post("1", "11"))["status"] == "exhausted"
    assert api.detail_calls == ["11"]
    repository.reset_post_fetch("1", "11")
    api.details["11"] = _tweet_result("11", reply_to="10")
    recovered = asyncio.run(service_at(repository, api).recover_post("1", "11"))
    assert recovered["status"] == "success"
    assert repository.post_fetch_counts("1")["failed"] == 0


def test_old_recovery_table_upgrade_stops_exhausted_tasks_but_preserves_active_tasks(tmp_path):
    repository = repository_at(tmp_path)
    lifecycle = TaskCenterRepository(repository.database_path, 3600, "archivex:crawl")
    for tweet_id in ("11", "12"):
        repository.register_post_fetch("1", tweet_id)
        labels = {TASK_OBSERVED_ACCOUNT_ID_LABEL: "1", TASK_FETCH_TWEET_ID_LABEL: tweet_id,
                  "_retries": 4, "max_retries": 5}
        lifecycle.record_finished(str(uuid.uuid4()), labels, result=None, error="failed",
            name="archivex.fetch_post_detail", worker="archivex:crawl")
    lifecycle.record_queued(str(uuid.uuid4()), "archivex.fetch_post_detail", "archivex:crawl", [], {},
        {TASK_OBSERVED_ACCOUNT_ID_LABEL: "1", TASK_FETCH_TWEET_ID_LABEL: "12", "max_retries": 5})
    with sqlite3.connect(repository.database_path) as c:
        c.execute("ALTER TABLE archive_post_fetches DROP COLUMN retry_exhausted")
    upgraded = ArchiveRepository(repository.database_path, repository.archive_data_dir)
    assert upgraded.post_fetch("1", "11")["retry_exhausted"] == 1
    assert upgraded.post_fetch("1", "12")["retry_exhausted"] == 0
    assert upgraded.post_fetch_counts("1") == {"pending": 1, "unavailable": 0, "failed": 1}


def test_other_reply_tombstone_does_not_mark_focal_tweet_unavailable():
    import archivex.source as source_module
    payload = {"entries": [{"entryId": "tweet-12", "content": {"__typename": "TweetTombstone"}}]}
    assert not source_module._focal_tweet_unavailable(payload, "11")
    assert source_module._focal_tweet_unavailable(payload, "12")


def test_unrelated_missing_reply_error_does_not_turn_parse_failure_into_unavailable():
    import archivex.source as source_module
    payload = {"errors": [{"code": 144, "message": "No status found"}],
               "data": {"tweet": {"__typename": "Tweet", "rest_id": "11", "legacy": {}}}}
    assert not source_module._focal_tweet_unavailable(payload, "11")


def test_all_discovered_ids_persist_before_global_feature_failure(tmp_path):
    repository = repository_at(tmp_path)
    api = ConversationApi([_page([_tweet_result()], ["10", "11", "12"])],
                          {"11": GqlFeaturesOutdatedError("Update GQL_FEATURES")})
    with pytest.raises(TwscrapeResponseError):
        asyncio.run(service_at(repository, api).sync_account("1"))
    assert {row["tweet_id"] for row in repository.post_fetches("1")} == {"11", "12"}
    assert api.timeline.close_called


def test_cached_unobserved_content_remains_recoverable_after_global_failure(tmp_path):
    repository = repository_at(tmp_path)
    from archivex.storage import PostInput
    payload = {"id": "11", "rawContent": "cached", "user": {"id": "1", "username": "user1"}}
    repository.upsert_post(PostInput("11", "1", "original", "cached", datetime(2026, 8, 8, tzinfo=UTC),
                                     "https://x.com/user1/status/11", payload))
    api = ConversationApi([_page([_tweet_result()], ["11", "12"])],
                          {"12": GqlFeaturesOutdatedError("Update GQL_FEATURES")})
    with pytest.raises(TwscrapeResponseError):
        asyncio.run(service_at(repository, api).sync_account("1"))
    assert not repository.has_post_observation("1", "11")
    assert {row["tweet_id"] for row in repository.post_fetches("1")} == {"11", "12"}
    recovery_api = ConversationApi([], {})
    result = asyncio.run(service_at(repository, recovery_api).recover_post("1", "11"))
    assert result["status"] == "success" and repository.has_post_observation("1", "11")
    assert recovery_api.detail_calls == []


def test_cancellation_preserves_all_gaps_and_closes_upstream(tmp_path):
    repository = repository_at(tmp_path)
    class Api(ConversationApi):
        async def tweet_details_raw(self, tweet_id):
            raise asyncio.CancelledError()
    api = Api([_page([_tweet_result()], ["10", "11", "12"])], {})
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service_at(repository, api).sync_account("1"))
    assert {row["tweet_id"] for row in repository.post_fetches("1")} == {"11", "12"}
    assert repository.list_sync_runs()[0].status == "interrupted"
    assert api.timeline.close_called


def test_foreign_author_remains_excluded_from_independent_recovery(tmp_path):
    repository = repository_at(tmp_path)
    api = ConversationApi([], {"11": _tweet_result("11", "2")})
    result = asyncio.run(service_at(repository, api).recover_post("1", "11"))
    assert result["status"] == "ignored"
    assert repository.get_post("11") is None
    assert repository.post_fetch_counts("1")["pending"] == 0


def test_recovery_enqueue_dedupes_per_post_not_per_account(monkeypatch):
    redis = FakeRedis()
    fake = FakeTask()
    monkeypatch.setattr(tasks, "_redis", lambda: redis)
    monkeypatch.setattr(tasks, "fetch_post_detail_task", fake)
    async def submit():
        first = await tasks.enqueue_post_fetch("1", "11")
        second = await tasks.enqueue_post_fetch("1", "12")
        duplicate = await tasks.enqueue_post_fetch("1", "11")
        return first, second, duplicate
    first, second, duplicate = asyncio.run(submit())
    assert not first.duplicate and not second.duplicate
    assert duplicate.duplicate and duplicate.task_id == first.task_id
    assert len(fake.calls) == 2


def test_recovery_tasks_have_distinct_failure_targets_and_context(tmp_path):
    repository = repository_at(tmp_path)
    lifecycle = TaskCenterRepository(repository.database_path, 3600, "archivex:crawl")
    for tweet_id in ("11", "12"):
        lifecycle.record_finished(str(uuid.uuid4()), {
            TASK_OBSERVED_ACCOUNT_ID_LABEL: "1", TASK_FETCH_TWEET_ID_LABEL: tweet_id,
        }, result=None, error="failed", name="archivex.fetch_post_detail", worker="archivex:crawl")
    failures = lifecycle.list_latest_failures()
    assert len(failures) == 2
    assert {item["context"]["post"]["tweet_id"] for item in failures} == {"11", "12"}
    assert all(item["retry_state"] == "ready" for item in failures)


def test_clearing_task_history_does_not_erase_durable_recovery_records(tmp_path):
    repository = repository_at(tmp_path)
    repository.register_post_fetch("1", "11")
    lifecycle = TaskCenterRepository(repository.database_path, 3600, "archivex:crawl")
    lifecycle.record_finished(str(uuid.uuid4()), {
        TASK_OBSERVED_ACCOUNT_ID_LABEL: "1", TASK_FETCH_TWEET_ID_LABEL: "11",
    }, result=None, error="failed", name="archivex.fetch_post_detail", worker="archivex:crawl")
    assert lifecycle.delete_task_history("failure") == 1
    assert repository.post_fetch("1", "11")["status"] == "pending"


def test_recovery_api_lists_counts_and_can_recheck_unavailable(tmp_path):
    class Dispatcher(FakeTaskDispatcher):
        def __init__(self):
            super().__init__()
            self.post_calls = []
        async def enqueue_post_fetch(self, account_id, tweet_id, retry_of=None, parent_task_id=None):
            self.post_calls.append((account_id, tweet_id))
            return TaskSubmission("post-task", "queued", False)
    settings = _settings(tmp_path)
    dispatcher = Dispatcher()
    with TestClient(create_app(settings, FakeSource(), FakeSessionAccountManager(), dispatcher)) as client:
        repository = ArchiveRepository(settings.archive_db_path, settings.archive_data_dir)
        repository.upsert_account("1", "user1")
        for tweet_id in ("11", "12"):
            repository.register_post_fetch("1", tweet_id)
        repository.finish_post_fetch("1", "12", "unavailable", "explicitly unavailable")
        repository.exhaust_post_fetch("1", "11")
        assert client.get("/api/accounts/1/post-fetches").status_code == 401
        headers = {"Authorization": "Bearer test-token"}
        summary = client.get("/api/accounts/1/post-fetches", headers=headers).json()
        assert (summary["pending"], summary["unavailable"], summary["failed"]) == (0, 1, 1)
        response = client.post("/api/accounts/1/post-fetches/retry", headers=headers)
        assert response.status_code == 202 and response.json()["queued"] == 2
        assert set(dispatcher.post_calls) == {("1", "11"), ("1", "12")}
        assert repository.post_fetch_counts("1") == {"pending": 2, "unavailable": 0, "failed": 0}


def test_old_v3_database_gains_auxiliary_table_without_offline_migration(tmp_path):
    repository = repository_at(tmp_path)
    with sqlite3.connect(repository.database_path) as c:
        c.execute("DROP TABLE archive_post_fetches")
    fresh_repository = ArchiveRepository(repository.database_path, repository.archive_data_dir)
    fresh_repository.register_post_fetch("1", "11")
    assert fresh_repository.post_fetch_counts("1")["pending"] == 1
    with sqlite3.connect(repository.database_path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 3


def test_sync_task_queues_only_failed_post_and_worker_recovers_it(monkeypatch, tmp_path):
    repository = repository_at(tmp_path)
    api = ConversationApi([_page([_tweet_result()], ["10", "11", "12"])],
                          {"11": None, "12": _tweet_result("12", reply_to="10")})
    source = service_at(repository, api).source
    redis = FakeRedis()
    fake = FakeTask()
    async def media(*args):
        return 0, 0
    monkeypatch.setattr(tasks, "_redis", lambda: redis)
    monkeypatch.setattr(tasks, "_repository", lambda: repository)
    monkeypatch.setattr(tasks, "TwscrapePostSource", lambda *args, **kwargs: source)
    monkeypatch.setattr(tasks, "fetch_post_detail_task", fake)
    monkeypatch.setattr(tasks, "_enqueue_account_media", media)
    root_context = SimpleNamespace(message=SimpleNamespace(task_id=str(uuid.uuid4()), labels={}))
    root = asyncio.run(tasks.sync_account_task.original_func("1", context=root_context))
    assert root["status"] == "partial" and root["post_fetch_queued"] == 1
    assert fake.calls[0][1] == ("1", "11")
    assert repository.get_post("12") is not None
    assert fake.labels[tasks.TASK_PARENT_ID_LABEL] == root_context.message.task_id

    api2 = ConversationApi([], {"11": _tweet_result("11", reply_to="10")})
    source2 = service_at(repository, api2).source
    monkeypatch.setattr(tasks, "TwscrapePostSource", lambda *args, **kwargs: source2)
    context = SimpleNamespace(message=SimpleNamespace(task_id=fake.task_id, labels={}))
    # Restore the registered real task for execution; only publication was mocked.
    real_task = tasks.broker.find_task("archivex.fetch_post_detail")
    recovered = asyncio.run(real_task.original_func("1", "11", context=context))
    assert recovered["status"] == "success"
    assert repository.post_fetch_counts("1")["pending"] == 0
    assert api2.detail_calls == ["11"]


def test_cached_content_with_new_observation_precedes_known_stop_limit(tmp_path):
    repository = repository_at(tmp_path)
    from archivex.storage import PostInput
    for identifier in ("11", "12"):
        payload = {"id": identifier, "rawContent": "cached", "user": {"id": "1", "username": "user1"}}
        repository.upsert_post(PostInput(identifier, "1", "original", "cached",
            datetime(2026, 8, 8, tzinfo=UTC), f"https://x.com/user1/status/{identifier}", payload))
    repository.observe_post("1", "11")
    repository.mark_account_sync_success("1", datetime.now(UTC))
    api = ConversationApi([_page([_tweet_result()], ["11", "12"])], {})
    result = asyncio.run(service_at(repository, api).sync_account("1"))
    assert result.posts_new >= 1 and repository.has_post_observation("1", "12")
    assert api.detail_calls == []


def test_cached_historical_video_is_rescanned_into_media_records(tmp_path):
    repository = repository_at(tmp_path)
    from archivex.storage import PostInput
    payload = {"id": "11", "rawContent": "video", "user": {"id": "1", "username": "user1"},
               "media": {"videos": [{"variants": [
                   {"url": "https://video.test/valid.mp4", "bitrate": 100},
                   {"url": "", "bitrate": 200},
               ]}]}}
    repository.upsert_post(PostInput("11", "1", "original", "video", datetime(2026, 8, 8, tzinfo=UTC),
                                     "https://x.com/user1/status/11", payload))
    assert repository.post_media("11") == []
    api = ConversationApi([_page([_tweet_result()], ["10", "11"])], {})
    result = asyncio.run(service_at(repository, api).sync_account("1"))
    assert result.media_new == 1
    assert repository.media_to_download("11")[0].source_url == "https://video.test/valid.mp4"
    assert api.detail_calls == []


def test_failure_list_and_manual_rerun_only_target_unresolved_post(tmp_path):
    class Dispatcher(FakeTaskDispatcher):
        def __init__(self):
            super().__init__()
            self.post_calls = []
        async def enqueue_post_fetch(self, account_id, tweet_id, retry_of=None, parent_task_id=None):
            self.post_calls.append((account_id, tweet_id, retry_of))
            return TaskSubmission("retry-task", "queued", False)
    settings = _settings(tmp_path)
    dispatcher = Dispatcher()
    with TestClient(create_app(settings, FakeSource(), FakeSessionAccountManager(), dispatcher)) as client:
        repository = ArchiveRepository(settings.archive_db_path, settings.archive_data_dir)
        repository.upsert_account("1", "user1")
        lifecycle = TaskCenterRepository(repository.database_path, 3600, "archivex:crawl")
        identifiers = {}
        for tweet_id in ("11", "12"):
            repository.register_post_fetch("1", tweet_id)
            identifiers[tweet_id] = str(uuid.uuid4())
            lifecycle.record_finished(identifiers[tweet_id], {
                TASK_OBSERVED_ACCOUNT_ID_LABEL: "1", TASK_FETCH_TWEET_ID_LABEL: tweet_id,
            }, result=None, error="failed", name="archivex.fetch_post_detail", worker="archivex:crawl")
        repository.finish_post_fetch("1", "11", "completed")
        headers = {"Authorization": "Bearer test-token"}
        failures = client.get("/api/task-center/tasks?status=failure", headers=headers).json()
        assert failures["total"] == 1
        assert failures["items"][0]["context"]["post"]["tweet_id"] == "12"
        rerun = client.post(f"/api/task-center/tasks/{identifiers['12']}/rerun", headers=headers)
        assert rerun.status_code == 202
        assert dispatcher.post_calls[-1] == ("1", "12", identifiers["12"])
        retry = client.post("/api/task-center/failures/retry", headers=headers)
        assert retry.status_code == 202 and retry.json()["queued"] == 1
        assert dispatcher.post_calls[-1] == ("1", "12", identifiers["12"])
