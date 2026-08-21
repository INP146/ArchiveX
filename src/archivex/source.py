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
from typing import Any, Mapping, Protocol

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows does not ship fcntl.
    fcntl = None  # type: ignore[assignment]

import twscrape.api as _twscrape_api
from twscrape import API
from twscrape.accounts_pool import NoAccountError
from twscrape.db import execute as execute_twscrape_query
from twscrape.queue_client import Ctx as _TwscrapeContext
from twscrape.queue_client import QueueClient as _TwscrapeQueueClient

from archivex.session import session_database_path

logger = logging.getLogger(__name__)


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


class _FatalTwscrapeResponse(BaseException):
    """Internal control flow used to escape twscrape's broad exception handler."""


def _response_has_unsupported_features(response: Any) -> bool:
    try:
        payload = response.json()
    except Exception:
        return False
    errors = payload.get("errors") if isinstance(payload, Mapping) else None
    if not isinstance(errors, list):
        return False
    for error in errors:
        if not isinstance(error, Mapping):
            continue
        try:
            code = int(error.get("code", -1))
        except (TypeError, ValueError):
            code = -1
        message = str(error.get("message", ""))
        if code == 336 or "features cannot be null" in message.lower():
            return True
    return False


class _SafeTwscrapeQueueClient(_TwscrapeQueueClient):
    """Prevent twscrape 0.19.2's feature error from calling ``exit(1)``."""

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

    async def _check_rep(self, response: Any) -> None:
        if _response_has_unsupported_features(response):
            # QueueClient normally exits the interpreter before its async
            # context manager can release the account lock. Close it here and
            # use a BaseException so the upstream broad ``except Exception``
            # cannot turn this into another 15-minute lock.
            try:
                await self._close_ctx()
            except Exception:
                logger.exception("Could not release twscrape account after feature error")
            raise _FatalTwscrapeResponse(
                "twscrape rejected the current GraphQL feature set (error 336)"
            )
        await super()._check_rep(response)


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


@dataclass(frozen=True)
class SourcePost:
    tweet_id: str
    x_user_id: str
    username: str
    post_type: str
    text: str
    posted_at: datetime
    permalink: str
    raw_payload: Mapping[str, Any]
    media: tuple["SourceMedia", ...] = ()


@dataclass(frozen=True)
class SourceMedia:
    media_type: str
    source_url: str


class PostSource(Protocol):
    async def resolve_account(self, username: str) -> SourceAccount | None: ...

    def fetch_timeline(self, x_user_id: str) -> AsyncIterator[SourcePost]: ...


class TwscrapePostSource:
    """Adapter around twscrape so archive logic is independent of its data model."""

    def __init__(
        self,
        session_path: Path,
        wait_timeout: float = 0.5,
        wait_interval: float = 0.25,
        recover_stale_locks: bool = False,
    ) -> None:
        database_path = session_database_path(session_path)
        _install_twscrape_safety_patch()
        self.database_path = database_path
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
        except _FatalTwscrapeResponse as exc:
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
        try:
            timeline = self.api.user_tweets_and_replies(int(x_user_id))
            async with aclosing(timeline):
                async for tweet in timeline:
                    # Conversation payloads can include another user's quoted/replied post.
                    if str(tweet.user.id) != x_user_id:
                        continue
                    raw_payload = tweet.dict()
                    yield SourcePost(
                        tweet_id=str(tweet.id),
                        x_user_id=str(tweet.user.id),
                        username=tweet.user.username,
                        post_type=_post_type(tweet),
                        text=tweet.rawContent,
                        posted_at=tweet.date,
                        permalink=tweet.url,
                        raw_payload=raw_payload,
                        media=media_from_payload(raw_payload),
                    )
        except NoAccountError as exc:
            retry_after = await _pool_retry_after(
                self.api.pool,
                "UserTweetsAndReplies",
            )
            raise _pool_unavailable_error("UserTweetsAndReplies", retry_after) from exc
        except _FatalTwscrapeResponse as exc:
            raise TwscrapeResponseError(str(exc)) from exc


def _post_type(tweet: Any) -> str:
    if tweet.retweetedTweet is not None:
        return "repost"
    if tweet.quotedTweet is not None or tweet.isQuoteStatus:
        return "quote"
    if tweet.inReplyToTweetId is not None:
        return "reply"
    return "original"


def media_from_payload(payload: Mapping[str, Any]) -> tuple[SourceMedia, ...]:
    """Extract downloadable media from a tweet and its embedded tweet payloads."""
    items: list[SourceMedia] = []
    seen_urls: set[str] = set()

    def append(media_type: str, source_url: object) -> None:
        if not isinstance(source_url, str) or not source_url or source_url in seen_urls:
            return
        seen_urls.add(source_url)
        items.append(SourceMedia(media_type, source_url))

    def extract(tweet: Mapping[str, Any]) -> None:
        media = tweet.get("media")
        if isinstance(media, Mapping):
            photos = media.get("photos")
            if isinstance(photos, list):
                for photo in photos:
                    if isinstance(photo, Mapping):
                        append("image", photo.get("url"))

            videos = media.get("videos")
            if isinstance(videos, list):
                for video in videos:
                    if not isinstance(video, Mapping):
                        continue
                    variants = video.get("variants")
                    if not isinstance(variants, list):
                        continue
                    downloadable = [
                        variant for variant in variants
                        if isinstance(variant, Mapping) and isinstance(variant.get("url"), str)
                    ]
                    if downloadable:
                        best = max(downloadable, key=lambda variant: variant.get("bitrate") or -1)
                        append("video", best.get("url"))

            animated_items = media.get("animated")
            if isinstance(animated_items, list):
                for animated in animated_items:
                    if isinstance(animated, Mapping):
                        append("gif", animated.get("videoUrl"))

        for field in ("retweetedTweet", "quotedTweet"):
            embedded = tweet.get(field)
            if isinstance(embedded, Mapping):
                extract(embedded)

    extract(payload)
    return tuple(items)
