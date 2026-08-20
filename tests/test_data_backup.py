import sqlite3

import pytest

from archivex.data_backup import create_backup, restore_backup, verify_backup


def _database(path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE records (value TEXT NOT NULL)")
        connection.execute("INSERT INTO records VALUES (?)", (value,))


def _value(path) -> str:
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT value FROM records").fetchone()[0]


def test_backup_verify_and_restore_round_trip(tmp_path) -> None:
    source = tmp_path / "data"
    _database(source / "archive.sqlite3", "archive")
    _database(source / "twscrape/accounts.db", "session")
    media = source / "archive/accounts/42/posts/2026/08/100/image.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"image")
    (source / ".archivex-owner-10001").touch()
    backup = tmp_path / "backup.tar.gz"

    create_backup(source, backup)
    manifest = verify_backup(backup)

    assert set(manifest["databases"]) == {"archive.sqlite3", "twscrape/accounts.db"}
    restored = tmp_path / "restored"
    assert restore_backup(backup, restored) is None
    assert _value(restored / "archive.sqlite3") == "archive"
    assert _value(restored / "twscrape/accounts.db") == "session"
    assert (restored / media.relative_to(source)).read_bytes() == b"image"
    assert not (restored / ".archivex-owner-10001").exists()


def test_restore_preserves_existing_data_when_replace_is_explicit(tmp_path) -> None:
    source = tmp_path / "source"
    _database(source / "archive.sqlite3", "new")
    backup = create_backup(source, tmp_path / "backup.tar.gz")
    target = tmp_path / "data"
    target.mkdir()
    (target / "existing.txt").write_text("old")

    with pytest.raises(ValueError, match="--replace"):
        restore_backup(backup, target)

    previous = restore_backup(backup, target, replace=True)
    assert previous is not None
    assert (previous / "existing.txt").read_text() == "old"
    assert _value(target / "archive.sqlite3") == "new"
