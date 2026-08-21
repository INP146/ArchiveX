import json
import os
import sqlite3

import pytest

from archivex.docker_state import STATE_MARKER, migrate_legacy_state


def _database(path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE records (value TEXT NOT NULL)")
        connection.execute("INSERT INTO records VALUES (?)", (value,))


def _value(path) -> str:
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT value FROM records").fetchone()[0]


def test_migrate_legacy_state_copies_databases_and_preserves_source(tmp_path) -> None:
    source = tmp_path / "legacy"
    target = tmp_path / "state"
    _database(source / "archive.sqlite3", "archive")
    _database(source / "twscrape/accounts.db", "session")
    (source / "archive.sqlite3-wal").write_bytes(b"not-copied")
    target.mkdir()
    (target / ".archivex-owner-10001").touch()
    (target / "archive").mkdir()
    (target / "twscrape").mkdir()

    result = migrate_legacy_state(source, target)

    assert result.status == "migrated"
    assert result.databases == ("archive.sqlite3", "twscrape/accounts.db")
    assert _value(target / "archive.sqlite3") == "archive"
    assert _value(target / "twscrape/accounts.db") == "session"
    assert (source / "archive.sqlite3-wal").read_bytes() == b"not-copied"
    assert not (target / "archive.sqlite3-wal").exists()
    assert not list(target.glob(".archive.sqlite3.migrating-*"))
    marker = json.loads((target / STATE_MARKER).read_text())
    assert marker["mode"] == "migrated"


def test_migrate_legacy_state_is_idempotent_and_validates_state(tmp_path) -> None:
    source = tmp_path / "legacy"
    target = tmp_path / "state"
    _database(source / "archive.sqlite3", "original")
    migrate_legacy_state(source, target)
    with sqlite3.connect(source / "archive.sqlite3") as connection:
        connection.execute("UPDATE records SET value = 'changed'")

    result = migrate_legacy_state(source, target)

    assert result.status == "already_migrated"
    assert _value(target / "archive.sqlite3") == "original"


def test_migrate_legacy_state_cleans_artifacts_from_interrupted_migration(tmp_path) -> None:
    source = tmp_path / "legacy"
    target = tmp_path / "state"
    _database(source / "archive.sqlite3", "original")
    migrate_legacy_state(source, target)
    stale = target / ".archive.sqlite3.migrating-old-wal"
    stale.write_bytes(b"stale")

    result = migrate_legacy_state(source, target)

    assert result.status == "already_migrated"
    assert not stale.exists()


def test_migrate_legacy_state_rejects_unmarked_target_files(tmp_path) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    target = tmp_path / "state"
    _database(target / "archive.sqlite3", "unknown")

    with pytest.raises(ValueError, match="no migration marker"):
        migrate_legacy_state(source, target)


def test_migrate_legacy_state_allows_prepared_state_directories(tmp_path) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    target = tmp_path / "state"
    (target / "archive/accounts/42").mkdir(parents=True)
    (target / "archive/accounts/42/metadata.json").write_text("{}")
    (target / "twscrape/cache").mkdir(parents=True)
    (target / "twscrape/cache/index").write_text("cache")

    result = migrate_legacy_state(source, target)

    assert result.status == "fresh"


def test_migrate_legacy_state_marks_fresh_install(tmp_path) -> None:
    source = tmp_path / "legacy"
    source.mkdir()

    result = migrate_legacy_state(source, tmp_path / "state")

    assert result.status == "fresh"
    assert result.databases == ()


def test_migrate_legacy_state_resumes_in_progress_marker(tmp_path) -> None:
    source = tmp_path / "legacy"
    target = tmp_path / "state"
    _database(source / "archive.sqlite3", "new")
    _database(target / "archive.sqlite3", "partial")
    (target / STATE_MARKER).write_text(json.dumps({
        "format": 1,
        "status": "in_progress",
        "source": str(source.resolve()),
    }))

    result = migrate_legacy_state(source, target)

    assert result.status == "migrated"
    assert _value(target / "archive.sqlite3") == "new"
    assert json.loads((target / STATE_MARKER).read_text())["status"] == "complete"


def test_migrate_legacy_state_reads_committed_wal_snapshot(tmp_path) -> None:
    source = tmp_path / "legacy"
    database = source / "archive.sqlite3"
    database.parent.mkdir(parents=True)
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE records (value TEXT NOT NULL)")
        writer.execute("INSERT INTO records VALUES ('from-wal')")
        writer.commit()
        assert database.with_name("archive.sqlite3-wal").is_file()

        migrate_legacy_state(source, tmp_path / "state")

        assert _value(tmp_path / "state/archive.sqlite3") == "from-wal"
    finally:
        writer.close()


def test_migrate_legacy_state_reads_wal_database_from_read_only_source(tmp_path) -> None:
    source = tmp_path / "legacy"
    database = source / "archive.sqlite3"
    database.parent.mkdir(parents=True)
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE records (value TEXT NOT NULL)")
        writer.execute("INSERT INTO records VALUES ('read-only-source')")
        writer.commit()
        assert database.with_name("archive.sqlite3-wal").is_file()
    finally:
        writer.close()

    os.chmod(database, 0o444)
    os.chmod(source, 0o555)
    try:
        result = migrate_legacy_state(source, tmp_path / "state")
    finally:
        os.chmod(source, 0o755)
        os.chmod(database, 0o644)

    assert result.status == "migrated"
    assert _value(tmp_path / "state/archive.sqlite3") == "read-only-source"
