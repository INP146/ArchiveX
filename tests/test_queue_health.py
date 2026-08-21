import asyncio
from types import SimpleNamespace

from archivex.config import Settings
from archivex.queue_health import (
    DEAD_CONSUMER_GRACE_SECONDS,
    QueueServiceHeartbeatMiddleware,
    SystemReadinessProbe,
    consumer_heartbeat_key,
    heartbeat_key,
)
from archivex.storage import initialize_storage


class FakeRedis:
    def __init__(
        self,
        *,
        heartbeats=True,
        consumer_heartbeats=True,
        pending=0,
        pending_idle_ms=0,
        retry_data_keys=None,
        retry_time_buckets=None,
    ):
        self.values = {}
        self.heartbeats = heartbeats
        self.consumer_heartbeats = consumer_heartbeats
        self.pending = pending
        self.pending_idle_ms = pending_idle_ms
        self.retry_data_keys = retry_data_keys or []
        self.retry_time_buckets = retry_time_buckets or {}

    async def set(self, key, value, *, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script, key_count, key, owner):
        if self.values.get(key) == owner:
            del self.values[key]
            return 1
        return 0

    async def ping(self):
        return True

    async def mget(self, keys):
        values = []
        for index, key in enumerate(keys):
            is_consumer = str(key).startswith("archivex:consumer-heartbeat:")
            present = self.consumer_heartbeats if is_consumer else self.heartbeats
            values.append(f"owner-{index}" if present else None)
        return values

    async def xinfo_groups(self, stream):
        return [{
            "name": "archivex-workers",
            "consumers": 1,
            "pending": self.pending,
            "lag": 2,
        }]

    async def xinfo_consumers(self, stream, group):
        assert group == "archivex-workers"
        return [{
            "name": "worker-1",
            "pending": self.pending,
            "idle": self.pending_idle_ms,
        }]

    async def xpending_range(self, *args, **kwargs):
        if kwargs.get("idle") and self.pending_idle_ms < kwargs["idle"]:
            return []
        return [{
            "time_since_delivered": self.pending_idle_ms,
            "times_delivered": 2,
        }]

    async def scan_iter(self, match):
        keys = (
            self.retry_data_keys
            if match.endswith(":data:*")
            else self.retry_time_buckets
        )
        for key in keys:
            yield key

    async def llen(self, key):
        return self.retry_time_buckets.get(key, 0)

    async def aclose(self):
        return None


def _settings(tmp_path, **overrides):
    values = {
        "_env_file": None,
        "archive_db_path": tmp_path / "archive.sqlite3",
        "archive_data_dir": tmp_path / "archive",
        "twscrape_session_path": tmp_path / "sessions",
        "web_auth_token": "test-token",
    }
    values.update(overrides)
    settings = Settings(**values)
    initialize_storage(
        settings.archive_db_path,
        settings.archive_data_dir,
        settings.twscrape_session_path,
    )
    return settings


def test_queue_service_heartbeat_tracks_worker_role_and_clears_owned_key(tmp_path) -> None:
    settings = _settings(tmp_path)
    fake_redis = FakeRedis()
    middleware = QueueServiceHeartbeatMiddleware(settings)
    middleware.set_broker(SimpleNamespace(
        is_scheduler_process=False,
        is_worker_process=True,
        queue_name=settings.task_media_queue_name,
        consumer_name="worker-1",
    ))
    middleware._redis = lambda: fake_redis

    async def run_heartbeat():
        await middleware.startup()
        await asyncio.sleep(0)
        assert middleware.role == "media-worker"
        assert fake_redis.values[heartbeat_key("media-worker")] == middleware.owner
        assert fake_redis.values[consumer_heartbeat_key(
            settings.task_media_queue_name,
            "worker-1",
        )] == middleware.owner
        await middleware.shutdown()

    asyncio.run(run_heartbeat())

    assert heartbeat_key("media-worker") not in fake_redis.values
    assert consumer_heartbeat_key(settings.task_media_queue_name, "worker-1") not in fake_redis.values


def test_readiness_reports_pending_owned_by_inactive_consumer_immediately(tmp_path) -> None:
    settings = _settings(tmp_path)
    fake_redis = FakeRedis(
        consumer_heartbeats=False,
        pending=3,
        pending_idle_ms=(DEAD_CONSUMER_GRACE_SECONDS + 1) * 1000,
    )

    result = asyncio.run(SystemReadinessProbe(
        settings,
        redis_factory=lambda: fake_redis,
    ).check())

    assert result["status"] == "not_ready"
    assert result["checks"]["queues"]["crawl"]["status"] == "stalled"
    assert result["checks"]["queues"]["crawl"]["orphaned_pending"] == 3
    assert (
        f"{settings.task_crawl_queue_name} has pending tasks owned by inactive consumers"
        in result["issues"]
    )


def test_readiness_reports_redis_services_and_stream_metrics(tmp_path) -> None:
    settings = _settings(tmp_path)
    fake_redis = FakeRedis()
    result = asyncio.run(SystemReadinessProbe(
        settings,
        redis_factory=lambda: fake_redis,
    ).check())

    assert result["status"] == "ready"
    assert result["checks"]["redis"]["status"] == "ok"
    assert result["checks"]["services"]["scheduler"]["status"] == "ok"
    assert result["checks"]["queues"]["crawl"] == {
        "status": "ok",
        "lag": 2,
        "pending": 0,
        "consumers": 1,
    }
    assert result["checks"]["retries"] == {
        "status": "ok",
        "scheduled": 0,
        "overdue_buckets": 0,
    }


def test_readiness_fails_for_missing_heartbeats_and_stalled_pending_task(tmp_path) -> None:
    settings = _settings(tmp_path)
    fake_redis = FakeRedis(
        heartbeats=False,
        pending=1,
        pending_idle_ms=(settings.task_sync_timeout_seconds + 61) * 1000,
    )
    result = asyncio.run(SystemReadinessProbe(
        settings,
        redis_factory=lambda: fake_redis,
    ).check())

    assert result["status"] == "not_ready"
    assert result["checks"]["services"]["crawl-worker"]["status"] == "missing"
    assert result["checks"]["queues"]["crawl"]["status"] == "stalled"
    assert "crawl-worker heartbeat is missing" in result["issues"]
    assert f"{settings.task_crawl_queue_name} has a stalled pending task" in result["issues"]


def test_readiness_fails_when_retry_schedule_has_overdue_tasks(tmp_path) -> None:
    settings = _settings(tmp_path)
    overdue_bucket = "archivex:retry-schedules:time:2020-01-01T00:00"
    fake_redis = FakeRedis(
        retry_data_keys=["archivex:retry-schedules:data:task-1"],
        retry_time_buckets={overdue_bucket: 1},
    )

    result = asyncio.run(SystemReadinessProbe(
        settings,
        redis_factory=lambda: fake_redis,
    ).check())

    assert result["status"] == "not_ready"
    assert result["checks"]["retries"] == {
        "status": "stalled",
        "scheduled": 1,
        "overdue_buckets": 1,
    }
    assert "retry scheduler has overdue tasks" in result["issues"]


def test_readiness_contains_redis_factory_failures(tmp_path) -> None:
    settings = _settings(tmp_path)

    def fail_redis():
        raise ConnectionError("redis unavailable")

    result = asyncio.run(SystemReadinessProbe(
        settings,
        redis_factory=fail_redis,
    ).check())

    assert result["status"] == "not_ready"
    assert result["checks"]["redis"] == {
        "status": "error",
        "error": "ConnectionError",
    }
