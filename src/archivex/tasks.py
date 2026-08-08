from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from typing import Any
from urllib.parse import urljoin

from redis.asyncio import Redis
from taskiq import Context, TaskiqDepends, TaskiqScheduler
from taskiq.message import TaskiqMessage
from taskiq.middlewares import SmartRetryMiddleware, TaskiqAdminMiddleware
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import ListRedisScheduleSource, RedisAsyncResultBackend, RedisStreamBroker

from archivex.config import Settings, get_settings
from archivex.media import GalleryDlMediaDownloader
from archivex.source import TwscrapePostSource
from archivex.storage import ArchiveRepository
from archivex.sync import ArchiveSyncService
from archivex.task_center import TaskCenterRepository
from archivex.task_dispatcher import TaskSubmission

logger = logging.getLogger(__name__)
settings = get_settings()

_DELETE_OWNED_LOCK = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class MountedTaskiqAdminMiddleware(TaskiqAdminMiddleware):
    """Report mounted-dashboard events in lifecycle order."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.url = self.url.rstrip("/") + "/"

    async def _spawn_request(self, endpoint: str, payload: dict[str, Any]) -> None:
        client = self._get_client()
        async with client.post(
            urljoin(self.url, endpoint.lstrip("/")),
            headers={"access-token": self.api_token},
            json=payload,
        ) as response:
            response.raise_for_status()

    async def pre_send(self, message: TaskiqMessage) -> TaskiqMessage:
        await super().post_send(message)
        return message

    async def post_send(self, message: TaskiqMessage) -> None:
        pass


def dashboard_api_token(app_settings: Settings = settings) -> str:
    value = f"archivex-task-dashboard:{app_settings.web_auth_token}"
    return hashlib.sha256(value.encode()).hexdigest()


result_backend = RedisAsyncResultBackend(
    redis_url=settings.task_redis_url,
    result_ex_time=settings.task_result_ttl_seconds,
    health_check_interval=30,
)
retry_schedule_source = ListRedisScheduleSource(
    settings.task_redis_url,
    prefix="archivex:retry-schedules",
)

_worker_timeout = (
    settings.task_media_timeout_seconds
    if settings.task_worker_queue_name == settings.task_media_queue_name
    else settings.task_sync_timeout_seconds
)
broker = RedisStreamBroker(
    url=settings.task_redis_url,
    queue_name=settings.task_worker_queue_name,
    consumer_group_name="archivex-workers",
    idle_timeout=(_worker_timeout + 60) * 1000,
    unacknowledged_lock_timeout=30,
    maxlen=100_000,
    health_check_interval=30,
).with_result_backend(result_backend).with_middlewares(
    SmartRetryMiddleware(
        default_retry_count=settings.task_retry_count,
        default_delay=settings.task_retry_delay_seconds,
        use_jitter=True,
        use_delay_exponent=True,
        max_delay_exponent=settings.task_retry_max_delay_seconds,
        schedule_source=retry_schedule_source,
    ),
    MountedTaskiqAdminMiddleware(
        url=settings.task_dashboard_url.rstrip("/"),
        api_token=dashboard_api_token(),
        taskiq_broker_name=settings.task_worker_queue_name,
    ),
)

scheduler = TaskiqScheduler(
    broker=broker,
    sources=[LabelScheduleSource(broker), retry_schedule_source],
)


def _repository() -> ArchiveRepository:
    return ArchiveRepository(settings.archive_db_path, settings.archive_data_dir)


def _task_center_repository() -> TaskCenterRepository:
    return TaskCenterRepository(
        settings.task_dashboard_db_path,
        settings.archive_sync_interval_seconds,
        settings.task_crawl_queue_name,
    )


def _redis() -> Redis:
    return Redis.from_url(settings.task_redis_url, decode_responses=True)


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
        existing = await client.get(key)
        if existing == task_id:
            await client.expire(key, settings.task_dedupe_ttl_seconds)
            return True
        if existing is not None:
            return False
        return bool(await client.set(
            key,
            task_id,
            ex=settings.task_dedupe_ttl_seconds,
            nx=True,
        ))
    finally:
        await client.aclose()


async def _release_lock(key: str, task_id: str) -> None:
    client = _redis()
    try:
        await client.eval(_DELETE_OWNED_LOCK, 1, key, task_id)
    finally:
        await client.aclose()


async def _rollback_failed_publish(key: str, task_id: str, exc: BaseException) -> None:
    await _release_lock(key, task_id)
    message = (
        "Task was not published to the broker: "
        f"{exc.__class__.__name__}: {str(exc) or 'unknown error'}"
    )
    if not _task_center_repository().abandon_queued_task(task_id, message):
        logger.warning("No queued dashboard row to abandon for task %s", task_id)


def _is_final_attempt(context: Context) -> bool:
    retries = int(context.message.labels.get("_retries", 0))
    max_retries = int(context.message.labels.get("max_retries", settings.task_retry_count))
    return retries + 1 >= max_retries


async def enqueue_account_sync(x_user_id: str, trigger: str = "manual") -> TaskSubmission:
    task_id = str(uuid.uuid4())
    lock_key = _sync_lock_key(x_user_id)
    reserved, owner_task_id = await _reserve_lock(lock_key, task_id)
    if not reserved:
        return TaskSubmission(owner_task_id, "queued", True)
    try:
        await (
            sync_account_task.kicker()
            .with_task_id(task_id)
            .kiq(x_user_id, trigger)
        )
    except Exception as exc:
        await _rollback_failed_publish(lock_key, task_id, exc)
        raise
    return TaskSubmission(task_id, "queued", False)


async def enqueue_media_download(media_id: str) -> TaskSubmission:
    task_id = str(uuid.uuid4())
    lock_key = _media_lock_key(media_id)
    reserved, owner_task_id = await _reserve_lock(lock_key, task_id)
    if not reserved:
        return TaskSubmission(owner_task_id, "queued", True)
    try:
        await (
            download_media_task.kicker()
            .with_task_id(task_id)
            .kiq(media_id)
        )
    except Exception as exc:
        await _rollback_failed_publish(lock_key, task_id, exc)
        raise
    return TaskSubmission(task_id, "queued", False)


async def _enqueue_account_media(x_user_id: str) -> tuple[int, int]:
    if not settings.archive_media_enabled:
        return 0, 0
    queued = 0
    duplicates = 0
    for media_id in _repository().media_ids_to_download(x_user_id):
        submission = await enqueue_media_download(media_id)
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

    source = TwscrapePostSource(settings.twscrape_session_path)
    service = ArchiveSyncService(
        _repository(),
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
        media_queued, media_duplicates = await _enqueue_account_media(x_user_id)
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
    schedule=[{"interval": settings.archive_sync_interval_seconds}],
)
async def schedule_enabled_accounts_task() -> dict[str, int]:
    queued = 0
    duplicates = 0
    for x_user_id in _repository().list_enabled_account_ids():
        submission = await enqueue_account_sync(x_user_id, "scheduled")
        if submission.duplicate:
            duplicates += 1
        else:
            queued += 1
    logger.info("Scheduled account synchronization: %d queued, %d duplicate", queued, duplicates)
    return {"queued": queued, "duplicates": duplicates}
