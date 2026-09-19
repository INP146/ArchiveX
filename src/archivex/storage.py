from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

from archivex.post_model import (
    SourceMedia,
    SourcePost,
    UNKNOWN_USER_ID,
    UserSnapshot,
    user_from_payload,
)
from archivex.task_center import TASK_SCHEMA

SCHEMA_VERSION = 3
SCHEMA = """
CREATE TABLE IF NOT EXISTS x_users (
    x_user_id TEXT PRIMARY KEY,
    current_username TEXT,
    display_name TEXT,
    profile_image_url TEXT,
    description TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observed_accounts (
    x_user_id TEXT PRIMARY KEY REFERENCES x_users(x_user_id),
    archive_enabled INTEGER NOT NULL DEFAULT 1 CHECK (archive_enabled IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'active',
    last_sync_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observed_account_username_history (
    id INTEGER PRIMARY KEY,
    x_user_id TEXT NOT NULL REFERENCES observed_accounts(x_user_id),
    username TEXT NOT NULL,
    observed_from TEXT NOT NULL,
    observed_to TEXT,
    last_observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    tweet_id TEXT PRIMARY KEY,
    author_x_user_id TEXT NOT NULL REFERENCES x_users(x_user_id),
    post_type TEXT NOT NULL CHECK (post_type IN ('original', 'reply', 'quote')),
    reference_tweet_id TEXT REFERENCES posts(tweet_id),
    text TEXT NOT NULL DEFAULT '',
    posted_at TEXT NOT NULL,
    permalink TEXT NOT NULL,
    raw_json_path TEXT,
    capture_source TEXT NOT NULL DEFAULT 'timeline',
    availability TEXT NOT NULL DEFAULT 'available'
        CHECK (availability IN ('available', 'partial', 'deleted_or_unavailable', 'unknown')),
    media_scanned_at TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (reference_tweet_id IS NULL OR reference_tweet_id != tweet_id)
);
CREATE TABLE IF NOT EXISTS reposts (
    repost_tweet_id TEXT PRIMARY KEY,
    origin_tweet_id TEXT NOT NULL REFERENCES posts(tweet_id),
    reposter_x_user_id TEXT NOT NULL REFERENCES x_users(x_user_id),
    reposted_at TEXT NOT NULL,
    permalink TEXT NOT NULL,
    raw_json_path TEXT,
    availability TEXT NOT NULL DEFAULT 'available',
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_post_observations (
    observed_account_x_user_id TEXT NOT NULL REFERENCES observed_accounts(x_user_id),
    tweet_id TEXT NOT NULL REFERENCES posts(tweet_id) ON DELETE CASCADE,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    PRIMARY KEY (observed_account_x_user_id, tweet_id)
);
CREATE TABLE IF NOT EXISTS archive_repost_observations (
    observed_account_x_user_id TEXT NOT NULL REFERENCES observed_accounts(x_user_id),
    repost_tweet_id TEXT NOT NULL REFERENCES reposts(repost_tweet_id) ON DELETE CASCADE,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    PRIMARY KEY (observed_account_x_user_id, repost_tweet_id)
);
CREATE TABLE IF NOT EXISTS media (
    id TEXT PRIMARY KEY,
    owner_tweet_id TEXT NOT NULL REFERENCES posts(tweet_id) ON DELETE CASCADE,
    media_type TEXT NOT NULL,
    source_url TEXT NOT NULL,
    local_path TEXT,
    download_status TEXT NOT NULL DEFAULT 'pending',
    sha256 TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_tweet_id, source_url)
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id TEXT PRIMARY KEY,
    observed_account_x_user_id TEXT NOT NULL REFERENCES observed_accounts(x_user_id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    posts_seen INTEGER NOT NULL DEFAULT 0,
    posts_new INTEGER NOT NULL DEFAULT 0,
    referenced_new INTEGER NOT NULL DEFAULT 0,
    reposts_new INTEGER NOT NULL DEFAULT 0,
    media_new INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_author_posted ON posts(author_x_user_id, posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_posted ON posts(posted_at DESC, tweet_id DESC);
CREATE INDEX IF NOT EXISTS idx_posts_reference ON posts(reference_tweet_id);
CREATE INDEX IF NOT EXISTS idx_reposts_posted ON reposts(reposted_at DESC, repost_tweet_id DESC);
CREATE INDEX IF NOT EXISTS idx_reposts_origin ON reposts(origin_tweet_id);
CREATE INDEX IF NOT EXISTS idx_reposts_reposter ON reposts(reposter_x_user_id);
CREATE INDEX IF NOT EXISTS idx_post_observations_post ON archive_post_observations(tweet_id);
CREATE INDEX IF NOT EXISTS idx_repost_observations_repost ON archive_repost_observations(repost_tweet_id);
CREATE INDEX IF NOT EXISTS idx_media_owner ON media(owner_tweet_id);
CREATE INDEX IF NOT EXISTS idx_media_download_status ON media(download_status, updated_at);
CREATE INDEX IF NOT EXISTS idx_sync_runs_account_started
    ON sync_runs(observed_account_x_user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_username_history_account_observed
    ON observed_account_username_history(x_user_id, observed_from DESC);
CREATE INDEX IF NOT EXISTS idx_username_history_username_observed
    ON observed_account_username_history(username COLLATE NOCASE, observed_from DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_username_history_current
    ON observed_account_username_history(x_user_id) WHERE observed_to IS NULL;
CREATE TABLE IF NOT EXISTS archive_migrations (
    id TEXT PRIMARY KEY,
    from_version INTEGER NOT NULL,
    to_version INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS archive_migration_errors (
    id INTEGER PRIMARY KEY,
    migration_id TEXT NOT NULL REFERENCES archive_migrations(id) ON DELETE CASCADE,
    source_table TEXT NOT NULL,
    source_id TEXT NOT NULL,
    error_code TEXT NOT NULL,
    details TEXT NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL
);
"""

ACCOUNT_SELECT = """SELECT a.x_user_id, u.current_username, u.display_name,
    a.archive_enabled, a.status, a.last_sync_at, a.last_error
    FROM observed_accounts a JOIN x_users u USING (x_user_id)"""

# UNION occurs before filtering, ordering and offset pagination. External content
# is visible in details but enters a timeline only through an observation.
TIMELINE_SQL = """
WITH timeline AS (
    SELECT 'post' AS item_type, p.tweet_id, p.post_type, p.author_x_user_id,
        p.reference_tweet_id, NULL AS origin_tweet_id, NULL AS reposter_x_user_id,
        p.text, p.posted_at, p.permalink, p.availability, p.capture_source,
        p.first_seen_at, p.updated_at,
        (SELECT MIN(observed_account_x_user_id) FROM archive_post_observations o
            WHERE o.tweet_id = p.tweet_id) AS observed_account_x_user_id,
        (SELECT COUNT(*) FROM media m WHERE m.owner_tweet_id = p.tweet_id) AS media_count
    FROM posts p
    UNION ALL
    SELECT 'repost', r.repost_tweet_id, 'repost', NULL, NULL, r.origin_tweet_id,
        r.reposter_x_user_id, '', r.reposted_at, r.permalink, r.availability, 'timeline',
        r.first_seen_at, r.updated_at,
        (SELECT MIN(observed_account_x_user_id) FROM archive_repost_observations o
            WHERE o.repost_tweet_id = r.repost_tweet_id),
        (SELECT COUNT(*) FROM media m WHERE m.owner_tweet_id = r.origin_tweet_id)
    FROM reposts r
)
"""

ACCOUNT_CONTENT_SQL = """WITH RECURSIVE content(tweet_id) AS (
    SELECT tweet_id FROM archive_post_observations WHERE observed_account_x_user_id = ?
    UNION
    SELECT r.origin_tweet_id FROM archive_repost_observations o
        JOIN reposts r USING (repost_tweet_id) WHERE o.observed_account_x_user_id = ?
    UNION
    SELECT p.reference_tweet_id FROM posts p JOIN content c USING (tweet_id)
        WHERE p.reference_tweet_id IS NOT NULL
)
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
    author_x_user_id: str
    post_type: str
    text: str
    posted_at: datetime
    permalink: str
    raw_payload: Mapping[str, Any] | None
    reference_tweet_id: str | None = None
    capture_source: str = "timeline"
    availability: str = "available"


@dataclass(frozen=True)
class RepostInput:
    repost_tweet_id: str
    origin_tweet_id: str
    reposter_x_user_id: str
    reposted_at: datetime
    permalink: str
    raw_payload: Mapping[str, Any] | None = None
    availability: str = "available"


@dataclass(frozen=True)
class ReferencePostInput:
    tweet_id: str
    author_x_user_id: str
    text: str
    posted_at: datetime
    permalink: str
    raw_payload: Mapping[str, Any] | None = None
    post_type: str = "original"
    availability: str = "unknown"


@dataclass(frozen=True)
class MediaInput:
    owner_tweet_id: str
    media_type: str
    source_url: str
    download_status: str = "pending"
    local_path: str | None = None
    sha256: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class MediaRecord:
    id: str
    owner_tweet_id: str
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
class TimelineItem:
    item_type: str
    tweet_id: str
    post_type: str
    author_x_user_id: str | None
    reference_tweet_id: str | None
    origin_tweet_id: str | None
    reposter_x_user_id: str | None
    text: str
    posted_at: str
    permalink: str
    availability: str
    capture_source: str
    first_seen_at: str
    updated_at: str
    observed_account_x_user_id: str | None
    media_count: int


@dataclass(frozen=True)
class IngestResult:
    observation_new: bool
    referenced_new: int
    reposts_new: int
    media_new: int
    content_ids: tuple[str, ...]


@dataclass(frozen=True)
class SyncRun:
    id: str
    observed_account_x_user_id: str
    username: str | None
    started_at: str
    finished_at: str | None
    posts_seen: int
    posts_new: int
    referenced_new: int
    reposts_new: int
    media_new: int
    status: str
    error: str | None


def initialize_storage(database_path: Path, archive_data_dir: Path, session_path: Path) -> None:
    """Initialize v3; old data must be migrated explicitly while writers are stopped."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    archive_data_dir.mkdir(parents=True, exist_ok=True)
    (session_path.parent if session_path.suffix else session_path).mkdir(
        parents=True, exist_ok=True
    )
    with _connect(database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "accounts" in tables or version not in (0, SCHEMA_VERSION):
            raise RuntimeError(
                f"Archive schema v{version} requires offline migration; "
                "run python -m archivex.migrate --help before starting services"
            )
        if version == 0 and tables - {"queue_tasks", "queue_attempts"}:
            raise RuntimeError("Unversioned archive schema; run the offline migration")
        connection.executescript(SCHEMA + TASK_SCHEMA)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


class ArchiveRepository:
    def __init__(self, database_path: Path, archive_data_dir: Path) -> None:
        self.database_path = database_path
        self.archive_data_dir = archive_data_dir

    def ensure_x_user(self, snapshot: UserSnapshot) -> None:
        with _connect(self.database_path) as c:
            _ensure_x_user(c, snapshot, _timestamp())

    def get_x_user(self, x_user_id: str) -> dict[str, Any] | None:
        with _connect(self.database_path) as c:
            row = c.execute("SELECT * FROM x_users WHERE x_user_id = ?", (x_user_id,)).fetchone()
        if row is None:
            return None
        return {
            "x_user_id": row["x_user_id"],
            "username": row["current_username"],
            "display_name": row["display_name"],
            "profile_image_url": _avatar(row["profile_image_url"]),
            "description": row["description"],
        }

    def upsert_account(
        self,
        x_user_id: str,
        current_username: str | None,
        display_name: str | None = None,
        status: str = "active",
    ) -> Account:
        now = _timestamp()
        with _connect(self.database_path) as c:
            _ensure_x_user(c, UserSnapshot(x_user_id, None, display_name), now)
            c.execute(
                """INSERT INTO observed_accounts
                (x_user_id, archive_enabled, status, created_at, updated_at) VALUES (?, 1, ?, ?, ?)
                ON CONFLICT(x_user_id) DO UPDATE SET archive_enabled=1,
                status=excluded.status, updated_at=excluded.updated_at""",
                (x_user_id, status, now, now),
            )
            if current_username:
                _observe_username(c, x_user_id, current_username, now)
            if display_name is not None:
                c.execute(
                    "UPDATE x_users SET display_name = ? WHERE x_user_id = ?",
                    (display_name, x_user_id),
                )
        return self.get_account(x_user_id)

    def observe_account_identity(
        self, x_user_id: str, username: str | None, display_name: str | None = None
    ) -> None:
        now = _timestamp()
        with _connect(self.database_path) as c:
            if display_name is not None:
                c.execute(
                    "UPDATE x_users SET display_name=?, updated_at=? WHERE x_user_id=?",
                    (display_name, now, x_user_id),
                )
            if username:
                _observe_username(c, x_user_id, username, now)

    def get_account(self, x_user_id: str) -> Account | None:
        with _connect(self.database_path) as c:
            row = c.execute(ACCOUNT_SELECT + " WHERE a.x_user_id = ?", (x_user_id,)).fetchone()
        return _account_from_row(row) if row else None

    def list_enabled_account_ids(self) -> list[str]:
        with _connect(self.database_path) as c:
            rows = c.execute(
                ACCOUNT_SELECT
                + " WHERE a.archive_enabled=1 ORDER BY COALESCE(u.current_username,a.x_user_id) COLLATE NOCASE"
            ).fetchall()
        return [r["x_user_id"] for r in rows]

    def set_account_enabled(self, x_user_id: str, enabled: bool) -> Account | None:
        with _connect(self.database_path) as c:
            c.execute(
                "UPDATE observed_accounts SET archive_enabled=?,status=?,updated_at=? WHERE x_user_id=?",
                (int(enabled), "active" if enabled else "paused", _timestamp(), x_user_id),
            )
        return self.get_account(x_user_id)

    def username_history(self, x_user_id: str) -> list[dict[str, Any]]:
        with _connect(self.database_path) as c:
            return [
                dict(r)
                for r in c.execute(
                    """SELECT * FROM observed_account_username_history
                WHERE x_user_id=? ORDER BY observed_from DESC,id DESC""",
                    (x_user_id,),
                )
            ]

    def list_accounts(self) -> list[dict[str, Any]]:
        with _connect(self.database_path) as c:
            ids = [
                r[0]
                for r in c.execute("""SELECT a.x_user_id FROM observed_accounts a JOIN x_users u USING(x_user_id)
                ORDER BY COALESCE(u.current_username,a.x_user_id) COLLATE NOCASE""")
            ]
        return [self.get_account_details(i) for i in ids]

    def get_account_details(self, x_user_id: str) -> dict[str, Any] | None:
        account = self.get_account(x_user_id)
        if account is None:
            return None
        with _connect(self.database_path) as c:
            count = c.execute(
                """SELECT
                (SELECT COUNT(*) FROM archive_post_observations WHERE observed_account_x_user_id=?) +
                (SELECT COUNT(*) FROM archive_repost_observations WHERE observed_account_x_user_id=?)""",
                (x_user_id, x_user_id),
            ).fetchone()[0]
        return {**account.__dict__, "post_count": count, **self._account_profile(x_user_id)}

    def list_posts(
        self,
        *,
        observed_account_x_user_id: str | None = None,
        query: str | None = None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        has_media: bool | None = None,
        post_type: str | None = None,
        exclude_post_type: str | None = None,
        search_origin: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TimelineItem]:
        clauses = ["t.observed_account_x_user_id IS NOT NULL"]
        params: list[Any] = []
        if observed_account_x_user_id is not None:
            clauses.append("""((t.item_type='post' AND EXISTS (SELECT 1 FROM archive_post_observations o
                WHERE o.tweet_id=t.tweet_id AND o.observed_account_x_user_id=?)) OR
                (t.item_type='repost' AND EXISTS (SELECT 1 FROM archive_repost_observations o
                WHERE o.repost_tweet_id=t.tweet_id AND o.observed_account_x_user_id=?)))""")
            params.extend([observed_account_x_user_id] * 2)
        if query:
            search = "t.text LIKE ? ESCAPE '\\'"
            params.append(f"%{_escape_like(query)}%")
            if search_origin:
                search += " OR EXISTS (SELECT 1 FROM posts p WHERE p.tweet_id=t.origin_tweet_id AND p.text LIKE ? ESCAPE '\\')"
                params.append(f"%{_escape_like(query)}%")
            clauses.append("(" + search + ")")
        for value, operator in ((from_at, ">="), (to_at, "<=")):
            if value is not None:
                clauses.append(f"t.posted_at {operator} ?")
                params.append(_timestamp(value))
        if has_media is not None:
            # The media tab includes the content visible in a quote's reference.
            clauses.append(
                ("" if has_media else "NOT ")
                + """EXISTS (
                WITH RECURSIVE visible(id) AS (
                    SELECT COALESCE(t.origin_tweet_id,t.tweet_id)
                    UNION SELECT p.reference_tweet_id FROM posts p JOIN visible v ON p.tweet_id=v.id
                        WHERE p.post_type='quote' AND p.reference_tweet_id IS NOT NULL
                ) SELECT 1 FROM media m JOIN visible v ON m.owner_tweet_id=v.id)"""
            )
        for value, operator in ((post_type, "="), (exclude_post_type, "!=")):
            if value is not None:
                clauses.append(f"t.post_type {operator} ?")
                params.append(value)
        with _connect(self.database_path) as c:
            rows = c.execute(
                TIMELINE_SQL
                + "SELECT t.* FROM timeline t WHERE "
                + " AND ".join(clauses)
                + " ORDER BY t.posted_at DESC,t.tweet_id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        values = [dict(r) for r in rows]
        if observed_account_x_user_id is not None:
            for value in values:
                value["observed_account_x_user_id"] = observed_account_x_user_id
        return [TimelineItem(**v) for v in values]

    def get_post(self, tweet_id: str) -> TimelineItem | None:
        with _connect(self.database_path) as c:
            row = c.execute(
                TIMELINE_SQL + "SELECT * FROM timeline WHERE tweet_id=?", (tweet_id,)
            ).fetchone()
        return TimelineItem(**dict(row)) if row else None

    def upsert_post(self, post: PostInput) -> bool:
        with _connect(self.database_path) as c:
            return self._upsert_post(c, post, _timestamp())

    def _upsert_post(self, c: sqlite3.Connection, post: PostInput, now: str) -> bool:
        posted_at = _timestamp(post.posted_at)
        if post.post_type not in {"original", "reply", "quote"}:
            raise ValueError("content posts must be original, reply or quote; use upsert_repost")
        if c.execute("SELECT 1 FROM reposts WHERE repost_tweet_id=?", (post.tweet_id,)).fetchone():
            raise ValueError(f"tweet {post.tweet_id} is already a repost event")
        existing = c.execute("SELECT * FROM posts WHERE tweet_id=?", (post.tweet_id,)).fetchone()
        if post.reference_tweet_id:
            if not c.execute(
                "SELECT 1 FROM posts WHERE tweet_id=?", (post.reference_tweet_id,)
            ).fetchone():
                raise ValueError(f"missing reference: {post.reference_tweet_id}")
            cyclic = c.execute(
                """WITH RECURSIVE refs(id) AS (SELECT ? UNION
                SELECT p.reference_tweet_id FROM posts p JOIN refs r ON p.tweet_id=r.id
                WHERE p.reference_tweet_id IS NOT NULL) SELECT 1 FROM refs WHERE id=?""",
                (post.reference_tweet_id, post.tweet_id),
            ).fetchone()
            if cyclic:
                raise ValueError(f"cyclic reference for {post.tweet_id}")
        if (
            existing
            and existing["author_x_user_id"] not in {UNKNOWN_USER_ID, post.author_x_user_id}
            and post.author_x_user_id != UNKNOWN_USER_ID
        ):
            raise ValueError(
                f"tweet {post.tweet_id} already belongs to X user {existing['author_x_user_id']}"
            )
        _ensure_x_user(
            c, user_from_payload((post.raw_payload or {}).get("user"), post.author_x_user_id), now
        )
        quality = {"unknown": 0, "deleted_or_unavailable": 1, "partial": 2, "available": 3}
        if existing:
            old_rank = (quality[existing["availability"]], existing["capture_source"] == "timeline")
            new_rank = (quality[post.availability], post.capture_source == "timeline")
            if new_rank < old_rank:
                return False
        raw_path = (
            self._write_raw("posts", post.tweet_id, post.raw_payload)
            if post.raw_payload is not None
            else None
        )
        c.execute(
            """INSERT INTO posts (tweet_id,author_x_user_id,post_type,reference_tweet_id,text,
            posted_at,permalink,raw_json_path,capture_source,availability,first_seen_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tweet_id) DO UPDATE SET
            author_x_user_id=CASE WHEN excluded.author_x_user_id='unknown' THEN posts.author_x_user_id ELSE excluded.author_x_user_id END,
            post_type=excluded.post_type,reference_tweet_id=COALESCE(excluded.reference_tweet_id,posts.reference_tweet_id),
            text=excluded.text,posted_at=excluded.posted_at,permalink=excluded.permalink,
            raw_json_path=COALESCE(excluded.raw_json_path,posts.raw_json_path),
            capture_source=CASE WHEN posts.capture_source='timeline' THEN 'timeline' ELSE excluded.capture_source END,
            availability=excluded.availability,media_scanned_at=NULL,updated_at=excluded.updated_at""",
            (
                post.tweet_id,
                post.author_x_user_id,
                post.post_type,
                post.reference_tweet_id,
                post.text,
                posted_at,
                post.permalink,
                raw_path,
                post.capture_source,
                post.availability,
                now,
                now,
            ),
        )
        return existing is None

    def upsert_repost(self, repost: RepostInput) -> bool:
        with _connect(self.database_path) as c:
            return self._upsert_repost(c, repost, _timestamp())

    def _upsert_repost(self, c: sqlite3.Connection, repost: RepostInput, now: str) -> bool:
        reposted_at = _timestamp(repost.reposted_at)
        if not c.execute(
            "SELECT 1 FROM posts WHERE tweet_id=?", (repost.origin_tweet_id,)
        ).fetchone():
            raise ValueError(f"missing repost origin: {repost.origin_tweet_id}")
        if c.execute("SELECT 1 FROM posts WHERE tweet_id=?", (repost.repost_tweet_id,)).fetchone():
            raise ValueError(f"tweet {repost.repost_tweet_id} is already a content post")
        existing = c.execute(
            "SELECT * FROM reposts WHERE repost_tweet_id=?", (repost.repost_tweet_id,)
        ).fetchone()
        if existing and (
            existing["origin_tweet_id"] != repost.origin_tweet_id
            or existing["reposter_x_user_id"] != repost.reposter_x_user_id
        ):
            raise ValueError(f"repost {repost.repost_tweet_id} has conflicting identity")
        _ensure_x_user(
            c,
            user_from_payload((repost.raw_payload or {}).get("user"), repost.reposter_x_user_id),
            now,
        )
        raw_path = (
            self._write_raw("reposts", repost.repost_tweet_id, repost.raw_payload)
            if repost.raw_payload is not None
            else None
        )
        c.execute(
            """INSERT INTO reposts (repost_tweet_id,origin_tweet_id,reposter_x_user_id,reposted_at,
            permalink,raw_json_path,availability,first_seen_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(repost_tweet_id) DO UPDATE SET reposted_at=excluded.reposted_at,
            permalink=excluded.permalink,raw_json_path=COALESCE(excluded.raw_json_path,reposts.raw_json_path),
            availability=excluded.availability,updated_at=excluded.updated_at""",
            (
                repost.repost_tweet_id,
                repost.origin_tweet_id,
                repost.reposter_x_user_id,
                reposted_at,
                repost.permalink,
                raw_path,
                repost.availability,
                now,
                now,
            ),
        )
        return existing is None

    def ensure_reference_post(self, reference: ReferencePostInput) -> bool:
        """Create or complete an embedded reference without observing it."""
        return self.upsert_post(
            PostInput(
                reference.tweet_id,
                reference.author_x_user_id,
                reference.post_type,
                reference.text,
                reference.posted_at,
                reference.permalink,
                reference.raw_payload,
                None,
                "embedded_reference",
                reference.availability,
            )
        )

    def upsert_post_media(self, owner_tweet_id: str, media: SourceMedia) -> bool:
        return self.create_media_if_missing(
            MediaInput(owner_tweet_id, media.media_type, media.source_url)
        )

    def repost_directory(self, repost_tweet_id: str) -> Path:
        with _connect(self.database_path) as c:
            if not c.execute(
                "SELECT 1 FROM reposts WHERE repost_tweet_id=?", (repost_tweet_id,)
            ).fetchone():
                raise ValueError(f"unknown repost tweet ID: {repost_tweet_id}")
        path = self.archive_data_dir / "reposts" / _path_component(repost_tweet_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def observe_post(
        self, observed_account_x_user_id: str, tweet_id: str, observed_at: str | None = None
    ) -> bool:
        with _connect(self.database_path) as c:
            return _observe(
                c, "post", observed_account_x_user_id, tweet_id, observed_at or _timestamp()
            )

    def observe_repost(
        self, observed_account_x_user_id: str, repost_tweet_id: str, observed_at: str | None = None
    ) -> bool:
        with _connect(self.database_path) as c:
            return _observe(
                c,
                "repost",
                observed_account_x_user_id,
                repost_tweet_id,
                observed_at or _timestamp(),
            )

    def ingest_item(
        self, item: SourcePost, observed_account_x_user_id: str, *, observed_at: str | None = None
    ) -> IngestResult:
        """One transaction for content, owned media, event and observation."""
        if item.x_user_id != observed_account_x_user_id:
            raise ValueError(
                f"source returned X user {item.x_user_id} while synchronizing {observed_account_x_user_id}"
            )
        now = observed_at or _timestamp()
        content_ids: list[str] = []
        referenced_new = media_new = reposts_new = 0
        with _connect(self.database_path) as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute(
                "SELECT 1 FROM observed_accounts WHERE x_user_id=?", (observed_account_x_user_id,)
            ).fetchone():
                raise ValueError(f"unknown observed account: {observed_account_x_user_id}")

            def validate(post: SourcePost, ancestors: frozenset[str]) -> None:
                _path_component(post.tweet_id)
                _timestamp(post.posted_at)
                if post.tweet_id in ancestors or len(ancestors) >= 32:
                    raise ValueError("cyclic or excessively deep post reference")
                if post.availability not in {
                    "available",
                    "partial",
                    "unknown",
                    "deleted_or_unavailable",
                }:
                    raise ValueError("invalid post availability")
                table = "reposts" if post.post_type == "repost" else "posts"
                column = "repost_tweet_id" if table == "reposts" else "tweet_id"
                other, other_column = (
                    ("posts", "tweet_id") if table == "reposts" else ("reposts", "repost_tweet_id")
                )
                if c.execute(
                    f"SELECT 1 FROM {other} WHERE {other_column}=?", (post.tweet_id,)
                ).fetchone():
                    raise ValueError(f"conflicting entity type for {post.tweet_id}")
                old = c.execute(
                    f"SELECT * FROM {table} WHERE {column}=?", (post.tweet_id,)
                ).fetchone()
                author_field = "reposter_x_user_id" if table == "reposts" else "author_x_user_id"
                if (
                    old
                    and old[author_field] not in {UNKNOWN_USER_ID, post.x_user_id}
                    and post.x_user_id != UNKNOWN_USER_ID
                ):
                    raise ValueError(f"conflicting author for {post.tweet_id}")
                if post.post_type == "repost" and (
                    post.referenced is None or post.referenced.post_type == "repost"
                ):
                    raise ValueError("repost requires a content origin")
                if post.post_type not in {"original", "reply", "quote", "repost"}:
                    raise ValueError("invalid post type")
                if (
                    old
                    and table == "reposts"
                    and old["origin_tweet_id"] != post.referenced.tweet_id
                ):
                    raise ValueError("conflicting repost origin")
                if post.referenced:
                    validate(post.referenced, ancestors | {post.tweet_id})

            validate(item, frozenset())

            def content(post: SourcePost, embedded: bool, ancestors: frozenset[str]) -> None:
                nonlocal referenced_new, media_new
                if post.tweet_id in ancestors or len(ancestors) >= 32:
                    raise ValueError("cyclic or excessively deep post reference")
                if post.referenced:
                    content(post.referenced, True, ancestors | {post.tweet_id})
                _ensure_x_user(c, post.author, now)
                created = self._upsert_post(
                    c,
                    PostInput(
                        post.tweet_id,
                        post.x_user_id,
                        post.post_type,
                        post.text,
                        post.posted_at,
                        post.permalink,
                        post.raw_payload,
                        post.referenced.tweet_id if post.referenced else None,
                        "embedded_reference" if embedded else "timeline",
                        post.availability,
                    ),
                    now,
                )
                referenced_new += int(created and embedded)
                content_ids.append(post.tweet_id)
                for media in post.own_media:
                    media_new += self._create_media(
                        c, MediaInput(post.tweet_id, media.media_type, media.source_url), now
                    )
                if post.raw_payload is not None:
                    c.execute(
                        "UPDATE posts SET media_scanned_at=? WHERE tweet_id=?", (now, post.tweet_id)
                    )

            if item.post_type == "repost":
                if item.referenced is None:
                    raise ValueError(f"repost {item.tweet_id} has no origin")
                content(item.referenced, True, frozenset({item.tweet_id}))
                _ensure_x_user(c, item.author, now)
                reposts_new = int(
                    self._upsert_repost(
                        c,
                        RepostInput(
                            item.tweet_id,
                            item.referenced.tweet_id,
                            item.x_user_id,
                            item.posted_at,
                            item.permalink,
                            item.raw_payload,
                            item.availability,
                        ),
                        now,
                    )
                )
                new = _observe(c, "repost", observed_account_x_user_id, item.tweet_id, now)
            else:
                content(item, False, frozenset())
                new = _observe(c, "post", observed_account_x_user_id, item.tweet_id, now)
        return IngestResult(
            new, referenced_new, reposts_new, media_new, tuple(dict.fromkeys(content_ids))
        )

    def post_media(self, tweet_id: str) -> list[ArchiveMedia]:
        with _connect(self.database_path) as c:
            rows = c.execute(
                """SELECT id,media_type,local_path,download_status,sha256,error FROM media
                WHERE owner_tweet_id=? ORDER BY created_at,id""",
                (tweet_id,),
            ).fetchall()
        return [ArchiveMedia(**dict(r)) for r in rows]

    def get_media(self, media_id: str) -> ArchiveMedia | None:
        with _connect(self.database_path) as c:
            row = c.execute(
                "SELECT id,media_type,local_path,download_status,sha256,error FROM media WHERE id=?",
                (media_id,),
            ).fetchone()
        return ArchiveMedia(**dict(row)) if row else None

    def create_media_if_missing(self, media: MediaInput) -> bool:
        with _connect(self.database_path) as c:
            return self._create_media(c, media, _timestamp())

    @staticmethod
    def _create_media(c: sqlite3.Connection, media: MediaInput, now: str) -> bool:
        return (
            c.execute(
                """INSERT INTO media (id,owner_tweet_id,media_type,source_url,local_path,download_status,
            sha256,error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(owner_tweet_id,source_url) DO NOTHING""",
                (
                    str(uuid.uuid4()),
                    media.owner_tweet_id,
                    media.media_type,
                    media.source_url,
                    media.local_path,
                    media.download_status,
                    media.sha256,
                    media.error,
                    now,
                    now,
                ),
            ).rowcount
            == 1
        )

    def upsert_media(self, media: MediaInput) -> str:
        now = _timestamp()
        with _connect(self.database_path) as c:
            self._create_media(c, media, now)
            c.execute(
                """UPDATE media SET media_type=?,local_path=?,download_status=?,sha256=?,error=?,updated_at=?
                WHERE owner_tweet_id=? AND source_url=?""",
                (
                    media.media_type,
                    media.local_path,
                    media.download_status,
                    media.sha256,
                    media.error,
                    now,
                    media.owner_tweet_id,
                    media.source_url,
                ),
            )
            return c.execute(
                "SELECT id FROM media WHERE owner_tweet_id=? AND source_url=?",
                (media.owner_tweet_id, media.source_url),
            ).fetchone()[0]

    def media_to_download(self, tweet_id: str) -> list[MediaRecord]:
        with _connect(self.database_path) as c:
            return [
                MediaRecord(**dict(r))
                for r in c.execute(
                    """SELECT id,owner_tweet_id,source_url,download_status
                FROM media WHERE owner_tweet_id=? AND download_status IN ('pending','failed') ORDER BY created_at,id""",
                    (tweet_id,),
                )
            ]

    def get_media_record(self, media_id: str) -> MediaRecord | None:
        with _connect(self.database_path) as c:
            row = c.execute(
                "SELECT id,owner_tweet_id,source_url,download_status FROM media WHERE id=?",
                (media_id,),
            ).fetchone()
        return MediaRecord(**dict(row)) if row else None

    def media_ids_to_download(self, observed_account_x_user_id: str) -> list[str]:
        with _connect(self.database_path) as c:
            return [
                r[0]
                for r in c.execute(
                    ACCOUNT_CONTENT_SQL
                    + """SELECT m.id FROM media m
                JOIN content c ON c.tweet_id=m.owner_tweet_id WHERE m.download_status='pending'
                ORDER BY m.created_at,m.id""",
                    (observed_account_x_user_id,) * 2,
                )
            ]

    def failed_media_post_ids(self, observed_account_x_user_id: str) -> list[str]:
        with _connect(self.database_path) as c:
            return [
                r[0]
                for r in c.execute(
                    ACCOUNT_CONTENT_SQL
                    + """SELECT DISTINCT m.owner_tweet_id FROM media m
                JOIN content c ON c.tweet_id=m.owner_tweet_id WHERE m.download_status='failed'
                ORDER BY m.owner_tweet_id""",
                    (observed_account_x_user_id,) * 2,
                )
            ]

    def post_directory(self, tweet_id: str) -> Path:
        with _connect(self.database_path) as c:
            if not c.execute("SELECT 1 FROM posts WHERE tweet_id=?", (tweet_id,)).fetchone():
                raise ValueError(f"unknown content tweet ID: {tweet_id}")
        path = self.archive_data_dir / "posts" / _path_component(tweet_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def complete_media(self, media_id: str, local_path: Path, sha256: str) -> None:
        relative = local_path.resolve().relative_to(self.archive_data_dir.resolve()).as_posix()
        with _connect(self.database_path) as c:
            c.execute(
                "UPDATE media SET local_path=?,sha256=?,download_status='completed',error=NULL,updated_at=? WHERE id=?",
                (relative, sha256, _timestamp(), media_id),
            )

    def fail_media(self, media_id: str, error: str) -> None:
        with _connect(self.database_path) as c:
            c.execute(
                "UPDATE media SET download_status='failed',error=?,updated_at=? WHERE id=?",
                (error, _timestamp(), media_id),
            )

    def post_metrics(self, tweet_id: str) -> dict[str, int | None]:
        payload = self._post_payload(tweet_id)
        return {
            key: _optional_int(payload.get(field))
            for key, field in (
                ("reply_count", "replyCount"),
                ("repost_count", "retweetCount"),
                ("like_count", "likeCount"),
                ("view_count", "viewCount"),
            )
        }

    def post_presentation(self, tweet_id: str) -> dict[str, Any]:
        """Read annotations for a content post; never infer relationships here."""
        payload = self._post_payload(tweet_id)
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        reply_user = payload.get("inReplyToUser")
        reply_user = reply_user if isinstance(reply_user, dict) else {}
        post = self.get_post(tweet_id)
        body = payload.get("rawContent")
        return {
            "display_text": _display_text(
                body if isinstance(body, str) else post.text if post else "",
                payload.get("displayTextRange"),
            ),
            "author_verified": bool(user.get("verified") or user.get("blue")),
            "reply_to_username": payload.get("inReplyToScreenName") or reply_user.get("username"),
            "language": payload.get("lang"),
            "is_translatable": bool(payload.get("isTranslatable")),
            "is_ai_generated": bool(payload.get("isAiGenerated") or payload.get("isAIGenerated")),
        }

    def list_sync_runs(
        self, *, observed_account_x_user_id: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[SyncRun]:
        where = (
            "WHERE r.observed_account_x_user_id=?" if observed_account_x_user_id is not None else ""
        )
        params = (observed_account_x_user_id,) if observed_account_x_user_id is not None else ()
        with _connect(self.database_path) as c:
            rows = c.execute(
                f"""SELECT r.*,u.current_username AS username FROM sync_runs r
                JOIN x_users u ON u.x_user_id=r.observed_account_x_user_id {where}
                ORDER BY r.started_at DESC LIMIT ? OFFSET ?""",
                (*params, limit, offset),
            ).fetchall()
        return [SyncRun(**dict(r)) for r in rows]

    def start_sync_run(self, observed_account_x_user_id: str) -> str:
        run_id = str(uuid.uuid4())
        with _connect(self.database_path) as c:
            c.execute(
                "INSERT INTO sync_runs (id,observed_account_x_user_id,started_at,status) VALUES (?,?,?,'running')",
                (run_id, observed_account_x_user_id, _timestamp()),
            )
        return run_id

    def finish_sync_run(
        self,
        run_id: str,
        *,
        posts_seen: int,
        posts_new: int,
        media_new: int,
        status: str,
        error: str | None = None,
        referenced_new: int = 0,
        reposts_new: int = 0,
    ) -> None:
        # posts_new counts new observations, not newly inserted content rows.
        with _connect(self.database_path) as c:
            c.execute(
                """UPDATE sync_runs SET finished_at=?,posts_seen=?,posts_new=?,media_new=?,
                status=?,error=?,referenced_new=?,reposts_new=? WHERE id=?""",
                (
                    _timestamp(),
                    posts_seen,
                    posts_new,
                    media_new,
                    status,
                    error,
                    referenced_new,
                    reposts_new,
                    run_id,
                ),
            )

    def interrupt_running_sync_runs(
        self,
        error: str = "process stopped before synchronization completed",
        *,
        observed_account_x_user_id: str | None = None,
    ) -> int:
        where = (
            " AND observed_account_x_user_id=?" if observed_account_x_user_id is not None else ""
        )
        params = (observed_account_x_user_id,) if observed_account_x_user_id is not None else ()
        with _connect(self.database_path) as c:
            return c.execute(
                "UPDATE sync_runs SET finished_at=?,status='interrupted',error=? WHERE status='running' AND finished_at IS NULL"
                + where,
                (_timestamp(), error, *params),
            ).rowcount

    def mark_account_sync_success(self, x_user_id: str, completed_at: datetime) -> None:
        now = _timestamp(completed_at)
        with _connect(self.database_path) as c:
            c.execute(
                """UPDATE observed_accounts SET status=CASE WHEN archive_enabled=1 THEN 'active' ELSE 'paused' END,
                last_sync_at=?,last_error=NULL,updated_at=? WHERE x_user_id=?""",
                (now, now, x_user_id),
            )

    def mark_account_sync_error(self, x_user_id: str, error: str) -> None:
        with _connect(self.database_path) as c:
            c.execute(
                "UPDATE observed_accounts SET status='error',last_error=?,updated_at=? WHERE x_user_id=?",
                (error, _timestamp(), x_user_id),
            )

    def _write_raw(self, kind: str, tweet_id: str, payload: Mapping[str, Any]) -> str:
        relative = (
            Path(kind)
            / _path_component(tweet_id)
            / ("repost.json" if kind == "reposts" else "post.json")
        )
        _atomic_json(self.archive_data_dir / relative, payload)
        return relative.as_posix()

    def _post_payload(self, tweet_id: str) -> Mapping[str, Any]:
        with _connect(self.database_path) as c:
            row = c.execute(
                "SELECT raw_json_path FROM posts WHERE tweet_id=?", (tweet_id,)
            ).fetchone()
        return _read_payload(self.archive_data_dir / row[0]) if row and row[0] else {}

    def _account_profile(self, x_user_id: str) -> dict[str, Any]:
        with _connect(self.database_path) as c:
            row = c.execute(
                """SELECT raw_json_path FROM (
                SELECT raw_json_path,posted_at FROM posts WHERE author_x_user_id=? AND raw_json_path IS NOT NULL
                UNION ALL SELECT raw_json_path,reposted_at FROM reposts WHERE reposter_x_user_id=? AND raw_json_path IS NOT NULL
                ) ORDER BY posted_at DESC LIMIT 1""",
                (x_user_id, x_user_id),
            ).fetchone()
        payload = _read_payload(self.archive_data_dir / row[0]) if row else {}
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        identity = self.get_x_user(x_user_id) or {}
        banner = (user.get("profileBannerUrl") or "").strip().rstrip("/")
        return {
            "description": identity.get("description")
            or user.get("rawDescription")
            or user.get("description")
            or None,
            "location": user.get("location") or None,
            "profile_image_url": identity.get("profile_image_url")
            or _avatar(user.get("profileImageUrl")),
            "profile_banner_url": (banner if banner.endswith("1500x500") else f"{banner}/1500x500")
            if banner
            else None,
            "verified": bool(user.get("blue")),
            "followers_count": _optional_int(user.get("followersCount")),
            "following_count": _optional_int(user.get("friendsCount")),
            "joined_at": user.get("created") or None,
        }


def _ensure_x_user(c: sqlite3.Connection, user: UserSnapshot, now: str) -> None:
    if user.x_user_id == UNKNOWN_USER_ID:
        user = UserSnapshot(UNKNOWN_USER_ID)
    c.execute(
        """INSERT INTO x_users (x_user_id,current_username,display_name,profile_image_url,description,first_seen_at,updated_at)
        VALUES (?,?,?,?,?,?,?) ON CONFLICT(x_user_id) DO UPDATE SET
        current_username=CASE WHEN EXISTS (SELECT 1 FROM observed_accounts WHERE x_user_id=excluded.x_user_id)
            THEN x_users.current_username ELSE COALESCE(excluded.current_username,x_users.current_username) END,
        display_name=COALESCE(excluded.display_name,x_users.display_name),
        profile_image_url=COALESCE(excluded.profile_image_url,x_users.profile_image_url),
        description=COALESCE(excluded.description,x_users.description),updated_at=excluded.updated_at""",
        (
            user.x_user_id,
            user.username,
            user.display_name,
            user.profile_image_url,
            user.description,
            now,
            now,
        ),
    )


def _observe(c: sqlite3.Connection, kind: str, account: str, tweet_id: str, now: str) -> bool:
    table = "archive_repost_observations" if kind == "repost" else "archive_post_observations"
    column = "repost_tweet_id" if kind == "repost" else "tweet_id"
    existed = c.execute(
        f"SELECT 1 FROM {table} WHERE observed_account_x_user_id=? AND {column}=?",
        (account, tweet_id),
    ).fetchone()
    c.execute(
        f"""INSERT INTO {table} (observed_account_x_user_id,{column},first_observed_at,last_observed_at)
        VALUES (?,?,?,?) ON CONFLICT(observed_account_x_user_id,{column}) DO UPDATE SET
        first_observed_at=MIN(first_observed_at,excluded.first_observed_at),
        last_observed_at=MAX(last_observed_at,excluded.last_observed_at)""",
        (account, tweet_id, now, now),
    )
    return existed is None


def _observe_username(c: sqlite3.Connection, x_user_id: str, username: str, now: str) -> None:
    normalized = username.strip().lstrip("@")
    if not normalized:
        return
    if not c.execute("SELECT 1 FROM observed_accounts WHERE x_user_id=?", (x_user_id,)).fetchone():
        raise ValueError(f"unknown observed X user ID: {x_user_id}")
    row = c.execute(
        "SELECT username FROM observed_account_username_history WHERE x_user_id=? AND observed_to IS NULL",
        (x_user_id,),
    ).fetchone()
    if row and row[0].casefold() == normalized.casefold():
        c.execute(
            "UPDATE observed_account_username_history SET username=?,last_observed_at=? WHERE x_user_id=? AND observed_to IS NULL",
            (normalized, now, x_user_id),
        )
    else:
        c.execute(
            "UPDATE observed_account_username_history SET observed_to=?,last_observed_at=? WHERE x_user_id=? AND observed_to IS NULL",
            (now, now, x_user_id),
        )
        c.execute(
            "INSERT INTO observed_account_username_history (x_user_id,username,observed_from,last_observed_at) VALUES (?,?,?,?)",
            (x_user_id, normalized, now, now),
        )
    c.execute(
        "UPDATE x_users SET current_username=?,updated_at=? WHERE x_user_id=?",
        (normalized, now, x_user_id),
    )


def _account_from_row(row: sqlite3.Row) -> Account:
    values = dict(row)
    values["archive_enabled"] = bool(values["archive_enabled"])
    return Account(**values)


@contextmanager
def _connect(database_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _timestamp(value: datetime | None = None) -> str:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("posted_at must include a timezone")
    return value.astimezone(UTC).isoformat()


def _path_component(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("ID must contain only filename-safe characters")
    return value


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as f:
            temporary_path = Path(f.name)
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)


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


def _avatar(value: Any) -> str | None:
    return (
        (value.strip().replace("_normal.", "_400x400.") or None) if isinstance(value, str) else None
    )


def _display_text(value: Any, display_range: Any = None) -> str:
    if not isinstance(value, str):
        return ""
    if (
        isinstance(display_range, list)
        and len(display_range) == 2
        and all(isinstance(i, int) for i in display_range)
    ):
        start, end = display_range
        if 0 <= start <= end <= len(value):
            value = value[start:end]
    return re.sub(r"(?:\s*https://t\.co/[A-Za-z0-9]+)+\s*$", "", value).strip()
