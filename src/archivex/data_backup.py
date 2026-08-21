"""Create, verify, and restore ArchiveX data backups."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Sequence


DATABASE_PATHS = (Path("archive.sqlite3"), Path("twscrape/accounts.db"))
OWNER_MARKER = Path(".archivex-owner-10001")
MEDIA_OWNER_MARKER = ".archivex-media-owner-10001"
MANIFEST_NAME = "manifest.json"


def create_backup(source: Path, output: Path) -> Path:
    source = source.resolve()
    if not (source / DATABASE_PATHS[0]).is_file():
        raise ValueError(f"Archive database not found: {source / DATABASE_PATHS[0]}")

    output = output.resolve()
    if source == output or source in output.parents:
        raise ValueError("Backup output must be outside the data directory")
    if output.exists():
        raise ValueError(f"Backup already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="archivex-backup-") as temporary:
        snapshot_root = Path(temporary)
        databases: dict[str, str] = {}
        for relative_path in DATABASE_PATHS:
            database = source / relative_path
            if not database.is_file():
                continue
            snapshot = snapshot_root / relative_path
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            _backup_database(database, snapshot)
            databases[relative_path.as_posix()] = _sha256(snapshot)

        manifest = {
            "format": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "databases": databases,
        }
        manifest_path = snapshot_root / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        temporary_output = output.with_name(f".{output.name}.partial")
        try:
            with tarfile.open(temporary_output, "w:gz") as archive:
                archive.add(manifest_path, MANIFEST_NAME)
                for path in sorted(source.rglob("*")):
                    if path.is_symlink() or not path.is_file():
                        continue
                    relative_path = path.relative_to(source)
                    if _skip_live_file(relative_path):
                        continue
                    archive.add(path, (Path("data") / relative_path).as_posix())
                for relative_path in databases:
                    archive.add(
                        snapshot_root / relative_path,
                        (Path("data") / relative_path).as_posix(),
                    )
            temporary_output.replace(output)
        finally:
            temporary_output.unlink(missing_ok=True)

    verify_backup(output)
    return output


def verify_backup(backup: Path) -> dict[str, object]:
    backup = backup.resolve()
    with tempfile.TemporaryDirectory(prefix="archivex-verify-") as temporary:
        extracted = Path(temporary)
        manifest = _extract_backup(backup, extracted)
        data_root = extracted / "data"
        for relative_path, expected_hash in _database_entries(manifest).items():
            database = data_root / relative_path
            if _sha256(database) != expected_hash:
                raise ValueError(f"Database checksum mismatch: {relative_path}")
            _check_database(database)
        if not (data_root / DATABASE_PATHS[0]).is_file():
            raise ValueError("Backup does not contain archive.sqlite3")
        return manifest


def restore_backup(backup: Path, target: Path, replace: bool = False) -> Path | None:
    target = target.resolve()
    if target.exists() and (os.path.ismount(target) or target.is_mount()):
        raise ValueError(
            f"Cannot replace Docker mountpoint in place: {target}; "
            "restore to a stopped host directory, then run the state migration"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="archivex-restore-", dir=target.parent) as temporary:
        extracted = Path(temporary)
        manifest = _extract_backup(backup.resolve(), extracted)
        restored_data = extracted / "data"
        for relative_path, expected_hash in _database_entries(manifest).items():
            database = restored_data / relative_path
            if _sha256(database) != expected_hash:
                raise ValueError(f"Database checksum mismatch: {relative_path}")
            _check_database(database)
        (restored_data / OWNER_MARKER).unlink(missing_ok=True)

        previous: Path | None = None
        if target.exists() and any(target.iterdir()):
            if not replace:
                raise ValueError(f"Target is not empty: {target}; pass --replace to preserve and replace it")
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            previous = target.with_name(f"{target.name}.pre-restore-{timestamp}")
            if previous.exists():
                raise ValueError(f"Recovery directory already exists: {previous}")
            target.replace(previous)
        elif target.exists():
            target.rmdir()

        restored_data.replace(target)
        return previous


def default_backup_path() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path("backups") / f"archivex-{timestamp}.tar.gz"


def _backup_database(source: Path, destination: Path) -> None:
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_connection:
        with sqlite3.connect(destination) as destination_connection:
            source_connection.backup(destination_connection)
    _check_database(destination)


def _check_database(path: Path) -> None:
    with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise ValueError(f"SQLite integrity check failed: {path}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _skip_live_file(relative_path: Path) -> bool:
    if (
        relative_path in DATABASE_PATHS
        or relative_path == OWNER_MARKER
        or relative_path.name == MEDIA_OWNER_MARKER
    ):
        return True
    return relative_path.name.endswith(("-wal", "-shm"))


def _extract_backup(backup: Path, destination: Path) -> dict[str, object]:
    if not backup.is_file():
        raise ValueError(f"Backup not found: {backup}")
    with tarfile.open(backup, "r:gz") as archive:
        members = archive.getmembers()
        names = {member.name for member in members}
        if MANIFEST_NAME not in names:
            raise ValueError("Backup manifest is missing")
        for member in members:
            member_path = PurePosixPath(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError(f"Unsafe backup path: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"Unsupported backup entry: {member.name}")
            target = destination.joinpath(*member_path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"Could not read backup entry: {member.name}")
            with source, target.open("wb") as stream:
                shutil.copyfileobj(source, stream)

    manifest = json.loads((destination / MANIFEST_NAME).read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or not isinstance(manifest.get("databases"), dict):
        raise ValueError("Unsupported backup manifest")
    _database_entries(manifest)
    return manifest


def _database_entries(manifest: dict[str, object]) -> dict[str, str]:
    databases = manifest.get("databases")
    if not isinstance(databases, dict):
        raise ValueError("Backup database manifest is invalid")
    validated: dict[str, str] = {}
    for name, checksum in databases.items():
        if not isinstance(name, str) or not isinstance(checksum, str):
            raise ValueError("Backup database manifest is invalid")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError(f"Unsafe database path in manifest: {name}")
        validated[name] = checksum
    return validated


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    backup_command = commands.add_parser("backup", help="Create and verify a data backup")
    backup_command.add_argument("--source", type=Path, default=Path("data"))
    backup_command.add_argument("--output", type=Path, default=None)

    verify_command = commands.add_parser("verify", help="Verify a backup and its databases")
    verify_command.add_argument("backup", type=Path)

    restore_command = commands.add_parser("restore", help="Restore a verified data backup")
    restore_command.add_argument("backup", type=Path)
    restore_command.add_argument("--target", type=Path, default=Path("data"))
    restore_command.add_argument("--replace", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            output = args.output or default_backup_path()
            print(f"Backup created: {create_backup(args.source, output)}")
        elif args.command == "verify":
            manifest = verify_backup(args.backup)
            print(f"Backup verified: {args.backup} ({len(_database_entries(manifest))} databases)")
        else:
            previous = restore_backup(args.backup, args.target, args.replace)
            print(f"Backup restored to: {args.target.resolve()}")
            if previous:
                print(f"Previous data preserved at: {previous}")
        return 0
    except (OSError, ValueError, tarfile.TarError, sqlite3.Error) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
