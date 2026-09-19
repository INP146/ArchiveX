"""Offline v2 -> v3 migration without a second copy of media.

Keep all writers stopped. Back up SQLite and raw JSON to a different disk,
verify every media file, then relocate retained files on the archive filesystem.
Only byte-identical, verified duplicates are removed. The backup depends on the
retained media; it is not an independent disaster-recovery backup.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Sequence

from archivex.migrate import (
    MigrationError, _build, _database_state, _install_file, _inventory, _open,
    _remove_staged_files, _require_idle_queue, _safe, _sha, _snapshot_db,
    _staged_files, validate_archive,
)
from archivex.storage import _atomic_json, initialize_storage


def _progress(phase: str, done: int, total: int) -> None:
    print(f"{phase}: {done}/{total}", flush=True)


def prepare_compact(database: Path, archive: Path, output: Path,
                    session_database: Path | None = None) -> dict[str, Any]:
    database, archive, output = database.resolve(), archive.resolve(), output.resolve()
    if archive == output or archive in output.parents:
        raise MigrationError("compact migration output must be outside the archive")
    if not database.is_file() or not archive.is_dir():
        raise MigrationError("database and archive directory must already exist")
    c = _open(database, readonly=True)
    try:
        if c.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise MigrationError("compact migration requires schema v2")
        _require_idle_queue(c)
        raw_paths = {r[0] for r in c.execute("SELECT raw_json_path FROM posts")}
    finally:
        c.close()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    backup, candidate = output / "backup", output / "candidate"
    (backup / "archive").mkdir(parents=True)
    _snapshot_db(database, backup / "archive.sqlite3")
    if session_database is not None:
        _snapshot_db(session_database, backup / "twscrape/accounts.db")
    report: dict[str, Any] = {
        "mode": "compact", "migration_id": uuid.uuid4().hex,
        "from_version": 2, "to_version": 3, "status": "building",
        "source_database": str(database), "source_archive": str(archive),
        "source_database_state": _database_state(database),
        "backup_database_sha256": _sha(backup / "archive.sqlite3"),
        "issues": [], "item_mapping": {}, "media_mapping": {}, "preserved_files": [],
        "source_raw": {},
    }
    _atomic_json(output / "report.json", report)
    try:
        for index, relative in enumerate(sorted(raw_paths), 1):
            source, dest = _safe(archive, relative), _safe(backup / "archive", relative)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            digest = _sha(dest)
            if _sha(source) != digest:
                raise MigrationError(f"raw JSON changed during backup: {relative}")
            report["source_raw"][relative] = digest
            if index % 500 == 0:
                _progress("backup JSON", index, len(raw_paths))
        initialize_storage(candidate / "archive.sqlite3", candidate / "archive", candidate / "sessions")
        _build(backup, candidate, report, media_archive=archive, progress=_progress)
        sources = {dest: _safe(archive, src) for dest, src in report["media_files"].items()}
        # _build has just hashed every source. Do not reread hundreds of GB;
        # apply rechecks their size/mtime before each move or duplicate removal.
        report["validation"] = validate_archive(
            candidate / "archive.sqlite3", candidate / "archive",
            check_hashes=False, media_sources=sources,
        )
        report["candidate_database_state"] = _database_state(candidate / "archive.sqlite3")
        report["candidate_raw"] = _inventory(candidate / "archive")
        report["duplicate_bytes"] = sum(
            report["verified_media"][p]["size"] for p in report["duplicate_files"]
        )
        report["status"] = "unresolved" if report["issues"] else "ready"
        _atomic_json(output / "report.json", report)
        return report
    except Exception as exc:
        report["failure"] = str(exc)
        _atomic_json(output / "report.json", report)
        raise


def _read_plan(output: Path) -> tuple[dict[str, Any], Path, Path]:
    report = json.loads((output / "report.json").read_text())
    if report.get("mode") != "compact":
        raise MigrationError("not a compact migration output")
    if report.get("issues") or report.get("status") not in {"ready", "applying", "applied", "rolled_back"}:
        raise MigrationError("compact candidate is incomplete or unresolved; rebuild before applying")
    if _sha(output / "backup/archive.sqlite3") != report["backup_database_sha256"]:
        raise MigrationError("database backup checksum mismatch")
    if _inventory(output / "backup/archive") != report["source_raw"]:
        raise MigrationError("raw JSON backup checksum mismatch")
    if _database_state(output / "candidate/archive.sqlite3") != report["candidate_database_state"]:
        raise MigrationError("candidate database changed")
    if _inventory(output / "candidate/archive") != report["candidate_raw"]:
        raise MigrationError("candidate JSON changed")
    return report, Path(report["source_database"]), Path(report["source_archive"])


def _unchanged(path: Path, verified: dict[str, Any]) -> None:
    stat = path.stat()
    if (stat.st_size, stat.st_mtime_ns) != (verified["size"], verified["mtime_ns"]):
        raise MigrationError(f"verified media changed: {path}")


def _primary_targets(report: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for target, source in sorted(report["media_files"].items()):
        result.setdefault(source, target)
    return result


def _prune_empty(archive: Path, originals: set[str]) -> None:
    parents = {parent for rel in originals for parent in (archive / rel).parents if archive in parent.parents}
    for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()


def apply_compact(output: Path) -> dict[str, Any]:
    output = output.resolve()
    report, database, archive = _read_plan(output)
    if report["status"] == "rolled_back":
        raise MigrationError("rollback changed legacy paths; prepare a new compact migration")
    state = _database_state(database)
    if state == report["candidate_database_state"]:
        report["validation"] = validate_archive(database, archive, check_hashes=False)
        report["status"] = "applied"
        _atomic_json(output / "report.json", report)
        return report
    if state != report["source_database_state"]:
        raise MigrationError("source database changed; keep all writers stopped")
    c = _open(database, readonly=True)
    try:
        _require_idle_queue(c)
    finally:
        c.close()
    primary = _primary_targets(report)
    for source, verified in report["verified_media"].items():
        path = _safe(archive, source)
        if path.exists():
            _unchanged(path, verified)
        elif report["status"] == "ready":
            raise MigrationError(f"source media disappeared: {source}")
    for relative, digest in report["source_raw"].items():
        path = _safe(archive, relative)
        if path.exists():
            if _sha(path) != digest:
                raise MigrationError(f"source JSON changed: {relative}")
        elif report["status"] == "ready":
            raise MigrationError(f"source JSON disappeared: {relative}")
    # Check all destinations before removing backed-up JSON to make room.
    for target, source in report["media_files"].items():
        dest = _safe(archive, target)
        if dest.exists() and target != source and (
            report["status"] == "ready" or _sha(dest) != report["verified_media"][source]["sha256"]
        ):
            raise MigrationError(f"unexpected canonical media: {target}")
    for target, digest in report["candidate_raw"].items():
        dest = _safe(archive, target)
        if dest.exists() and _sha(dest) != digest:
            raise MigrationError(f"unexpected canonical JSON: {target}")

    if report["status"] == "ready":
        block = os.statvfs(archive).f_frsize
        allocated = lambda size: ((size + block - 1) // block) * block
        released = sum(allocated(_safe(archive, p).stat().st_size) for p in report["source_raw"])
        released += sum(allocated(report["verified_media"][p]["size"]) for p in report["duplicate_files"])
        extra_media = sum(allocated(report["verified_media"][src]["size"])
                          for dest, src in report["media_files"].items() if primary[src] != dest)
        metadata = sum(allocated(_safe(output / "candidate/archive", p).stat().st_size) + 2 * block
                       for p in report["candidate_raw"])
        if shutil.disk_usage(archive).free + released < metadata + extra_media + 16 * 1024 * 1024:
            raise MigrationError("not enough space even after verified deduplication; source unchanged")
    report["status"] = "applying"
    _atomic_json(output / "report.json", report)
    staged = _staged_files(report["candidate_raw"] | report["media_files"], report["migration_id"])
    _remove_staged_files(archive, staged - report["source_raw"].keys() - report["verified_media"].keys())
    # These small files have a separately verified, complete backup. Deleting
    # them first supplies directory space even on a completely full ExFAT disk.
    for relative in report["source_raw"]:
        _safe(archive, relative).unlink(missing_ok=True)
    verified_targets: set[str] = set()
    for index, (source, target) in enumerate(primary.items(), 1):
        src, dest = _safe(archive, source), _safe(archive, target)
        expected = report["verified_media"][source]
        if src == dest:
            _unchanged(dest, expected)
        elif src.exists():
            if dest.exists():
                raise MigrationError(f"both original and target exist: {source}")
            _unchanged(src, expected)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.stat().st_dev != dest.parent.stat().st_dev:
                raise MigrationError("media relocation must stay on the same filesystem")
            os.rename(src, dest)
            _unchanged(dest, expected)
        elif not dest.is_file() or _sha(dest) != expected["sha256"]:
            raise MigrationError(f"retained media is missing or changed: {target}")
        verified_targets.add(target)
        if index % 100 == 0:
            _progress("move media", index, len(primary))
    for index, (duplicate, target) in enumerate(report["duplicate_files"].items(), 1):
        retained = primary[report["media_files"][target]]
        expected = report["verified_media"][duplicate]
        winner = report["verified_media"][report["media_files"][retained]]
        if retained not in verified_targets or winner["sha256"] != expected["sha256"]:
            raise MigrationError(f"duplicate has no verified retained copy: {duplicate}")
        path = _safe(archive, duplicate)
        if path.exists():
            _unchanged(path, expected)
            path.unlink()
        if index % 100 == 0:
            _progress("remove duplicate", index, len(report["duplicate_files"]))
    for target, source in report["media_files"].items():
        if target != primary[source]:
            _install_file(_safe(archive, primary[source]), archive, target,
                          report["verified_media"][source]["sha256"], report["migration_id"])
    for index, (relative, digest) in enumerate(report["candidate_raw"].items(), 1):
        _install_file(_safe(output / "candidate/archive", relative), archive, relative,
                      digest, report["migration_id"])
        if index % 500 == 0:
            _progress("install JSON", index, len(report["candidate_raw"]))
    validation = validate_archive(output / "candidate/archive.sqlite3", archive, check_hashes=False)
    if validation != report["validation"]:
        raise MigrationError("installed archive does not match verified candidate")
    _snapshot_db(output / "candidate/archive.sqlite3", database)
    _prune_empty(archive, set(report["source_raw"]) | set(report["verified_media"]))
    report["status"] = "applied"
    _atomic_json(output / "report.json", report)
    return report


def rollback_compact(output: Path) -> None:
    """Restore v2 records, keeping media compact and remapping legacy paths."""
    output = output.resolve()
    report, database, archive = _read_plan(output)
    if report["status"] == "rolled_back":
        return
    if _database_state(database) not in {report["source_database_state"], report["candidate_database_state"]}:
        raise MigrationError("database has newer writes; rollback refused")
    primary = _primary_targets(report)
    paths: dict[str, str] = {}
    for source, expected in report["verified_media"].items():
        target = source
        if not _safe(archive, source).is_file():
            if source in primary:
                target = primary[source]
            elif source in report["duplicate_files"]:
                winner = report["media_files"][report["duplicate_files"][source]]
                target = primary[winner]
        path = _safe(archive, target)
        if not path.is_file() or _sha(path) != expected["sha256"]:
            raise MigrationError(f"cannot restore media reference: {source}")
        paths[source] = target
    for relative, digest in report["candidate_raw"].items():
        path = _safe(archive, relative)
        if relative not in report["source_raw"] and path.exists():
            if _sha(path) != digest:
                raise MigrationError(f"canonical JSON has newer writes: {relative}")
    _remove_staged_files(archive, _staged_files(report["candidate_raw"] | report["media_files"], report["migration_id"]))
    for relative in report["candidate_raw"]:
        if relative not in report["source_raw"]:
            _safe(archive, relative).unlink(missing_ok=True)
    for relative, digest in report["source_raw"].items():
        _install_file(_safe(output / "backup/archive", relative), archive, relative,
                      digest, report["migration_id"])
    rollback_db = output / "rollback.sqlite3"
    _snapshot_db(output / "backup/archive.sqlite3", rollback_db)
    c = _open(rollback_db)
    try:
        with c:
            for source, target in paths.items():
                c.execute("UPDATE media SET local_path=? WHERE local_path=?", (target, source))
        if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or c.execute("PRAGMA foreign_key_check").fetchall():
            raise MigrationError("rollback database failed validation")
    finally:
        c.close()
    _snapshot_db(rollback_db, database)
    report["status"] = "rolled_back"
    _atomic_json(output / "report.json", report)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--archive-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--session-database", type=Path)
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--rollback-from", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.rollback_from:
            rollback_compact(args.rollback_from)
            print("rolled_back (v2 database, compact media paths)")
            return 0
        if args.resume_from:
            report = apply_compact(args.resume_from)
        else:
            if not all((args.database, args.archive_dir, args.output_dir)):
                parser.error("--database, --archive-dir and --output-dir are required for preparation")
            report = prepare_compact(args.database, args.archive_dir, args.output_dir, args.session_database)
        print(json.dumps({k: report.get(k) for k in ("status", "duplicate_bytes", "validation")}, indent=2))
        return 2 if report.get("issues") else 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
