from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from taskiq import TaskiqMiddleware

from archivex.config import Settings

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 10
HEARTBEAT_TTL_SECONDS = 30
DEAD_CONSUMER_GRACE_SECONDS = HEARTBEAT_TTL_SECONDS + HEARTBEAT_INTERVAL_SECONDS
LONG_RUNNING_RECLAIM_MARGIN_SECONDS = 60
WORKER_CONSUMER_GROUP = "archivex-workers"
_SERVICE_ROLES = ("crawl-worker", "media-worker", "scheduler")
_DELETE_OWNED_HEARTBEAT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
_REDIS_CONNECTION_KWARGS = {
    "health_check_interval": 30,
    "socket_connect_timeout": 5,
    "socket_timeout": 5,
    "retry_on_timeout": True,
}


def heartbeat_key(role: str) -> str:
    return f"archivex:heartbeat:{role}"


def consumer_heartbeat_key(stream: str, consumer: str) -> str:
    return f"archivex:consumer-heartbeat:{stream}:{consumer}"


class QueueServiceHeartbeatMiddleware(TaskiqMiddleware):
    """Publish expiring process heartbeats without affecting queue operations."""

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.owner = str(uuid.uuid4())
        self.role: str | None = None
        self.stream: str | None = None
        self.consumer: str | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        if self.broker.is_scheduler_process:
            self.role = "scheduler"
        elif self.broker.is_worker_process:
            queue_name = getattr(self.broker, "queue_name", self.settings.task_worker_queue_name)
            self.stream = str(queue_name)
            self.consumer = str(getattr(self.broker, "consumer_name"))
            self.role = (
                "media-worker"
                if queue_name == self.settings.task_media_queue_name
                else "crawl-worker"
            )
        if self.role is not None and self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(),
                name=f"archivex-{self.role}-heartbeat",
            )

    async def shutdown(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None
        if self.role is None:
            return
        client = self._redis()
        try:
            keys = [heartbeat_key(self.role)]
            if self.stream is not None and self.consumer is not None:
                keys.append(consumer_heartbeat_key(self.stream, self.consumer))
            for key in keys:
                await client.eval(
                    _DELETE_OWNED_HEARTBEAT,
                    1,
                    key,
                    self.owner,
                )
        except Exception:
            logger.warning("Could not clear %s heartbeat during shutdown", self.role, exc_info=True)
        finally:
            try:
                await client.aclose()
            except Exception:
                logger.warning("Could not close heartbeat Redis client", exc_info=True)

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                client = self._redis()
                try:
                    await client.set(
                        heartbeat_key(self.role or "unknown"),
                        self.owner,
                        ex=HEARTBEAT_TTL_SECONDS,
                    )
                    if self.stream is not None and self.consumer is not None:
                        await client.set(
                            consumer_heartbeat_key(self.stream, self.consumer),
                            self.owner,
                            ex=HEARTBEAT_TTL_SECONDS,
                        )
                finally:
                    await client.aclose()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Could not publish %s heartbeat", self.role, exc_info=True)
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

    def _redis(self) -> Redis:
        return Redis.from_url(
            self.settings.task_redis_url,
            decode_responses=True,
            **_REDIS_CONNECTION_KWARGS,
        )


class SystemReadinessProbe:
    def __init__(
        self,
        settings: Settings,
        redis_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings
        self._redis_factory = redis_factory or self._redis

    async def check(self) -> dict[str, Any]:
        checks: dict[str, Any] = {}
        issues: list[str] = []

        checks["archive_database"] = await self._database_check(
            self.settings.archive_db_path,
            "archive database",
            issues,
        )
        if not self.settings.task_queue_enabled:
            checks["queue"] = {"status": "disabled"}
            return {
                "status": "ready" if not issues else "not_ready",
                "checks": checks,
                "issues": issues,
            }

        client = None
        try:
            client = self._redis_factory()
            await client.ping()
            checks["redis"] = {"status": "ok"}
            heartbeat_values = await client.mget([heartbeat_key(role) for role in _SERVICE_ROLES])
            services: dict[str, dict[str, str]] = {}
            for role, value in zip(_SERVICE_ROLES, heartbeat_values, strict=True):
                service_status = "ok" if value else "missing"
                services[role] = {"status": service_status}
                if value is None:
                    issues.append(f"{role} heartbeat is missing")
            checks["services"] = services

            queues = {
                "crawl": await self._queue_metrics(
                    client,
                    self.settings.task_crawl_queue_name,
                    self.settings.task_sync_timeout_seconds,
                    issues,
                ),
                "media": await self._queue_metrics(
                    client,
                    self.settings.task_media_queue_name,
                    self.settings.task_media_timeout_seconds,
                    issues,
                ),
            }
            checks["queues"] = queues
            checks["retries"] = await self._retry_metrics(client, issues)
        except Exception as exc:
            checks["redis"] = {"status": "error", "error": exc.__class__.__name__}
            issues.append("Redis is unavailable")
        finally:
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    logger.warning("Could not close readiness Redis client", exc_info=True)

        return {
            "status": "ready" if not issues else "not_ready",
            "checks": checks,
            "issues": issues,
        }

    async def _database_check(
        self,
        path: Path,
        label: str,
        issues: list[str],
    ) -> dict[str, str]:
        try:
            await asyncio.to_thread(_check_sqlite_read_write, path)
        except Exception as exc:
            issues.append(f"{label} is unavailable")
            return {"status": "error", "error": exc.__class__.__name__}
        return {"status": "ok"}

    async def _queue_metrics(
        self,
        client: Any,
        stream: str,
        task_timeout_seconds: int,
        issues: list[str],
    ) -> dict[str, Any]:
        try:
            groups = await client.xinfo_groups(stream)
        except ResponseError as exc:
            if "no such key" in str(exc).lower():
                issues.append(f"consumer group for {stream} is missing")
                return {"status": "missing", "lag": 0, "pending": 0, "consumers": 0}
            raise

        group = next(
            (item for item in groups if item.get("name") == WORKER_CONSUMER_GROUP),
            None,
        )
        if group is None:
            issues.append(f"consumer group for {stream} is missing")
            return {"status": "missing", "lag": 0, "pending": 0, "consumers": 0}

        pending = int(group.get("pending") or 0)
        metrics: dict[str, Any] = {
            "status": "ok",
            "lag": int(group.get("lag") or 0),
            "pending": pending,
            "consumers": int(group.get("consumers") or 0),
        }
        if pending:
            consumers = await client.xinfo_consumers(
                stream,
                WORKER_CONSUMER_GROUP,
            )
            pending_consumers = [
                item for item in consumers if int(item.get("pending") or 0) > 0
            ]
            heartbeat_keys = [
                consumer_heartbeat_key(stream, _redis_text(item.get("name")))
                for item in pending_consumers
            ]
            heartbeat_values = await client.mget(heartbeat_keys) if heartbeat_keys else []
            dead_consumer_after_ms = DEAD_CONSUMER_GRACE_SECONDS * 1000
            orphaned_pending = sum(
                int(item.get("pending") or 0)
                for item, heartbeat in zip(
                    pending_consumers,
                    heartbeat_values,
                    strict=True,
                )
                if heartbeat is None and int(item.get("idle") or 0) >= dead_consumer_after_ms
            )
            metrics["orphaned_pending"] = orphaned_pending
            if orphaned_pending:
                metrics["status"] = "stalled"
                issues.append(f"{stream} has pending tasks owned by inactive consumers")

            oldest = await client.xpending_range(
                stream,
                WORKER_CONSUMER_GROUP,
                min="-",
                max="+",
                count=1,
            )
            if oldest:
                idle_ms = int(oldest[0].get("time_since_delivered") or 0)
                metrics["oldest_pending_idle_ms"] = idle_ms
                metrics["oldest_pending_deliveries"] = int(
                    oldest[0].get("times_delivered") or 0
                )
                stale_after_ms = (
                    task_timeout_seconds + LONG_RUNNING_RECLAIM_MARGIN_SECONDS
                ) * 1000
                stalled = await client.xpending_range(
                    stream,
                    WORKER_CONSUMER_GROUP,
                    min="-",
                    max="+",
                    count=1,
                    idle=stale_after_ms,
                )
                if stalled:
                    metrics["status"] = "stalled"
                    issues.append(f"{stream} has a stalled pending task")
        return metrics

    async def _retry_metrics(
        self,
        client: Any,
        issues: list[str],
    ) -> dict[str, Any]:
        data_keys = [
            key async for key in client.scan_iter(
                match="archivex:retry-schedules:data:*"
            )
        ]
        overdue = 0
        cutoff = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=1)
        async for key in client.scan_iter(match="archivex:retry-schedules:time:*"):
            key_text = key.decode() if isinstance(key, bytes) else str(key)
            try:
                scheduled_minute = datetime.strptime(
                    key_text.rsplit(":time:", 1)[1],
                    "%Y-%m-%dT%H:%M",
                ).replace(tzinfo=UTC)
            except (IndexError, ValueError):
                continue
            if scheduled_minute <= cutoff and await client.llen(key):
                overdue += 1
        status = "stalled" if overdue else "ok"
        if overdue:
            issues.append("retry scheduler has overdue tasks")
        return {
            "status": status,
            "scheduled": len(data_keys),
            "overdue_buckets": overdue,
        }

    def _redis(self) -> Redis:
        return Redis.from_url(
            self.settings.task_redis_url,
            decode_responses=True,
            **_REDIS_CONNECTION_KWARGS,
        )


def _check_sqlite_read_write(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    with sqlite3.connect(path, timeout=2) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("SELECT 1")
        connection.rollback()


def _redis_text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)
