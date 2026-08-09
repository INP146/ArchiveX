import asyncio
import sqlite3
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from taskiq.abc.schedule_source import ScheduleSource
from taskiq.message import TaskiqMessage
from taskiq.result import TaskiqResult

from archivex.media import GalleryDlMediaDownloader, PermanentMediaDownloadError
from archivex import tasks
from archivex.storage import ArchiveRepository, MediaInput, PostInput, initialize_storage
from archivex.task_center import TaskCenterRepository, _automatic_retries_exhausted


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, *, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def expire(self, key, seconds):
        return key in self.values

    async def eval(self, script, key_count, key, expected, *args):
        if "local owner" in script:
            owner = self.values.get(key)
            if owner == expected:
                return 1
            if owner is None:
                self.values[key] = expected
                return "OK"
            return 0
        if "expire" in script:
            return int(self.values.get(key) == expected)
        if self.values.get(key) == expected:
            del self.values[key]
            return 1
        return 0

    async def aclose(self):
        return None


def task_repository(database_path: Path) -> TaskCenterRepository:
    initialize_storage(
        database_path,
        database_path.parent / "archive",
        database_path.parent / "sessions",
    )
    return TaskCenterRepository(database_path, 3600, "archivex:crawl")


class FakeTask:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.task_id = None
        self.calls = []
        self.labels = {
            "queue_name": "archivex:crawl",
            "retry_on_error": True,
            "max_retries": 5,
            "delay": 30,
        }

    def kicker(self):
        return self

    def with_task_id(self, task_id):
        self.task_id = task_id
        return self

    def with_labels(self, **labels):
        self.labels.update(labels)
        return self

    async def kiq(self, *args):
        self.calls.append((self.task_id, args))
        if self.should_fail:
            raise RuntimeError("redis unavailable")


def test_redis_clients_have_bounded_connection_operations() -> None:
    pools = [
        tasks.broker.connection_pool,
        tasks.result_backend.redis_pool,
        tasks.retry_schedule_source._connection_pool,
    ]
    standalone = tasks._redis()
    pools.append(standalone.connection_pool)

    for pool in pools:
        assert pool.connection_kwargs["health_check_interval"] == 30
        assert pool.connection_kwargs["socket_connect_timeout"] == 5
        assert pool.connection_kwargs["socket_timeout"] == 5
        assert pool.connection_kwargs["retry_on_timeout"] is True

    asyncio.run(standalone.aclose())


def test_retry_schedule_failure_becomes_terminal_and_releases_lock(monkeypatch) -> None:
    released = []

    class FailingScheduleSource(ScheduleSource):
        async def get_schedules(self):
            return []

        async def add_schedule(self, schedule):
            raise ConnectionError("redis connection closed")

    async def release_lock(message):
        released.append(message.task_id)

    monkeypatch.setattr(tasks, "_release_failed_retry_lock", release_lock)
    middleware = tasks.ResilientSmartRetryMiddleware(
        schedule_source=FailingScheduleSource(),
    )
    middleware.set_broker(SimpleNamespace(id_generator=lambda: uuid.uuid4().hex))
    message = TaskiqMessage(
        task_id=str(uuid.uuid4()),
        task_name="archivex.download_media",
        labels={"retry_on_error": True, "max_retries": 5},
        args=["media-id"],
        kwargs={},
    )
    original_error = RuntimeError("download failed")
    result = TaskiqResult(
        is_err=True,
        return_value=None,
        execution_time=1.0,
        error=original_error,
    )

    asyncio.run(middleware.on_error(message, result, original_error))

    assert released == [message.task_id]
    assert isinstance(result.error, RuntimeError)
    assert str(result.error) == (
        "download failed; retry scheduling failed: "
        "ConnectionError: redis connection closed"
    )


def test_permanent_media_failure_is_not_automatically_retried(monkeypatch) -> None:
    released = []

    async def release_lock(message):
        released.append(message.task_id)

    monkeypatch.setattr(tasks, "_release_failed_retry_lock", release_lock)
    middleware = tasks.ResilientSmartRetryMiddleware()
    message = TaskiqMessage(
        task_id=str(uuid.uuid4()),
        task_name="archivex.download_media",
        labels={"retry_on_error": True, "max_retries": 5},
        args=["media-id"],
        kwargs={},
    )
    error = PermanentMediaDownloadError("403 Forbidden")
    result = TaskiqResult(
        is_err=True,
        return_value=None,
        execution_time=1.0,
        error=error,
    )

    asyncio.run(middleware.on_error(message, result, error))

    assert released == [message.task_id]
    assert result.error is error
    assert _automatic_retries_exhausted({
        "error": repr(error),
        "labels": {"max_retries": 5, "_retries": 0},
    }) is True


def test_automatic_retry_is_one_logical_task_with_distinct_attempts(
    tmp_path, monkeypatch,
) -> None:
    class MemoryScheduleSource(ScheduleSource):
        def __init__(self):
            self.schedules = []

        async def get_schedules(self):
            return self.schedules

        async def add_schedule(self, schedule):
            self.schedules.append(schedule)

    repository = task_repository(tmp_path / "archive.sqlite3")
    monkeypatch.setattr(tasks, "_task_center_repository", lambda: repository)
    source = MemoryScheduleSource()
    retry = tasks.ResilientSmartRetryMiddleware(
        default_delay=30,
        use_jitter=False,
        schedule_source=source,
    )
    retry.set_broker(SimpleNamespace(id_generator=lambda: uuid.uuid4().hex))
    lifecycle = tasks.TaskLifecycleMiddleware()
    task_id = str(uuid.uuid4())
    first = TaskiqMessage(
        task_id=task_id,
        task_name="archivex.download_media",
        labels={
            "queue_name": "archivex:media",
            "retry_on_error": True,
            "max_retries": 5,
            "delay": 30,
        },
        args=["media-id"],
        kwargs={},
    )
    failure = RuntimeError("download failed")
    result = TaskiqResult(
        is_err=True,
        return_value=None,
        execution_time=1.0,
        error=failure,
    )

    async def run_first_attempt():
        await lifecycle.post_send(first)
        await lifecycle.pre_execute(first)
        await retry.on_error(first, result, failure)
        await lifecycle.post_execute(first, result)

    asyncio.run(run_first_attempt())

    waiting = repository.get_task(task_id)
    assert waiting["status"] == "retry_scheduled"
    assert waiting["error"] == "RuntimeError('download failed')"
    assert waiting["current_attempt"] == 1
    assert waiting["next_retry_at"] is not None
    assert isinstance(result.error, tasks.NoResultError)
    assert len(source.schedules) == 1
    assert source.schedules[0].task_id == task_id
    assert source.schedules[0].labels["_retries"] == "1"

    second = TaskiqMessage(
        task_id=task_id,
        task_name=first.task_name,
        labels=source.schedules[0].labels,
        args=first.args,
        kwargs={},
    )

    async def run_second_attempt():
        await lifecycle.post_send(second)
        await lifecycle.pre_execute(second)
        await lifecycle.post_execute(second, TaskiqResult(
            is_err=False,
            return_value={"status": "success"},
            execution_time=1.0,
            error=None,
        ))

    asyncio.run(run_second_attempt())

    completed = repository.get_task(task_id)
    assert completed["status"] == "completed"
    assert completed["current_attempt"] == 2
    assert [attempt["status"] for attempt in completed["attempts"]] == [
        "completed",
        "failure",
    ]


def test_account_enqueue_coalesces_duplicate_tasks(monkeypatch) -> None:
    fake_redis = FakeRedis()
    fake_task = FakeTask()
    monkeypatch.setattr(tasks, "_redis", lambda: fake_redis)
    monkeypatch.setattr(tasks, "sync_account_task", fake_task)

    async def enqueue_twice():
        first = await tasks.enqueue_account_sync("42")
        second = await tasks.enqueue_account_sync("42")
        return first, second

    first, second = asyncio.run(enqueue_twice())

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.task_id == first.task_id
    assert len(fake_task.calls) == 1
    assert fake_task.labels[tasks.TASK_ACCOUNT_ID_LABEL] == "42"
    assert fake_task.labels[tasks.TASK_TRIGGER_LABEL] == "manual"


def test_media_enqueue_carries_parent_and_retry_relationships(monkeypatch) -> None:
    fake_redis = FakeRedis()
    fake_task = FakeTask()
    parent_task_id = str(uuid.uuid4())
    retry_of = str(uuid.uuid4())
    monkeypatch.setattr(tasks, "_redis", lambda: fake_redis)
    monkeypatch.setattr(tasks, "download_media_task", fake_task)

    submission = asyncio.run(tasks.enqueue_media_download(
        "media-id",
        retry_of=retry_of,
        parent_task_id=parent_task_id,
    ))

    assert submission.duplicate is False
    assert fake_task.calls == [(submission.task_id, ("media-id",))]
    assert fake_task.labels[tasks.TASK_MEDIA_ID_LABEL] == "media-id"
    assert fake_task.labels[tasks.TASK_PARENT_ID_LABEL] == parent_task_id
    assert fake_task.labels["retry_of"] == retry_of


def test_execution_claim_and_extension_never_refresh_another_owner(monkeypatch) -> None:
    fake_redis = FakeRedis()
    monkeypatch.setattr(tasks, "_redis", lambda: fake_redis)

    async def exercise_locks():
        assert await tasks._claim_execution("lock", "first") is True
        await tasks._extend_execution_lock("lock", "first")
        assert await tasks._claim_execution("lock", "second") is False
        with pytest.raises(RuntimeError, match="ownership was lost"):
            await tasks._extend_execution_lock("lock", "second")

    asyncio.run(exercise_locks())

    assert fake_redis.values["lock"] == "first"


def test_enqueue_failure_releases_dedupe_lock(monkeypatch) -> None:
    fake_redis = FakeRedis()
    fake_task = FakeTask(should_fail=True)
    abandoned = []

    class FakeTaskCenter:
        def record_publish_failed(self, task_id, name, worker, args, kwargs, labels, error):
            abandoned.append((task_id, error))

    monkeypatch.setattr(tasks, "_redis", lambda: fake_redis)
    monkeypatch.setattr(tasks, "sync_account_task", fake_task)
    monkeypatch.setattr(tasks, "_task_center_repository", FakeTaskCenter)

    with pytest.raises(RuntimeError, match="redis unavailable"):
        asyncio.run(tasks.enqueue_account_sync("42"))

    assert fake_redis.values == {}
    assert abandoned == [(
        fake_task.task_id,
        "Task was not published to the broker: RuntimeError: redis unavailable",
    )]


def test_enqueue_failure_still_updates_lifecycle_when_lock_rollback_fails(
    monkeypatch,
) -> None:
    class RollbackFailingRedis(FakeRedis):
        async def eval(self, script, key_count, key, expected):
            raise ConnectionError("redis still unavailable")

    fake_redis = RollbackFailingRedis()
    fake_task = FakeTask(should_fail=True)
    abandoned = []

    class FakeTaskCenter:
        def record_publish_failed(self, task_id, name, worker, args, kwargs, labels, error):
            abandoned.append((task_id, error))

    monkeypatch.setattr(tasks, "_redis", lambda: fake_redis)
    monkeypatch.setattr(tasks, "sync_account_task", fake_task)
    monkeypatch.setattr(tasks, "_task_center_repository", FakeTaskCenter)

    with pytest.raises(RuntimeError, match="redis unavailable"):
        asyncio.run(tasks.enqueue_account_sync("42"))

    assert abandoned == [(
        fake_task.task_id,
        "Task was not published to the broker: RuntimeError: redis unavailable",
    )]


def test_abandon_queued_task_finishes_lifecycle_record(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    task_id = uuid.uuid4()
    repository = task_repository(database_path)
    repository.record_queued(
        str(task_id),
        "archivex.sync_account",
        "archivex:crawl",
        ["42", "manual"],
        {},
        {},
        "2026-08-08T12:00:00Z",
    )

    assert repository.abandon_queued_task(str(task_id), "publish failed") is True
    task = repository.get_task(str(task_id))
    assert task is not None
    assert task["status"] == "abandoned"
    assert task["error"] == "publish failed"
    assert task["finished_at"] is not None
    assert repository.abandon_queued_task(str(task_id), "again") is False
    assert repository.delete_task_history("abandoned") == 1
    assert repository.get_task(str(task_id)) is None
    assert repository.delete_task_history("abandoned") == 0


def test_delete_task_history_removes_only_terminal_tasks_and_attempts(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    repository = task_repository(database_path)
    task_ids = {
        status: str(uuid.uuid4())
        for status in ("completed", "failure", "abandoned", "queued")
    }
    for task_id in task_ids.values():
        repository.record_queued(
            task_id,
            "archivex.sync_account",
            "archivex:crawl",
            ["42", "manual"],
            {},
            {},
        )
    repository.record_finished(
        task_ids["completed"], {}, result={"ok": True}, error=None
    )
    repository.record_finished(
        task_ids["failure"], {}, result=None, error="failed"
    )
    repository.abandon_queued_task(task_ids["abandoned"], "publish failed")

    with pytest.raises(ValueError, match="cannot delete active task status"):
        repository.delete_task_history("queued")
    assert repository.delete_task_history("completed") == 1
    assert repository.get_task(task_ids["completed"]) is None
    assert repository.get_task(task_ids["failure"]) is not None
    assert repository.get_task(task_ids["queued"]) is not None

    assert repository.delete_task_history() == 2
    assert repository.get_task(task_ids["failure"]) is None
    assert repository.get_task(task_ids["abandoned"]) is None
    assert repository.get_task(task_ids["queued"]) is not None
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM queue_tasks").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM queue_attempts").fetchone()[0] == 1


def test_task_context_links_sync_media_and_immutable_business_snapshot(tmp_path) -> None:
    database_path = tmp_path / "archive.sqlite3"
    lifecycle = task_repository(database_path)
    archive = ArchiveRepository(database_path, tmp_path / "archive")
    archive.upsert_account("42", "example", "Example")
    archive.upsert_post(PostInput(
        "100",
        "42",
        "original",
        "A post whose media should be downloaded",
        datetime(2026, 8, 8, tzinfo=UTC),
        "https://x.com/example/status/100",
        {},
    ))
    media_id = archive.upsert_media(MediaInput(
        "100", "image", "https://example.test/image.jpg"
    ))
    parent_task_id = str(uuid.uuid4())
    child_task_id = str(uuid.uuid4())
    lifecycle.record_queued(
        parent_task_id,
        "archivex.sync_account",
        "archivex:crawl",
        ["42", "manual"],
        {},
        {
            tasks.TASK_ACCOUNT_ID_LABEL: "42",
            tasks.TASK_TRIGGER_LABEL: "manual",
        },
    )
    lifecycle.record_queued(
        child_task_id,
        "archivex.download_media",
        "archivex:media",
        [media_id],
        {},
        {
            tasks.TASK_MEDIA_ID_LABEL: media_id,
            tasks.TASK_PARENT_ID_LABEL: parent_task_id,
        },
    )

    child = lifecycle.get_task(child_task_id)
    assert child is not None
    assert child["account_x_user_id"] == "42"
    assert child["media_id"] == media_id
    assert child["parent_task_id"] == parent_task_id
    assert child["context"] == {
        "account": {
            "x_user_id": "42",
            "username": "example",
            "display_name": "Example",
        },
        "post": {
            "tweet_id": "100",
            "permalink": "https://x.com/example/status/100",
            "text_preview": "A post whose media should be downloaded",
        },
        "media": {
            "id": media_id,
            "media_type": "image",
            "source_url": "https://example.test/image.jpg",
            "download_status": "pending",
        },
    }
    assert lifecycle.get_task(parent_task_id)["child_counts"]["queued"] == 1
    assert lifecycle.list_tasks(query="100")["items"][0]["id"] == child_task_id

    archive.observe_account_identity("42", "renamed", "Renamed")
    assert lifecycle.get_task(child_task_id)["context"]["account"]["username"] == "example"

    lifecycle.record_finished(child_task_id, {}, result=None, error="failed")
    retry_task_id = str(uuid.uuid4())
    lifecycle.record_queued(
        retry_task_id,
        "archivex.download_media",
        "archivex:media",
        [media_id],
        {},
        {
            tasks.TASK_MEDIA_ID_LABEL: media_id,
            tasks.TASK_PARENT_ID_LABEL: parent_task_id,
            "retry_of": child_task_id,
        },
    )
    retried = lifecycle.get_task(retry_task_id)
    assert retried["parent_task_id"] == parent_task_id
    assert retried["retry_of"] == child_task_id


def test_retry_attempts_preserve_failure_history_and_reject_late_events(tmp_path) -> None:
    repository = task_repository(tmp_path / "archive.sqlite3")
    task_id = str(uuid.uuid4())
    first = {"max_retries": "5"}
    second = {"max_retries": "5", "_retries": "1"}

    repository.record_queued(
        task_id, "archivex.download_media", "archivex:media", ["media-id"], {}, first,
        "2026-08-08T12:00:00Z",
    )
    repository.record_started(
        task_id, "archivex.download_media", "archivex:media", ["media-id"], {}, first,
        "2026-08-08T12:00:01Z",
    )
    repository.record_retry_scheduled(
        task_id, first, "first failure", "2026-08-08T12:00:31Z",
        "2026-08-08T12:00:02Z",
    )
    assert repository.get_task(task_id)["status"] == "retry_scheduled"

    repository.record_started(
        task_id, "archivex.download_media", "archivex:media", ["media-id"], {}, second,
        "2026-08-08T12:00:32Z",
    )
    repository.record_finished(
        task_id, second, result={"ok": True}, error=None,
        finished_at="2026-08-08T12:00:33Z",
    )
    repository.record_queued(
        task_id, "archivex.download_media", "archivex:media", ["media-id"], {}, second,
        "2026-08-08T12:00:31Z",
    )

    task = repository.get_task(task_id)
    assert task["status"] == "completed"
    assert task["current_attempt"] == 2
    assert [attempt["status"] for attempt in task["attempts"]] == ["completed", "failure"]
    assert task["attempts"][1]["error"] == "first failure"


def test_terminal_events_recover_tasks_when_earlier_lifecycle_events_are_missing(
    tmp_path,
) -> None:
    repository = task_repository(tmp_path / "archive.sqlite3")
    completed_id = str(uuid.uuid4())
    completed_labels = {"max_retries": "5", "_retries": "2"}

    repository.record_finished(
        completed_id,
        completed_labels,
        result={"ok": True},
        error=None,
        finished_at="2026-08-08T12:00:03Z",
        name="archivex.download_media",
        worker="archivex:media",
        args=["media-id"],
        kwargs={},
    )

    completed = repository.get_task(completed_id)
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["current_attempt"] == 3
    assert completed["name"] == "archivex.download_media"
    assert completed["args"] == ["media-id"]
    assert completed["attempts"] == [{
        "attempt": 3,
        "status": "completed",
        "labels": completed_labels,
        "result": {"ok": True},
        "error": None,
        "queued_at": None,
        "started_at": None,
        "finished_at": "2026-08-08T12:00:03+00:00",
        "next_retry_at": None,
        "duration_ms": None,
    }]

    retrying_id = str(uuid.uuid4())
    retrying_labels = {"max_retries": "5", "_retries": "3"}
    repository.record_retry_scheduled(
        retrying_id,
        retrying_labels,
        "fourth failure",
        "2026-08-08T12:01:00Z",
        "2026-08-08T12:00:04Z",
        name="archivex.sync_account",
        worker="archivex:crawl",
        args=["42", "scheduled"],
        kwargs={},
    )

    retrying = repository.get_task(retrying_id)
    assert retrying is not None
    assert retrying["status"] == "retry_scheduled"
    assert retrying["current_attempt"] == 4
    assert retrying["next_retry_at"] == "2026-08-08T12:01:00+00:00"
    assert retrying["attempts"][0]["attempt"] == 4
    assert retrying["attempts"][0]["status"] == "failure"
    assert retrying["attempts"][0]["error"] == "fourth failure"


def test_lifecycle_middleware_records_attempts_and_discards_consumed_schedule(
    tmp_path, monkeypatch,
) -> None:
    repository = task_repository(tmp_path / "archive.sqlite3")
    monkeypatch.setattr(tasks, "_task_center_repository", lambda: repository)
    middleware = tasks.TaskLifecycleMiddleware()
    message = TaskiqMessage(
        task_id=str(uuid.uuid4()),
        task_name="archivex.download_media",
        labels={"_retries": "1", "schedule_id": "consumed"},
        args=["media-id"],
        kwargs={},
    )

    async def run_hooks():
        await middleware.post_send(message)
        await middleware.pre_execute(message)
        await middleware.post_execute(message, TaskiqResult(
            is_err=True,
            return_value=None,
            execution_time=1.0,
            error=RuntimeError("failed"),
        ))

    asyncio.run(run_hooks())

    assert "schedule_id" not in message.labels
    task = repository.get_task(message.task_id)
    assert task["status"] == "failure"
    assert task["current_attempt"] == 2
    assert task["attempts"][0]["error"] == "RuntimeError('failed')"


def test_lifecycle_storage_failure_does_not_block_queue_hooks(monkeypatch) -> None:
    class FailedRepository:
        def __getattr__(self, name):
            def fail(*args, **kwargs):
                raise sqlite3.OperationalError("database unavailable")
            return fail

    middleware = tasks.TaskLifecycleMiddleware()
    monkeypatch.setattr(tasks, "_task_center_repository", FailedRepository)
    message = TaskiqMessage(
        task_id=str(uuid.uuid4()),
        task_name="archivex.download_media",
        labels={},
        args=["media-id"],
        kwargs={},
    )

    async def run_hooks():
        await middleware.post_send(message)
        await middleware.pre_execute(message)
        await middleware.post_execute(message, TaskiqResult(
            is_err=False,
            return_value={"ok": True},
            execution_time=1.0,
            error=None,
        ))

    asyncio.run(run_hooks())


def test_scheduler_dispatch_state_survives_restart_and_serializes_dispatch(monkeypatch) -> None:
    fake_redis = FakeRedis()
    monkeypatch.setattr(tasks, "_redis", lambda: fake_redis)
    monkeypatch.setattr(tasks.settings, "archive_sync_interval_seconds", 60)
    monkeypatch.setattr(tasks.settings, "archive_schedule_run_immediately_on_start", False)

    async def exercise_gate():
        assert await tasks._begin_schedule_dispatch("first", now=100) == "not_due"
        assert await tasks._begin_schedule_dispatch("restart", now=130) == "not_due"
        assert await tasks._begin_schedule_dispatch("due", now=160) == "due"
        await tasks._renew_schedule_dispatch("due")
        assert await tasks._begin_schedule_dispatch("concurrent", now=161) == "busy"
        await tasks._finish_schedule_dispatch("due", completed=True, now=161)
        assert await tasks._begin_schedule_dispatch("another-restart", now=180) == "not_due"

    asyncio.run(exercise_gate())

    assert fake_redis.values[tasks._SCHEDULE_LAST_DISPATCH_KEY] == "161"
    schedule = tasks.schedule_enabled_accounts_task.labels["schedule"][0]
    assert schedule["schedule_id"] == "archivex-enabled-account-sync"


def test_scheduler_immediate_start_is_explicit(monkeypatch) -> None:
    fake_redis = FakeRedis()
    monkeypatch.setattr(tasks, "_redis", lambda: fake_redis)
    monkeypatch.setattr(tasks.settings, "archive_schedule_run_immediately_on_start", True)

    async def exercise_gate():
        assert await tasks._begin_schedule_dispatch("first", now=100) == "due"
        await tasks._finish_schedule_dispatch("first", completed=True, now=101)

    asyncio.run(exercise_gate())

    assert fake_redis.values[tasks._SCHEDULE_LAST_DISPATCH_KEY] == "101"


def test_gallery_dl_download_has_a_hard_timeout(tmp_path, monkeypatch) -> None:
    command = []
    options = {}

    def time_out(*args, **kwargs):
        command.extend(args[0])
        options.update(kwargs)
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", time_out)
    downloader = GalleryDlMediaDownloader(timeout_seconds=7)

    with pytest.raises(RuntimeError, match="timed out after 7 seconds"):
        downloader.download("https://example.test/image.jpg", tmp_path, 0)

    assert command[:3] == [sys.executable, "-m", "gallery_dl"]
    assert options["stdin"] is subprocess.DEVNULL


def test_gallery_dl_download_supports_an_explicit_executable(tmp_path, monkeypatch) -> None:
    command = []

    def time_out(*args, **kwargs):
        command.extend(args[0])
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", time_out)
    downloader = GalleryDlMediaDownloader(executable="custom-gallery-dl", timeout_seconds=7)

    with pytest.raises(RuntimeError, match="timed out after 7 seconds"):
        downloader.download("https://example.test/image.jpg", tmp_path, 0)

    assert command[0] == "custom-gallery-dl"


def test_gallery_dl_marks_forbidden_media_as_permanent(tmp_path, monkeypatch) -> None:
    def forbidden(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            "",
            "403 Forbidden for https://pbs.twimg.com/media/private.jpg",
        )

    monkeypatch.setattr(subprocess, "run", forbidden)
    downloader = GalleryDlMediaDownloader()

    with pytest.raises(PermanentMediaDownloadError) as raised:
        downloader.download("https://example.test/image.jpg", tmp_path, 0)

    assert "403 Forbidden" in str(raised.value)
    assert "pbs.twimg.com" not in str(raised.value)


def test_gallery_dl_isolates_concurrent_downloads_in_the_same_directory(
    tmp_path, monkeypatch
) -> None:
    barrier = threading.Barrier(2)

    def download_file(command, **kwargs):
        download_dir = Path(command[command.index("--directory") + 1])
        filename = command[-1].rsplit("/", 1)[-1]
        barrier.wait(timeout=2)
        (download_dir / filename).write_bytes(filename.encode())
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", download_file)
    downloader = GalleryDlMediaDownloader()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda url: downloader.download(url, tmp_path, 0),
            ["https://example.test/one.jpg", "https://example.test/two.jpg"],
        ))

    assert {result.local_path.name for result in results} == {"one.jpg", "two.jpg"}
    assert not list(tmp_path.glob(".archivex-media-*"))
