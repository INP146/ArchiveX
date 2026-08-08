import asyncio
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from archivex.media import GalleryDlMediaDownloader
from archivex import tasks


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
    monkeypatch.setattr(tasks, "_redis", lambda: fake_redis)
    monkeypatch.setattr(tasks, "sync_account_task", FakeTask(should_fail=True))

    with pytest.raises(RuntimeError, match="redis unavailable"):
        asyncio.run(tasks.enqueue_account_sync("42"))

    assert fake_redis.values == {}


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
