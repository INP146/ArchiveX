import json
import sqlite3
from datetime import UTC, datetime

from archivex.storage import ArchiveRepository, MediaInput, PostInput, initialize_storage


def _repository(tmp_path):
    database_path = tmp_path / "archive.sqlite3"
    archive_data_dir = tmp_path / "archive"
    initialize_storage(database_path, archive_data_dir, tmp_path / "sessions")
    return ArchiveRepository(database_path, archive_data_dir), database_path, archive_data_dir


def test_post_upsert_updates_data_without_duplication(tmp_path) -> None:
    repository, database_path, archive_data_dir = _repository(tmp_path)
    account = repository.upsert_account("42", "example", "Example")
    common = dict(
        tweet_id="100",
        account_x_user_id=account.x_user_id,
        post_type="original",
        posted_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
        permalink="https://x.com/example/status/100",
    )

    assert repository.upsert_post(PostInput(**common, text="first", raw_payload={"text": "first"}))
    assert not repository.upsert_post(
        PostInput(**common, text="updated", raw_payload={"text": "updated"})
    )

    with sqlite3.connect(database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        text, raw_path, first_seen_at = connection.execute(
            "SELECT text, raw_json_path, first_seen_at FROM posts WHERE tweet_id = '100'"
        ).fetchone()
    assert count == 1
    assert text == "updated"
    assert first_seen_at
    assert raw_path == "accounts/42/posts/2026/08/100/post.json"
    assert json.loads((archive_data_dir / raw_path).read_text()) == {"text": "updated"}


def test_media_and_sync_runs_are_persisted(tmp_path) -> None:
    repository, database_path, _ = _repository(tmp_path)
    account = repository.upsert_account("42", "example")
    repository.upsert_post(PostInput(
        "100", account.x_user_id, "original", "post", datetime(2026, 8, 5, tzinfo=UTC),
        "https://x.com/example/status/100", {},
    ))
    first_id = repository.upsert_media(MediaInput("100", "image", "https://example.test/media.jpg"))
    second_id = repository.upsert_media(
        MediaInput("100", "image", "https://example.test/media.jpg", download_status="completed")
    )
    run_id = repository.start_sync_run(account.x_user_id)
    repository.finish_sync_run(
        run_id, posts_seen=1, posts_new=1, media_new=1, status="success"
    )

    with sqlite3.connect(database_path) as connection:
        media_count = connection.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        media_status = connection.execute("SELECT download_status FROM media").fetchone()[0]
        run = connection.execute(
            "SELECT status, finished_at, posts_new FROM sync_runs WHERE id = ?", (run_id,)
        ).fetchone()
    assert first_id == second_id
    assert media_count == 1
    assert media_status == "completed"
    assert run[0] == "success"
    assert run[1]
    assert run[2] == 1


def test_reply_presentation_includes_target_account(tmp_path) -> None:
    repository, _, _ = _repository(tmp_path)
    account = repository.upsert_account("42", "example")
    repository.upsert_post(PostInput(
        "101", account.x_user_id, "reply", "Yup", datetime(2026, 8, 5, tzinfo=UTC),
        "https://x.com/example/status/101",
        {
            "rawContent": "@Rothmus Yup",
            "displayTextRange": [9, 12],
            "inReplyToScreenName": "Rothmus",
            "user": {"displayname": "Example", "username": "example"},
        },
    ))

    presentation = repository.post_presentation("101")

    assert presentation["reply_to_username"] == "Rothmus"
    assert presentation["display_text"] == "Yup"


def test_username_history_allows_reuse_by_different_x_user_ids(tmp_path) -> None:
    repository, _, _ = _repository(tmp_path)
    repository.upsert_account("1", "alice", "First Alice")
    repository.observe_account_identity("1", "alice_new")
    repository.upsert_account("2", "alice", "Second Alice")

    first_history = repository.username_history("1")
    second_history = repository.username_history("2")

    assert [item["username"] for item in first_history] == ["alice_new", "alice"]
    assert first_history[0]["observed_to"] is None
    assert first_history[1]["observed_to"] is not None
    assert [item["username"] for item in second_history] == ["alice"]
    assert repository.get_account("1").current_username == "alice_new"
    assert repository.get_account("2").current_username == "alice"


def test_legacy_schema_and_username_paths_are_migrated(tmp_path) -> None:
    database_path = tmp_path / "archive.sqlite3"
    archive_data_dir = tmp_path / "archive"
    old_post_dir = archive_data_dir / "accounts/alice/posts/2026/08/100"
    old_post_dir.mkdir(parents=True)
    (old_post_dir / "post.json").write_text('{"id":"100"}\n')
    (old_post_dir / "image.jpg").write_bytes(b"image")
    _create_legacy_database(database_path)

    initialize_storage(database_path, archive_data_dir, tmp_path / "sessions")

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        account_columns = {
            row["name"]: row for row in connection.execute("PRAGMA table_info(accounts)")
        }
        post = connection.execute(
            "SELECT account_x_user_id, raw_json_path FROM posts WHERE tweet_id = '100'"
        ).fetchone()
        media_path = connection.execute(
            "SELECT local_path FROM media WHERE tweet_id = '100'"
        ).fetchone()[0]
        history = connection.execute(
            "SELECT x_user_id, username FROM account_username_history"
        ).fetchone()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    assert "id" not in account_columns
    assert account_columns["x_user_id"]["pk"] == 1
    assert dict(post) == {
        "account_x_user_id": "42",
        "raw_json_path": "accounts/42/posts/2026/08/100/post.json",
    }
    assert media_path == "accounts/42/posts/2026/08/100/image.jpg"
    assert tuple(history) == ("42", "alice")
    assert (archive_data_dir / post["raw_json_path"]).is_file()
    assert (archive_data_dir / media_path).read_bytes() == b"image"


def _create_legacy_database(database_path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.executescript("""
            CREATE TABLE accounts (
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
            CREATE TABLE posts (
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
            CREATE TABLE media (
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
            CREATE TABLE sync_runs (
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
            INSERT INTO accounts VALUES (
                7, '42', 'alice', 'Alice', 'active', NULL, NULL,
                '2026-08-01T00:00:00+00:00', '2026-08-05T00:00:00+00:00'
            );
            INSERT INTO posts VALUES (
                '100', 7, 'original', 'post', '2026-08-05T12:00:00+00:00',
                'https://x.com/alice/status/100',
                'accounts/alice/posts/2026/08/100/post.json', NULL,
                '2026-08-05T12:00:00+00:00', '2026-08-05T12:00:00+00:00'
            );
            INSERT INTO media VALUES (
                'media-1', '100', 'image', 'https://example.test/image.jpg',
                'accounts/alice/posts/2026/08/100/image.jpg', 'completed', 'hash', NULL,
                '2026-08-05T12:00:00+00:00', '2026-08-05T12:00:00+00:00'
            );
            INSERT INTO sync_runs VALUES (
                'run-1', 7, '2026-08-05T12:00:00+00:00',
                '2026-08-05T12:01:00+00:00', 1, 1, 1, 'success', NULL
            );
        """)
