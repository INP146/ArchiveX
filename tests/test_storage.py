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
    common = dict(tweet_id="100", account_id=account.id, username=account.username,
                  post_type="original", posted_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
                  permalink="https://x.com/example/status/100")

    assert repository.upsert_post(PostInput(**common, text="first", raw_payload={"text": "first"}))
    assert not repository.upsert_post(PostInput(**common, text="updated", raw_payload={"text": "updated"}))

    with sqlite3.connect(database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        text, raw_path, first_seen_at = connection.execute(
            "SELECT text, raw_json_path, first_seen_at FROM posts WHERE tweet_id = '100'"
        ).fetchone()
    assert count == 1
    assert text == "updated"
    assert first_seen_at
    assert raw_path == "accounts/example/posts/2026/08/100/post.json"
    assert json.loads((archive_data_dir / raw_path).read_text()) == {"text": "updated"}


def test_media_and_sync_runs_are_persisted(tmp_path) -> None:
    repository, database_path, _ = _repository(tmp_path)
    account = repository.upsert_account("42", "example")
    repository.upsert_post(PostInput("100", account.id, account.username, "original", "post",
                                     datetime(2026, 8, 5, tzinfo=UTC), "https://x.com/example/status/100", {}))
    first_id = repository.upsert_media(MediaInput("100", "image", "https://example.test/media.jpg"))
    second_id = repository.upsert_media(
        MediaInput("100", "image", "https://example.test/media.jpg", download_status="completed")
    )
    run_id = repository.start_sync_run(account.id)
    repository.finish_sync_run(run_id, posts_seen=1, posts_new=1, media_new=1, status="success")

    with sqlite3.connect(database_path) as connection:
        media_count = connection.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        media_status = connection.execute("SELECT download_status FROM media").fetchone()[0]
        run = connection.execute("SELECT status, finished_at, posts_new FROM sync_runs WHERE id = ?", (run_id,)).fetchone()
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
        "101", account.id, account.username, "reply", "Yup",
        datetime(2026, 8, 5, tzinfo=UTC), "https://x.com/example/status/101",
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
