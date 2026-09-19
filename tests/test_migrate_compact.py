import json
import sqlite3
from pathlib import Path

import pytest

from archivex import migrate as migration
from archivex import migrate_compact as compact
from archivex.migrate import MigrationError, validate_archive
from test_migrate import legacy


def test_compact_preparation_keeps_media_in_place_and_apply_preserves_content(tmp_path):
    db, archive, _, _ = legacy(tmp_path)
    untouched = archive / "untracked-video.mp4"
    untouched.write_bytes(b"untracked unique bytes")
    before = migration._inventory(archive)
    state = migration._database_state(db)
    output = tmp_path / "compact"
    report = compact.prepare_compact(db, archive, output)
    assert report["status"] == "ready" and report["duplicate_bytes"] == len(b"origin")
    assert not list((output / "backup").rglob("*.jpg"))
    assert not list((output / "candidate").rglob("*.jpg"))
    assert migration._inventory(archive) == before
    assert migration._database_state(db) == state
    assert compact.apply_compact(output)["status"] == "applied"
    counts = validate_archive(db, archive)["counts"]
    assert (counts["posts"], counts["reposts"], counts["media"], counts["queue_tasks"], counts["queue_attempts"]) == (4, 2, 2, 2, 4)
    assert len(list(archive.rglob("*.jpg"))) == 2
    assert untouched.read_bytes() == b"untracked unique bytes"
    assert (archive / "untracked.txt").is_file()
    assert compact.apply_compact(output)["status"] == "applied"


@pytest.mark.parametrize("phase", ["move", "delete", "json"])
def test_compact_resume_after_interruption(tmp_path, monkeypatch, phase):
    db, archive, _, _ = legacy(tmp_path)
    output = tmp_path / "compact"
    report = compact.prepare_compact(db, archive, output)
    with monkeypatch.context() as patch:
        if phase == "move":
            original = compact.os.rename

            def interrupt(src, dest):
                original(src, dest)
                raise OSError("simulated crash after rename")

            patch.setattr(compact.os, "rename", interrupt)
        elif phase == "delete":
            original = Path.unlink
            duplicate = next(iter(report["duplicate_files"]))

            def interrupt(path, *args, **kwargs):
                original(path, *args, **kwargs)
                if path == archive / duplicate:
                    raise OSError("simulated crash after duplicate removal")

            patch.setattr(Path, "unlink", interrupt)
        else:
            def interrupt(source, target):
                Path(target).write_bytes(b"partial")
                raise OSError("simulated crash during JSON installation")

            patch.setattr(compact.shutil, "copy2", interrupt)
        with pytest.raises(OSError, match="simulated"):
            compact.apply_compact(output)
    assert compact.apply_compact(output)["status"] == "applied"
    assert validate_archive(db, archive)["counts"]["media"] == 2
    assert not list(archive.rglob("*.tmp"))


def test_compact_rollback_restores_v2_without_recreating_duplicate_media(tmp_path):
    db, archive, _, _ = legacy(tmp_path)
    output = tmp_path / "compact"
    compact.prepare_compact(db, archive, output)
    compact.apply_compact(output)
    compact.rollback_compact(output)
    with sqlite3.connect(db) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 4
        assert c.execute("SELECT COUNT(*) FROM queue_attempts").fetchone()[0] == 4
        for relative, digest in c.execute("SELECT local_path,sha256 FROM media WHERE local_path IS NOT NULL"):
            assert migration._sha(archive / relative) == digest
        for (relative,) in c.execute("SELECT raw_json_path FROM posts"):
            assert (archive / relative).is_file()
    assert len(list(archive.rglob("*.jpg"))) == 2
    assert json.loads((output / "report.json").read_text())["status"] == "rolled_back"


def test_compact_refuses_changed_sources_before_removing_anything(tmp_path):
    db, archive, _, _ = legacy(tmp_path)
    output = tmp_path / "compact"
    compact.prepare_compact(db, archive, output)
    next(archive.rglob("completed.jpg")).write_bytes(b"new unique content")
    before = migration._inventory(archive)
    with pytest.raises(MigrationError, match="media changed"):
        compact.apply_compact(output)
    assert migration._inventory(archive) == before
    with sqlite3.connect(db) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 2


def test_compact_rejects_invalid_media_without_mutating_sources(tmp_path):
    db, archive, _, _ = legacy(tmp_path)
    next(archive.rglob("completed.jpg")).write_bytes(b"corrupt")
    before = migration._inventory(archive)
    output = tmp_path / "compact"
    assert compact.prepare_compact(db, archive, output)["status"] == "unresolved"
    with pytest.raises(MigrationError, match="incomplete or unresolved"):
        compact.apply_compact(output)
    assert migration._inventory(archive) == before


def test_compact_rollback_refuses_newer_database_writes(tmp_path):
    db, archive, _, _ = legacy(tmp_path)
    output = tmp_path / "compact"
    compact.prepare_compact(db, archive, output)
    compact.apply_compact(output)
    with sqlite3.connect(db) as c:
        c.execute("UPDATE posts SET text='new data' WHERE tweet_id='20'")
    with pytest.raises(MigrationError, match="newer writes"):
        compact.rollback_compact(output)
