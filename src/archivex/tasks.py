from __future__ import annotations

import asyncio
import datetime
import logging
import time
import uuid
from functools import lru_cache
from typing import Any

from redis.asyncio import Redis
from taskiq import Context, TaskiqDepends, TaskiqMiddleware, TaskiqScheduler
from taskiq.exceptions import NoResultError
from taskiq.kicker import AsyncKicker
from taskiq.message import TaskiqMessage
from taskiq.middlewares import SmartRetryMiddleware
from taskiq.result import TaskiqResult
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import ListRedisScheduleSource, RedisAsyncResultBackend

from archivex.config import Settings, get_settings
from archivex.media import GalleryDlMediaDownloader, PermanentMediaDownloadError
from archivex.queue_broker import ReclaimingRedisStreamBroker
from archivex.queue_health import QueueServiceHeartbeatMiddleware
from archivex.source import TwscrapePostSource
from archivex.storage import ArchiveRepository
from archivex.sync import ArchiveSyncService
from archivex.task_center import (
    TASK_ACCOUNT_ID_LABEL,
    TASK_MEDIA_ID_LABEL,
    TASK_PARENT_ID_LABEL,
    TASK_TRIGGER_LABEL,
    TaskCenterRepository,
)
from archivex.task_dispatcher import TaskSubmission

logger = logging.getLogger(__name__)
settings = get_settings()

_REDIS_SOCKET_TIMEOUT_SECONDS = 5
_RETRY_SCHEDULE_TIMEOUT_SECONDS = 12
_REDIS_CONNECTION_KWARGS = {
    "health_check_interval": 30,
    "socket_connect_timeout": _REDIS_SOCKET_TIMEOUT_SECONDS,
    "socket_timeout": _REDIS_SOCKET_TIMEOUT_SECONDS,
    "retry_on_timeout": True,
}
_SCHEDULE_DISPATCH_LEASE_SECONDS = 300
_SCHEDULE_DISPATCH_LEASE_KEY = "archivex:schedule:enabled-accounts:lease"
_SCHEDULE_LAST_DISPATCH_KEY = "archivex:schedule:enabled-accounts:last-dispatched-at"

_DELETE_OWNED_LOCK = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
_EXTEND_OWNED_LOCK = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""
_CLAIM_EXECUTION_LOCK = """
local owner = redis.call('get', KEYS[1])
if owner == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
if not owner then
    return redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[2], 'NX')
end
return 0
"""


class TaskLifecycleMiddleware(TaskiqMiddleware):
    """Persist monotonic task-attempt transitions after broker operations."""

    @staticmethod
    def _now_iso() -> str:
        return datetime.datetime.now(datetime.UTC).isoformat()

    async def _record(self, method: str, *args: Any, **kwargs: Any) -> None:
        try:
            repository = _task_center_repository()
            await asyncio.to_thread(getattr(repository, method), *args, **kwargs)
        except Exception:
            logger.warning("Could not persist task lifecycle event %s", method, exc_info=True)

    async def post_send(self, message: TaskiqMessage) -> None:
        await self._record(
            "record_queued",
            message.task_id,
            message.task_name,
            str(message.labels.get("queue_name", settings.task_worker_queue_name)),
            message.args,
            message.kwargs,
            message.labels,
            self._now_iso(),
        )

    async def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        # A consumed one-time schedule must not be reused for the next retry.
        if "_retries" in message.labels:
            message.labels.pop("schedule_id", None)
        await self._record(
            "record_started",
            message.task_id,
            message.task_name,
            str(message.labels.get("queue_name", settings.task_worker_queue_name)),
            message.args,
            message.kwargs,
            message.labels,
            self._now_iso(),
        )
        return message

    async def post_execute(
        self,
        message: TaskiqMessage,
        result: TaskiqResult[Any],
    ) -> None:
        if isinstance(result.error, NoResultError) and result.labels.get(
            "_archivex_retry_scheduled"
        ):
            return
        await self._record(
            "record_finished",
            message.task_id,
            message.labels,
            result=result.return_value,
            error=None if result.error is None else repr(result.error),
            finished_at=self._now_iso(),
            name=message.task_name,
            worker=str(message.labels.get("queue_name", settings.task_worker_queue_name)),
            args=message.args,
            kwargs=message.kwargs,
        )


class ResilientSmartRetryMiddleware(SmartRetryMiddleware):
    """Schedule retries and persist their state without hiding failed attempts."""

    async def on_error(
        self,
        message: TaskiqMessage,
        result: TaskiqResult[Any],
        exception: BaseException,
    ) -> None:
        if isinstance(exception, PermanentMediaDownloadError):
            await _release_failed_retry_lock(message)
            return

        if self.types_of_exceptions is not None and not isinstance(
            exception,
            tuple(self.types_of_exceptions),
        ):
            await _release_failed_retry_lock(message)
            return
        if isinstance(exception, NoResultError) or not self.is_retry_on_error(message):
            await _release_failed_retry_lock(message)
            return

        retries = int(message.labels.get("_retries", 0)) + 1
        max_retries = int(message.labels.get("max_retries", self.default_retry_count))
        if retries >= max_retries:
            await _release_failed_retry_lock(message)
            return

        delay = self.make_delay(message, retries)
        target_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=delay)
        kicker = (
            AsyncKicker(
                task_name=message.task_name,
                broker=self.broker,
                labels=dict(message.labels),
            )
            .with_task_id(message.task_id)
            .with_labels(_retries=retries)
        )
        try:
            async with asyncio.timeout(_RETRY_SCHEDULE_TIMEOUT_SECONDS):
                if self.schedule_source is None:
                    await kicker.with_labels(delay=delay).kiq(
                        *message.args,
                        **message.kwargs,
                    )
                else:
                    await kicker.schedule_by_time(
                        self.schedule_source,
                        target_time,
                        *message.args,
                        **message.kwargs,
                    )
        except Exception as retry_error:
            logger.exception(
                "Could not schedule retry for task %s; finishing the attempt as failed",
                message.task_id,
            )
            result.error = RuntimeError(
                f"{exception}; retry scheduling failed: "
                f"{retry_error.__class__.__name__}: {str(retry_error) or 'unknown error'}"
            )
            await _release_failed_retry_lock(message)
            return

        try:
            await asyncio.to_thread(
                _task_center_repository().record_retry_scheduled,
                message.task_id,
                message.labels,
                repr(exception),
                target_time.isoformat(),
                name=message.task_name,
                worker=str(message.labels.get("queue_name", settings.task_worker_queue_name)),
                args=message.args,
                kwargs=message.kwargs,
            )
        except Exception:
            logger.warning(
                "Could not persist retry schedule for task %s",
                message.task_id,
                exc_info=True,
            )
        result.labels["_archivex_retry_scheduled"] = True
        result.error = NoResultError()


result_backend = RedisAsyncResultBackend(
    redis_url=settings.task_redis_url,
    result_ex_time=settings.task_result_ttl_seconds,
    **_REDIS_CONNECTION_KWARGS,
)
retry_schedule_source = ListRedisScheduleSource(
    settings.task_redis_url,
    prefix="archivex:retry-schedules",
    **_REDIS_CONNECTION_KWARGS,
)

_worker_timeout = (
    settings.task_media_timeout_seconds
    if settings.task_worker_queue_name == settings.task_media_queue_name
    else settings.task_sync_timeout_seconds
)
_worker_concurrency = (
    4
    if settings.task_worker_queue_name == settings.task_media_queue_name
    else 1
)
broker = ReclaimingRedisStreamBroker(
    url=settings.task_redis_url,
    queue_name=settings.task_worker_queue_name,
    consumer_group_name="archivex-workers",
    idle_timeout=(_worker_timeout + 60) * 1000,
    unacknowledged_lock_timeout=30,
    unacknowledged_batch_size=_worker_concurrency,
    xread_count=_worker_concurrency,
    maxlen=100_000,
    **_REDIS_CONNECTION_KWARGS,
).with_result_backend(result_backend).with_middlewares(
    ResilientSmartRetryMiddleware(
        default_retry_count=settings.task_retry_count,
        default_delay=settings.task_retry_delay_seconds,
        use_jitter=True,
        use_delay_exponent=True,
        max_delay_exponent=settings.task_retry_max_delay_seconds,
        schedule_source=retry_schedule_source,
    ),
    TaskLifecycleMiddleware(),
    QueueServiceHeartbeatMiddleware(settings),
)

scheduler = TaskiqScheduler(
    broker=broker,
    sources=[LabelScheduleSource(broker), retry_schedule_source],
)


def _repository() -> ArchiveRepository:
    return ArchiveRepository(settings.archive_db_path, settings.archive_data_dir)


@lru_cache(maxsize=1)
def _task_center_repository() -> TaskCenterRepository:
    return TaskCenterRepository(
        settings.archive_db_path,
        settings.archive_sync_interval_seconds,
        settings.task_crawl_queue_name,
    )


def _redis() -> Redis:
    return Redis.from_url(
        settings.task_redis_url,
        decode_responses=True,
        **_REDIS_CONNECTION_KWARGS,
    )


def _sync_lock_key(x_user_id: str) -> str:
    return f"archivex:dedupe:sync:{x_user_id}"


def _media_lock_key(media_id: str) -> str:
    return f"archivex:dedupe:media:{media_id}"


async def _reserve_lock(key: str, task_id: str) -> tuple[bool, str]:
    client = _redis()
    try:
        reserved = await client.set(
            key,
            task_id,
            ex=settings.task_dedupe_ttl_seconds,
            nx=True,
        )
        if reserved:
            return True, task_id
        existing = await client.get(key)
        return False, str(existing or task_id)
    finally:
        await client.aclose()


async def _claim_execution(key: str, task_id: str) -> bool:
    client = _redis()
    try:
        return bool(await client.eval(
            _CLAIM_EXECUTION_LOCK,
            1,
            key,
            task_id,
            settings.task_dedupe_ttl_seconds,
        ))
    finally:
        await client.aclose()


async def _extend_execution_lock(key: str, task_id: str) -> None:
    client = _redis()
    try:
        extended = await client.eval(
            _EXTEND_OWNED_LOCK,
            1,
            key,
            task_id,
            settings.task_dedupe_ttl_seconds,
        )
        if not extended:
            raise RuntimeError("task dedupe lock ownership was lost")
    finally:
        await client.aclose()


async def _release_lock(key: str, task_id: str) -> None:
    client = _redis()
    try:
        await client.eval(_DELETE_OWNED_LOCK, 1, key, task_id)
    finally:
        await client.aclose()


async def _release_failed_retry_lock(message: TaskiqMessage) -> None:
    if not message.args:
        return
    target_id = str(message.args[0])
    if message.task_name == "archivex.sync_account":
        lock_key = _sync_lock_key(target_id)
    elif message.task_name == "archivex.download_media":
        lock_key = _media_lock_key(target_id)
    else:
        return
    try:
        await _release_lock(lock_key, message.task_id)
    except Exception:
        logger.exception(
            "Could not release dedupe lock for terminal task %s",
            message.task_id,
        )


async def _rollback_failed_publish(
    key: str,
    task_id: str,
    exc: BaseException,
    *,
    name: str,
    worker: str,
    args: list[Any],
    labels: dict[str, Any],
) -> None:
    try:
        await _release_lock(key, task_id)
    except Exception:
        logger.exception("Could not release dedupe lock after publish failed for task %s", task_id)
    message = (
        "Task was not published to the broker: "
        f"{exc.__class__.__name__}: {str(exc) or 'unknown error'}"
    )
    try:
        await asyncio.to_thread(
            _task_center_repository().record_publish_failed,
            task_id,
            name,
            worker,
            args,
            {},
            labels,
            message,
        )
    except Exception:
        logger.exception("Could not record failed publish for task %s", task_id)


async def _begin_schedule_dispatch(task_id: str, now: float | None = None) -> str:
    current_time = now if now is not None else time.time()
    client = _redis()
    lease_acquired = False
    dispatch_due = False
    try:
        lease_acquired = bool(await client.set(
            _SCHEDULE_DISPATCH_LEASE_KEY,
            task_id,
            ex=_SCHEDULE_DISPATCH_LEASE_SECONDS,
            nx=True,
        ))
        if not lease_acquired:
            return "busy"
        last_dispatch = await client.get(_SCHEDULE_LAST_DISPATCH_KEY)
        if last_dispatch is None:
            if settings.archive_schedule_run_immediately_on_start:
                dispatch_due = True
                return "due"
            await client.set(_SCHEDULE_LAST_DISPATCH_KEY, str(current_time))
            return "not_due"
        try:
            elapsed = current_time - float(last_dispatch)
        except (TypeError, ValueError):
            logger.warning("Replacing invalid persisted scheduler timestamp %r", last_dispatch)
            elapsed = settings.archive_sync_interval_seconds
        dispatch_due = elapsed >= settings.archive_sync_interval_seconds
        return "due" if dispatch_due else "not_due"
    finally:
        if lease_acquired and not dispatch_due:
            try:
                await client.eval(
                    _DELETE_OWNED_LOCK,
                    1,
                    _SCHEDULE_DISPATCH_LEASE_KEY,
                    task_id,
                )
            except Exception:
                logger.warning("Could not release skipped scheduler dispatch lease", exc_info=True)
        try:
            await client.aclose()
        except Exception:
            logger.warning("Could not close scheduler dispatch Redis client", exc_info=True)


async def _renew_schedule_dispatch(task_id: str) -> None:
    client = _redis()
    try:
        renewed = await client.eval(
            _EXTEND_OWNED_LOCK,
            1,
            _SCHEDULE_DISPATCH_LEASE_KEY,
            task_id,
            _SCHEDULE_DISPATCH_LEASE_SECONDS,
        )
        if not renewed:
            raise RuntimeError("scheduler dispatch lease ownership was lost")
    finally:
        try:
            await client.aclose()
        except Exception:
            logger.warning("Could not close scheduler dispatch Redis client", exc_info=True)


async def _finish_schedule_dispatch(
    task_id: str,
    *,
    completed: bool,
    now: float | None = None,
) -> None:
    client = _redis()
    try:
        if completed:
            await client.set(
                _SCHEDULE_LAST_DISPATCH_KEY,
                str(now if now is not None else time.time()),
            )
    finally:
        try:
            await client.eval(
                _DELETE_OWNED_LOCK,
                1,
                _SCHEDULE_DISPATCH_LEASE_KEY,
                task_id,
            )
        except Exception:
            logger.warning("Could not release scheduler dispatch lease", exc_info=True)
        finally:
            try:
                await client.aclose()
            except Exception:
                logger.warning("Could not close scheduler dispatch Redis client", exc_info=True)


def _is_final_attempt(context: Context) -> bool:
    retries = int(context.message.labels.get("_retries", 0))
    max_retries = int(context.message.labels.get("max_retries", settings.task_retry_count))
    return retries + 1 >= max_retries


def _task_labels(task: Any) -> dict[str, Any]:
    return dict(getattr(task, "labels", {}))


async def enqueue_account_sync(
    x_user_id: str,
    trigger: str = "manual",
    retry_of: str | None = None,
) -> TaskSubmission:
    task_id = str(uuid.uuid4())
    lock_key = _sync_lock_key(x_user_id)
    reserved, owner_task_id = await _reserve_lock(lock_key, task_id)
    if not reserved:
        return TaskSubmission(owner_task_id, "queued", True)
    labels = _task_labels(sync_account_task)
    task_labels = {
        TASK_ACCOUNT_ID_LABEL: x_user_id,
        TASK_TRIGGER_LABEL: trigger,
    }
    labels.update(task_labels)
    kicker = (
        sync_account_task.kicker()
        .with_task_id(task_id)
        .with_labels(**task_labels)
    )
    if retry_of is not None:
        labels["retry_of"] = retry_of
        kicker = kicker.with_labels(retry_of=retry_of)
    try:
        await kicker.kiq(x_user_id, trigger)
    except Exception as exc:
        await _rollback_failed_publish(
            lock_key,
            task_id,
            exc,
            name="archivex.sync_account",
            worker=settings.task_crawl_queue_name,
            args=[x_user_id, trigger],
            labels=labels,
        )
        raise
    return TaskSubmission(task_id, "queued", False)


async def enqueue_media_download(
    media_id: str,
    retry_of: str | None = None,
    parent_task_id: str | None = None,
) -> TaskSubmission:
    task_id = str(uuid.uuid4())
    lock_key = _media_lock_key(media_id)
    reserved, owner_task_id = await _reserve_lock(lock_key, task_id)
    if not reserved:
        return TaskSubmission(owner_task_id, "queued", True)
    labels = _task_labels(download_media_task)
    task_labels = {TASK_MEDIA_ID_LABEL: media_id}
    if parent_task_id is not None:
        task_labels[TASK_PARENT_ID_LABEL] = parent_task_id
    labels.update(task_labels)
    kicker = (
        download_media_task.kicker()
        .with_task_id(task_id)
        .with_labels(**task_labels)
    )
    if retry_of is not None:
        labels["retry_of"] = retry_of
        kicker = kicker.with_labels(retry_of=retry_of)
    try:
        await kicker.kiq(media_id)
    except Exception as exc:
        await _rollback_failed_publish(
            lock_key,
            task_id,
            exc,
            name="archivex.download_media",
            worker=settings.task_media_queue_name,
            args=[media_id],
            labels=labels,
        )
        raise
    return TaskSubmission(task_id, "queued", False)


async def _enqueue_account_media(
    x_user_id: str,
    lock_key: str,
    task_id: str,
) -> tuple[int, int]:
    if not settings.archive_media_enabled:
        return 0, 0
    queued = 0
    duplicates = 0
    for media_id in _repository().media_ids_to_download(x_user_id):
        await _extend_execution_lock(lock_key, task_id)
        submission = await enqueue_media_download(media_id, parent_task_id=task_id)
        if submission.duplicate:
            duplicates += 1
        else:
            queued += 1
    return queued, duplicates


@broker.task(
    task_name="archivex.sync_account",
    queue_name=settings.task_crawl_queue_name,
    retry_on_error=True,
    max_retries=settings.task_retry_count,
    delay=settings.task_retry_delay_seconds,
)
async def sync_account_task(
    x_user_id: str,
    trigger: str = "manual",
    context: Context = TaskiqDepends(),
) -> dict[str, Any]:
    task_id = context.message.task_id
    lock_key = _sync_lock_key(x_user_id)
    if not await _claim_execution(lock_key, task_id):
        return {"status": "skipped", "reason": "duplicate", "x_user_id": x_user_id}

    repository = _repository()
    source = TwscrapePostSource(settings.twscrape_session_path)
    service = ArchiveSyncService(
        repository,
        source,
        settings.archive_initial_post_limit,
        settings.archive_incremental_known_post_limit,
        media_downloader=None,
        media_enabled=settings.archive_media_enabled,
        media_max_bytes=settings.archive_media_max_bytes,
    )
    try:
        async with asyncio.timeout(settings.task_sync_timeout_seconds):
            result = await service.sync_account(x_user_id)
        await _extend_execution_lock(lock_key, task_id)
        media_queued, media_duplicates = await _enqueue_account_media(
            x_user_id,
            lock_key,
            task_id,
        )
        if result.status != "success":
            raise RuntimeError(result.error or "account synchronization failed")
    except BaseException:
        if _is_final_attempt(context):
            await _release_lock(lock_key, task_id)
        raise

    await _release_lock(lock_key, task_id)
    return {
        **result.__dict__,
        "trigger": trigger,
        "media_queued": media_queued,
        "media_duplicates": media_duplicates,
    }


@broker.task(
    task_name="archivex.download_media",
    queue_name=settings.task_media_queue_name,
    retry_on_error=True,
    max_retries=settings.task_retry_count,
    delay=settings.task_retry_delay_seconds,
)
async def download_media_task(
    media_id: str,
    context: Context = TaskiqDepends(),
) -> dict[str, Any]:
    task_id = context.message.task_id
    lock_key = _media_lock_key(media_id)
    if not await _claim_execution(lock_key, task_id):
        return {"status": "skipped", "reason": "duplicate", "media_id": media_id}

    repository = _repository()
    media = repository.get_media_record(media_id)
    if media is None:
        await _release_lock(lock_key, task_id)
        return {"status": "skipped", "reason": "not_found", "media_id": media_id}
    if media.download_status == "completed":
        await _release_lock(lock_key, task_id)
        return {"status": "skipped", "reason": "completed", "media_id": media_id}

    downloader = GalleryDlMediaDownloader(timeout_seconds=settings.task_media_timeout_seconds)
    try:
        result = await asyncio.to_thread(
            downloader.download,
            media.source_url,
            repository.post_directory(media.tweet_id),
            settings.archive_media_max_bytes,
        )
        repository.complete_media(media.id, result.local_path, result.sha256)
    except BaseException as exc:
        if isinstance(exc, asyncio.CancelledError):
            message = "media download cancelled"
        else:
            message = str(exc) or exc.__class__.__name__
        repository.fail_media(media.id, message)
        if _is_final_attempt(context):
            await _release_lock(lock_key, task_id)
        raise

    await _release_lock(lock_key, task_id)
    return {"status": "success", "media_id": media_id, "tweet_id": media.tweet_id}


@broker.task(
    task_name="archivex.schedule_enabled_accounts",
    queue_name=settings.task_crawl_queue_name,
    retry_on_error=True,
    max_retries=settings.task_retry_count,
    delay=settings.task_retry_delay_seconds,
    schedule=[{
        "interval": settings.archive_sync_interval_seconds,
        "schedule_id": "archivex-enabled-account-sync",
    }],
)
async def schedule_enabled_accounts_task(
    context: Context = TaskiqDepends(),
) -> dict[str, int]:
    task_id = context.message.task_id
    dispatch_decision = await _begin_schedule_dispatch(task_id)
    if dispatch_decision == "busy":
        raise RuntimeError("another scheduler dispatch is still in progress")
    if dispatch_decision == "not_due":
        logger.info("Skipping scheduled account synchronization before the next interval")
        return {"queued": 0, "duplicates": 0, "skipped": 1}

    queued = 0
    duplicates = 0
    try:
        for x_user_id in _repository().list_enabled_account_ids():
            await _renew_schedule_dispatch(task_id)
            submission = await enqueue_account_sync(x_user_id, "scheduled")
            if submission.duplicate:
                duplicates += 1
            else:
                queued += 1
    except BaseException:
        await _finish_schedule_dispatch(task_id, completed=False)
        raise
    await _finish_schedule_dispatch(task_id, completed=True)
    logger.info("Scheduled account synchronization: %d queued, %d duplicate", queued, duplicates)
    return {"queued": queued, "duplicates": duplicates, "skipped": 0}
