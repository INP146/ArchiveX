import asyncio
import json
import multiprocessing
import os
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from twscrape.accounts_pool import NoAccountError

from archivex.source import (
    AccountPoolUnavailableError,
    SourceMedia,
    TwscrapePostSource,
    media_from_payload,
)
import archivex.source as source_module


class CloseTrackingTimeline:
    def __init__(self, tweets):
        self.tweets = iter(tweets)
        self.close_called = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.tweets)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def aclose(self) -> None:
        self.close_called = True


class FakeApi:
    def __init__(self, timeline):
        self.timeline = timeline

    def user_tweets_and_replies(self, x_user_id: int):
        assert x_user_id == 1
        return self.timeline


class LockedPool:
    async def get_all(self):
        return [SimpleNamespace(
            active=True,
            locks={"UserTweetsAndReplies": datetime.now(UTC).replace(microsecond=0)},
        )]


class NoAccountApi(FakeApi):
    def __init__(self):
        self.pool = LockedPool()

    def user_tweets_and_replies(self, x_user_id: int):
        async def fail():
            raise NoAccountError("No account available")
            yield  # pragma: no cover

        return fail()


class FakeTweet:
    id = 10
    user = SimpleNamespace(id=1, username="first")
    retweetedTweet = None
    quotedTweet = None
    isQuoteStatus = False
    inReplyToTweetId = None
    rawContent = "post"
    date = datetime(2026, 8, 8, tzinfo=UTC)
    url = "https://x.com/first/status/10"

    def dict(self):
        return {"id": "10", "media": {}}


def test_closing_source_timeline_closes_twscrape_timeline() -> None:
    upstream = CloseTrackingTimeline([FakeTweet()])
    source = object.__new__(TwscrapePostSource)
    source.api = FakeApi(upstream)

    async def consume_one_post() -> None:
        timeline = source.fetch_timeline("1")
        await anext(timeline)
        await timeline.aclose()
        assert upstream.close_called is True

    asyncio.run(consume_one_post())


def test_locked_account_fails_without_the_30_second_pool_wait() -> None:
    source = object.__new__(TwscrapePostSource)
    source.api = NoAccountApi()
    source._recovery_checked = True

    async def consume() -> None:
        with pytest.raises(AccountPoolUnavailableError) as caught:
            await anext(source.fetch_timeline("1"))
        assert caught.value.queue == "UserTweetsAndReplies"
        assert caught.value.retry_after_seconds is not None

    asyncio.run(consume())


def test_source_passes_short_account_wait_settings(monkeypatch, tmp_path) -> None:
    captured = {}

    class FakeConfiguredApi:
        def __init__(self, database, **kwargs):
            captured["database"] = database
            captured.update(kwargs)

    monkeypatch.setattr("archivex.source.API", FakeConfiguredApi)
    TwscrapePostSource(
        tmp_path / "sessions",
        wait_timeout=0.25,
        wait_interval=0.1,
    )

    assert captured["wait_timeout"] == 0.25
    assert captured["wait_interval"] == 0.1
    assert captured["raise_when_no_account"] is True


def test_twscrape_feature_error_releases_context_instead_of_exiting() -> None:
    released = []

    class Client(source_module._SafeTwscrapeQueueClient):
        def __init__(self):
            pass

        async def _close_ctx(self, *args, **kwargs):
            released.append(True)

    class Response:
        def json(self):
            return {"errors": [{"code": 336, "message": "features cannot be null"}]}

    with pytest.raises(source_module._FatalTwscrapeResponse):
        asyncio.run(Client()._check_rep(Response()))

    assert released == [True]


def test_cancelled_client_close_still_releases_the_account_lock(monkeypatch) -> None:
    unlocks = []
    closed = []
    target = source_module._SessionLockTarget(
        "crawler",
        "UserTweetsAndReplies",
        "2026-08-21 12:00:00",
    )

    class Pool:
        async def unlock(self, username, queue, request_count):
            unlocks.append((username, queue, request_count))

    class Context:
        acc = SimpleNamespace(username="crawler")
        req_count = 3

        async def aclose(self):
            raise asyncio.CancelledError

    def context_closed(pool, closed_target, *, recover_lock):
        closed.append((closed_target, recover_lock))

    monkeypatch.setattr(source_module, "_session_context_closed", context_closed)
    client = source_module._SafeTwscrapeQueueClient(
        Pool(),
        "UserTweetsAndReplies",
    )
    client.ctx = Context()
    client._archivex_session_lock_target = target

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(client._close_ctx())

    assert unlocks == [("crawler", "UserTweetsAndReplies", 3)]
    assert closed == [(target, False)]


@pytest.mark.parametrize(
    ("reset_at", "inactive", "failed_method"),
    (
        (1_775_776_400, False, "lock_until"),
        (-1, True, "mark_inactive"),
    ),
)
def test_rate_limit_and_inactive_close_failures_do_not_request_lock_recovery(
    monkeypatch,
    reset_at,
    inactive,
    failed_method,
) -> None:
    calls = []
    closed = []
    target = source_module._SessionLockTarget(
        "crawler",
        "UserTweetsAndReplies",
        "2026-08-21 12:00:00",
    )

    class Pool:
        async def unlock(self, *args):
            calls.append(("unlock", args))

        async def lock_until(self, *args):
            calls.append(("lock_until", args))
            raise RuntimeError("rate-limit state write failed")

        async def mark_inactive(self, *args):
            calls.append(("mark_inactive", args))
            raise RuntimeError("inactive state write failed")

    class Context:
        acc = SimpleNamespace(username="crawler")
        req_count = 3

        async def aclose(self):
            return None

    monkeypatch.setattr(
        source_module,
        "_session_context_closed",
        lambda pool, closed_target, *, recover_lock: closed.append(
            (closed_target, recover_lock)
        ),
    )
    client = source_module._SafeTwscrapeQueueClient(
        Pool(),
        "UserTweetsAndReplies",
    )
    client.ctx = Context()
    client._archivex_session_lock_target = target

    with pytest.raises(RuntimeError):
        asyncio.run(client._close_ctx(reset_at=reset_at, inactive=inactive))

    assert [method for method, _ in calls] == [failed_method]
    assert closed == [(target, False)]


def test_client_construction_failure_releases_the_acquired_account(monkeypatch) -> None:
    unlocks = []
    closed = []
    target = source_module._SessionLockTarget(
        "crawler",
        "UserTweetsAndReplies",
        "2026-08-21 12:00:00",
    )

    class Account:
        username = "crawler"
        locks = {"UserTweetsAndReplies": datetime(2026, 8, 21, 12, tzinfo=UTC)}

        def resolve_proxy(self, proxy):
            return proxy

        def make_client(self, proxy=None):
            raise RuntimeError("invalid proxy configuration")

    class Pool:
        async def get_for_queue_or_wait(self, queue):
            return Account()

        async def unlock(self, username, queue):
            unlocks.append((username, queue))

    monkeypatch.setattr(
        source_module,
        "_session_context_opened",
        lambda pool, account, queue: target,
    )
    monkeypatch.setattr(
        source_module,
        "_session_context_closed",
        lambda pool, closed_target, *, recover_lock: closed.append(
            (closed_target, recover_lock)
        ),
    )
    client = source_module._SafeTwscrapeQueueClient(
        Pool(),
        "UserTweetsAndReplies",
    )

    with pytest.raises(RuntimeError, match="invalid proxy"):
        asyncio.run(client._get_ctx())

    assert unlocks == [("crawler", "UserTweetsAndReplies")]
    assert closed == [(target, False)]


def test_unclean_session_lock_recovery_runs_once_per_worker(monkeypatch, tmp_path) -> None:
    reset_calls = []
    target = source_module._SessionLockTarget(
        "crawler",
        "UserTweetsAndReplies",
        "2026-08-21 12:00:00",
    )

    async def reset_locks(database_path, targets):
        reset_calls.append((database_path, tuple(targets)))

    monkeypatch.setattr(source_module, "_reset_crawl_queue_locks", reset_locks)
    monkeypatch.setattr(source_module, "_session_recovery_consumed", set())
    monkeypatch.setattr(source_module, "_session_recovery_locks", {})
    source = object.__new__(TwscrapePostSource)
    source.database_path = tmp_path / "accounts.db"
    source._recovery_targets = (target,)
    source._recovery_checked = False

    async def recover_twice() -> None:
        await source._ensure_ready()
        await source._ensure_ready()

    asyncio.run(recover_twice())

    assert reset_calls == [(source.database_path, (target,))]


def test_unclean_session_lock_recovery_is_shared_by_new_sources(
    monkeypatch,
    tmp_path,
) -> None:
    database_path = tmp_path / "accounts.db"
    target = source_module._SessionLockTarget(
        "crawler",
        "UserTweetsAndReplies",
        "2026-08-21 12:00:00",
    )
    reset_calls = []

    async def reset_locks(path, targets):
        reset_calls.append((path, tuple(targets)))

    monkeypatch.setattr(source_module, "_reset_crawl_queue_locks", reset_locks)

    first = object.__new__(TwscrapePostSource)
    first.database_path = database_path
    first._recovery_targets = (target,)
    first._recovery_checked = False
    second = object.__new__(TwscrapePostSource)
    second.database_path = database_path
    second._recovery_targets = (target,)
    second._recovery_checked = False

    monkeypatch.setattr(source_module, "_session_recovery_consumed", set())
    monkeypatch.setattr(source_module, "_session_recovery_locks", {})
    asyncio.run(first._ensure_ready())
    asyncio.run(second._ensure_ready())

    assert reset_calls == [(database_path, (target,))]


def test_crash_recovery_only_clears_the_matching_timeline_lock(tmp_path) -> None:
    database_path = tmp_path / "accounts.db"
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA user_version = 4")
    connection.execute(
        "CREATE TABLE accounts (username TEXT PRIMARY KEY, locks TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO accounts (username, locks) VALUES (?, ?)",
        (
            "crawler",
            json.dumps({
                "UserTweetsAndReplies": "2026-08-21 12:00:00",
                "UserByScreenName": "2026-08-21 12:30:00",
                "OtherEndpoint": "2026-08-21 13:00:00",
            }),
        ),
    )
    connection.commit()
    connection.close()

    target = source_module._SessionLockTarget(
        "crawler",
        "UserTweetsAndReplies",
        "2026-08-21 12:00:00",
    )
    asyncio.run(source_module._reset_crawl_queue_locks(database_path, (target,)))

    connection = sqlite3.connect(database_path)
    locks = json.loads(
        connection.execute(
            "SELECT locks FROM accounts WHERE username = 'crawler'"
        ).fetchone()[0]
    )
    assert locks == {
        "UserByScreenName": "2026-08-21 12:30:00",
        "OtherEndpoint": "2026-08-21 13:00:00",
    }

    connection.execute(
        "UPDATE accounts SET locks = ? WHERE username = 'crawler'",
        (json.dumps({"UserTweetsAndReplies": "2026-08-21 14:00:00"}),),
    )
    connection.commit()
    connection.close()
    asyncio.run(source_module._reset_crawl_queue_locks(database_path, (target,)))

    connection = sqlite3.connect(database_path)
    locks = json.loads(
        connection.execute(
            "SELECT locks FROM accounts WHERE username = 'crawler'"
        ).fetchone()[0]
    )
    connection.close()
    assert locks == {"UserTweetsAndReplies": "2026-08-21 14:00:00"}


def test_clean_session_marker_does_not_request_recovery(monkeypatch, tmp_path) -> None:
    database_path = tmp_path / "accounts.db"
    lock_path = tmp_path / ".accounts.db.archivex-crawl.lock"
    lock_path.write_text(json.dumps({"pid": 123, "clean": True}))

    monkeypatch.setattr(source_module, "_session_leases", {})
    monkeypatch.setattr(source_module, "_session_recovery_consumed", set())
    monkeypatch.setattr(source_module, "_session_recovery_locks", {})
    monkeypatch.setattr(source_module, "_session_active_contexts", {})
    monkeypatch.setattr(source_module, "_session_leases_atexit_registered", False)

    assert source_module._acquire_session_lease(database_path) == ()
    source_module._release_session_leases()


def test_fork_child_drops_inherited_session_lease_state(monkeypatch, tmp_path) -> None:
    class Handle:
        closed = False

        def close(self):
            self.closed = True

    database_path = tmp_path / "accounts.db"
    handle = Handle()
    target = source_module._SessionLockTarget(
        "crawler",
        "UserTweetsAndReplies",
        "2026-08-21 12:00:00",
    )
    monkeypatch.setattr(
        source_module,
        "_session_leases",
        {database_path: (os.getpid(), handle, (target,))},
    )
    monkeypatch.setattr(source_module, "_session_recovery_consumed", {database_path})
    monkeypatch.setattr(source_module, "_session_recovery_locks", {database_path: object()})
    monkeypatch.setattr(source_module, "_session_active_contexts", {database_path: {target}})
    monkeypatch.setattr(source_module, "_session_failed_contexts", {database_path: {target}})
    monkeypatch.setattr(source_module, "_session_leases_atexit_registered", True)

    source_module._drop_inherited_session_leases()

    assert handle.closed is True
    assert source_module._session_leases == {}
    assert source_module._session_recovery_consumed == set()
    assert source_module._session_recovery_locks == {}
    assert source_module._session_active_contexts == {}
    assert source_module._session_failed_contexts == {}
    assert source_module._session_leases_atexit_registered is True


def _hold_session_lock(lock_path: str, ready, release) -> None:
    import fcntl

    with open(lock_path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        ready.set()
        release.wait(timeout=10)


@pytest.mark.skipif(source_module.fcntl is None, reason="requires fcntl")
def test_another_process_holding_session_lock_prevents_recovery(
    monkeypatch,
    tmp_path,
) -> None:
    database_path = tmp_path / "accounts.db"
    lock_path = tmp_path / ".accounts.db.archivex-crawl.lock"
    lock_path.write_text(json.dumps({"pid": 123, "clean": False}))
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_session_lock,
        args=(str(lock_path), ready, release),
    )
    process.start()
    assert ready.wait(timeout=10)
    try:
        monkeypatch.setattr(source_module, "_session_leases", {})
        assert source_module._acquire_session_lease(database_path) == ()
    finally:
        release.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)

    assert process.exitcode == 0


def test_graceful_exit_with_active_context_leaves_unclean_marker(
    monkeypatch,
    tmp_path,
) -> None:
    database_path = tmp_path / "accounts.db"
    marker = tmp_path / ".accounts.db.archivex-crawl.lock"
    pool = SimpleNamespace(_db_file=str(database_path))
    context = SimpleNamespace(acc=SimpleNamespace(
        username="crawler",
        locks={
            "UserTweetsAndReplies": datetime(2026, 8, 21, 12, tzinfo=UTC),
        },
    ))

    monkeypatch.setattr(source_module, "_session_leases", {})
    monkeypatch.setattr(source_module, "_session_recovery_consumed", set())
    monkeypatch.setattr(source_module, "_session_active_contexts", {})
    monkeypatch.setattr(source_module, "_session_failed_contexts", {})
    monkeypatch.setattr(source_module, "_session_leases_atexit_registered", False)
    assert source_module._acquire_session_lease(database_path) == ()
    target = source_module._session_context_opened(
        pool,
        context.acc,
        "UserTweetsAndReplies",
    )
    assert target is not None

    source_module._mark_session_leases_clean()

    state = json.loads(marker.read_text())
    assert state["clean"] is False
    assert state["active"] == [{
        "username": "crawler",
        "queue": "UserTweetsAndReplies",
        "locked_until": "2026-08-21 12:00:00",
    }]

    source_module._session_context_closed(pool, target)
    assert json.loads(marker.read_text()) == {
        "pid": os.getpid(),
        "clean": True,
        "active": [],
    }
    source_module._release_session_leases()


def test_session_context_tracking_marks_failed_cleanup_unclean(
    monkeypatch,
    tmp_path,
) -> None:
    database_path = tmp_path / "accounts.db"
    marker = tmp_path / ".accounts.db.archivex-crawl.lock"
    pool = SimpleNamespace(_db_file=str(database_path))
    context = SimpleNamespace(acc=SimpleNamespace(
        username="crawler",
        locks={
            "UserTweetsAndReplies": datetime(2026, 8, 21, 12, tzinfo=UTC),
        },
    ))

    monkeypatch.setattr(source_module, "_session_leases", {})
    monkeypatch.setattr(source_module, "_session_recovery_consumed", set())
    monkeypatch.setattr(source_module, "_session_active_contexts", {})
    monkeypatch.setattr(source_module, "_session_failed_contexts", {})
    monkeypatch.setattr(source_module, "_session_leases_atexit_registered", False)
    assert source_module._acquire_session_lease(database_path) == ()

    target = source_module._session_context_opened(
        pool,
        context.acc,
        "UserTweetsAndReplies",
    )
    assert target is not None
    source_module._session_context_closed(pool, target, recover_lock=True)
    source_module._mark_session_leases_clean()

    assert source_module._session_active_contexts == {database_path: {target}}
    assert source_module._session_failed_contexts == {database_path: {target}}
    state = json.loads(marker.read_text())
    assert state["clean"] is False
    assert state["active"][0]["username"] == "crawler"
    source_module._release_session_leases()


def test_new_source_consumes_cleanup_failure_from_the_same_worker(
    monkeypatch,
    tmp_path,
) -> None:
    database_path = tmp_path / "sessions" / "accounts.db"
    target = source_module._SessionLockTarget(
        "crawler",
        "UserTweetsAndReplies",
        "2026-08-21 12:00:00",
    )

    class FakeConfiguredApi:
        def __init__(self, database, **kwargs):
            self.pool = SimpleNamespace(_db_file=database)

    monkeypatch.setattr(source_module, "API", FakeConfiguredApi)
    monkeypatch.setattr(source_module, "_acquire_session_lease", lambda path: ())
    monkeypatch.setattr(
        source_module,
        "_session_failed_contexts",
        {database_path: {target}},
    )

    source = TwscrapePostSource(tmp_path / "sessions", recover_stale_locks=True)

    assert source._recovery_targets == (target,)


def test_media_from_payload_includes_reposted_tweet_video() -> None:
    payload = {
        "media": {},
        "retweetedTweet": {
            "media": {
                "videos": [{
                    "variants": [
                        {"bitrate": 256000, "url": "https://video.example/low.mp4"},
                        {"bitrate": 2176000, "url": "https://video.example/high.mp4"},
                    ]
                }]
            }
        },
    }

    assert media_from_payload(payload) == (
        SourceMedia("video", "https://video.example/high.mp4"),
    )


def test_media_from_payload_includes_quote_media_and_deduplicates_urls() -> None:
    shared_url = "https://pbs.twimg.com/media/shared.jpg"
    payload = {
        "media": {"photos": [{"url": shared_url}]},
        "quotedTweet": {
            "media": {
                "photos": [
                    {"url": shared_url},
                    {"url": "https://pbs.twimg.com/media/quoted.jpg"},
                ]
            }
        },
    }

    assert media_from_payload(payload) == (
        SourceMedia("image", shared_url),
        SourceMedia("image", "https://pbs.twimg.com/media/quoted.jpg"),
    )
