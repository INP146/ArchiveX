from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    x_user_id TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL,
    display_name TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    last_sync_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    tweet_id TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    post_type TEXT NOT NULL,
    text TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    permalink TEXT NOT NULL,
    raw_json_path TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media (
    id TEXT PRIMARY KEY,
    tweet_id TEXT NOT NULL REFERENCES posts(tweet_id) ON DELETE CASCADE,
    media_type TEXT NOT NULL,
    source_url TEXT NOT NULL,
    local_path TEXT,
    download_status TEXT NOT NULL DEFAULT 'pending',
    sha256 TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tweet_id, source_url)
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    posts_seen INTEGER NOT NULL DEFAULT 0,
    posts_new INTEGER NOT NULL DEFAULT 0,
    media_new INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_account_posted_at
    ON posts(account_id, posted_at DESC, tweet_id DESC);
CREATE INDEX IF NOT EXISTS idx_media_tweet_id ON media(tweet_id);
CREATE INDEX IF NOT EXISTS idx_media_download_status ON media(download_status, updated_at);
CREATE INDEX IF NOT EXISTS idx_sync_runs_account_started_at
    ON sync_runs(account_id, started_at DESC);
"""


@dataclass(frozen=True)
class Account:
    id: int
    x_user_id: str
    username: str
    display_name: str | None
    status: str


@dataclass(frozen=True)
class PostInput:
    tweet_id: str
    account_id: int
    username: str
    post_type: str
    text: str
    posted_at: datetime
    permalink: str
    raw_payload: Mapping[str, Any]


@dataclass(frozen=True)
class MediaInput:
    tweet_id: str
    media_type: str
    source_url: str
    download_status: str = "pending"
    local_path: str | None = None
    sha256: str | None = None
    error: str | None = None


def initialize_storage(database_path: Path, archive_data_dir: Path, session_path: Path) -> None:
    """Create persistent locations and apply the initial SQLite schema."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    archive_data_dir.mkdir(parents=True, exist_ok=True)
    session_path.mkdir(parents=True, exist_ok=True)
    with _connect(database_path) as connection:
        connection.executescript(SCHEMA)


class ArchiveRepository:
    def __init__(self, database_path: Path, archive_data_dir: Path) -> None:
        self.database_path = database_path
        self.archive_data_dir = archive_data_dir

    def upsert_account(self, x_user_id: str, username: str, display_name: str | None = None,
                       status: str = "active") -> Account:
        now = _timestamp()
        with _connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO accounts (x_user_id, username, display_name, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(x_user_id) DO UPDATE SET username = excluded.username,
                    display_name = excluded.display_name, status = excluded.status,
                    updated_at = excluded.updated_at""",
                (x_user_id, username, display_name, status, now, now),
            )
            row = connection.execute(
                "SELECT id, x_user_id, username, display_name, status FROM accounts WHERE x_user_id = ?",
                (x_user_id,),
            ).fetchone()
        return Account(**dict(row))

    def upsert_post(self, post: PostInput) -> bool:
        """Persist a post and its raw payload. Returns True only for a new tweet ID."""
        raw_json_path = self._write_raw_post(post)
        now = _timestamp()
        with _connect(self.database_path) as connection:
            is_new = connection.execute(
                "SELECT 1 FROM posts WHERE tweet_id = ?", (post.tweet_id,)
            ).fetchone() is None
            connection.execute(
                """INSERT INTO posts (tweet_id, account_id, post_type, text, posted_at, permalink,
                    raw_json_path, first_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tweet_id) DO UPDATE SET account_id = excluded.account_id,
                    post_type = excluded.post_type, text = excluded.text, posted_at = excluded.posted_at,
                    permalink = excluded.permalink, raw_json_path = excluded.raw_json_path,
                    updated_at = excluded.updated_at""",
                (post.tweet_id, post.account_id, post.post_type, post.text, _timestamp(post.posted_at),
                 post.permalink, raw_json_path, now, now),
            )
        return is_new

    def upsert_media(self, media: MediaInput) -> str:
        now = _timestamp()
        media_id = str(uuid.uuid4())
        with _connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO media (id, tweet_id, media_type, source_url, local_path, download_status,
                    sha256, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tweet_id, source_url) DO UPDATE SET media_type = excluded.media_type,
                    local_path = excluded.local_path, download_status = excluded.download_status,
                    sha256 = excluded.sha256, error = excluded.error, updated_at = excluded.updated_at""",
                (media_id, media.tweet_id, media.media_type, media.source_url, media.local_path,
                 media.download_status, media.sha256, media.error, now, now),
            )
            row = connection.execute(
                "SELECT id FROM media WHERE tweet_id = ? AND source_url = ?",
                (media.tweet_id, media.source_url),
            ).fetchone()
        return str(row["id"])

    def start_sync_run(self, account_id: int) -> str:
        run_id = str(uuid.uuid4())
        with _connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO sync_runs (id, account_id, started_at, status) VALUES (?, ?, ?, 'running')",
                (run_id, account_id, _timestamp()),
            )
        return run_id

    def finish_sync_run(self, run_id: str, *, posts_seen: int, posts_new: int, media_new: int,
                        status: str, error: str | None = None) -> None:
        with _connect(self.database_path) as connection:
            connection.execute(
                """UPDATE sync_runs SET finished_at = ?, posts_seen = ?, posts_new = ?, media_new = ?,
                    status = ?, error = ? WHERE id = ?""",
                (_timestamp(), posts_seen, posts_new, media_new, status, error, run_id),
            )

    def _write_raw_post(self, post: PostInput) -> str:
        posted_at = _as_utc(post.posted_at)
        relative_path = (Path("accounts") / _path_component(post.username) / "posts"
                         / f"{posted_at.year:04d}" / f"{posted_at.month:02d}"
                         / _path_component(post.tweet_id) / "post.json")
        target_path = self.archive_data_dir / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(post.raw_payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target_path.parent,
                                         suffix=".tmp", delete=False) as temporary_file:
            temporary_file.write(payload + "\n")
            temporary_path = Path(temporary_file.name)
        os.replace(temporary_path, target_path)
        return relative_path.as_posix()


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("posted_at must include a timezone")
    return value.astimezone(UTC)


def _timestamp(value: datetime | None = None) -> str:
    return _as_utc(value or datetime.now(UTC)).isoformat()


def _path_component(value: str) -> str:
    component = re.sub(r"[^A-Za-z0-9_.-]", "_", value)
    if component in {"", ".", ".."}:
        raise ValueError("path component must contain a filename-safe character")
    return component
