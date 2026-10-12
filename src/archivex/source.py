from __future__ import annotations

import atexit
import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Iterable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Protocol, TYPE_CHECKING

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows does not ship fcntl.
    fcntl = None  # type: ignore[assignment]

import twscrape.api as _twscrape_api
from twscrape import API
from twscrape.accounts_pool import NoAccountError
from twscrape.db import execute as execute_twscrape_query
from twscrape.models import Tweet, parse_tweet, parse_tweets
from twscrape.queue_client import Ctx as _TwscrapeContext
from twscrape.queue_client import GqlFeaturesOutdatedError
from twscrape.queue_client import QueueClient as _TwscrapeQueueClient

from archivex.session import session_database_path
from archivex.post_model import (
    SourceMedia as SourceMedia,
    SourcePost,
    media_from_payload as media_from_payload,
    parse_post,
)

logger = logging.getLogger(__name__)
if TYPE_CHECKING:
    from archivex.storage import ArchiveRepository


class AccountPoolUnavailableError(RuntimeError):
    """The crawler has no account available until a known future time."""

    def __init__(self, queue: str, retry_after_seconds: float | None) -> None:
        self.queue = queue
        self.retry_after_seconds = retry_after_seconds
        if retry_after_seconds is None:
            detail = "no active account is available"
        else:
            detail = f"next account is available in {max(0.0, retry_after_seconds):.1f}s"
        super().__init__(f"No account available for queue {queue}; {detail}")


class TwscrapeResponseError(RuntimeError):
    """A recoverable twscrape response error that must not kill the worker."""


class PostUnavailableError(RuntimeError):
    """The focal tweet response explicitly reports unavailable content."""


class _SafeTwscrapeQueueClient(_TwscrapeQueueClient):
    """Track request leases and release account locks on interrupted cleanup."""

    _archivex_safe_queue_client = True

    async def _get_ctx(self):
        if self.ctx is not None:
            return self.ctx

        account = await self.pool.get_for_queue_or_wait(self.queue)
        if account is None:
            return None
        target = _session_context_opened(self.pool, account, self.queue)
        cleanup_failed = False
        try:
            resolved_proxy = account.resolve_proxy(self.proxy)
            client = account.make_client(proxy=self.proxy)
            context = _TwscrapeContext(account, client, proxy=resolved_proxy)
        except BaseException:
            cleanup_failed = True
            try:
                await self.pool.unlock(account.username, self.queue)
                cleanup_failed = False
            except BaseException:
                logger.warning(
                    "Could not release twscrape account after client construction failure",
                    exc_info=True,
                )
            if target is not None:
                _session_context_closed(
                    self.pool,
                    target,
                    recover_lock=cleanup_failed,
                )
            raise

        self.ctx = context
        if target is not None:
            self._archivex_session_lock_target = target
        return context

    async def _close_ctx(self, reset_at=-1, inactive=False, msg=None):
        context = getattr(self, "ctx", None)
        if context is None:
            return
        target = getattr(self, "_archivex_session_lock_target", None)
        cleanup_failed = False
        try:
            try:
                await super()._close_ctx(reset_at, inactive, msg)
            except BaseException:
                cleanup_failed = True
                # twscrape clears ``self.ctx`` before awaiting ``aclose``. If the
                # HTTP client's close fails, make one best-effort owner release so
                # the SQLite lock does not survive an otherwise recoverable error.
                if reset_at <= 0 and not inactive:
                    try:
                        await self.pool.unlock(
                            context.acc.username,
                            self.queue,
                            getattr(context, "req_count", 0),
                        )
                        cleanup_failed = False
                    except BaseException:
                        logger.warning(
                            "Could not release twscrape account after client close failure",
                            exc_info=True,
                        )
                raise
        finally:
            if target is not None:
                _session_context_closed(
                    self.pool,
                    target,
                    recover_lock=(cleanup_failed and reset_at <= 0 and not inactive),
                )
                self._archivex_session_lock_target = None

def _install_twscrape_safety_patch() -> None:
    if not getattr(_twscrape_api.QueueClient, "_archivex_safe_queue_client", False):
        _twscrape_api.QueueClient = _SafeTwscrapeQueueClient


# A process holds this lock for the lifetime of its crawl session. The marker
# lets a replacement worker distinguish an unclean child exit from a normal
# shutdown before deciding whether to clear twscrape's ownerless locks.


@dataclass(frozen=True)
class _SessionLockTarget:
    username: str
    queue: str
    locked_until: str


_session_leases: dict[Path, tuple[int, Any, tuple[_SessionLockTarget, ...]]] = {}
_session_recovery_consumed: set[Path] = set()
_session_recovery_locks: dict[Path, asyncio.Lock] = {}
_session_active_contexts: dict[Path, set[_SessionLockTarget]] = {}
_session_failed_contexts: dict[Path, set[_SessionLockTarget]] = {}
_session_leases_atexit_registered = False


def _drop_inherited_session_leases() -> None:
    """Close parent-owned lease descriptors immediately after a fork."""
    for _, handle, _ in tuple(_session_leases.values()):
        try:
            handle.close()
        except Exception:
            pass
    _session_leases.clear()
    _session_recovery_consumed.clear()
    _session_recovery_locks.clear()
    _session_active_contexts.clear()
    _session_failed_contexts.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_drop_inherited_session_leases)


def _write_session_lease_marker(
    handle: Any,
    active: Iterable[_SessionLockTarget],
) -> None:
    targets = sorted(active, key=lambda item: (item.username, item.queue, item.locked_until))
    handle.seek(0)
    handle.truncate()
    json.dump(
        {
            "pid": os.getpid(),
            "clean": not targets,
            "active": [
                {
                    "username": target.username,
                    "queue": target.queue,
                    "locked_until": target.locked_until,
                }
                for target in targets
            ],
        },
        handle,
    )
    handle.flush()


def _read_session_lock_targets(marker: Any) -> tuple[_SessionLockTarget, ...]:
    if not isinstance(marker, Mapping) or marker.get("clean") is True:
        return ()
    raw_targets = marker.get("active")
    if not isinstance(raw_targets, list):
        return ()
    targets: list[_SessionLockTarget] = []
    for raw_target in raw_targets:
        if not isinstance(raw_target, Mapping):
            continue
        username = raw_target.get("username")
        queue = raw_target.get("queue")
        locked_until = raw_target.get("locked_until")
        if all(isinstance(value, str) and value for value in (username, queue, locked_until)):
            targets.append(_SessionLockTarget(username, queue, locked_until))
    return tuple(targets)


def _mark_session_leases_clean() -> None:
    for database_path, (pid, handle, _) in tuple(_session_leases.items()):
        if pid == os.getpid():
            try:
                active_contexts = _session_active_contexts.get(database_path, set())
                marker_targets = _session_marker_targets(database_path, active_contexts)
                _write_session_lease_marker(handle, marker_targets)
                if marker_targets:
                    logger.warning(
                        "Leaving twscrape session lease unclean because %d "
                        "request context(s) were still active",
                        len(marker_targets),
                    )
            except Exception:
                logger.debug("Could not mark twscrape session lease clean", exc_info=True)


def _pool_database_path(pool: Any) -> Path | None:
    value = getattr(pool, "_db_file", None)
    if value is None:
        return None
    try:
        return Path(os.fspath(value))
    except (TypeError, ValueError):
        return None


def _session_marker_targets(
    database_path: Path,
    active: Iterable[_SessionLockTarget],
) -> set[_SessionLockTarget]:
    targets = set(active)
    lease = _session_leases.get(database_path)
    if lease is not None and database_path not in _session_recovery_consumed:
        targets.update(lease[2])
    return targets


def _session_lock_target(account: Any, queue: str) -> _SessionLockTarget | None:
    username = getattr(account, "username", None)
    lock_until = getattr(account, "locks", {}).get(queue)
    if not isinstance(username, str) or not username or lock_until is None:
        return None
    if isinstance(lock_until, datetime):
        if lock_until.tzinfo is not None:
            lock_until = lock_until.astimezone(UTC)
        lock_value = lock_until.strftime("%Y-%m-%d %H:%M:%S")
    else:
        lock_value = str(lock_until)
    return _SessionLockTarget(username, queue, lock_value)


def _session_context_opened(
    pool: Any,
    account: Any,
    queue: str,
) -> _SessionLockTarget | None:
    database_path = _pool_database_path(pool)
    if database_path is None:
        return None
    lease = _session_leases.get(database_path)
    if lease is None or lease[0] != os.getpid():
        return None
    target = _session_lock_target(account, queue)
    if target is None:
        logger.warning("Could not identify the twscrape lock owned by the active request")
        return None
    active = _session_active_contexts.setdefault(database_path, set())
    active.add(target)
    try:
        _write_session_lease_marker(
            lease[1],
            _session_marker_targets(database_path, active),
        )
    except Exception:
        logger.warning("Could not persist active twscrape request lease", exc_info=True)
    return target


def _session_context_closed(
    pool: Any,
    target: _SessionLockTarget,
    *,
    recover_lock: bool = False,
) -> None:
    database_path = _pool_database_path(pool)
    if database_path is None:
        return
    lease = _session_leases.get(database_path)
    if lease is None or lease[0] != os.getpid():
        return
    active = _session_active_contexts.get(database_path, set())
    if recover_lock:
        _session_failed_contexts.setdefault(database_path, set()).add(target)
        _session_recovery_consumed.discard(database_path)
    else:
        active.discard(target)
        failed = _session_failed_contexts.get(database_path)
        if failed is not None:
            failed.discard(target)
            if not failed:
                _session_failed_contexts.pop(database_path, None)
    if not active:
        _session_active_contexts.pop(database_path, None)
    try:
        _write_session_lease_marker(
            lease[1],
            _session_marker_targets(database_path, active),
        )
    except Exception:
        logger.warning("Could not persist closed twscrape request lease", exc_info=True)


def _release_session_leases() -> None:
    for database_path, (pid, handle, _) in tuple(_session_leases.items()):
        if pid != os.getpid():
            continue
        try:
            handle.close()
        except Exception:
            logger.debug("Could not close twscrape session lease", exc_info=True)
        finally:
            _session_leases.pop(database_path, None)
            _session_recovery_consumed.discard(database_path)
            _session_recovery_locks.pop(database_path, None)
            _session_active_contexts.pop(database_path, None)
            _session_failed_contexts.pop(database_path, None)


def _acquire_session_lease(database_path: Path) -> tuple[_SessionLockTarget, ...]:
    """Acquire the per-session crawler lease and report whether recovery is needed."""
    global _session_leases_atexit_registered

    existing = _session_leases.get(database_path)
    if existing is not None:
        if existing[0] == os.getpid():
            if database_path in _session_recovery_consumed:
                return ()
            return existing[2]
        # A fork inherited the parent's descriptor. Do not treat it as a valid
        # lease for the child; closing this copy lets the child attempt a fresh
        # flock while the parent still retains its own lock.
        try:
            existing[1].close()
        finally:
            _session_leases.pop(database_path, None)
            _session_recovery_consumed.discard(database_path)
            _session_recovery_locks.pop(database_path, None)
            _session_active_contexts.pop(database_path, None)
            _session_failed_contexts.pop(database_path, None)
            _session_leases_atexit_registered = False

    if fcntl is None:
        return ()

    lock_path = database_path.with_name(f".{database_path.name}.archivex-crawl.lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+")
    except OSError:
        logger.warning("Could not create twscrape session lease %s", lock_path, exc_info=True)
        return ()
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.seek(0)
        previous = handle.read().strip()
    except BlockingIOError:
        handle.close()
        return ()
    except Exception:
        handle.close()
        raise

    try:
        previous_marker = json.loads(previous) if previous else {}
    except (TypeError, ValueError):
        previous_marker = {}
    recovery_targets = _read_session_lock_targets(previous_marker)
    _write_session_lease_marker(handle, recovery_targets)
    _session_leases[database_path] = (os.getpid(), handle, recovery_targets)
    if not _session_leases_atexit_registered:
        # atexit uses LIFO ordering: mark first while the descriptor is still
        # locked, then close it so a replacement worker can acquire recovery.
        atexit.register(_release_session_leases)
        atexit.register(_mark_session_leases_clean)
        _session_leases_atexit_registered = True
    return recovery_targets


async def _pool_retry_after(pool: Any, queue: str) -> float | None:
    """Return the earliest active account lock delay without waiting on it."""
    try:
        accounts = await pool.get_all()
    except Exception:
        logger.warning("Could not inspect twscrape account locks", exc_info=True)
        return None

    now = datetime.now(UTC)
    delays: list[float] = []
    for account in accounts:
        if not getattr(account, "active", False):
            continue
        lock_until = getattr(account, "locks", {}).get(queue)
        if lock_until is None:
            return 0.0
        if lock_until.tzinfo is None:
            lock_until = lock_until.replace(tzinfo=UTC)
        delay = (lock_until - now).total_seconds()
        if delay <= 0:
            return 0.0
        delays.append(delay)
    return min(delays) if delays else None


async def _reset_crawl_queue_locks(
    database_path: Path,
    targets: Iterable[_SessionLockTarget],
) -> None:
    """Clear only locks whose owner and original lease still match the crash marker."""
    for target in targets:
        if target.queue != "UserTweetsAndReplies":
            continue
        await execute_twscrape_query(
            str(database_path),
            """
            UPDATE accounts
            SET locks = json_remove(locks, '$."UserTweetsAndReplies"')
            WHERE username = :username
              AND json_extract(locks, '$."UserTweetsAndReplies"') = :locked_until
            """,
            {
                "username": target.username,
                "locked_until": target.locked_until,
            },
        )


def _pool_unavailable_error(
    queue: str,
    retry_after_seconds: float | None,
) -> AccountPoolUnavailableError:
    return AccountPoolUnavailableError(queue, retry_after_seconds)


@dataclass(frozen=True)
class SourceAccount:
    x_user_id: str
    username: str
    display_name: str | None
    profile_image_url: str | None = None
    description: str | None = None


class PostSource(Protocol):
    async def resolve_account(self, username: str) -> SourceAccount | None: ...

    def fetch_timeline(self, x_user_id: str) -> AsyncIterator[SourcePost]: ...

    async def fetch_post(self, tweet_id: str) -> SourcePost: ...


class TwscrapePostSource:
    """Adapter around twscrape so archive logic is independent of its data model."""

    def __init__(
        self,
        session_path: Path,
        wait_timeout: float = 0.5,
        wait_interval: float = 0.25,
        recover_stale_locks: bool = False,
        repository: ArchiveRepository | None = None,
    ) -> None:
        database_path = session_database_path(session_path)
        _install_twscrape_safety_patch()
        self.database_path = database_path
        self.repository = repository
        self._recover_stale_locks = recover_stale_locks
        if recover_stale_locks:
            lease_targets = _acquire_session_lease(database_path)
            failed_targets = _session_failed_contexts.get(database_path, set())
            self._recovery_targets = tuple(dict.fromkeys((*lease_targets, *failed_targets)))
        else:
            self._recovery_targets = ()
        self._recovery_checked = False
        self.api = API(
            str(database_path),
            raise_when_no_account=True,
            wait_timeout=wait_timeout,
            wait_interval=wait_interval,
        )

    async def _ensure_ready(self) -> None:
        if getattr(self, "_recovery_checked", False):
            return
        self._recovery_checked = True
        recovery_targets = getattr(self, "_recovery_targets", ())
        if not recovery_targets:
            return
        recovery_lock = _session_recovery_locks.setdefault(
            self.database_path,
            asyncio.Lock(),
        )
        try:
            async with recovery_lock:
                # Multiple task sources can be constructed before the first
                # request reaches this point. The lock and second check make
                # recovery a single operation per worker, not per task object.
                if self.database_path in _session_recovery_consumed:
                    return
                await _reset_crawl_queue_locks(self.database_path, recovery_targets)
                active = _session_active_contexts.get(self.database_path, set())
                active.difference_update(recovery_targets)
                if not active:
                    _session_active_contexts.pop(self.database_path, None)
                failed = _session_failed_contexts.get(self.database_path)
                if failed is not None:
                    failed.difference_update(recovery_targets)
                    if not failed:
                        _session_failed_contexts.pop(self.database_path, None)
                _session_recovery_consumed.add(self.database_path)
                lease = _session_leases.get(self.database_path)
                if lease is not None and lease[0] == os.getpid():
                    _write_session_lease_marker(
                        lease[1],
                        _session_marker_targets(
                            self.database_path,
                            _session_active_contexts.get(self.database_path, set()),
                        ),
                    )
                logger.warning(
                    "Cleared twscrape account locks left by an unclean crawler process exit"
                )
        except Exception as exc:
            self._recovery_checked = False
            raise RuntimeError("could not recover twscrape locks after crawler restart") from exc

    async def resolve_account(self, username: str) -> SourceAccount | None:
        await self._ensure_ready()
        try:
            user = await self.api.user_by_login(username)
        except NoAccountError as exc:
            retry_after = await _pool_retry_after(self.api.pool, "UserByScreenName")
            raise _pool_unavailable_error("UserByScreenName", retry_after) from exc
        except GqlFeaturesOutdatedError as exc:
            raise TwscrapeResponseError(str(exc)) from exc
        if user is None:
            return None
        return SourceAccount(
            x_user_id=str(user.id),
            username=user.username,
            display_name=user.displayname or None,
            profile_image_url=user.profileImageUrl or None,
            description=user.rawDescription or None,
        )

    async def fetch_timeline(self, x_user_id: str) -> AsyncIterator[SourcePost]:
        await self._ensure_ready()
        seen: set[str] = set()
        detail_deferred = False
        try:
            timeline = self.api.user_tweets_and_replies_raw(int(x_user_id))
            async with aclosing(timeline):
                async for response in timeline:
                    payload = response.json()
                    tweets = {str(tweet.id): tweet for tweet in parse_tweets(payload)}
                    recovered: dict[str, SourcePost] = {}
                    repository = getattr(self, "repository", None)
                    missing = list(dict.fromkeys(
                        identifier for identifier in _conversation_tweet_ids(payload)
                        if identifier not in tweets and identifier not in seen
                    ))
                    # Persist all discovered gaps before any detail request. A
                    # crash, rate limit or early stop must not lose later IDs.
                    if repository is not None:
                        for tweet_id in missing:
                            cached = repository.cached_post_snapshot(tweet_id)
                            if cached is not None:
                                recovered[tweet_id] = cached
                                if cached.x_user_id == x_user_id and not repository.has_post_observation(x_user_id, tweet_id):
                                    repository.register_post_fetch(x_user_id, tweet_id)
                            else:
                                repository.register_post_fetch(x_user_id, tweet_id)
                    for tweet_id in missing:
                        if tweet_id in recovered:
                            continue
                        if detail_deferred:
                            continue
                        if repository is not None:
                            pending = repository.post_fetch(x_user_id, tweet_id)
                            if pending and (pending["status"] in {"unavailable", "ignored"} or pending.get("retry_exhausted")):
                                continue
                            if pending and pending["last_attempt_at"] is not None:
                                # A previously attempted gap belongs to its
                                # independent recovery task, even on later pages.
                                continue
                            repository.attempt_post_fetch(x_user_id, tweet_id)
                        try:
                            recovered[tweet_id] = await self.fetch_post(tweet_id)
                        except AccountPoolUnavailableError as exc:
                            if repository is None:
                                raise
                            repository.finish_post_fetch(x_user_id, tweet_id, "pending", str(exc))
                            # Endpoint locks are independent. The timeline can
                            # still return content while TweetDetail is locked.
                            detail_deferred = True
                            continue
                        except GqlFeaturesOutdatedError as exc:
                            if repository is not None:
                                repository.finish_post_fetch(x_user_id, tweet_id, "pending", str(exc))
                            raise
                        except Exception as exc:
                            if repository is None:
                                raise
                            status = "unavailable" if isinstance(exc, PostUnavailableError) else "pending"
                            repository.finish_post_fetch(x_user_id, tweet_id, status, str(exc) or type(exc).__name__)
                            logger.warning("Conversation tweet %s recorded as %s", tweet_id, status)
                            continue

                    # Complete collapsed conversations before yielding the visible
                    # items: the sync service can stop on consecutive known posts.
                    recovered_posts = list(recovered.values())
                    if repository is not None:
                        recovered_posts.sort(key=lambda p: repository.has_post_observation(x_user_id, p.tweet_id))
                    for post in (*recovered_posts, *(_source_post_from_tweet(t) for t in tweets.values())):
                        tweet_id = post.tweet_id
                        if tweet_id in seen:
                            continue
                        seen.add(tweet_id)
                        # Conversation payloads can include other authors.
                        if post.x_user_id == x_user_id:
                            yield post
                        elif repository is not None:
                            repository.finish_post_fetch(x_user_id, tweet_id, "ignored")
        except NoAccountError as exc:
            retry_after = await _pool_retry_after(
                self.api.pool,
                "UserTweetsAndReplies",
            )
            raise _pool_unavailable_error("UserTweetsAndReplies", retry_after) from exc
        except GqlFeaturesOutdatedError as exc:
            raise TwscrapeResponseError(str(exc)) from exc

    async def fetch_post(self, tweet_id: str) -> SourcePost:
        await self._ensure_ready()
        repository = getattr(self, "repository", None)
        if repository is not None:
            cached = repository.cached_post_snapshot(tweet_id)
            if cached is not None:
                return cached
        try:
            response = await self.api.tweet_details_raw(int(tweet_id))
        except NoAccountError as exc:
            retry_after = await _pool_retry_after(self.api.pool, "TweetDetail")
            raise _pool_unavailable_error("TweetDetail", retry_after) from exc
        payload = response.json() if response is not None else None
        tweet = parse_tweet(payload, int(tweet_id)) if isinstance(payload, dict) else None
        if tweet is None:
            if _focal_tweet_unavailable(payload, tweet_id):
                raise PostUnavailableError(f"Tweet {tweet_id} is explicitly unavailable")
            # None can also mean an incomplete response or parsing failure;
            # it is insufficient evidence that a timeline-listed tweet was deleted.
            raise TwscrapeResponseError(
                f"Could not read conversation tweet {tweet_id} from TweetDetail"
            )
        if str(tweet.id) != tweet_id:
            raise TwscrapeResponseError(f"TweetDetail returned a different tweet for {tweet_id}")
        return _source_post_from_tweet(tweet)


def _focal_tweet_unavailable(payload: Any, tweet_id: str) -> bool:
    if not isinstance(payload, dict):
        return False

    def contains_focal_tweet(value: Any) -> bool:
        if isinstance(value, dict):
            if value.get("__typename") == "Tweet" and value.get("rest_id") == tweet_id:
                return True
            return any(contains_focal_tweet(child) for child in value.values())
        if isinstance(value, list):
            return any(contains_focal_tweet(child) for child in value)
        return False

    # Aggregated conversation errors may concern another reply. A present
    # focal Tweet that failed parsing must stay retryable.
    if contains_focal_tweet(payload):
        return False
    if any(isinstance(error, dict) and (error.get("code") == 144 or
           "_Missing: No status found with that ID" in str(error.get("message", "")))
           for error in payload.get("errors", []) or []):
        return True

    def walk(value: Any, focal: bool = False) -> bool:
        if isinstance(value, dict):
            focal = focal or value.get("entryId") == f"tweet-{tweet_id}" or value.get("rest_id") == tweet_id
            if focal and value.get("__typename") in {"TweetTombstone", "TweetUnavailable"}:
                return True
            return any(walk(child, focal) for child in value.values())
        if isinstance(value, list):
            return any(walk(child, focal) for child in value)
        return False

    if walk(payload):
        return True
    # X also represents an unavailable focal tweet with an empty result in a
    # complete, terminated conversation, rather than a tombstone or error 144.
    data = payload.get("data")
    timeline = data.get("threaded_conversation_with_injections_v2") if isinstance(data, dict) else None
    if payload.get("errors") or not isinstance(timeline, dict):
        return False
    instructions = timeline.get("instructions")
    if not isinstance(instructions, list):
        return False
    terminated = {item.get("direction") for item in instructions
                  if isinstance(item, dict) and item.get("type") == "TimelineTerminateTimeline"}
    if not {"Top", "Bottom"}.issubset(terminated):
        return False
    for instruction in instructions:
        if not isinstance(instruction, dict):
            continue
        for entry in instruction.get("entries", []) or []:
            if not isinstance(entry, dict) or entry.get("entryId") != f"tweet-{tweet_id}":
                continue
            content = (entry.get("content") or {}).get("itemContent")
            if not isinstance(content, dict) or content.get("itemType") != "TimelineTweet":
                continue
            results = content.get("tweet_results")
            if results == {} or (isinstance(results, dict) and "result" in results and results["result"] is None):
                return True
    return False


def _conversation_tweet_ids(payload: Any) -> Iterable[str]:
    """Read collapsed conversation IDs, not arbitrary references or cursors."""
    if isinstance(payload, Mapping):
        metadata = payload.get("conversationMetadata")
        if isinstance(metadata, Mapping):
            identifiers = metadata.get("allTweetIds")
            if isinstance(identifiers, list):
                for identifier in identifiers:
                    if (
                        isinstance(identifier, str)
                        and len(identifier) <= 32
                        and identifier.isascii()
                        and identifier.isdecimal()
                        and int(identifier) > 0
                    ):
                        yield identifier
        for value in payload.values():
            yield from _conversation_tweet_ids(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _conversation_tweet_ids(value)


def _source_post_from_tweet(tweet: Tweet) -> SourcePost:
    raw_payload = tweet.dict()
    raw_payload.setdefault("id", str(tweet.id))
    raw_payload.setdefault("user", {
        "id": str(tweet.user.id),
        "username": getattr(tweet.user, "username", None),
    })
    return parse_post(
        raw_payload,
        tweet_id=str(tweet.id),
        x_user_id=str(tweet.user.id),
        username=getattr(tweet.user, "username", None),
        text=getattr(tweet, "rawContent", ""),
        posted_at=tweet.date,
        permalink=tweet.url,
    )
