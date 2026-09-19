"""Build, verify and apply an offline v2 -> v3 archive migration.

Default: rehearse in an isolated directory. --apply requires all services and
Redis deliveries to be stopped/drained. Source data are backed up before any
change; unresolved rows block application and remain in the report/candidate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from archivex.post_model import SourcePost, parse_post
from archivex.storage import ArchiveRepository, initialize_storage, _atomic_json, _path_component
from archivex.task_center import TASK_MEDIA_ID_LABEL, TASK_OBSERVED_ACCOUNT_ID_LABEL


class MigrationError(ValueError):
    pass


def _open(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    c = (
        sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        if readonly
        else sqlite3.connect(path)
    )
    c.row_factory = sqlite3.Row
    return c


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or root.resolve() not in path.resolve().parents:
        raise MigrationError(f"archive path escapes root: {relative}")
    if path.is_symlink():
        raise MigrationError(f"symlink is not an archive file: {relative}")
    return path


def _inventory(root: Path) -> dict[str, str]:
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise MigrationError(f"archive contains symlink: {path}")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = _sha(path)
    return files


def _snapshot_db(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    src, dest = _open(source, readonly=True), _open(target)
    try:
        src.backup(dest)
        if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MigrationError(f"invalid database: {source}")
    finally:
        src.close()
        dest.close()


def _database_state(path: Path) -> str:
    # Hash logical state, excluding SQLite page/change counters that change on
    # backup or restore even when the rows and schema are identical.
    c = _open(path, readonly=True)
    try:
        c.execute("BEGIN")
        digest = hashlib.sha256()
        digest.update(str(c.execute("PRAGMA user_version").fetchone()[0]).encode())
        for statement in c.iterdump():
            digest.update(statement.encode())
        return digest.hexdigest()
    finally:
        c.close()


def _same_database(source: Path, snapshot: Path) -> bool:
    return _database_state(source) == _database_state(snapshot)


def _tables(c: sqlite3.Connection) -> set[str]:
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _rows(c: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if table not in _tables(c):
        return []
    return [dict(r) for r in c.execute(f'SELECT * FROM "{table}"')]


def _active_task_count(c: sqlite3.Connection) -> int:
    if "queue_tasks" not in _tables(c):
        return 0
    return c.execute(
        "SELECT COUNT(*) FROM queue_tasks WHERE status IN ('queued','in_progress','retry_scheduled')"
    ).fetchone()[0]


def _require_idle_queue(c: sqlite3.Connection) -> None:
    active = _active_task_count(c)
    if active:
        raise MigrationError(
            f"{active} active queue tasks: drain deliveries before applying the migration"
        )


def _insert(c: sqlite3.Connection, table: str, value: Mapping[str, Any]) -> None:
    columns = ",".join(value)
    placeholders = ",".join("?" for _ in value)
    c.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(value.values()))


def _timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC).isoformat()


def _account_id(row: dict[str, Any], legacy_ids: dict[Any, str]) -> str:
    return (
        str(row["account_x_user_id"])
        if "account_x_user_id" in row
        else legacy_ids[row["account_id"]]
    )


def validate_archive(
    database: Path, archive: Path, *, check_hashes: bool = True,
    media_sources: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    """Validate the final schema and every DB file reference, including media bytes."""
    c = _open(database, readonly=True)
    try:
        if c.execute("PRAGMA user_version").fetchone()[0] != 3:
            raise MigrationError("expected archive schema v3")
        if (
            c.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or c.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise MigrationError("SQLite integrity/foreign key validation failed")
        if "accounts" in _tables(c) or any(t.startswith("_archivex_v2") for t in _tables(c)):
            raise MigrationError("legacy tables remain")
        if c.execute(
            "SELECT tweet_id FROM posts INTERSECT SELECT repost_tweet_id FROM reposts"
        ).fetchone():
            raise MigrationError("one ID exists as both content and event")
        checked = 0
        for table in ("posts", "reposts"):
            column = "tweet_id" if table == "posts" else "repost_tweet_id"
            filename = "post.json" if table == "posts" else "repost.json"
            for row in c.execute(
                f"SELECT {column},raw_json_path FROM {table} WHERE raw_json_path IS NOT NULL"
            ):
                identifier, relative = row
                if relative != f"{table}/{_path_component(identifier)}/{filename}":
                    raise MigrationError(f"noncanonical raw JSON path: {relative}")
                path = _safe(archive, relative)
                if not path.is_file():
                    raise MigrationError(f"missing raw JSON: {relative}")
                payload = json.loads(path.read_text())
                if not isinstance(payload, dict):
                    raise MigrationError(f"invalid raw JSON: {relative}")
                payload_id = payload.get("id_str") or payload.get("id")
                if payload_id is not None and str(payload_id) != identifier:
                    raise MigrationError(f"raw JSON belongs to a different tweet: {relative}")
                checked += 1
        for row in c.execute("SELECT * FROM media"):
            if row["download_status"] == "completed" and not row["local_path"]:
                raise MigrationError(f"completed media lacks a file: {row['id']}")
            if row["local_path"]:
                if Path(row["local_path"]).parts[:2] != ("posts", row["owner_tweet_id"]):
                    raise MigrationError(f"media file belongs to a different owner: {row['id']}")
                path = (
                    media_sources[row["local_path"]]
                    if media_sources is not None else _safe(archive, row["local_path"])
                )
                if not path.is_file():
                    raise MigrationError(f"missing media file: {row['local_path']}")
                if check_hashes and row["sha256"] and _sha(path) != row["sha256"]:
                    raise MigrationError(f"media checksum mismatch: {row['id']}")
                checked += 1
        return {
            "integrity_check": "ok",
            "foreign_key_check": [],
            "files_checked": checked,
            "counts": {
                t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in (
                    "x_users",
                    "observed_accounts",
                    "posts",
                    "reposts",
                    "archive_post_observations",
                    "archive_repost_observations",
                    "media",
                    "sync_runs",
                    "queue_tasks",
                    "queue_attempts",
                )
            },
        }
    finally:
        c.close()


def migrate_archive(
    database_path: Path,
    archive_data_dir: Path,
    *,
    session_path: Path | None = None,
    output_dir: Path | None = None,
    apply: bool = False,
    cleanup: bool = True,
) -> dict[str, Any]:
    database, archive = database_path.resolve(), archive_data_dir.resolve()
    if not database.is_file() or not archive.is_dir():
        raise MigrationError("database and archive directory must already exist")
    c = _open(database, readonly=True)
    try:
        version = c.execute("PRAGMA user_version").fetchone()[0]
        if version == 3:
            return {"status": "already_current", **validate_archive(database, archive)}
        # v0 is the original, explicitly recognized pre-versioned schema.
        if version not in (0, 2) or "accounts" not in _tables(c):
            raise MigrationError(f"unsupported source schema version: {version}")
        if version == 0 and "id" not in {
            r["name"] for r in c.execute("PRAGMA table_info(accounts)")
        }:
            raise MigrationError("unsupported unversioned schema")
        active = _active_task_count(c)
        if apply:
            _require_idle_queue(c)
    finally:
        c.close()
    run_id = uuid.uuid4().hex
    output = (
        output_dir or database.parent.parent / "backups" / f"post-model-v3-{run_id}"
    ).resolve()
    if output == archive or archive in output.parents or output == database.parent:
        raise MigrationError("migration output must be outside the live archive/data directory")
    output.mkdir(parents=True, exist_ok=False)
    backup = output / "backup"
    backup.mkdir()
    _snapshot_db(database, backup / "archive.sqlite3")
    files = _inventory(archive)
    shutil.copytree(archive, backup / "archive")
    if _inventory(backup / "archive") != files:
        raise MigrationError("archive changed while making its backup; stop all writers and retry")
    session = session_path or database.parent / "twscrape"
    session_db = session if session.suffix else session / "accounts.db"
    if session_db.is_file():
        _snapshot_db(session_db, backup / "twscrape" / "accounts.db")
    _atomic_json(
        backup / "manifest.json",
        {
            "archive_files": files,
            "database_sha256": _sha(backup / "archive.sqlite3"),
            "source_database": str(database),
            "source_archive": str(archive),
            "session_database": str(session_db),
            "session_sha256": _sha(backup / "twscrape/accounts.db")
            if (backup / "twscrape/accounts.db").exists()
            else None,
        },
    )
    candidate = output / "candidate"
    candidate.mkdir()
    initialize_storage(candidate / "archive.sqlite3", candidate / "archive", candidate / "sessions")
    report = {
        "migration_id": run_id,
        "from_version": version,
        "to_version": 3,
        "status": "building",
        "output_dir": str(output),
        "source_files": len(files),
        "active_tasks": active,
        "issues": [],
        "item_mapping": {},
        "media_mapping": {},
        "preserved_files": [],
    }
    try:
        _build(backup, candidate, report)
        report["validation"] = validate_archive(
            candidate / "archive.sqlite3", candidate / "archive"
        )
        report["status"] = "ready" if not report["issues"] else "unresolved"
        _atomic_json(output / "report.json", report)
        if apply:
            if report["issues"]:
                raise MigrationError(
                    f"unresolved migration issues; review {output / 'report.json'}"
                )
            # Recheck every source byte and the DB snapshot immediately before
            # installation. This is an offline command, never an online upgrade.
            if (
                not _same_database(database, backup / "archive.sqlite3")
                or _inventory(archive) != files
            ):
                raise MigrationError("source changed during migration; candidate not applied")
            _apply_candidate(database, archive, output, report, cleanup=cleanup)
        return report
    except Exception as exc:
        report["failure"] = str(exc)
        _atomic_json(output / "report.json", report)
        raise


def _issue(report: dict[str, Any], table: str, row: dict[str, Any], error: str) -> None:
    report["issues"].append(
        {
            "source_table": table,
            "source_id": str(row.get("tweet_id") or row.get("id")),
            "error": error,
            "row": row,
        }
    )


def _content_nodes(item: SourcePost) -> list[SourcePost]:
    nodes = _content_nodes(item.referenced) if item.referenced else []
    if item.post_type != "repost":
        nodes.append(item)
    return nodes


def _build(
    backup: Path, candidate: Path, report: dict[str, Any], *,
    media_archive: Path | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> None:
    old = _open(backup / "archive.sqlite3", readonly=True)
    target = _open(candidate / "archive.sqlite3")
    target.execute("PRAGMA foreign_keys=ON")
    repo = ArchiveRepository(candidate / "archive.sqlite3", candidate / "archive")
    source_archive = backup / "archive"
    now = datetime.now(UTC).isoformat()
    consumed: set[str] = set()
    try:
        accounts = _rows(old, "accounts")
        legacy_ids = {a.get("id"): str(a["x_user_id"]) for a in accounts}
        usernames = {
            str(a["x_user_id"]): a.get("current_username", a.get("username")) for a in accounts
        }
        with target:
            _insert(
                target,
                "archive_migrations",
                {
                    "id": report["migration_id"],
                    "from_version": report["from_version"],
                    "to_version": 3,
                    "started_at": now,
                    "finished_at": now,
                    "status": "building",
                    "details": "{}",
                },
            )
            for a in accounts:
                _insert(
                    target,
                    "x_users",
                    {
                        "x_user_id": str(a["x_user_id"]),
                        "current_username": usernames[str(a["x_user_id"])],
                        "display_name": a["display_name"],
                        "first_seen_at": a["created_at"],
                        "updated_at": a["updated_at"],
                    },
                )
                _insert(
                    target,
                    "observed_accounts",
                    {
                        k: a[k]
                        for k in (
                            "x_user_id",
                            "status",
                            "last_sync_at",
                            "last_error",
                            "created_at",
                            "updated_at",
                        )
                    }
                    | {"archive_enabled": a.get("archive_enabled", 1)},
                )
            histories = _rows(old, "account_username_history")
            for h in histories:
                _insert(target, "observed_account_username_history", h)
            if not histories:
                for a in accounts:
                    if usernames[str(a["x_user_id"])]:
                        _insert(
                            target,
                            "observed_account_username_history",
                            {
                                "x_user_id": a["x_user_id"],
                                "username": usernames[str(a["x_user_id"])],
                                "observed_from": a["created_at"],
                                "observed_to": None,
                                "last_observed_at": a["updated_at"],
                            },
                        )
        parsed: dict[str, SourcePost] = {}
        old_posts = _rows(old, "posts")
        # Older observations populate first; top-level captures outrank embedded
        # snapshots and unknown placeholders never overwrite complete content.
        for index, row in enumerate(sorted(old_posts, key=lambda r: r["updated_at"]), 1):
            tweet_id = str(row["tweet_id"])
            try:
                account_id = _account_id(row, legacy_ids)
                raw_path = _safe(source_archive, row["raw_json_path"])
                payload = json.loads(raw_path.read_text())
                if not isinstance(payload, dict):
                    raise MigrationError("raw JSON is not an object")
                item = parse_post(
                    payload,
                    tweet_id=tweet_id,
                    x_user_id=account_id,
                    username=usernames[account_id],
                    post_type=row["post_type"],
                    text=row["text"],
                    posted_at=datetime.fromisoformat(_timestamp(row["posted_at"])),
                    permalink=row["permalink"],
                )
                # The normalized v2 text is authoritative for the outer item.
                if item.post_type != "repost":
                    item = replace(item, text=row["text"])
                repo.ingest_item(item, account_id, observed_at=_timestamp(row["updated_at"]))
                parsed[tweet_id] = item
                consumed.add(row["raw_json_path"])
                kind = "repost" if item.post_type == "repost" else "post"
                report["item_mapping"][tweet_id] = {
                    "item_type": kind,
                    "tweet_id": tweet_id,
                    "observed_account_x_user_id": account_id,
                }
                with target:
                    for node in _content_nodes(item):
                        target.execute(
                            "UPDATE posts SET first_seen_at=MIN(first_seen_at,?),updated_at=MAX(updated_at,?) WHERE tweet_id=?",
                            (
                                _timestamp(row["first_seen_at"]),
                                _timestamp(row["updated_at"]),
                                node.tweet_id,
                            ),
                        )
                    if kind == "repost":
                        target.execute(
                            "UPDATE reposts SET first_seen_at=?,updated_at=? WHERE repost_tweet_id=?",
                            (
                                _timestamp(row["first_seen_at"]),
                                _timestamp(row["updated_at"]),
                                tweet_id,
                            ),
                        )
                    table, column = (
                        ("archive_repost_observations", "repost_tweet_id")
                        if kind == "repost"
                        else ("archive_post_observations", "tweet_id")
                    )
                    target.execute(
                        f"UPDATE {table} SET first_observed_at=?,last_observed_at=? WHERE observed_account_x_user_id=? AND {column}=?",
                        (
                            _timestamp(row["first_seen_at"]),
                            _timestamp(row["updated_at"]),
                            account_id,
                            tweet_id,
                        ),
                    )
            except (ValueError, OSError, sqlite3.IntegrityError) as exc:
                _issue(report, "posts", row, str(exc))
                report["item_mapping"][tweet_id] = {"item_type": "unresolved", "tweet_id": tweet_id}
            if progress and index % 500 == 0:
                progress("posts", index, len(old_posts))
        with target:
            # Preserve the most recent known identity/history from v2, including
            # paused accounts. Historical payloads must not roll it back.
            for a in accounts:
                target.execute(
                    "UPDATE x_users SET current_username=?,display_name=?,first_seen_at=?,updated_at=? WHERE x_user_id=?",
                    (
                        usernames[str(a["x_user_id"])],
                        a["display_name"],
                        a["created_at"],
                        a["updated_at"],
                        a["x_user_id"],
                    ),
                )
        _migrate_media(
            target,
            parsed,
            _rows(old, "media"),
            media_archive or source_archive,
            candidate / "archive",
            consumed,
            report,
            reuse_files=media_archive is not None,
            progress=progress,
        )
        with target:
            for row in _rows(old, "sync_runs"):
                value = {
                    k: v for k, v in row.items() if k not in {"account_id", "account_x_user_id"}
                }
                value["observed_account_x_user_id"] = _account_id(row, legacy_ids)
                _insert(target, "sync_runs", value)
            _migrate_tasks(target, _rows(old, "queue_tasks"), _rows(old, "queue_attempts"), report)
            for issue in report["issues"]:
                _insert(
                    target,
                    "archive_migration_errors",
                    {
                        "migration_id": report["migration_id"],
                        "source_table": issue["source_table"],
                        "source_id": issue["source_id"],
                        "error_code": "unresolved",
                        "details": issue["error"],
                        "payload": json.dumps(issue["row"], ensure_ascii=False),
                        "created_at": now,
                    },
                )
            target.execute(
                "UPDATE archive_migrations SET status=?,finished_at=?,details=? WHERE id=?",
                (
                    "unresolved" if report["issues"] else "success",
                    datetime.now(UTC).isoformat(),
                    json.dumps(
                        {
                            "old_items": len(old_posts),
                            "mapped_items": len(parsed),
                            "old_media": len(_rows(old, "media")),
                            "media_mapping": report["media_mapping"],
                        },
                        ensure_ascii=False,
                    ),
                    report["migration_id"],
                ),
            )
        # Untracked files, broken raw snapshots and alternate downloads are
        # retained explicitly. Nothing disappears just because it lacks a row.
        for path in sorted(source_archive.rglob("*")):
            if path.is_file():
                relative = path.relative_to(source_archive).as_posix()
                if relative not in consumed:
                    destination = candidate / "archive" / "preserved" / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination)
                    report["preserved_files"].append(relative)
        mapped = sum(i["item_type"] != "unresolved" for i in report["item_mapping"].values())
        observations = sum(
            target.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("archive_post_observations", "archive_repost_observations")
        )
        if len(report["item_mapping"]) != len(old_posts) or mapped != observations:
            raise MigrationError("top-level item/observation mapping is incomplete")
        for old_post in old_posts:
            mapping = report["item_mapping"][str(old_post["tweet_id"])]
            if mapping["item_type"] == "post":
                content = target.execute(
                    "SELECT text FROM posts WHERE tweet_id=?", (old_post["tweet_id"],)
                ).fetchone()
                if content is None or content["text"] != old_post["text"]:
                    raise MigrationError(f"outer post text was lost: {old_post['tweet_id']}")
    finally:
        old.close()
        target.close()


def _owned_urls(item: SourcePost) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for node in _content_nodes(item):
        for media in node.own_media:
            result[media.source_url].add(node.tweet_id)
        # Historical rows can contain a different video variant of the same
        # owned video. Preserve it, using the payload layer as evidence.
        for video in ((node.raw_payload or {}).get("media") or {}).get("videos") or ():
            for variant in video.get("variants") or ():
                if variant.get("url"):
                    result[variant["url"]].add(node.tweet_id)
    return result


def _migrate_media(
    c: sqlite3.Connection,
    parsed: dict[str, SourcePost],
    rows: list[dict[str, Any]],
    source: Path,
    destination: Path,
    consumed: set[str],
    report: dict[str, Any],
    *,
    reuse_files: bool = False,
    progress: Callable[[str, int, int], None] | None = None,
) -> None:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    used_ids: set[str] = set()
    verified: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows, 1):
        item = parsed.get(str(row["tweet_id"]))
        owners = _owned_urls(item).get(row["source_url"], set()) if item else set()
        if not owners and item and item.post_type == "original" and item.referenced is None:
            owners = {item.tweet_id}  # no reference layer to confuse with this owner
        if not owners:
            _issue(report, "media", row, "cannot determine canonical content owner from payload")
            continue
        valid = dict(row)
        if row["local_path"]:
            try:
                path = _safe(source, row["local_path"])
                if row["local_path"] not in verified:
                    before = path.stat()
                    digest = _sha(path)
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise MigrationError("media changed during verification")
                    verified[row["local_path"]] = {
                        "sha256": digest, "size": after.st_size, "mtime_ns": after.st_mtime_ns,
                    }
                digest = verified[row["local_path"]]["sha256"]
                if row["sha256"] and digest != row["sha256"]:
                    raise MigrationError("local media SHA-256 does not match database")
                valid["sha256"] = digest
            except (OSError, ValueError) as exc:
                _issue(report, "media", row, str(exc))
                valid.update(local_path=None, sha256=None, download_status="failed", error=str(exc))
        elif row["download_status"] == "completed":
            _issue(report, "media", row, "completed media has no local_path")
            valid.update(download_status="failed", error="missing legacy file")
        for owner in sorted(owners):
            groups[(owner, row["source_url"])].append(valid)
        if progress and index % 100 == 0:
            progress("media", index, len(rows))
    if reuse_files:
        report["verified_media"] = verified
        report["media_files"] = {}
        report["duplicate_files"] = {}
    with c:
        for (owner, url), candidates in groups.items():
            # Prefer a verified completed file over an earlier failed/pending
            # duplicate. Keep one stable old ID and rewrite all task references.
            candidates.sort(
                key=lambda r: (
                    r["download_status"] == "completed" and bool(r["local_path"]),
                    bool(r["local_path"]),
                    r["updated_at"],
                    r["id"],
                ),
                reverse=True,
            )
            winner = candidates[0]
            identifier = winner["id"] if winner["id"] not in used_ids else str(uuid.uuid4())
            used_ids.add(identifier)
            generated = c.execute(
                "SELECT id FROM media WHERE owner_tweet_id=? AND source_url=?", (owner, url)
            ).fetchone()
            if generated:
                c.execute("DELETE FROM media WHERE id=?", (generated["id"],))
            local = None
            if winner["local_path"]:
                ext = Path(winner["local_path"]).suffix
                local = f"posts/{_path_component(owner)}/media-{_path_component(identifier)}{ext}"
                if reuse_files:
                    report["media_files"][local] = winner["local_path"]
                else:
                    dest = _safe(destination, local)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(_safe(source, winner["local_path"]), dest)
                    if _sha(dest) != winner["sha256"]:
                        raise MigrationError(f"media copy verification failed: {identifier}")
            _insert(
                c,
                "media",
                {
                    "id": identifier,
                    "owner_tweet_id": owner,
                    "media_type": winner["media_type"],
                    "source_url": url,
                    "local_path": local,
                    "download_status": winner["download_status"],
                    "sha256": winner["sha256"],
                    "error": None if winner["download_status"] == "completed" else winner["error"],
                    "created_at": min(r["created_at"] for r in candidates),
                    "updated_at": max(r["updated_at"] for r in candidates),
                },
            )
            for row in candidates:
                # An old flattened URL may legitimately be attached to both
                # quote layers. Both owners receive it; historic tasks follow
                # the first canonical record. The other owner is downloadable
                # through its observation or already has the verified file.
                report["media_mapping"].setdefault(row["id"], []).append(identifier)
                if row["local_path"] and row["sha256"] == winner["sha256"]:
                    consumed.add(row["local_path"])
                    if reuse_files and row["local_path"] != winner["local_path"]:
                        report["duplicate_files"][row["local_path"]] = local
        unresolved_ids = {r["id"] for r in rows} - set(report["media_mapping"])
        for identifier in unresolved_ids:
            report["media_mapping"][identifier] = []
    if reuse_files:
        # A file selected by another owner is a retained source, never a loser.
        for source_path in report["media_files"].values():
            report["duplicate_files"].pop(source_path, None)


def _migrate_tasks(
    c: sqlite3.Connection,
    tasks: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    report: dict[str, Any],
) -> None:
    c.execute("PRAGMA defer_foreign_keys=ON")
    for row in tasks:
        value = dict(row)
        value["observed_account_x_user_id"] = value.pop("account_x_user_id", None)
        old_id = value.get("media_id")
        if old_id:
            mapped = report["media_mapping"].get(old_id, [])
            value["media_id"] = mapped[0] if mapped else None
            if not mapped:
                _issue(report, "queue_tasks", row, "media task has an unresolved owner")
            else:
                # Only execution targets change. Original snapshots are retained
                # under migration_context; result/error/attempt history survives.
                context = json.loads(value["context"])
                context["migration_context"] = dict(context)
                media = c.execute(
                    """SELECT m.*,p.permalink,p.text,p.author_x_user_id,u.current_username,u.display_name
                    FROM media m JOIN posts p ON p.tweet_id=m.owner_tweet_id JOIN x_users u ON u.x_user_id=p.author_x_user_id
                    WHERE m.id=?""",
                    (mapped[0],),
                ).fetchone()
                context["media"] = {
                    "id": mapped[0],
                    "owner_tweet_id": media["owner_tweet_id"],
                    "media_type": media["media_type"],
                    "source_url": media["source_url"],
                    "download_status": media["download_status"],
                }
                context["post"] = {
                    "tweet_id": media["owner_tweet_id"],
                    "permalink": media["permalink"],
                    "text_preview": media["text"][:240],
                }
                context["post_author"] = {
                    "x_user_id": media["author_x_user_id"],
                    "username": media["current_username"],
                    "display_name": media["display_name"],
                }
                value["context"] = json.dumps(context, ensure_ascii=False)
                args = json.loads(value["args"])
                kwargs = json.loads(value["kwargs"])
                labels = json.loads(value["labels"])
                if value["name"] == "archivex.download_media":
                    if args:
                        args[0] = mapped[0]
                    if "media_id" in kwargs:
                        kwargs["media_id"] = mapped[0]
                    labels[TASK_MEDIA_ID_LABEL] = mapped[0]
                value.update(
                    args=json.dumps(args), kwargs=json.dumps(kwargs), labels=json.dumps(labels)
                )
        labels = json.loads(value["labels"])
        legacy_account = labels.pop("_archivex_account_x_user_id", None)
        if legacy_account:
            labels[TASK_OBSERVED_ACCOUNT_ID_LABEL] = legacy_account
        value["labels"] = json.dumps(labels)
        _insert(c, "queue_tasks", value)
    for row in attempts:
        # Attempt payloads are immutable historical evidence, not execution input.
        _insert(c, "queue_attempts", row)


def _validate_candidate(output: Path, report: dict[str, Any]) -> None:
    if report.get("issues"):
        raise MigrationError("cannot apply a candidate with unresolved issues")
    if report.get("status") not in {
        "ready", "installing_files", "installing_database", "applied",
        "applied_and_cleaned", "rolled_back",
    } or not report.get("validation"):
        raise MigrationError("candidate build is incomplete; rebuild in a new output directory")
    database = output / "candidate/archive.sqlite3"
    c = _open(database, readonly=True)
    try:
        completed = c.execute(
            "SELECT 1 FROM archive_migrations WHERE id=? AND status='success'",
            (report["migration_id"],),
        ).fetchone()
        if not completed:
            raise MigrationError("candidate build is incomplete; rebuild in a new output directory")
        _require_idle_queue(c)
    finally:
        c.close()
    if validate_archive(database, output / "candidate/archive") != report["validation"]:
        raise MigrationError("candidate changed after validation; rebuild in a new output directory")


def _staged_file(relative: str, migration_id: str) -> str:
    # Stable, run-specific names let resume/rollback recognize files left by a
    # killed copy without treating arbitrary new archive files as disposable.
    name_hash = hashlib.sha256(relative.encode()).hexdigest()
    filename = f".archivex-{_path_component(migration_id)}-{name_hash}.tmp"
    return (Path(relative).parent / filename).as_posix()


def _staged_files(files: Mapping[str, str], migration_id: str) -> set[str]:
    return {_staged_file(relative, migration_id) for relative in files}


def _remove_staged_files(archive: Path, staged: set[str]) -> None:
    for relative in staged:
        _safe(archive, relative).unlink(missing_ok=True)


def _install_file(
    source: Path, archive: Path, relative: str, digest: str, migration_id: str
) -> None:
    dest = _safe(archive, relative)
    if dest.exists():
        if _sha(dest) != digest:
            raise MigrationError(f"canonical path already exists with different bytes: {relative}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    staged = _safe(archive, _staged_file(relative, migration_id))
    # Reserve exclusively so an unexpected existing file cannot be overwritten.
    with staged.open("xb"):
        pass
    try:
        shutil.copy2(source, staged)
        with staged.open("rb") as f:
            os.fsync(f.fileno())
        if _sha(staged) != digest:
            raise MigrationError(f"installed file verification failed: {relative}")
        os.replace(staged, dest)
    finally:
        staged.unlink(missing_ok=True)


def _apply_candidate(
    database: Path, archive: Path, output: Path, report: dict[str, Any], *, cleanup: bool
) -> None:
    _validate_candidate(output, report)
    live = _open(database, readonly=True)
    try:
        _require_idle_queue(live)
    finally:
        live.close()
    candidate = output / "candidate"
    files = _inventory(candidate / "archive")
    report["status"] = "installing_files"
    _atomic_json(output / "report.json", report)
    for relative, digest in files.items():
        source = _safe(candidate / "archive", relative)
        _install_file(source, archive, relative, digest, report["migration_id"])
    # SQLite backup replaces the destination transactionally and keeps its
    # filename/inode and any WAL handling under SQLite's control.
    report["status"] = "installing_database"
    _atomic_json(output / "report.json", report)
    _snapshot_db(candidate / "archive.sqlite3", database)
    report["validation"] = validate_archive(database, archive)
    report["status"] = "applied"
    _atomic_json(output / "report.json", report)
    if cleanup:
        cleanup_legacy_files(database, archive, output)
    report["status"] = "applied_and_cleaned" if cleanup else "applied"
    _atomic_json(output / "report.json", report)


def cleanup_legacy_files(database: Path, archive: Path, output: Path) -> None:
    """Resume cleanup safely after a crash; only remove backed-up, unchanged files."""
    validate_archive(database, archive)
    backup = output / "backup"
    manifest = json.loads((backup / "manifest.json").read_text())
    if _sha(backup / "archive.sqlite3") != manifest["database_sha256"]:
        raise MigrationError("migration database backup checksum mismatch")
    live = _open(database, readonly=True)
    try:
        run_id = json.loads((output / "report.json").read_text())["migration_id"]
        if not live.execute("SELECT 1 FROM archive_migrations WHERE id=?", (run_id,)).fetchone():
            raise MigrationError("live database does not contain this migration")
        keep = {
            r[0]
            for r in live.execute(
                "SELECT raw_json_path FROM posts UNION SELECT raw_json_path FROM reposts UNION SELECT local_path FROM media"
            )
            if r[0]
        }
    finally:
        live.close()
    for relative, digest in manifest["archive_files"].items():
        if _sha(_safe(backup / "archive", relative)) != digest:
            raise MigrationError(f"archive backup checksum mismatch: {relative}")
        path = _safe(archive, relative)
        if relative not in keep and path.exists():
            if _sha(path) != digest:
                raise MigrationError(f"legacy file changed; not removing: {relative}")
            path.unlink()
    for path in sorted(archive.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def resume_migration(output: Path, *, cleanup: bool = True) -> dict[str, Any]:
    """Continue a verified candidate after interruption without rebuilding it."""
    report = json.loads((output / "report.json").read_text())
    manifest = json.loads((output / "backup/manifest.json").read_text())
    _validate_candidate(output, report)
    database, archive = Path(manifest["source_database"]), Path(manifest["source_archive"])
    if _sha(output / "backup/archive.sqlite3") != manifest["database_sha256"]:
        raise MigrationError("migration database backup checksum mismatch")
    for relative, digest in manifest["archive_files"].items():
        if _sha(_safe(output / "backup/archive", relative)) != digest:
            raise MigrationError("archive backup checksum mismatch")
    c = _open(database, readonly=True)
    try:
        version = c.execute("PRAGMA user_version").fetchone()[0]
        if version == 3:
            if not c.execute(
                "SELECT 1 FROM archive_migrations WHERE id=?", (report["migration_id"],)
            ).fetchone():
                raise MigrationError("another migration has replaced the source database")
            if cleanup:
                cleanup_legacy_files(database, archive, output)
            report["status"] = "applied_and_cleaned" if cleanup else "applied"
            report.pop("failure", None)
            _atomic_json(output / "report.json", report)
            return report
    finally:
        c.close()
    if not _same_database(database, output / "backup/archive.sqlite3"):
        raise MigrationError("source database changed; cannot resume")
    candidate_files = _inventory(output / "candidate/archive")
    allowed = manifest["archive_files"] | candidate_files
    staged = _staged_files(candidate_files, report["migration_id"]) - allowed.keys()
    current = _inventory(archive)
    if any(current.get(p) != digest for p, digest in manifest["archive_files"].items()):
        raise MigrationError("source archive changed; cannot resume")
    if any(allowed.get(p) != digest for p, digest in current.items() if p not in staged):
        raise MigrationError("unexpected archive changes; cannot resume")
    _remove_staged_files(archive, staged)
    report.pop("failure", None)
    _apply_candidate(database, archive, output, report, cleanup=cleanup)
    return report


def rollback_migration(output: Path) -> None:
    """Restore this run's backup, refusing to discard post-migration changes."""
    manifest = json.loads((output / "backup/manifest.json").read_text())
    report = json.loads((output / "report.json").read_text())
    database, archive = Path(manifest["source_database"]), Path(manifest["source_archive"])
    backup_db, candidate_db = (
        output / "backup/archive.sqlite3",
        output / "candidate/archive.sqlite3",
    )
    if _sha(backup_db) != manifest["database_sha256"]:
        raise MigrationError("migration database backup checksum mismatch")

    if _database_state(database) not in {_database_state(backup_db), _database_state(candidate_db)}:
        raise MigrationError(
            "database has changed since migration; rollback would discard newer data"
        )
    candidate_files = _inventory(output / "candidate/archive")
    originals = manifest["archive_files"]
    allowed = originals | candidate_files
    staged = _staged_files(candidate_files, report["migration_id"]) - allowed.keys()
    for relative, digest in _inventory(archive).items():
        if relative not in staged and allowed.get(relative) != digest:
            raise MigrationError(f"archive changed since migration: {relative}")
    for relative, digest in originals.items():
        if _sha(_safe(output / "backup/archive", relative)) != digest:
            raise MigrationError(f"archive backup checksum mismatch: {relative}")
    _remove_staged_files(archive, staged)
    for relative in originals:
        target = _safe(archive, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_safe(output / "backup/archive", relative), target)
    _snapshot_db(backup_db, database)
    for relative in candidate_files.keys() - originals.keys():
        _safe(archive, relative).unlink(missing_ok=True)
    for path in sorted(archive.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    report["status"] = "rolled_back"
    _atomic_json(output / "report.json", report)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/archive.sqlite3"))
    parser.add_argument("--archive-dir", type=Path, default=Path("data/archive"))
    parser.add_argument("--session-path", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--keep-legacy-files", action="store_true")
    parser.add_argument(
        "--cleanup-from",
        type=Path,
        help="Resume verified cleanup using an existing migration output",
    )
    parser.add_argument(
        "--resume-from", type=Path, help="Resume/apply a previously verified candidate"
    )
    parser.add_argument(
        "--rollback-from", type=Path, help="Restore a migration backup if no newer data exist"
    )
    args = parser.parse_args(argv)
    try:
        if args.rollback_from:
            rollback_migration(args.rollback_from)
            print("Migration rolled back; start the v2 application version")
            return 0
        if args.cleanup_from:
            cleanup_legacy_files(args.database, args.archive_dir, args.cleanup_from)
            print("Verified legacy file cleanup completed")
            return 0
        if args.resume_from:
            report = resume_migration(args.resume_from, cleanup=not args.keep_legacy_files)
            print(report["status"])
            return 0
        report = migrate_archive(
            args.database,
            args.archive_dir,
            session_path=args.session_path,
            output_dir=args.output_dir,
            apply=args.apply,
            cleanup=not args.keep_legacy_files,
        )
        print(
            json.dumps(
                {
                    k: v
                    for k, v in report.items()
                    if k not in {"item_mapping", "media_mapping", "issues", "preserved_files"}
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if report.get("issues"):
            print(f"Unresolved issues: {len(report['issues'])}; see report.json")
            return 2
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
