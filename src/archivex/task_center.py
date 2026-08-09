from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


TASK_STATUSES = (
    "queued",
    "in_progress",
    "retry_scheduled",
    "completed",
    "failure",
    "abandoned",
)
TASK_STATUS_VALUES = {status: status for status in TASK_STATUSES}
TERMINAL_TASK_STATUSES = ("completed", "failure", "abandoned")
TASK_ACCOUNT_ID_LABEL = "_archivex_account_x_user_id"
TASK_MEDIA_ID_LABEL = "_archivex_media_id"
TASK_PARENT_ID_LABEL = "_archivex_parent_task_id"
TASK_TRIGGER_LABEL = "_archivex_trigger"
TASK_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue_tasks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    worker TEXT NOT NULL,
    account_x_user_id TEXT REFERENCES accounts(x_user_id) ON DELETE SET NULL,
    media_id TEXT REFERENCES media(id) ON DELETE SET NULL,
    parent_task_id TEXT REFERENCES queue_tasks(id) ON DELETE SET NULL,
    trigger TEXT,
    context TEXT NOT NULL,
    args TEXT NOT NULL,
    kwargs TEXT NOT NULL,
    labels TEXT NOT NULL,
    result TEXT,
    error TEXT,
    queued_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    next_retry_at TEXT,
    current_attempt INTEGER NOT NULL DEFAULT 1,
    max_attempts INTEGER NOT NULL DEFAULT 1,
    retry_of TEXT REFERENCES queue_tasks(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queue_attempts (
    task_id TEXT NOT NULL REFERENCES queue_tasks(id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    labels TEXT NOT NULL,
    result TEXT,
    error TEXT,
    queued_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    next_retry_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (task_id, attempt)
);
CREATE INDEX IF NOT EXISTS idx_queue_tasks_status_updated
    ON queue_tasks(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_queue_tasks_target
    ON queue_tasks(name, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_queue_tasks_account
    ON queue_tasks(account_x_user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_queue_tasks_media
    ON queue_tasks(media_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_queue_tasks_parent
    ON queue_tasks(parent_task_id, updated_at DESC);
"""


@dataclass(frozen=True)
class _TaskMetadata:
    account_x_user_id: str | None
    media_id: str | None
    parent_task_id: str | None
    trigger: str | None
    context: str
    retry_of: str | None


class TaskCenterRepository:
    """Own the observable lifecycle for logical tasks and their attempts."""

    def __init__(
        self,
        database_path: Path,
        sync_interval_seconds: int,
        crawl_queue_name: str,
    ) -> None:
        self.database_path = database_path
        self.sync_interval_seconds = sync_interval_seconds
        self.crawl_queue_name = crawl_queue_name
        self._initialize()

    def record_queued(
        self,
        task_id: str,
        name: str,
        worker: str,
        args: list[Any],
        kwargs: dict[str, Any],
        labels: dict[str, Any],
        queued_at: str | None = None,
    ) -> None:
        normalized_id = self._required_task_id(task_id)
        timestamp = _sqlite_timestamp(queued_at) if queued_at else _now()
        attempt = _attempt_number(labels)
        max_attempts = _max_attempts(labels)
        serialized = _serialize_message(args, kwargs, labels)

        with self._write() as connection:
            metadata = self._task_metadata(connection, name, labels)
            self._ensure_task(
                connection,
                normalized_id,
                name,
                worker,
                serialized,
                timestamp,
                attempt,
                max_attempts,
                metadata,
            )
            connection.execute(
                """INSERT INTO queue_attempts (
                    task_id, attempt, status, labels, queued_at, updated_at
                ) VALUES (?, ?, 'queued', ?, ?, ?)
                ON CONFLICT(task_id, attempt) DO UPDATE SET
                    labels = excluded.labels,
                    queued_at = COALESCE(queue_attempts.queued_at, excluded.queued_at),
                    updated_at = MAX(queue_attempts.updated_at, excluded.updated_at)""",
                (normalized_id, attempt, serialized[2], timestamp, timestamp),
            )
            connection.execute(
                """UPDATE queue_tasks SET
                    name = ?, worker = ?, args = ?, kwargs = ?, labels = ?,
                    queued_at = CASE
                        WHEN current_attempt < ? THEN ?
                        ELSE COALESCE(queued_at, ?)
                    END,
                    started_at = CASE WHEN current_attempt < ? THEN NULL ELSE started_at END,
                    finished_at = CASE WHEN current_attempt < ? THEN NULL ELSE finished_at END,
                    result = CASE WHEN current_attempt < ? THEN NULL ELSE result END,
                    error = CASE WHEN current_attempt < ? THEN NULL ELSE error END,
                    next_retry_at = CASE WHEN current_attempt <= ? THEN NULL ELSE next_retry_at END,
                    status = CASE
                        WHEN current_attempt < ? THEN 'queued'
                        WHEN current_attempt = ? AND status IN ('queued', 'retry_scheduled', 'abandoned')
                            THEN 'queued'
                        ELSE status
                    END,
                    current_attempt = MAX(current_attempt, ?),
                    max_attempts = MAX(max_attempts, ?),
                    retry_of = COALESCE(retry_of, ?),
                    updated_at = MAX(updated_at, ?)
                WHERE id = ?""",
                (
                    name,
                    worker,
                    *serialized,
                    attempt,
                    timestamp,
                    timestamp,
                    attempt,
                    attempt,
                    attempt,
                    attempt,
                    attempt,
                    attempt,
                    attempt,
                    attempt,
                    max_attempts,
                    metadata.retry_of,
                    timestamp,
                    normalized_id,
                ),
            )

    def record_started(
        self,
        task_id: str,
        name: str,
        worker: str,
        args: list[Any],
        kwargs: dict[str, Any],
        labels: dict[str, Any],
        started_at: str | None = None,
    ) -> None:
        normalized_id = self._required_task_id(task_id)
        timestamp = _sqlite_timestamp(started_at) if started_at else _now()
        attempt = _attempt_number(labels)
        max_attempts = _max_attempts(labels)
        serialized = _serialize_message(args, kwargs, labels)

        with self._write() as connection:
            metadata = self._task_metadata(connection, name, labels)
            self._ensure_task(
                connection,
                normalized_id,
                name,
                worker,
                serialized,
                timestamp,
                attempt,
                max_attempts,
                metadata,
            )
            connection.execute(
                """INSERT INTO queue_attempts (
                    task_id, attempt, status, labels, started_at, updated_at
                ) VALUES (?, ?, 'in_progress', ?, ?, ?)
                ON CONFLICT(task_id, attempt) DO UPDATE SET
                    status = CASE
                        WHEN queue_attempts.status IN ('completed', 'failure')
                            THEN queue_attempts.status
                        ELSE 'in_progress'
                    END,
                    labels = excluded.labels,
                    started_at = COALESCE(queue_attempts.started_at, excluded.started_at),
                    updated_at = MAX(queue_attempts.updated_at, excluded.updated_at)""",
                (normalized_id, attempt, serialized[2], timestamp, timestamp),
            )
            connection.execute(
                """UPDATE queue_tasks SET
                    name = ?, worker = ?, args = ?, kwargs = ?, labels = ?,
                    status = CASE
                        WHEN current_attempt < ? THEN 'in_progress'
                        WHEN current_attempt = ? AND status NOT IN ('completed', 'failure')
                            THEN 'in_progress'
                        ELSE status
                    END,
                    queued_at = CASE WHEN current_attempt < ? THEN NULL ELSE queued_at END,
                    started_at = CASE
                        WHEN current_attempt < ? THEN ?
                        ELSE COALESCE(started_at, ?)
                    END,
                    finished_at = CASE WHEN current_attempt < ? THEN NULL ELSE finished_at END,
                    result = CASE WHEN current_attempt < ? THEN NULL ELSE result END,
                    error = CASE WHEN current_attempt < ? THEN NULL ELSE error END,
                    next_retry_at = CASE WHEN current_attempt <= ? THEN NULL ELSE next_retry_at END,
                    current_attempt = MAX(current_attempt, ?),
                    max_attempts = MAX(max_attempts, ?),
                    retry_of = COALESCE(retry_of, ?),
                    updated_at = MAX(updated_at, ?)
                WHERE id = ?""",
                (
                    name,
                    worker,
                    *serialized,
                    attempt,
                    attempt,
                    attempt,
                    attempt,
                    timestamp,
                    timestamp,
                    attempt,
                    attempt,
                    attempt,
                    attempt,
                    attempt,
                    max_attempts,
                    metadata.retry_of,
                    timestamp,
                    normalized_id,
                ),
            )

    def record_finished(
        self,
        task_id: str,
        labels: dict[str, Any],
        *,
        result: Any,
        error: str | None,
        finished_at: str | None = None,
        name: str = "unknown",
        worker: str = "unknown",
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> None:
        normalized_id = self._required_task_id(task_id)
        timestamp = _sqlite_timestamp(finished_at) if finished_at else _now()
        attempt = _attempt_number(labels)
        status = "failure" if error is not None else "completed"
        result_json = _json_dump(result) if error is None else None
        serialized = _serialize_message(args or [], kwargs or {}, labels)

        with self._write() as connection:
            metadata = self._task_metadata(connection, name, labels)
            self._ensure_task(
                connection,
                normalized_id,
                name,
                worker,
                serialized,
                timestamp,
                attempt,
                _max_attempts(labels),
                metadata,
            )
            connection.execute(
                """INSERT INTO queue_attempts (
                    task_id, attempt, status, labels, result, error, finished_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, attempt) DO UPDATE SET
                    status = excluded.status,
                    labels = excluded.labels,
                    result = excluded.result,
                    error = excluded.error,
                    finished_at = excluded.finished_at,
                    next_retry_at = NULL,
                    updated_at = MAX(queue_attempts.updated_at, excluded.updated_at)""",
                (
                    normalized_id,
                    attempt,
                    status,
                    _json_dump(labels),
                    result_json,
                    error,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """UPDATE queue_tasks SET
                    name = ?, worker = ?, args = ?, kwargs = ?, labels = ?,
                    status = ?, result = ?, error = ?,
                    queued_at = CASE WHEN current_attempt < ? THEN NULL ELSE queued_at END,
                    started_at = CASE WHEN current_attempt < ? THEN NULL ELSE started_at END,
                    finished_at = ?, next_retry_at = NULL,
                    current_attempt = MAX(current_attempt, ?),
                    max_attempts = MAX(max_attempts, ?),
                    updated_at = MAX(updated_at, ?)
                WHERE id = ? AND current_attempt <= ?""",
                (
                    name,
                    worker,
                    *serialized,
                    status,
                    result_json,
                    error,
                    attempt,
                    attempt,
                    timestamp,
                    attempt,
                    _max_attempts(labels),
                    timestamp,
                    normalized_id,
                    attempt,
                ),
            )

    def record_retry_scheduled(
        self,
        task_id: str,
        labels: dict[str, Any],
        error: str,
        next_retry_at: str,
        finished_at: str | None = None,
        name: str = "unknown",
        worker: str = "unknown",
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> None:
        normalized_id = self._required_task_id(task_id)
        timestamp = _sqlite_timestamp(finished_at) if finished_at else _now()
        retry_at = _sqlite_timestamp(next_retry_at)
        attempt = _attempt_number(labels)
        serialized = _serialize_message(args or [], kwargs or {}, labels)

        with self._write() as connection:
            metadata = self._task_metadata(connection, name, labels)
            self._ensure_task(
                connection,
                normalized_id,
                name,
                worker,
                serialized,
                timestamp,
                attempt,
                _max_attempts(labels),
                metadata,
            )
            connection.execute(
                """INSERT OR IGNORE INTO queue_attempts (
                    task_id, attempt, status, labels, updated_at
                ) VALUES (?, ?, 'in_progress', ?, ?)""",
                (normalized_id, attempt, _json_dump(labels), timestamp),
            )
            connection.execute(
                """UPDATE queue_attempts SET
                    status = 'failure', error = ?, finished_at = ?, next_retry_at = ?,
                    updated_at = MAX(updated_at, ?)
                WHERE task_id = ? AND attempt = ?""",
                (error, timestamp, retry_at, timestamp, normalized_id, attempt),
            )
            connection.execute(
                """UPDATE queue_tasks SET
                    name = ?, worker = ?, args = ?, kwargs = ?, labels = ?,
                    status = 'retry_scheduled', result = NULL, error = ?,
                    queued_at = CASE WHEN current_attempt < ? THEN NULL ELSE queued_at END,
                    started_at = CASE WHEN current_attempt < ? THEN NULL ELSE started_at END,
                    finished_at = ?, next_retry_at = ?,
                    current_attempt = MAX(current_attempt, ?),
                    max_attempts = MAX(max_attempts, ?),
                    updated_at = MAX(updated_at, ?)
                WHERE id = ? AND current_attempt <= ?""",
                (
                    name,
                    worker,
                    *serialized,
                    error,
                    attempt,
                    attempt,
                    timestamp,
                    retry_at,
                    attempt,
                    _max_attempts(labels),
                    timestamp,
                    normalized_id,
                    attempt,
                ),
            )

    def record_publish_failed(
        self,
        task_id: str,
        name: str,
        worker: str,
        args: list[Any],
        kwargs: dict[str, Any],
        labels: dict[str, Any],
        error: str,
    ) -> None:
        self.record_queued(task_id, name, worker, args, kwargs, labels)
        normalized_id = self._required_task_id(task_id)
        timestamp = _now()
        attempt = _attempt_number(labels)
        with self._write() as connection:
            connection.execute(
                """UPDATE queue_attempts SET status = 'abandoned', error = ?,
                    finished_at = ?, updated_at = ? WHERE task_id = ? AND attempt = ?""",
                (error, timestamp, timestamp, normalized_id, attempt),
            )
            connection.execute(
                """UPDATE queue_tasks SET status = 'abandoned', error = ?,
                    finished_at = ?, updated_at = ? WHERE id = ?""",
                (error, timestamp, timestamp, normalized_id),
            )

    def list_tasks(
        self,
        *,
        query: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        filters: list[str] = []
        parameters: list[Any] = []
        if query:
            filters.append(
                "(name LIKE ? OR id LIKE ? OR worker LIKE ? "
                "OR account_x_user_id LIKE ? OR media_id LIKE ? OR context LIKE ?)"
            )
            pattern = f"%{query}%"
            parameters.extend([
                pattern,
                f"%{query.replace('-', '')}%",
                pattern,
                pattern,
                pattern,
                pattern,
            ])
        if status:
            filters.append("status = ?")
            parameters.append(status)
        where = f" WHERE {' AND '.join(filters)}" if filters else ""

        try:
            with self._read() as connection:
                rows = connection.execute(
                    f"""SELECT * FROM queue_tasks{where}
                    ORDER BY COALESCE(started_at, queued_at, created_at) DESC, id DESC
                    LIMIT ? OFFSET ?""",
                    (*parameters, limit, offset),
                ).fetchall()
                total = int(connection.execute(
                    f"SELECT COUNT(*) FROM queue_tasks{where}", parameters
                ).fetchone()[0])
                count_rows = connection.execute(
                    "SELECT status, COUNT(*) AS count FROM queue_tasks GROUP BY status"
                ).fetchall()
        except sqlite3.OperationalError:
            return {"items": [], "total": 0, "counts": self._empty_counts()}

        counts = self._empty_counts()
        for row in count_rows:
            name = str(row["status"])
            if name in counts:
                counts[name] = int(row["count"])
                counts["all"] += int(row["count"])
        return {
            "items": [self._task_response(row) for row in rows],
            "total": total,
            "counts": counts,
        }

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        normalized_id = _optional_task_id(task_id)
        if normalized_id is None:
            return None
        try:
            with self._read() as connection:
                row = connection.execute(
                    "SELECT * FROM queue_tasks WHERE id = ?", (normalized_id,)
                ).fetchone()
                if row is None:
                    return None
                attempts = connection.execute(
                    """SELECT * FROM queue_attempts WHERE task_id = ?
                    ORDER BY attempt DESC""",
                    (normalized_id,),
                ).fetchall()
                child_rows = connection.execute(
                    """SELECT status, COUNT(*) AS count FROM queue_tasks
                    WHERE parent_task_id = ? GROUP BY status""",
                    (normalized_id,),
                ).fetchall()
        except sqlite3.OperationalError:
            return None
        response = self._task_response(row)
        response["attempts"] = [self._attempt_response(item) for item in attempts]
        child_counts = self._empty_counts()
        for child_row in child_rows:
            child_status = str(child_row["status"])
            if child_status in child_counts:
                count = int(child_row["count"])
                child_counts[child_status] = count
                child_counts["all"] += count
        response["child_counts"] = child_counts
        return response

    def abandon_queued_task(self, task_id: str, error: str) -> bool:
        normalized_id = _optional_task_id(task_id)
        if normalized_id is None:
            return False
        timestamp = _now()
        with self._write() as connection:
            cursor = connection.execute(
                """UPDATE queue_tasks SET status = 'abandoned', error = ?,
                    finished_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'retry_scheduled')""",
                (error, timestamp, timestamp, normalized_id),
            )
        return cursor.rowcount == 1

    def delete_task_history(self, status: str | None = None) -> int:
        if status is not None and status not in TERMINAL_TASK_STATUSES:
            raise ValueError(f"cannot delete active task status: {status}")
        with self._write() as connection:
            if status is None:
                placeholders = ", ".join("?" for _ in TERMINAL_TASK_STATUSES)
                cursor = connection.execute(
                    f"DELETE FROM queue_tasks WHERE status IN ({placeholders})",
                    TERMINAL_TASK_STATUSES,
                )
            else:
                cursor = connection.execute(
                    "DELETE FROM queue_tasks WHERE status = ?",
                    (status,),
                )
        return cursor.rowcount

    def list_latest_failures(self) -> list[dict[str, Any]]:
        try:
            with self._read() as connection:
                rows = connection.execute(
                    """SELECT * FROM queue_tasks
                    ORDER BY COALESCE(finished_at, started_at, queued_at, created_at) DESC, id DESC"""
                ).fetchall()
        except sqlite3.OperationalError:
            return []

        latest_tasks: dict[tuple[str, str], dict[str, Any]] = {}
        latest_failures: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            task = self._task_response(row)
            key = _task_target_key(task)
            latest_tasks.setdefault(key, task)
            if task["status"] == "failure":
                latest_failures.setdefault(key, task)

        failures: list[dict[str, Any]] = []
        for key, failure in latest_failures.items():
            entry = failure.copy()
            entry["retry_state"] = (
                "ready"
                if latest_tasks[key]["id"] == failure["id"]
                else "superseded"
            )
            failures.append(entry)
        return failures

    def list_schedules(self, enabled_accounts: int) -> list[dict[str, Any]]:
        latest = self._latest_task("archivex.schedule_enabled_accounts")
        return [{
            "id": "enabled-account-sync",
            "name": "archivex.schedule_enabled_accounts",
            "title": "归档账号定时同步",
            "enabled": True,
            "interval_seconds": self.sync_interval_seconds,
            "queue_name": self.crawl_queue_name,
            "enabled_accounts": enabled_accounts,
            "last_task": latest,
        }]

    def _latest_task(self, name: str) -> dict[str, Any] | None:
        try:
            with self._read() as connection:
                row = connection.execute(
                    """SELECT * FROM queue_tasks WHERE name = ?
                    ORDER BY COALESCE(started_at, queued_at, created_at) DESC LIMIT 1""",
                    (name,),
                ).fetchone()
        except sqlite3.OperationalError:
            return None
        return self._task_response(row) if row else None

    def _initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(TASK_SCHEMA)

    @staticmethod
    def _task_metadata(
        connection: sqlite3.Connection,
        name: str,
        labels: dict[str, Any],
    ) -> _TaskMetadata:
        account_candidate = _optional_text(labels.get(TASK_ACCOUNT_ID_LABEL))
        media_candidate = _optional_text(labels.get(TASK_MEDIA_ID_LABEL))
        parent_task_id = _existing_task_id(connection, labels.get(TASK_PARENT_ID_LABEL))
        retry_of = _existing_task_id(connection, labels.get("retry_of"))
        trigger = _optional_text(labels.get(TASK_TRIGGER_LABEL))

        context: dict[str, Any] = {}
        account_x_user_id: str | None = None
        media_id: str | None = None
        if account_candidate is not None:
            account = _fetchone_optional(
                connection,
                """SELECT x_user_id, current_username, display_name
                FROM accounts WHERE x_user_id = ?""",
                (account_candidate,),
            )
            if account:
                account_x_user_id = str(account["x_user_id"])
            context["account"] = {
                "x_user_id": account_x_user_id if account else account_candidate,
                "username": account["current_username"] if account else None,
                "display_name": account["display_name"] if account else None,
            }

        if media_candidate is not None:
            media = _fetchone_optional(
                connection,
                """SELECT media.id, media.media_type, media.source_url,
                    media.download_status, posts.tweet_id, posts.permalink,
                    posts.text, accounts.x_user_id, accounts.current_username,
                    accounts.display_name
                FROM media
                JOIN posts ON posts.tweet_id = media.tweet_id
                JOIN accounts ON accounts.x_user_id = posts.account_x_user_id
                WHERE media.id = ?""",
                (media_candidate,),
            )
            if media:
                media_id = str(media["id"])
                media_account_id = str(media["x_user_id"])
                context["account"] = {
                    "x_user_id": media_account_id,
                    "username": media["current_username"],
                    "display_name": media["display_name"],
                }
                account_x_user_id = media_account_id
                context["post"] = {
                    "tweet_id": str(media["tweet_id"]),
                    "permalink": str(media["permalink"]),
                    "text_preview": _text_preview(media["text"]),
                }
                context["media"] = {
                    "id": str(media["id"]),
                    "media_type": str(media["media_type"]),
                    "source_url": str(media["source_url"]),
                    "download_status": str(media["download_status"]),
                }
            else:
                context["media"] = {"id": media_candidate}

        if not context and name == "archivex.schedule_enabled_accounts":
            context["schedule"] = {"id": "enabled-account-sync"}

        return _TaskMetadata(
            account_x_user_id=account_x_user_id,
            media_id=media_id,
            parent_task_id=parent_task_id,
            trigger=trigger,
            context=_json_dump(context),
            retry_of=retry_of,
        )

    def _ensure_task(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        name: str,
        worker: str,
        serialized: tuple[str, str, str],
        timestamp: str,
        attempt: int,
        max_attempts: int,
        metadata: _TaskMetadata,
    ) -> None:
        connection.execute(
            """INSERT OR IGNORE INTO queue_tasks (
                id, name, status, worker, account_x_user_id, media_id,
                parent_task_id, trigger, context, args, kwargs, labels, queued_at,
                current_attempt, max_attempts, retry_of, created_at, updated_at
            ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                name,
                worker,
                metadata.account_x_user_id,
                metadata.media_id,
                metadata.parent_task_id,
                metadata.trigger,
                metadata.context,
                *serialized,
                timestamp,
                attempt,
                max_attempts,
                metadata.retry_of,
                timestamp,
                timestamp,
            ),
        )

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            f"file:{self.database_path}?mode=ro", uri=True, timeout=5
        )
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _required_task_id(task_id: str) -> str:
        normalized = _optional_task_id(task_id)
        if normalized is None:
            raise ValueError(f"invalid task ID: {task_id}")
        return normalized

    @staticmethod
    def _empty_counts() -> dict[str, int]:
        return {"all": 0, **{name: 0 for name in TASK_STATUSES}}

    @staticmethod
    def _task_response(row: sqlite3.Row) -> dict[str, Any]:
        started_at = _utc_timestamp(row["started_at"])
        finished_at = _utc_timestamp(row["finished_at"])
        duration_ms = _duration_ms(started_at, finished_at)
        return {
            "id": str(uuid.UUID(hex=str(row["id"]))),
            "name": str(row["name"]),
            "status": str(row["status"]),
            "worker": str(row["worker"] or ""),
            "account_x_user_id": row["account_x_user_id"],
            "media_id": row["media_id"],
            "parent_task_id": (
                str(uuid.UUID(hex=str(row["parent_task_id"])))
                if row["parent_task_id"] else None
            ),
            "trigger": row["trigger"],
            "context": _json_value(row["context"], {}),
            "args": _json_value(row["args"], []),
            "kwargs": _json_value(row["kwargs"], {}),
            "labels": _json_value(row["labels"], {}),
            "result": _json_value(row["result"], None),
            "error": row["error"],
            "queued_at": _utc_timestamp(row["queued_at"]),
            "started_at": started_at,
            "finished_at": finished_at,
            "next_retry_at": _utc_timestamp(row["next_retry_at"]),
            "current_attempt": int(row["current_attempt"]),
            "max_attempts": int(row["max_attempts"]),
            "retry_of": (
                str(uuid.UUID(hex=str(row["retry_of"]))) if row["retry_of"] else None
            ),
            "duration_ms": duration_ms,
        }

    @staticmethod
    def _attempt_response(row: sqlite3.Row) -> dict[str, Any]:
        started_at = _utc_timestamp(row["started_at"])
        finished_at = _utc_timestamp(row["finished_at"])
        return {
            "attempt": int(row["attempt"]),
            "status": str(row["status"]),
            "labels": _json_value(row["labels"], {}),
            "result": _json_value(row["result"], None),
            "error": row["error"],
            "queued_at": _utc_timestamp(row["queued_at"]),
            "started_at": started_at,
            "finished_at": finished_at,
            "next_retry_at": _utc_timestamp(row["next_retry_at"]),
            "duration_ms": _duration_ms(started_at, finished_at),
        }


def _serialize_message(
    args: list[Any],
    kwargs: dict[str, Any],
    labels: dict[str, Any],
) -> tuple[str, str, str]:
    return _json_dump(args), _json_dump(kwargs), _json_dump(labels)


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_value(value: str | None, fallback: Any) -> Any:
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return fallback


def _fetchone_optional(
    connection: sqlite3.Connection,
    query: str,
    parameters: tuple[Any, ...],
) -> sqlite3.Row | None:
    try:
        return connection.execute(query, parameters).fetchone()
    except sqlite3.OperationalError:
        return None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _text_preview(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _attempt_number(labels: dict[str, Any]) -> int:
    try:
        return max(1, int(labels.get("_retries", 0)) + 1)
    except (TypeError, ValueError):
        return 1


def _max_attempts(labels: dict[str, Any]) -> int:
    try:
        return max(1, int(labels.get("max_retries", 1)))
    except (TypeError, ValueError):
        return 1


def _optional_task_id(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value)).hex
    except (ValueError, TypeError, AttributeError):
        return None


def _existing_task_id(connection: sqlite3.Connection, value: Any) -> str | None:
    task_id = _optional_task_id(value)
    if task_id is None:
        return None
    row = connection.execute(
        "SELECT 1 FROM queue_tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return task_id if row else None


def _task_target_key(task: dict[str, Any]) -> tuple[str, str]:
    context = task.get("context") if isinstance(task.get("context"), dict) else {}
    media = context.get("media") if isinstance(context.get("media"), dict) else {}
    account = context.get("account") if isinstance(context.get("account"), dict) else {}
    schedule = context.get("schedule") if isinstance(context.get("schedule"), dict) else {}
    structured_target = (
        task.get("media_id")
        or task.get("account_x_user_id")
        or media.get("id")
        or account.get("x_user_id")
        or schedule.get("id")
    )
    if structured_target is not None:
        return task["name"], str(structured_target)
    return task["name"], task["id"]


def _automatic_retries_exhausted(task: dict[str, Any]) -> bool:
    return task.get("status") != "retry_scheduled"


def _now() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ")


def _normalize_db_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    return _sqlite_timestamp(str(value))


def _utc_timestamp(value: str | None) -> str | None:
    if not value:
        return None
    timestamp = value.replace(" ", "T")
    if timestamp.endswith("Z") or "+" in timestamp[10:]:
        return timestamp
    return timestamp + "+00:00"


def _sqlite_timestamp(value: str) -> str:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(UTC).replace(tzinfo=None)
    return timestamp.isoformat(sep=" ")


def _duration_ms(started_at: str | None, finished_at: str | None) -> int | None:
    if not started_at or not finished_at:
        return None
    elapsed = (
        datetime.fromisoformat(finished_at).timestamp()
        - datetime.fromisoformat(started_at).timestamp()
    )
    return round(elapsed * 1000) if elapsed >= 0 else None
