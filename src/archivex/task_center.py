from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


TASK_STATUS_NAMES = {
    0: "in_progress",
    1: "completed",
    2: "failure",
    3: "queued",
    4: "abandoned",
}
TASK_STATUS_VALUES = {name: value for value, name in TASK_STATUS_NAMES.items()}


class TaskCenterRepository:
    def __init__(self, database_path: Path, sync_interval_seconds: int,
                 crawl_queue_name: str) -> None:
        self.database_path = database_path
        self.sync_interval_seconds = sync_interval_seconds
        self.crawl_queue_name = crawl_queue_name

    def list_tasks(
        self,
        *,
        query: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if not self.database_path.is_file():
            return {"items": [], "total": 0, "counts": self._empty_counts()}

        filters: list[str] = []
        parameters: list[Any] = []
        if query:
            filters.append("(name LIKE ? OR id LIKE ? OR worker LIKE ?)")
            pattern = f"%{query}%"
            parameters.extend([pattern, f"%{query.replace('-', '')}%", pattern])
        if status:
            filters.append("status = ?")
            parameters.append(TASK_STATUS_VALUES[status])
        where = f" WHERE {' AND '.join(filters)}" if filters else ""

        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"""SELECT * FROM tasks{where}
                    ORDER BY COALESCE(started_at, queued_at) DESC, id DESC
                    LIMIT ? OFFSET ?""",
                    (*parameters, limit, offset),
                ).fetchall()
                total = int(connection.execute(
                    f"SELECT COUNT(*) FROM tasks{where}", parameters
                ).fetchone()[0])
                count_rows = connection.execute(
                    "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
                ).fetchall()
        except sqlite3.OperationalError:
            return {"items": [], "total": 0, "counts": self._empty_counts()}

        counts = self._empty_counts()
        for row in count_rows:
            name = TASK_STATUS_NAMES.get(int(row["status"]))
            if name:
                counts[name] = int(row["count"])
                counts["all"] += int(row["count"])
        return {
            "items": [self._task_response(row) for row in rows],
            "total": total,
            "counts": counts,
        }

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        normalized_id = self._normalize_task_id(task_id)
        if normalized_id is None or not self.database_path.is_file():
            return None
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM tasks WHERE id = ?", (normalized_id,)
                ).fetchone()
        except sqlite3.OperationalError:
            return None
        return self._task_response(row) if row else None

    def list_latest_failures(self) -> list[dict[str, Any]]:
        """Return the latest failure and its current retry state for each target."""
        if not self.database_path.is_file():
            return []
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """SELECT * FROM tasks
                    ORDER BY COALESCE(finished_at, started_at, queued_at) DESC, id DESC"""
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
            if latest_tasks[key]["id"] != failure["id"]:
                entry["retry_state"] = "superseded"
            elif not _automatic_retries_exhausted(failure):
                entry["retry_state"] = "automatic_retrying"
            else:
                entry["retry_state"] = "ready"
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
        if not self.database_path.is_file():
            return None
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """SELECT * FROM tasks WHERE name = ?
                    ORDER BY COALESCE(started_at, queued_at) DESC LIMIT 1""",
                    (name,),
                ).fetchone()
        except sqlite3.OperationalError:
            return None
        return self._task_response(row) if row else None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{self.database_path}?mode=ro", uri=True, timeout=5
        )
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _empty_counts() -> dict[str, int]:
        return {"all": 0, **{name: 0 for name in TASK_STATUS_VALUES}}

    @staticmethod
    def _normalize_task_id(task_id: str) -> str | None:
        try:
            return uuid.UUID(task_id).hex
        except ValueError:
            return None

    @staticmethod
    def _task_response(row: sqlite3.Row) -> dict[str, Any]:
        started_at = _utc_timestamp(row["started_at"])
        finished_at = _utc_timestamp(row["finished_at"])
        duration_ms = None
        if started_at and finished_at:
            duration_ms = round(
                (datetime.fromisoformat(finished_at).timestamp()
                 - datetime.fromisoformat(started_at).timestamp()) * 1000
            )
        return {
            "id": str(uuid.UUID(hex=str(row["id"]))),
            "name": str(row["name"]),
            "status": TASK_STATUS_NAMES.get(int(row["status"]), "unknown"),
            "worker": str(row["worker"] or ""),
            "args": _json_value(row["args"], []),
            "kwargs": _json_value(row["kwargs"], {}),
            "labels": _json_value(row["labels"], {}),
            "result": _json_value(row["result"], None),
            "error": row["error"],
            "queued_at": _utc_timestamp(row["queued_at"]),
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
        }


def _json_value(value: str | None, fallback: Any) -> Any:
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return fallback


def _automatic_retries_exhausted(task: dict[str, Any]) -> bool:
    labels = task.get("labels")
    if not isinstance(labels, dict) or "max_retries" not in labels:
        return True
    try:
        max_retries = int(labels["max_retries"])
        retries = int(labels.get("_retries", 0))
    except (TypeError, ValueError):
        return True
    return retries + 1 >= max_retries


def _task_target_key(task: dict[str, Any]) -> tuple[str, str]:
    args = task["args"]
    target = args[0] if isinstance(args, list) and args else None
    return task["name"], json.dumps(target, sort_keys=True, default=str)


def _utc_timestamp(value: str | None) -> str | None:
    if not value:
        return None
    timestamp = value.replace(" ", "T")
    if timestamp.endswith("Z") or "+" in timestamp[10:]:
        return timestamp
    return timestamp + "+00:00"
