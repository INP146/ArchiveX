from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    x_user_id TEXT PRIMARY KEY,
    current_username TEXT,
    display_name TEXT,
    archive_enabled INTEGER NOT NULL DEFAULT 1 CHECK (archive_enabled IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'active',
    last_sync_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account_username_history (
    id INTEGER PRIMARY KEY,
    x_user_id TEXT NOT NULL REFERENCES accounts(x_user_id),
    username TEXT NOT NULL,
    observed_from TEXT NOT NULL,
    observed_to TEXT,
    last_observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    tweet_id TEXT PRIMARY KEY,
    account_x_user_id TEXT NOT NULL REFERENCES accounts(x_user_id),
    post_type TEXT NOT NULL,
    text TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    permalink TEXT NOT NULL,
    raw_json_path TEXT NOT NULL,
    media_scanned_at TEXT,
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
    account_x_user_id TEXT NOT NULL REFERENCES accounts(x_user_id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    posts_seen INTEGER NOT NULL DEFAULT 0,
    posts_new INTEGER NOT NULL DEFAULT 0,
    media_new INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_account_posted_at
    ON posts(account_x_user_id, posted_at DESC, tweet_id DESC);
CREATE INDEX IF NOT EXISTS idx_media_tweet_id ON media(tweet_id);
CREATE INDEX IF NOT EXISTS idx_media_download_status ON media(download_status, updated_at);
CREATE INDEX IF NOT EXISTS idx_sync_runs_account_started_at
    ON sync_runs(account_x_user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_username_history_account_observed
    ON account_username_history(x_user_id, observed_from DESC);
CREATE INDEX IF NOT EXISTS idx_username_history_username_observed
    ON account_username_history(username COLLATE NOCASE, observed_from DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_username_history_current
    ON account_username_history(x_user_id) WHERE observed_to IS NULL;
PRAGMA user_version = 2;
"""


@dataclass(frozen=True)
class Account:
    x_user_id: str
    current_username: str | None
    display_name: str | None
    archive_enabled: bool
    status: str
    last_sync_at: str | None
    last_error: str | None


@dataclass(frozen=True)
class PostInput:
    tweet_id: str
    account_x_user_id: str
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


@dataclass(frozen=True)
class MediaRecord:
    id: str
    tweet_id: str
    source_url: str
    download_status: str


@dataclass(frozen=True)
class ArchiveMedia:
    id: str
    media_type: str
    local_path: str | None
    download_status: str
    sha256: str | None
    error: str | None


@dataclass(frozen=True)
class ArchivedPost:
    tweet_id: str
    account_x_user_id: str
    username: str | None
    post_type: str
    text: str
    posted_at: str
    permalink: str
    first_seen_at: str
    updated_at: str
    media_count: int


@dataclass(frozen=True)
class SyncRun:
    id: str
    account_x_user_id: str
    username: str | None
    started_at: str
    finished_at: str | None
    posts_seen: int
    posts_new: int
    media_new: int
    status: str
    error: str | None


def initialize_storage(database_path: Path, archive_data_dir: Path, session_path: Path) -> None:
    """Create persistent locations and migrate storage to the current schema."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    archive_data_dir.mkdir(parents=True, exist_ok=True)
    session_directory = session_path.parent if session_path.suffix else session_path
    session_directory.mkdir(parents=True, exist_ok=True)
    with _connect(database_path) as connection:
        _initialize_schema(connection)
    _migrate_archive_paths(database_path, archive_data_dir)


def _initialize_schema(connection: sqlite3.Connection) -> None:
    tables = {
        row["name"]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "accounts" not in tables:
        connection.executescript(SCHEMA)
        return

    account_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(accounts)")
    }
    if "id" in account_columns:
        _migrate_legacy_schema(connection)
        return

    connection.executescript(SCHEMA)


def _migrate_legacy_schema(connection: sqlite3.Connection) -> None:
    post_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(posts)")
    }
    media_scanned_at = "media_scanned_at" if "media_scanned_at" in post_columns else "NULL"
    connection.executescript(f"""
        PRAGMA foreign_keys = OFF;
        BEGIN IMMEDIATE;
        CREATE TABLE accounts_v2 (
            x_user_id TEXT PRIMARY KEY,
            current_username TEXT,
            display_name TEXT,
            archive_enabled INTEGER NOT NULL DEFAULT 1 CHECK (archive_enabled IN (0, 1)),
            status TEXT NOT NULL DEFAULT 'active',
            last_sync_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE account_username_history_v2 (
            id INTEGER PRIMARY KEY,
            x_user_id TEXT NOT NULL REFERENCES accounts_v2(x_user_id),
            username TEXT NOT NULL,
            observed_from TEXT NOT NULL,
            observed_to TEXT,
            last_observed_at TEXT NOT NULL
        );
        CREATE TABLE posts_v2 (
            tweet_id TEXT PRIMARY KEY,
            account_x_user_id TEXT NOT NULL REFERENCES accounts_v2(x_user_id),
            post_type TEXT NOT NULL,
            text TEXT NOT NULL,
            posted_at TEXT NOT NULL,
            permalink TEXT NOT NULL,
            raw_json_path TEXT NOT NULL,
            media_scanned_at TEXT,
            first_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE media_v2 (
            id TEXT PRIMARY KEY,
            tweet_id TEXT NOT NULL REFERENCES posts_v2(tweet_id) ON DELETE CASCADE,
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
        CREATE TABLE sync_runs_v2 (
            id TEXT PRIMARY KEY,
            account_x_user_id TEXT NOT NULL REFERENCES accounts_v2(x_user_id),
            started_at TEXT NOT NULL,
            finished_at TEXT,
            posts_seen INTEGER NOT NULL DEFAULT 0,
            posts_new INTEGER NOT NULL DEFAULT 0,
            media_new INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            error TEXT
        );

        INSERT INTO accounts_v2 (
            x_user_id, current_username, display_name, archive_enabled, status,
            last_sync_at, last_error, created_at, updated_at
        )
        SELECT x_user_id, username, display_name, 1, status,
            last_sync_at, last_error, created_at, updated_at
        FROM accounts;
        INSERT INTO account_username_history_v2 (
            x_user_id, username, observed_from, observed_to, last_observed_at
        )
        SELECT x_user_id, username, created_at, NULL, updated_at FROM accounts;
        INSERT INTO posts_v2 (
            tweet_id, account_x_user_id, post_type, text, posted_at, permalink,
            raw_json_path, media_scanned_at, first_seen_at, updated_at
        )
        SELECT posts.tweet_id, accounts.x_user_id, posts.post_type, posts.text,
            posts.posted_at, posts.permalink, posts.raw_json_path, {media_scanned_at},
            posts.first_seen_at, posts.updated_at
        FROM posts JOIN accounts ON accounts.id = posts.account_id;
        INSERT INTO media_v2
        SELECT id, tweet_id, media_type, source_url, local_path, download_status,
            sha256, error, created_at, updated_at
        FROM media;
        INSERT INTO sync_runs_v2 (
            id, account_x_user_id, started_at, finished_at, posts_seen, posts_new,
            media_new, status, error
        )
        SELECT sync_runs.id, accounts.x_user_id, sync_runs.started_at,
            sync_runs.finished_at, sync_runs.posts_seen, sync_runs.posts_new,
            sync_runs.media_new, sync_runs.status, sync_runs.error
        FROM sync_runs JOIN accounts ON accounts.id = sync_runs.account_id;

        DROP TABLE media;
        DROP TABLE sync_runs;
        DROP TABLE posts;
        DROP TABLE accounts;
        ALTER TABLE accounts_v2 RENAME TO accounts;
        ALTER TABLE account_username_history_v2 RENAME TO account_username_history;
        ALTER TABLE posts_v2 RENAME TO posts;
        ALTER TABLE media_v2 RENAME TO media;
        ALTER TABLE sync_runs_v2 RENAME TO sync_runs;
        CREATE INDEX idx_posts_account_posted_at
            ON posts(account_x_user_id, posted_at DESC, tweet_id DESC);
        CREATE INDEX idx_media_tweet_id ON media(tweet_id);
        CREATE INDEX idx_media_download_status ON media(download_status, updated_at);
        CREATE INDEX idx_sync_runs_account_started_at
            ON sync_runs(account_x_user_id, started_at DESC);
        CREATE INDEX idx_username_history_account_observed
            ON account_username_history(x_user_id, observed_from DESC);
        CREATE INDEX idx_username_history_username_observed
            ON account_username_history(username COLLATE NOCASE, observed_from DESC);
        CREATE UNIQUE INDEX idx_username_history_current
            ON account_username_history(x_user_id) WHERE observed_to IS NULL;
        PRAGMA user_version = {SCHEMA_VERSION};
        COMMIT;
        PRAGMA foreign_keys = ON;
    """)


def _migrate_archive_paths(database_path: Path, archive_data_dir: Path) -> None:
    with _connect(database_path) as connection:
        rows = connection.execute(
            """SELECT tweet_id, account_x_user_id, posted_at, raw_json_path
            FROM posts ORDER BY posted_at, tweet_id"""
        ).fetchall()
        for row in rows:
            posted_at = datetime.fromisoformat(str(row["posted_at"]))
            relative_dir = (
                Path("accounts") / _path_component(str(row["account_x_user_id"])) / "posts"
                / f"{posted_at.year:04d}" / f"{posted_at.month:02d}"
                / _path_component(str(row["tweet_id"]))
            )
            target_json = relative_dir / "post.json"
            current_json = Path(str(row["raw_json_path"]))
            if current_json == target_json:
                continue

            source_dir = archive_data_dir / current_json.parent
            target_dir = archive_data_dir / relative_dir
            if source_dir.is_dir():
                target_dir.parent.mkdir(parents=True, exist_ok=True)
                if not target_dir.exists():
                    shutil.move(str(source_dir), str(target_dir))
                else:
                    _merge_directory(source_dir, target_dir)
            if not (archive_data_dir / target_json).is_file():
                continue

            old_prefix = current_json.parent.as_posix() + "/"
            new_prefix = relative_dir.as_posix() + "/"
            connection.execute(
                "UPDATE posts SET raw_json_path = ? WHERE tweet_id = ?",
                (target_json.as_posix(), row["tweet_id"]),
            )
            connection.execute(
                """UPDATE media SET local_path = ? || substr(local_path, ?)
                WHERE tweet_id = ? AND local_path LIKE ?""",
                (new_prefix, len(old_prefix) + 1, row["tweet_id"], old_prefix + "%"),
            )


def _merge_directory(source: Path, target: Path) -> None:
    for path in source.iterdir():
        destination = target / path.name
        if destination.exists():
            continue
        shutil.move(str(path), str(destination))


class ArchiveRepository:
    def __init__(self, database_path: Path, archive_data_dir: Path) -> None:
        self.database_path = database_path
        self.archive_data_dir = archive_data_dir

    def upsert_account(self, x_user_id: str, current_username: str | None,
                       display_name: str | None = None, status: str = "active") -> Account:
        now = _timestamp()
        with _connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO accounts (
                    x_user_id, current_username, display_name, archive_enabled, status,
                    created_at, updated_at
                ) VALUES (?, NULL, ?, 1, ?, ?, ?)
                ON CONFLICT(x_user_id) DO UPDATE SET
                    display_name = COALESCE(excluded.display_name, accounts.display_name),
                    archive_enabled = 1, status = excluded.status, updated_at = excluded.updated_at""",
                (x_user_id, display_name, status, now, now),
            )
            if current_username:
                _observe_username(connection, x_user_id, current_username, now)
            row = connection.execute(
                """SELECT x_user_id, current_username, display_name, archive_enabled,
                    status, last_sync_at, last_error
                FROM accounts WHERE x_user_id = ?""",
                (x_user_id,),
            ).fetchone()
        return _account_from_row(row)

    def observe_account_identity(self, x_user_id: str, username: str,
                                 display_name: str | None = None) -> None:
        now = _timestamp()
        with _connect(self.database_path) as connection:
            if display_name is not None:
                connection.execute(
                    "UPDATE accounts SET display_name = ?, updated_at = ? WHERE x_user_id = ?",
                    (display_name, now, x_user_id),
                )
            _observe_username(connection, x_user_id, username, now)

    def get_account(self, x_user_id: str) -> Account | None:
        with _connect(self.database_path) as connection:
            row = connection.execute(
                """SELECT x_user_id, current_username, display_name, archive_enabled,
                    status, last_sync_at, last_error
                FROM accounts WHERE x_user_id = ?""",
                (x_user_id,),
            ).fetchone()
        return _account_from_row(row) if row else None

    def list_enabled_account_ids(self) -> list[str]:
        with _connect(self.database_path) as connection:
            rows = connection.execute(
                """SELECT x_user_id FROM accounts WHERE archive_enabled = 1
                ORDER BY COALESCE(current_username, x_user_id) COLLATE NOCASE"""
            ).fetchall()
        return [str(row["x_user_id"]) for row in rows]

    def set_account_enabled(self, x_user_id: str, enabled: bool) -> Account | None:
        now = _timestamp()
        with _connect(self.database_path) as connection:
            connection.execute(
                """UPDATE accounts SET archive_enabled = ?, status = ?, updated_at = ?
                WHERE x_user_id = ?""",
                (int(enabled), "active" if enabled else "paused", now, x_user_id),
            )
        return self.get_account(x_user_id)

    def username_history(self, x_user_id: str) -> list[dict[str, Any]]:
        with _connect(self.database_path) as connection:
            rows = connection.execute(
                """SELECT id, x_user_id, username, observed_from, observed_to, last_observed_at
                FROM account_username_history WHERE x_user_id = ?
                ORDER BY observed_from DESC, id DESC""",
                (x_user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_accounts(self) -> list[dict[str, Any]]:
        with _connect(self.database_path) as connection:
            rows = connection.execute(
                """SELECT accounts.x_user_id, accounts.current_username, accounts.display_name,
                    accounts.archive_enabled, accounts.status, accounts.last_sync_at, accounts.last_error,
                    COUNT(posts.tweet_id) AS post_count
                FROM accounts LEFT JOIN posts ON posts.account_x_user_id = accounts.x_user_id
                GROUP BY accounts.x_user_id
                ORDER BY COALESCE(accounts.current_username, accounts.x_user_id) COLLATE NOCASE"""
            ).fetchall()
        accounts = [dict(row) for row in rows]
        for account in accounts:
            account["archive_enabled"] = bool(account["archive_enabled"])
            account.update(self._account_profile(account["x_user_id"]))
        return accounts

    def get_account_details(self, x_user_id: str) -> dict[str, Any] | None:
        with _connect(self.database_path) as connection:
            row = connection.execute(
                """SELECT accounts.x_user_id, accounts.current_username, accounts.display_name,
                    accounts.archive_enabled, accounts.status, accounts.last_sync_at, accounts.last_error,
                    COUNT(posts.tweet_id) AS post_count
                FROM accounts LEFT JOIN posts ON posts.account_x_user_id = accounts.x_user_id
                WHERE accounts.x_user_id = ? GROUP BY accounts.x_user_id""",
                (x_user_id,),
            ).fetchone()
        if row is None:
            return None
        account = dict(row)
        account["archive_enabled"] = bool(account["archive_enabled"])
        account.update(self._account_profile(x_user_id))
        return account

    def list_posts(self, *, account_x_user_id: str | None = None, query: str | None = None,
                   from_at: datetime | None = None, to_at: datetime | None = None,
                   has_media: bool | None = None, post_type: str | None = None,
                   exclude_post_type: str | None = None, limit: int = 50,
                   offset: int = 0) -> list[ArchivedPost]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if account_x_user_id is not None:
            clauses.append("posts.account_x_user_id = ?")
            parameters.append(account_x_user_id)
        if query:
            clauses.append("posts.text LIKE ? ESCAPE '\\'")
            parameters.append(f"%{_escape_like(query)}%")
        if from_at is not None:
            clauses.append("posts.posted_at >= ?")
            parameters.append(_timestamp(from_at))
        if to_at is not None:
            clauses.append("posts.posted_at <= ?")
            parameters.append(_timestamp(to_at))
        if has_media is True:
            clauses.append("EXISTS (SELECT 1 FROM media WHERE media.tweet_id = posts.tweet_id)")
        elif has_media is False:
            clauses.append("NOT EXISTS (SELECT 1 FROM media WHERE media.tweet_id = posts.tweet_id)")
        if post_type is not None:
            clauses.append("posts.post_type = ?")
            parameters.append(post_type)
        if exclude_post_type is not None:
            clauses.append("posts.post_type != ?")
            parameters.append(exclude_post_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with _connect(self.database_path) as connection:
            rows = connection.execute(
                f"""SELECT posts.tweet_id, posts.account_x_user_id,
                    accounts.current_username AS username, posts.post_type,
                    posts.text, posts.posted_at, posts.permalink, posts.first_seen_at, posts.updated_at,
                    COUNT(media.id) AS media_count
                FROM posts JOIN accounts ON accounts.x_user_id = posts.account_x_user_id
                LEFT JOIN media ON media.tweet_id = posts.tweet_id
                {where}
                GROUP BY posts.tweet_id
                ORDER BY posts.posted_at DESC, posts.tweet_id DESC LIMIT ? OFFSET ?""",
                (*parameters, limit, offset),
            ).fetchall()
        return [ArchivedPost(**dict(row)) for row in rows]

    def get_post(self, tweet_id: str) -> ArchivedPost | None:
        with _connect(self.database_path) as connection:
            row = connection.execute(
                """SELECT posts.tweet_id, posts.account_x_user_id,
                    accounts.current_username AS username, posts.post_type,
                    posts.text, posts.posted_at, posts.permalink, posts.first_seen_at, posts.updated_at,
                    COUNT(media.id) AS media_count
                FROM posts JOIN accounts ON accounts.x_user_id = posts.account_x_user_id
                LEFT JOIN media ON media.tweet_id = posts.tweet_id
                WHERE posts.tweet_id = ? GROUP BY posts.tweet_id""",
                (tweet_id,),
            ).fetchone()
        return ArchivedPost(**dict(row)) if row else None

    def post_media(self, tweet_id: str) -> list[ArchiveMedia]:
        with _connect(self.database_path) as connection:
            rows = connection.execute(
                """SELECT id, media_type, local_path, download_status, sha256, error
                FROM media WHERE tweet_id = ? ORDER BY created_at, id""",
                (tweet_id,),
            ).fetchall()
        return [ArchiveMedia(**dict(row)) for row in rows]

    def get_media(self, media_id: str) -> ArchiveMedia | None:
        with _connect(self.database_path) as connection:
            row = connection.execute(
                """SELECT id, media_type, local_path, download_status, sha256, error
                FROM media WHERE id = ?""",
                (media_id,),
            ).fetchone()
        return ArchiveMedia(**dict(row)) if row else None

    def post_metrics(self, tweet_id: str) -> dict[str, int | None]:
        payload = self._post_payload(tweet_id)
        return {
            "reply_count": _optional_int(payload.get("replyCount")),
            "repost_count": _optional_int(payload.get("retweetCount")),
            "like_count": _optional_int(payload.get("likeCount")),
            "view_count": _optional_int(payload.get("viewCount")),
        }

    def post_presentation(self, tweet_id: str) -> dict[str, Any]:
        payload = self._post_payload(tweet_id)
        retweeted = payload.get("retweetedTweet")
        content = retweeted if isinstance(retweeted, dict) else payload
        author = content.get("user") if isinstance(content.get("user"), dict) else {}
        reposter = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        replied_to = content.get("inReplyToUser")
        replied_to = replied_to if isinstance(replied_to, dict) else {}
        profile_image_url = author.get("profileImageUrl")
        if isinstance(profile_image_url, str):
            profile_image_url = profile_image_url.replace("_normal.", "_400x400.")
        return {
            "display_text": _display_text(
                content.get("rawContent"), content.get("displayTextRange")
            ),
            "author_display_name": (
                author.get("displayname") or author.get("displayName") or author.get("username")
            ),
            "author_username": author.get("username"),
            "author_profile_image_url": profile_image_url,
            "author_verified": bool(author.get("verified") or author.get("blue")),
            "reposted_by_display_name": (
                reposter.get("displayname") or reposter.get("displayName") or reposter.get("username")
            ) if isinstance(retweeted, dict) else None,
            "reply_to_username": content.get("inReplyToScreenName") or replied_to.get("username"),
            "language": content.get("lang") or payload.get("lang"),
            "is_translatable": bool(
                content.get("isTranslatable", payload.get("isTranslatable", False))
            ),
            "is_ai_generated": bool(
                content.get("isAiGenerated") or content.get("isAIGenerated")
                or payload.get("isAiGenerated") or payload.get("isAIGenerated")
            ),
        }

    def list_sync_runs(self, *, account_x_user_id: str | None = None, limit: int = 50,
                       offset: int = 0) -> list[SyncRun]:
        where = "WHERE sync_runs.account_x_user_id = ?" if account_x_user_id is not None else ""
        parameters: tuple[Any, ...] = (
            (account_x_user_id,) if account_x_user_id is not None else ()
        ) + (limit, offset)
        with _connect(self.database_path) as connection:
            rows = connection.execute(
                f"""SELECT sync_runs.id, sync_runs.account_x_user_id,
                    accounts.current_username AS username, sync_runs.started_at,
                    sync_runs.finished_at, sync_runs.posts_seen, sync_runs.posts_new, sync_runs.media_new,
                    sync_runs.status, sync_runs.error
                FROM sync_runs JOIN accounts
                    ON accounts.x_user_id = sync_runs.account_x_user_id {where}
                ORDER BY sync_runs.started_at DESC LIMIT ? OFFSET ?""",
                parameters,
            ).fetchall()
        return [SyncRun(**dict(row)) for row in rows]

    def mark_account_sync_success(self, x_user_id: str, completed_at: datetime) -> None:
        timestamp = _timestamp(completed_at)
        with _connect(self.database_path) as connection:
            connection.execute(
                """UPDATE accounts SET status = CASE WHEN archive_enabled = 1
                    THEN 'active' ELSE 'paused' END, last_sync_at = ?, last_error = NULL,
                    updated_at = ? WHERE x_user_id = ?""",
                (timestamp, timestamp, x_user_id),
            )

    def mark_account_sync_error(self, x_user_id: str, error: str) -> None:
        with _connect(self.database_path) as connection:
            connection.execute(
                """UPDATE accounts SET status = 'error', last_error = ?, updated_at = ?
                WHERE x_user_id = ?""",
                (error, _timestamp(), x_user_id),
            )

    def upsert_post(self, post: PostInput) -> bool:
        """Persist a post and its raw payload. Returns True only for a new tweet ID."""
        raw_json_path = self._write_raw_post(post)
        now = _timestamp()
        with _connect(self.database_path) as connection:
            existing = connection.execute(
                "SELECT account_x_user_id FROM posts WHERE tweet_id = ?", (post.tweet_id,)
            ).fetchone()
            if existing is not None and existing["account_x_user_id"] != post.account_x_user_id:
                raise ValueError(
                    f"tweet {post.tweet_id} already belongs to X user "
                    f"{existing['account_x_user_id']}"
                )
            is_new = existing is None
            connection.execute(
                """INSERT INTO posts (
                    tweet_id, account_x_user_id, post_type, text, posted_at, permalink,
                    raw_json_path, first_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tweet_id) DO UPDATE SET
                    post_type = excluded.post_type, text = excluded.text, posted_at = excluded.posted_at,
                    permalink = excluded.permalink, raw_json_path = excluded.raw_json_path,
                    media_scanned_at = NULL, updated_at = excluded.updated_at""",
                (post.tweet_id, post.account_x_user_id, post.post_type, post.text,
                 _timestamp(post.posted_at), post.permalink, raw_json_path, now, now),
            )
        return is_new

    def unscanned_post_media(
        self, account_x_user_id: str | None = None
    ) -> list[tuple[str, Mapping[str, Any]]]:
        with _connect(self.database_path) as connection:
            query = "SELECT tweet_id, raw_json_path FROM posts WHERE media_scanned_at IS NULL"
            parameters: tuple[str, ...] = ()
            if account_x_user_id is not None:
                query += " AND account_x_user_id = ?"
                parameters = (account_x_user_id,)
            rows = connection.execute(query, parameters).fetchall()
        posts = []
        for row in rows:
            try:
                payload = json.loads((self.archive_data_dir / row["raw_json_path"]).read_text())
            except (OSError, json.JSONDecodeError):
                continue
            posts.append((str(row["tweet_id"]), payload))
        return posts

    def mark_post_media_scanned(self, tweet_id: str) -> None:
        with _connect(self.database_path) as connection:
            connection.execute(
                "UPDATE posts SET media_scanned_at = ? WHERE tweet_id = ?",
                (_timestamp(), tweet_id),
            )

    def create_media_if_missing(self, media: MediaInput) -> bool:
        now = _timestamp()
        with _connect(self.database_path) as connection:
            result = connection.execute(
                """INSERT INTO media (id, tweet_id, media_type, source_url, local_path, download_status,
                    sha256, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tweet_id, source_url) DO NOTHING""",
                (str(uuid.uuid4()), media.tweet_id, media.media_type, media.source_url,
                 media.local_path, media.download_status, media.sha256, media.error, now, now),
            )
        return result.rowcount == 1

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

    def media_to_download(self, tweet_id: str) -> list[MediaRecord]:
        with _connect(self.database_path) as connection:
            rows = connection.execute(
                """SELECT id, tweet_id, source_url, download_status FROM media
                WHERE tweet_id = ? AND download_status IN ('pending', 'failed') ORDER BY created_at, id""",
                (tweet_id,),
            ).fetchall()
        return [MediaRecord(**dict(row)) for row in rows]

    def get_media_record(self, media_id: str) -> MediaRecord | None:
        with _connect(self.database_path) as connection:
            row = connection.execute(
                """SELECT id, tweet_id, source_url, download_status
                FROM media WHERE id = ?""",
                (media_id,),
            ).fetchone()
        return MediaRecord(**dict(row)) if row else None

    def media_ids_to_download(self, account_x_user_id: str) -> list[str]:
        with _connect(self.database_path) as connection:
            rows = connection.execute(
                """SELECT media.id FROM media
                JOIN posts ON posts.tweet_id = media.tweet_id
                WHERE posts.account_x_user_id = ?
                    AND media.download_status = 'pending'
                ORDER BY media.created_at, media.id""",
                (account_x_user_id,),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def failed_media_post_ids(self, account_x_user_id: str) -> list[str]:
        with _connect(self.database_path) as connection:
            rows = connection.execute(
                """SELECT DISTINCT media.tweet_id FROM media
                JOIN posts ON posts.tweet_id = media.tweet_id
                WHERE posts.account_x_user_id = ? AND media.download_status = 'failed'
                ORDER BY media.tweet_id""",
                (account_x_user_id,),
            ).fetchall()
        return [str(row["tweet_id"]) for row in rows]

    def post_directory(self, tweet_id: str) -> Path:
        with _connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT raw_json_path FROM posts WHERE tweet_id = ?", (tweet_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown tweet ID: {tweet_id}")
        return (self.archive_data_dir / str(row["raw_json_path"])).parent

    def complete_media(self, media_id: str, local_path: Path, sha256: str) -> None:
        relative_path = local_path.relative_to(self.archive_data_dir).as_posix()
        with _connect(self.database_path) as connection:
            connection.execute(
                """UPDATE media SET local_path = ?, sha256 = ?, download_status = 'completed', error = NULL,
                updated_at = ? WHERE id = ?""",
                (relative_path, sha256, _timestamp(), media_id),
            )

    def fail_media(self, media_id: str, error: str) -> None:
        with _connect(self.database_path) as connection:
            connection.execute(
                "UPDATE media SET download_status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                (error, _timestamp(), media_id),
            )

    def start_sync_run(self, account_x_user_id: str) -> str:
        run_id = str(uuid.uuid4())
        with _connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO sync_runs (id, account_x_user_id, started_at, status)
                VALUES (?, ?, ?, 'running')""",
                (run_id, account_x_user_id, _timestamp()),
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

    def interrupt_running_sync_runs(
        self, error: str = "process stopped before synchronization completed"
    ) -> int:
        """Close sync runs left open by a previous process."""
        with _connect(self.database_path) as connection:
            cursor = connection.execute(
                """UPDATE sync_runs SET finished_at = ?, status = 'interrupted', error = ?
                WHERE status = 'running' AND finished_at IS NULL""",
                (_timestamp(), error),
            )
        return cursor.rowcount

    def _write_raw_post(self, post: PostInput) -> str:
        posted_at = _as_utc(post.posted_at)
        relative_path = (Path("accounts") / _path_component(post.account_x_user_id) / "posts"
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

    def _account_profile(self, x_user_id: str) -> dict[str, Any]:
        with _connect(self.database_path) as connection:
            row = connection.execute(
                """SELECT raw_json_path FROM posts WHERE account_x_user_id = ?
                ORDER BY posted_at DESC, tweet_id DESC LIMIT 1""",
                (x_user_id,),
            ).fetchone()
        if row is None:
            return _empty_account_profile()
        payload = _read_payload(self.archive_data_dir / str(row["raw_json_path"]))
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        profile_image_url = user.get("profileImageUrl")
        if isinstance(profile_image_url, str):
            profile_image_url = profile_image_url.replace("_normal.", "_400x400.")
        banner_url = user.get("profileBannerUrl")
        if isinstance(banner_url, str):
            banner_url = banner_url.strip().rstrip("/")
            banner_url = (
                banner_url if banner_url.endswith("1500x500")
                else f"{banner_url}/1500x500" if banner_url
                else None
            )
        return {
            "description": user.get("rawDescription") or user.get("description") or None,
            "location": user.get("location") or None,
            "profile_image_url": profile_image_url,
            "profile_banner_url": banner_url,
            "verified": bool(user.get("blue")),
            "followers_count": _optional_int(user.get("followersCount")),
            "following_count": _optional_int(user.get("friendsCount")),
            "joined_at": user.get("created") or None,
        }

    def _post_payload(self, tweet_id: str) -> Mapping[str, Any]:
        with _connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT raw_json_path FROM posts WHERE tweet_id = ?", (tweet_id,)
            ).fetchone()
        if row is None:
            return {}
        return _read_payload(self.archive_data_dir / str(row["raw_json_path"]))


def _observe_username(connection: sqlite3.Connection, x_user_id: str, username: str,
                      observed_at: str) -> None:
    normalized = username.strip().lstrip("@")
    if not normalized:
        return
    account = connection.execute(
        "SELECT current_username FROM accounts WHERE x_user_id = ?", (x_user_id,)
    ).fetchone()
    if account is None:
        raise ValueError(f"unknown X user ID: {x_user_id}")

    current = account["current_username"]
    if isinstance(current, str) and current.casefold() == normalized.casefold():
        connection.execute(
            """UPDATE account_username_history SET username = ?, last_observed_at = ?
            WHERE x_user_id = ? AND observed_to IS NULL""",
            (normalized, observed_at, x_user_id),
        )
    else:
        connection.execute(
            """UPDATE account_username_history SET observed_to = ?, last_observed_at = ?
            WHERE x_user_id = ? AND observed_to IS NULL""",
            (observed_at, observed_at, x_user_id),
        )
        connection.execute(
            """INSERT INTO account_username_history (
                x_user_id, username, observed_from, observed_to, last_observed_at
            ) VALUES (?, ?, ?, NULL, ?)""",
            (x_user_id, normalized, observed_at, observed_at),
        )
    connection.execute(
        "UPDATE accounts SET current_username = ?, updated_at = ? WHERE x_user_id = ?",
        (normalized, observed_at, x_user_id),
    )


def _account_from_row(row: sqlite3.Row) -> Account:
    values = dict(row)
    values["archive_enabled"] = bool(values["archive_enabled"])
    return Account(**values)


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


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _read_payload(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _display_text(value: Any, display_range: Any = None) -> str:
    if not isinstance(value, str):
        return ""
    if (
        isinstance(display_range, list) and len(display_range) == 2
        and all(isinstance(item, int) for item in display_range)
    ):
        start, end = display_range
        if 0 <= start <= end <= len(value):
            value = value[start:end]
    return re.sub(r"(?:\s*https://t\.co/[A-Za-z0-9]+)+\s*$", "", value).strip()


def _empty_account_profile() -> dict[str, Any]:
    return {
        "description": None,
        "location": None,
        "profile_image_url": None,
        "profile_banner_url": None,
        "verified": False,
        "followers_count": None,
        "following_count": None,
        "joined_at": None,
    }
