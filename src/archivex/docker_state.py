"""Migrate Docker SQLite state off a host bind mount."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence


STATE_MARKER = Path(".archivex-docker-state-v1.json")
STATE_DATABASES = (Path("archive.sqlite3"), Path("twscrape/accounts.db"))


@dataclass(frozen=True)
class MigrationResult:
    status: str
    databases: tuple[str, ...]


def migrate_legacy_state(source: Path, target: Path) -> MigrationResult:
    """Copy legacy SQLite databases with SQLite's online backup API."""
    source = source.resolve()
    target = target.resolve()
    if source == target:
        raise ValueError("Legacy source and Docker state target must be different")
    if not source.is_dir():
        raise ValueError(f"Legacy data directory not found: {source}")

    target.mkdir(parents=True, exist_ok=True)
    _cleanup_migration_artifacts(target)
    marker = target / STATE_MARKER
    if marker.is_file():
        payload = _read_marker(marker)
        if payload.get("status") == "complete":
            databases = _validate_migrated_state(target, marker, payload)
            return MigrationResult("already_migrated", databases)
        if payload.get("status") != "in_progress" or payload.get("source") != str(source):
            raise ValueError(f"Docker state migration marker is invalid: {marker}")
    else:
        legacy_entries = {path.name for path in source.iterdir()}
        legacy_databases = {
            relative_path.name
            for relative_path in STATE_DATABASES
            if (source / relative_path).is_file()
        }
        if not legacy_databases and legacy_entries:
            names = ", ".join(sorted(legacy_entries))
            raise ValueError(
                "Legacy data directory is not empty but its SQLite databases are missing: "
                f"{names}; refuse to mark Docker state as fresh"
            )
        unexpected = [
            path.relative_to(target)
            for path in target.rglob("*")
            if path.is_file() and not _is_ignored_target_file(path.relative_to(target))
        ]
        if unexpected:
            names = ", ".join(sorted(path.as_posix() for path in unexpected))
            raise ValueError(
                "Docker state contains files but has no migration marker: "
                f"{names}"
            )
        _write_marker(marker, {
            "format": 1,
            "status": "in_progress",
            "source": str(source),
        })

    migrated: list[str] = []
    for relative_path in STATE_DATABASES:
        legacy_database = source / relative_path
        if not legacy_database.is_file():
            continue
        destination = target / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        _backup_database(legacy_database, destination)
        migrated.append(relative_path.as_posix())

    payload = {
        "format": 1,
        "status": "complete",
        "created_at": datetime.now(UTC).isoformat(),
        "source": str(source),
        "databases": migrated,
        "mode": "migrated" if migrated else "fresh",
    }
    _write_marker(marker, payload)
    return MigrationResult("migrated" if migrated else "fresh", tuple(migrated))


def _backup_database(source: Path, destination: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.migrating-",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        # A WAL database opened with ``mode=ro`` still needs to create its
        # shared-memory sidecar. The legacy directory is deliberately mounted
        # read-only, so make a private snapshot first and keep all SQLite
        # journaling inside the writable Docker volume.
        with tempfile.TemporaryDirectory(
            prefix=f".{source.name}.source-",
            dir=destination.parent,
        ) as source_directory:
            source_snapshot = Path(source_directory) / source.name
            shutil.copyfile(source, source_snapshot)
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = source.with_name(source.name + suffix)
                if sidecar.is_file():
                    shutil.copyfile(
                        sidecar,
                        source_snapshot.with_name(source_snapshot.name + suffix),
                    )
            with sqlite3.connect(source_snapshot, timeout=30) as source_connection:
                with sqlite3.connect(temporary, timeout=30) as destination_connection:
                    source_connection.backup(destination_connection)
        _check_database(temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm", "-journal"):
            temporary.with_name(temporary.name + suffix).unlink(missing_ok=True)


def _check_database(path: Path) -> None:
    with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity != ("ok",):
        raise ValueError(f"SQLite integrity check failed: {path}")


def _cleanup_migration_artifacts(target: Path) -> None:
    """Remove temporary backup files left by an interrupted older migration."""
    for relative_path in STATE_DATABASES:
        parent = target / relative_path.parent
        prefix = f".{relative_path.name}.migrating-"
        if not parent.is_dir():
            continue
        for artifact in parent.glob(f"{prefix}*"):
            if artifact.is_file():
                artifact.unlink()


def _write_marker(marker: Path, payload: dict[str, object]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{marker.name}.writing-",
        dir=marker.parent,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary_name).replace(marker)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _read_marker(marker: Path) -> dict[str, object]:
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Docker state migration marker is invalid: {marker}") from exc
    if not isinstance(payload, dict) or payload.get("format") != 1:
        raise ValueError(f"Docker state migration marker is invalid: {marker}")
    return payload


def _validate_migrated_state(
    target: Path,
    marker: Path,
    payload: dict[str, object] | None = None,
) -> tuple[str, ...]:
    payload = payload or _read_marker(marker)
    if payload.get("status") != "complete" or not isinstance(payload.get("databases"), list):
        raise ValueError(f"Docker state migration marker is invalid: {marker}")

    databases: list[str] = []
    allowed = {path.as_posix() for path in STATE_DATABASES}
    for value in payload["databases"]:
        if not isinstance(value, str) or value not in allowed:
            raise ValueError(f"Docker state migration marker is invalid: {marker}")
        database = target / value
        if not database.is_file():
            raise ValueError(f"Migrated Docker database is missing: {database}")
        _check_database(database)
        databases.append(value)
    return tuple(databases)


def _is_ignored_target_file(relative_path: Path) -> bool:
    """Allow only directories owned by the application in a fresh volume."""
    if relative_path.name in {
        ".archivex-owner-10001",
        ".archivex-media-owner-10001",
    }:
        return True
    return bool(relative_path.parts) and relative_path.parts[0] in {"archive", "twscrape"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/legacy-data"))
    parser.add_argument("--target", type=Path, default=Path("/data"))
    args = parser.parse_args(argv)
    try:
        result = migrate_legacy_state(args.source, args.target)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"error: {exc}\n")
    databases = ", ".join(result.databases) if result.databases else "none"
    print(f"Docker state: {result.status}; databases: {databases}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
