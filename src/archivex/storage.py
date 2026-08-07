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
    last_sync_at: str | None
    last_error: str | None


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
    account_id: int
    username: str
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
    account_id: int
    username: str
    started_at: str
    finished_at: str | None
    posts_seen: int
    posts_new: int
    media_new: int
    status: str
    error: str | None


def initialize_storage(database_path: Path, archive_data_dir: Path, session_path: Path) -> None:
    """Create persistent locations and apply the initial SQLite schema."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    archive_data_dir.mkdir(parents=True, exist_ok=True)
    session_directory = session_path.parent if session_path.suffix else session_path
    session_directory.mkdir(parents=True, exist_ok=True)
    with _connect(database_path) as connection:
        connection.executescript(SCHEMA)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(posts)")}
        if "media_scanned_at" not in columns:
            connection.execute("ALTER TABLE posts ADD COLUMN media_scanned_at TEXT")


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
                """SELECT id, x_user_id, username, display_name, status, last_sync_at, last_error
                FROM accounts WHERE x_user_id = ?""",
                (x_user_id,),
            ).fetchone()
        return Account(**dict(row))

    def get_account(self, x_user_id: str) -> Account | None:
        with _connect(self.database_path) as connection:
            row = connection.execute(
                """SELECT id, x_user_id, username, display_name, status, last_sync_at, last_error
                FROM accounts WHERE x_user_id = ?""",
                (x_user_id,),
            ).fetchone()
        return Account(**dict(row)) if row else None

    def list_accounts(self) -> list[dict[str, Any]]:
        with _connect(self.database_path) as connection:
            rows = connection.execute(
                """SELECT accounts.id, accounts.x_user_id, accounts.username, accounts.display_name,
                    accounts.status, accounts.last_sync_at, accounts.last_error,
                    COUNT(posts.tweet_id) AS post_count
                FROM accounts LEFT JOIN posts ON posts.account_id = accounts.id
                GROUP BY accounts.id ORDER BY accounts.username COLLATE NOCASE"""
            ).fetchall()
        accounts = [dict(row) for row in rows]
        for account in accounts:
            account.update(self._account_profile(account["id"]))
        return accounts

    def get_account_by_id(self, account_id: int) -> dict[str, Any] | None:
        with _connect(self.database_path) as connection:
            row = connection.execute(
                """SELECT accounts.id, accounts.x_user_id, accounts.username, accounts.display_name,
                    accounts.status, accounts.last_sync_at, accounts.last_error,
                    COUNT(posts.tweet_id) AS post_count
                FROM accounts LEFT JOIN posts ON posts.account_id = accounts.id
                WHERE accounts.id = ? GROUP BY accounts.id""",
                (account_id,),
            ).fetchone()
        if row is None:
            return None
        account = dict(row)
        account.update(self._account_profile(account_id))
        return account

    def list_posts(self, *, account_id: int | None = None, query: str | None = None,
                   from_at: datetime | None = None, to_at: datetime | None = None,
                   has_media: bool | None = None, limit: int = 50, offset: int = 0) -> list[ArchivedPost]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if account_id is not None:
            clauses.append("posts.account_id = ?")
            parameters.append(account_id)
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
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with _connect(self.database_path) as connection:
            rows = connection.execute(
                f"""SELECT posts.tweet_id, posts.account_id, accounts.username, posts.post_type,
                    posts.text, posts.posted_at, posts.permalink, posts.first_seen_at, posts.updated_at,
                    COUNT(media.id) AS media_count
                FROM posts JOIN accounts ON accounts.id = posts.account_id
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
                """SELECT posts.tweet_id, posts.account_id, accounts.username, posts.post_type,
                    posts.text, posts.posted_at, posts.permalink, posts.first_seen_at, posts.updated_at,
                    COUNT(media.id) AS media_count
                FROM posts JOIN accounts ON accounts.id = posts.account_id
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

    def list_sync_runs(self, *, account_id: int | None = None, limit: int = 50,
                       offset: int = 0) -> list[SyncRun]:
        where = "WHERE sync_runs.account_id = ?" if account_id is not None else ""
        parameters: tuple[Any, ...] = ((account_id,) if account_id is not None else ()) + (limit, offset)
        with _connect(self.database_path) as connection:
            rows = connection.execute(
                f"""SELECT sync_runs.id, sync_runs.account_id, accounts.username, sync_runs.started_at,
                    sync_runs.finished_at, sync_runs.posts_seen, sync_runs.posts_new, sync_runs.media_new,
                    sync_runs.status, sync_runs.error
                FROM sync_runs JOIN accounts ON accounts.id = sync_runs.account_id {where}
                ORDER BY sync_runs.started_at DESC LIMIT ? OFFSET ?""",
                parameters,
            ).fetchall()
        return [SyncRun(**dict(row)) for row in rows]

    def mark_account_sync_success(self, account_id: int, completed_at: datetime) -> None:
        timestamp = _timestamp(completed_at)
        with _connect(self.database_path) as connection:
            connection.execute(
                """UPDATE accounts SET status = 'active', last_sync_at = ?, last_error = NULL,
                updated_at = ? WHERE id = ?""",
                (timestamp, timestamp, account_id),
            )

    def mark_account_sync_error(self, account_id: int, error: str) -> None:
        with _connect(self.database_path) as connection:
            connection.execute(
                "UPDATE accounts SET status = 'error', last_error = ?, updated_at = ? WHERE id = ?",
                (error, _timestamp(), account_id),
            )

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
                    media_scanned_at = NULL, updated_at = excluded.updated_at""",
                (post.tweet_id, post.account_id, post.post_type, post.text, _timestamp(post.posted_at),
                 post.permalink, raw_json_path, now, now),
            )
        return is_new

    def unscanned_post_media(self) -> list[tuple[str, Mapping[str, Any]]]:
        with _connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT tweet_id, raw_json_path FROM posts WHERE media_scanned_at IS NULL"
            ).fetchall()
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

    def failed_media_post_ids(self, account_id: int) -> list[str]:
        with _connect(self.database_path) as connection:
            rows = connection.execute(
                """SELECT DISTINCT media.tweet_id FROM media
                JOIN posts ON posts.tweet_id = media.tweet_id
                WHERE posts.account_id = ? AND media.download_status = 'failed'
                ORDER BY media.tweet_id""",
                (account_id,),
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

    def _account_profile(self, account_id: int) -> dict[str, Any]:
        with _connect(self.database_path) as connection:
            row = connection.execute(
                """SELECT raw_json_path FROM posts WHERE account_id = ?
                ORDER BY posted_at DESC, tweet_id DESC LIMIT 1""",
                (account_id,),
            ).fetchone()
        if row is None:
            return _empty_account_profile()
        payload = _read_payload(self.archive_data_dir / str(row["raw_json_path"]))
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        profile_image_url = user.get("profileImageUrl")
        if isinstance(profile_image_url, str):
            profile_image_url = profile_image_url.replace("_normal.", "_400x400.")
        banner_url = user.get("profileBannerUrl")
        if isinstance(banner_url, str) and not banner_url.rstrip("/").endswith("1500x500"):
            banner_url = f"{banner_url.rstrip('/')}/1500x500"
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
