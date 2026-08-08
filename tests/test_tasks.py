import asyncio
import json
import sqlite3
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from taskiq.exceptions import NoResultError
from taskiq.message import TaskiqMessage
from taskiq.result import TaskiqResult

from archivex.media import GalleryDlMediaDownloader, PermanentMediaDownloadError
from archivex import tasks
from archivex.task_center import TaskCenterRepository, _automatic_retries_exhausted


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, *, ex, nx):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def expire(self, key, seconds):
        return key in self.values

    async def eval(self, script, key_count, key, expected):
        if self.values.get(key) == expected:
            del self.values[key]
            return 1
        return 0

    async def aclose(self):
        return None


class FakeTask:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.task_id = None
        self.calls = []

    def kicker(self):
        return self

    def with_task_id(self, task_id):
        self.task_id = task_id
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

    async def fail_retry(*args, **kwargs):
        raise ConnectionError("redis connection closed")

    async def release_lock(message):
        released.append(message.task_id)

    monkeypatch.setattr(tasks.SmartRetryMiddleware, "on_error", fail_retry)
    monkeypatch.setattr(tasks, "_release_failed_retry_lock", release_lock)
    middleware = tasks.ResilientSmartRetryMiddleware()
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
    retried = []

    async def record_retry(*args, **kwargs):
        retried.append(True)

    monkeypatch.setattr(tasks.SmartRetryMiddleware, "on_error", record_retry)
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

    assert retried == []
    assert result.error is error
    assert _automatic_retries_exhausted({
        "error": repr(error),
        "labels": {"max_retries": 5, "_retries": 0},
    }) is True


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


def test_enqueue_failure_releases_dedupe_lock(monkeypatch) -> None:
    fake_redis = FakeRedis()
    fake_task = FakeTask(should_fail=True)
    abandoned = []

    class FakeTaskCenter:
        def abandon_queued_task(self, task_id, error):
            abandoned.append((task_id, error))
            return True

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


def test_abandon_queued_task_finishes_dashboard_record(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    task_id = uuid.uuid4()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """CREATE TABLE tasks (
                id CHAR(32) PRIMARY KEY, name TEXT NOT NULL, status INTEGER NOT NULL,
                worker TEXT NOT NULL, args JSON NOT NULL, kwargs JSON NOT NULL,
                labels JSON NOT NULL, result JSON, error TEXT, queued_at DATETIME,
                started_at DATETIME, finished_at DATETIME
            )"""
        )
        connection.execute(
            """INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id.hex, "archivex.sync_account", 3, "archivex:crawl",
                json.dumps(["42", "manual"]), "{}", "{}", None, None,
                "2026-08-08 12:00:00", None, None,
            ),
        )

    repository = TaskCenterRepository(database_path, 3600, "archivex:crawl")

    assert repository.abandon_queued_task(str(task_id), "publish failed") is True
    task = repository.get_task(str(task_id))
    assert task is not None
    assert task["status"] == "abandoned"
    assert task["error"] == "publish failed"
    assert task["finished_at"] is not None
    assert repository.abandon_queued_task(str(task_id), "again") is False
    assert repository.delete_abandoned_tasks() == 1
    assert repository.get_task(str(task_id)) is None
    assert repository.delete_abandoned_tasks() == 0


def test_reset_retried_task_clears_previous_attempt_lifecycle(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    task_id = uuid.uuid4()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """CREATE TABLE tasks (
                id CHAR(32) PRIMARY KEY, name TEXT NOT NULL, status INTEGER NOT NULL,
                worker TEXT NOT NULL, args JSON NOT NULL, kwargs JSON NOT NULL,
                labels JSON NOT NULL, result JSON, error TEXT, queued_at DATETIME,
                started_at DATETIME, finished_at DATETIME
            )"""
        )
        connection.execute(
            """INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id.hex, "archivex.download_media", 2, "archivex:media",
                "[]", "{}", "{}", "old result", "old error",
                "2026-08-08 12:01:00", "2026-08-08 12:00:00",
                "2026-08-08 12:00:30",
            ),
        )

    repository = TaskCenterRepository(database_path, 3600, "archivex:crawl")

    assert repository.reset_retried_task(str(task_id)) is True
    task = repository.get_task(str(task_id))
    assert task is not None
    assert task["status"] == "queued"
    assert task["started_at"] is None
    assert task["finished_at"] is None
    assert task["result"] is None
    assert task["error"] is None
    assert task["duration_ms"] is None


def test_retry_middleware_resets_dashboard_and_discards_consumed_schedule(monkeypatch) -> None:
    reset = []
    events = []

    class FakeTaskCenter:
        def reset_retried_task(self, task_id):
            reset.append(task_id)
            return True

    async def record_event(endpoint, payload):
        events.append((endpoint, payload))

    middleware = tasks.MountedTaskiqAdminMiddleware(
        url="http://example.test", api_token="token", taskiq_broker_name="archivex:media"
    )
    monkeypatch.setattr(tasks, "_task_center_repository", FakeTaskCenter)
    monkeypatch.setattr(middleware, "_spawn_request", record_event)
    message = TaskiqMessage(
        task_id=str(uuid.uuid4()),
        task_name="archivex.download_media",
        labels={"_retries": "1", "schedule_id": "consumed"},
        args=["media-id"],
        kwargs={},
    )

    async def run_hooks():
        await middleware.pre_send(message)
        await middleware.pre_execute(message)
        await middleware.post_execute(message, TaskiqResult(
            is_err=True,
            return_value=None,
            execution_time=1.0,
            error=NoResultError(),
        ))

    asyncio.run(run_hooks())

    assert reset == [message.task_id, message.task_id]
    assert "schedule_id" not in message.labels
    assert [event[0].rsplit("/", 1)[-1] for event in events] == [
        "queued", "started", "executed",
    ]


def test_gallery_dl_download_has_a_hard_timeout(tmp_path, monkeypatch) -> None:
    command = []

    def time_out(*args, **kwargs):
        command.extend(args[0])
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", time_out)
    downloader = GalleryDlMediaDownloader(timeout_seconds=7)

    with pytest.raises(RuntimeError, match="timed out after 7 seconds"):
        downloader.download("https://example.test/image.jpg", tmp_path, 0)

    assert command[:3] == [sys.executable, "-m", "gallery_dl"]


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
