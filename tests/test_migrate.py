import hashlib
import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import archivex.migrate as migration
from archivex.migrate import migrate_archive, resume_migration, validate_archive, MigrationError
from archivex.task_center import TaskCenterRepository

NOW = "2026-08-01T00:00:00+00:00"
LATER = "2026-09-01T00:00:00+00:00"


def payload(identifier, author="1", text="outer", media=None, **extra):
    return {
        "id": identifier,
        "user": {"id": author, "username": f"user{author}"},
        "rawContent": text,
        "date": NOW,
        "url": f"https://x.com/user{author}/status/{identifier}",
        "media": {"photos": [{"url": url} for url in media or []]},
        **extra,
    }


def insert(c, table, row):
    c.execute(
        f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
        tuple(row.values()),
    )


def legacy(tmp_path, *, bad_origin=False, bad_media=False):
    db, archive = tmp_path / "archive.sqlite3", tmp_path / "archive"
    archive.mkdir()
    with sqlite3.connect(db) as c:
        c.executescript((Path(__file__).parent / "fixtures/archive_v2.sql").read_text())
        for identifier in ["1", "3"]:
            insert(
                c,
                "accounts",
                dict(
                    x_user_id=identifier,
                    current_username=f"current{identifier}",
                    display_name="Latest",
                    archive_enabled=0 if identifier == "3" else 1,
                    status="paused" if identifier == "3" else "active",
                    last_sync_at=LATER,
                    last_error=None,
                    created_at=NOW,
                    updated_at=LATER,
                ),
            )
            insert(
                c,
                "account_username_history",
                dict(
                    x_user_id=identifier,
                    username=f"current{identifier}",
                    observed_from=NOW,
                    observed_to=None,
                    last_observed_at=LATER,
                ),
            )
        original = payload("10", "2", "original", ["https://media.test/origin.jpg"])
        outer = payload(
            "20", text="comment", media=["https://media.test/outer.jpg"], quotedTweet=original
        )
        post_rows = [
            (outer, "quote"),
            (payload("30", retweetedTweet=original), "repost"),
            (payload("31", retweetedTweet=None if bad_origin else original), "repost"),
            (
                payload(
                    "40", inReplyToTweetId="50", inReplyToUser={"id": "4", "username": "user4"}
                ),
                "reply",
            ),
        ]
        for raw, kind in post_rows:
            identifier = raw["id"]
            relative = f"accounts/1/posts/2026/08/{identifier}/post.json"
            path = archive / relative
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(raw))
            insert(
                c,
                "posts",
                dict(
                    tweet_id=identifier,
                    account_x_user_id="1",
                    post_type=kind,
                    text=raw["rawContent"],
                    posted_at=NOW,
                    permalink=raw["url"],
                    raw_json_path=relative,
                    media_scanned_at=LATER,
                    first_seen_at=NOW,
                    updated_at=LATER,
                ),
            )
        for identifier, tweet_id, url, completed in [
            ("failed", "30", "origin", False),
            ("completed", "31", "origin", True),
            ("quote-ref", "20", "origin", True),
            ("quote-own", "20", "outer", True),
        ]:
            relative = (
                f"accounts/1/posts/2026/08/{tweet_id}/{identifier}.jpg" if completed else None
            )
            data = url.encode()
            if relative:
                (archive / relative).write_bytes(data)
            insert(
                c,
                "media",
                dict(
                    id=identifier,
                    tweet_id=tweet_id,
                    media_type="image",
                    source_url=f"https://media.test/{url}.jpg",
                    local_path=relative,
                    download_status="completed" if completed else "failed",
                    sha256=hashlib.sha256(data).hexdigest() if completed else None,
                    error=None if completed else "timeout",
                    created_at=NOW,
                    updated_at=LATER,
                ),
            )
        if bad_media:
            c.execute(
                "UPDATE media SET source_url='https://unknown.test/image' WHERE id='quote-ref'"
            )
        insert(
            c,
            "sync_runs",
            dict(
                id="run",
                account_x_user_id="1",
                started_at=NOW,
                finished_at=LATER,
                posts_seen=4,
                posts_new=4,
                media_new=4,
                status="success",
                error=None,
            ),
        )
        parent, child = uuid.uuid4().hex, uuid.uuid4().hex
        for identifier, name, media_id, parent_id in [
            (parent, "archivex.sync_account", None, None),
            (child, "archivex.download_media", "failed", parent),
        ]:
            labels = {"_archivex_account_x_user_id": "1", "_archivex_media_id": media_id}
            insert(
                c,
                "queue_tasks",
                dict(
                    id=identifier,
                    name=name,
                    status="failure",
                    worker="media",
                    account_x_user_id="1",
                    media_id=media_id,
                    parent_task_id=parent_id,
                    trigger="manual",
                    context=json.dumps({"account": {"x_user_id": "1"}, "media": {"id": media_id}}),
                    args=json.dumps([media_id or "1"]),
                    kwargs="{}",
                    labels=json.dumps(labels),
                    result="null",
                    error="timeout",
                    queued_at=NOW,
                    started_at=NOW,
                    finished_at=LATER,
                    current_attempt=2,
                    max_attempts=2,
                    retry_of=None,
                    created_at=NOW,
                    updated_at=LATER,
                ),
            )
            for attempt in [1, 2]:
                insert(
                    c,
                    "queue_attempts",
                    dict(
                        task_id=identifier,
                        attempt=attempt,
                        status="failure",
                        labels=json.dumps(labels),
                        error="timeout",
                        queued_at=NOW,
                        started_at=NOW,
                        finished_at=LATER,
                        updated_at=LATER,
                    ),
                )
    (archive / "untracked.txt").write_text("untracked file must survive")
    sessions = tmp_path / "twscrape"
    sessions.mkdir()
    with sqlite3.connect(sessions / "accounts.db") as c:
        c.execute("CREATE TABLE accounts (id TEXT)")
        c.execute("INSERT INTO accounts VALUES ('crawler')")
    return db, archive, parent, child


def test_complete_migration_preserves_files_history_and_remaps_duplicate_media_tasks(tmp_path):
    db, archive, parent, child = legacy(tmp_path)
    report = migrate_archive(db, archive, output_dir=tmp_path / "migration")
    assert report["status"] == "ready" and report["issues"] == []
    with sqlite3.connect(db) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 2
    assert (tmp_path / "migration/backup/twscrape/accounts.db").is_file()
    applied = resume_migration(tmp_path / "migration")
    assert applied["status"] == "applied_and_cleaned"
    assert validate_archive(db, archive)["counts"]["media"] == 2
    assert not (archive / "accounts").exists()
    assert (archive / "preserved/untracked.txt").read_text() == "untracked file must survive"
    assert len(list((archive / "posts").rglob("*.jpg"))) == 2
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM archive_post_observations").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM archive_repost_observations").fetchone()[0] == 2
        assert c.execute(
            'SELECT text,reference_tweet_id,first_seen_at,updated_at FROM posts WHERE tweet_id="20"'
        ).fetchone() == ("comment", "10", NOW, LATER)
        assert c.execute(
            'SELECT author_x_user_id,availability,raw_json_path FROM posts WHERE tweet_id="50"'
        ).fetchone() == ("4", "unknown", None)
        assert c.execute("SELECT COUNT(*) FROM observed_accounts").fetchone()[0] == 2
        assert (
            c.execute('SELECT current_username FROM x_users WHERE x_user_id="1"').fetchone()[0]
            == "current1"
        )
        assert (
            c.execute(
                'SELECT archive_enabled FROM observed_accounts WHERE x_user_id="3"'
            ).fetchone()[0]
            == 0
        )
        assert c.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 1
        assert not c.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '_archivex_v2_%'"
        ).fetchall()
        assert c.execute("SELECT COUNT(*) FROM queue_attempts").fetchone()[0] == 4
    mapping = report["media_mapping"]
    assert mapping["failed"] == mapping["completed"] == mapping["quote-ref"]
    media_id = mapping["failed"][0]
    lifecycle = TaskCenterRepository(db, 3600, "crawl")
    task = lifecycle.get_task(child)
    assert task["args"] == [media_id] and task["media_id"] == media_id
    assert task["context"]["media"]["owner_tweet_id"] == "10"
    assert task["context"]["post_author"]["x_user_id"] == "2"
    assert task["context"]["migration_context"]["media"]["id"] == "failed"
    assert task["parent_task_id"] == str(uuid.UUID(parent))
    assert task["attempts"][0]["labels"]["_archivex_media_id"] == "failed"
    assert task["status"] == "failure" and task["error"] == "timeout"
    assert migrate_archive(db, archive)["status"] == "already_current"
    assert resume_migration(tmp_path / "migration")["status"] == "applied_and_cleaned"


@pytest.mark.parametrize("fault", ["origin", "media", "missing_file", "corrupt_json"])
def test_unresolved_data_blocks_apply_and_preserves_originals(tmp_path, fault):
    db, archive, _, _ = legacy(tmp_path, bad_origin=fault == "origin", bad_media=fault == "media")
    if fault == "missing_file":
        next(archive.rglob("completed.jpg")).unlink()
    if fault == "corrupt_json":
        next(archive.rglob("post.json")).write_text("broken json")
    report = migrate_archive(db, archive, output_dir=tmp_path / "migration")
    assert report["status"] == "unresolved" and report["issues"]
    with pytest.raises(MigrationError, match="unresolved"):
        resume_migration(tmp_path / "migration")
    with sqlite3.connect(db) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 4
    with sqlite3.connect(tmp_path / "migration/candidate/archive.sqlite3") as c:
        assert c.execute("SELECT COUNT(*) FROM archive_migration_errors").fetchone()[0] > 0
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert (archive / "accounts").exists()


def test_installation_failure_can_resume_without_data_loss(tmp_path, monkeypatch):
    db, archive, _, _ = legacy(tmp_path)
    original = migration._snapshot_db

    def fail_install(source, target):
        if target == db.resolve():
            raise OSError("simulated interruption before database commit")
        return original(source, target)

    monkeypatch.setattr(migration, "_snapshot_db", fail_install)
    with pytest.raises(OSError, match="simulated"):
        migrate_archive(db, archive, output_dir=tmp_path / "migration", apply=True)
    with sqlite3.connect(db) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 2
    assert (archive / "accounts").exists()
    monkeypatch.setattr(migration, "_snapshot_db", original)
    assert resume_migration(tmp_path / "migration")["status"] == "applied_and_cleaned"
    validate_archive(db, archive)


def test_resume_rejects_a_structurally_valid_but_incomplete_candidate(tmp_path, monkeypatch):
    db, archive, _, _ = legacy(tmp_path)
    output = tmp_path / "migration"
    original_database = migration._database_state(db)
    original_files = migration._inventory(archive)

    def fail_build(*args, **kwargs):
        raise OSError("simulated I/O failure while migrating media")

    with monkeypatch.context() as patch:
        patch.setattr(migration, "_migrate_media", fail_build)
        with pytest.raises(OSError, match="simulated"):
            migrate_archive(db, archive, output_dir=output)

    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "building" and not report["issues"]
    # Integrity checks alone accept this database, despite missing history and
    # completed media. A failed build must never reach installation or cleanup.
    candidate = validate_archive(
        output / "candidate/archive.sqlite3", output / "candidate/archive"
    )
    assert candidate["counts"]["sync_runs"] == candidate["counts"]["queue_tasks"] == 0
    with pytest.raises(MigrationError, match="build is incomplete"):
        resume_migration(output)
    assert migration._database_state(db) == original_database
    assert migration._inventory(archive) == original_files


@pytest.mark.parametrize("status", ["queued", "in_progress", "retry_scheduled"])
def test_rehearsal_cannot_bypass_active_queue_checks_on_resume(tmp_path, status):
    db, archive, _, _ = legacy(tmp_path)
    with sqlite3.connect(db) as c:
        c.execute("UPDATE queue_tasks SET status=?", (status,))
    original_database = migration._database_state(db)
    original_files = migration._inventory(archive)
    output = tmp_path / "migration"
    assert migrate_archive(db, archive, output_dir=output)["active_tasks"] == 2

    with pytest.raises(MigrationError, match="active queue"):
        resume_migration(output)
    assert migration._database_state(db) == original_database
    assert migration._inventory(archive) == original_files


def test_failed_file_copy_never_publishes_partial_bytes(tmp_path, monkeypatch):
    db, archive, _, _ = legacy(tmp_path)
    output = tmp_path / "migration"
    migrate_archive(db, archive, output_dir=output)
    original_files = migration._inventory(archive)

    def fail_copy(source, destination):
        Path(destination).write_bytes(b"partial")
        raise OSError("simulated interruption during copy")

    with monkeypatch.context() as patch:
        patch.setattr(migration.shutil, "copy2", fail_copy)
        with pytest.raises(OSError, match="simulated"):
            resume_migration(output)
    assert migration._inventory(archive) == original_files
    assert resume_migration(output)["status"] == "applied_and_cleaned"
    assert validate_archive(db, archive)["counts"]["media"] == 2


@pytest.mark.parametrize("action", ["resume", "rollback"])
def test_killed_file_copy_can_resume_or_rollback(tmp_path, action):
    db, archive, _, _ = legacy(tmp_path)
    output = tmp_path / "migration"
    original_database = migration._database_state(db)
    original_files = migration._inventory(archive)
    migrate_archive(db, archive, output_dir=output)
    # os._exit skips finally blocks, reproducing an actual stopped process with
    # an unfinished copy on disk rather than just a catchable Python exception.
    killed = subprocess.run(
        [sys.executable, "-c", """
import os
import sys
from pathlib import Path
import archivex.migrate as migration

def stop_during_copy(source, destination):
    Path(destination).write_bytes(b'partial')
    os._exit(73)

migration.shutil.copy2 = stop_during_copy
migration.resume_migration(Path(sys.argv[1]))
""", str(output)],
        capture_output=True, text=True, timeout=15,
    )
    assert killed.returncode == 73, killed.stderr
    assert migration._database_state(db) == original_database
    new_files = migration._inventory(archive).keys() - original_files.keys()
    assert len(new_files) == 1
    assert all(relative.endswith(".tmp") for relative in new_files)

    if action == "resume":
        assert resume_migration(output)["status"] == "applied_and_cleaned"
        counts = validate_archive(db, archive)["counts"]
        assert (
            counts["media"], counts["sync_runs"], counts["queue_tasks"], counts["queue_attempts"]
        ) == (2, 1, 2, 4)
        assert not list(archive.rglob("*.tmp"))
    else:
        migration.rollback_migration(output)
        assert migration._database_state(db) == original_database
        assert migration._inventory(archive) == original_files


@pytest.mark.parametrize("action", ["resume", "rollback"])
def test_recovery_preserves_unrecognized_temporary_files(tmp_path, action):
    db, archive, _, _ = legacy(tmp_path)
    output = tmp_path / "migration"
    migrate_archive(db, archive, output_dir=output)
    unrelated = archive / ".archivex-unrelated.tmp"
    unrelated.write_bytes(b"new user data")
    recover = resume_migration if action == "resume" else migration.rollback_migration
    with pytest.raises(MigrationError, match="archive change"):
        recover(output)
    assert unrelated.read_bytes() == b"new user data"
    with sqlite3.connect(db) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 2


def test_future_schema_and_active_queue_refuse_mutation(tmp_path):
    db, archive, _, _ = legacy(tmp_path)
    with sqlite3.connect(db) as c:
        c.execute("UPDATE queue_tasks SET status='queued'")
    with pytest.raises(MigrationError, match="active queue"):
        migrate_archive(db, archive, apply=True, output_dir=tmp_path / "migration")
    assert not (tmp_path / "migration").exists()
    with sqlite3.connect(db) as c:
        c.execute("PRAGMA user_version=99")
    with pytest.raises(MigrationError, match="unsupported"):
        migrate_archive(db, archive)


def test_rollback_restores_v2_files_and_rejects_newer_writes(tmp_path):
    db, archive, _, _ = legacy(tmp_path)
    output = tmp_path / "migration"
    report = migrate_archive(db, archive, output_dir=output, apply=True)
    assert report["status"] == "applied_and_cleaned"
    migration.rollback_migration(output)
    with sqlite3.connect(db) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 4
    assert (archive / "accounts").is_dir() and not (archive / "posts").exists()
    assert (archive / "untracked.txt").is_file()
    resume_migration(output)
    with sqlite3.connect(db) as c:
        c.execute("UPDATE posts SET text='a newer snapshot' WHERE tweet_id='20'")
    with pytest.raises(MigrationError, match="newer data"):
        migration.rollback_migration(output)
