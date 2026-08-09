from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class DownloadResult:
    local_path: Path
    sha256: str


class PermanentMediaDownloadError(RuntimeError):
    """A source response that should only be retried by explicit user action."""


class MediaDownloader(Protocol):
    def download(self, source_url: str, target_dir: Path, max_bytes: int) -> DownloadResult: ...


class GalleryDlMediaDownloader:
    """Downloads one direct media URL using the installed gallery-dl module."""

    def __init__(self, executable: str | None = None, timeout_seconds: int = 300) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def download(self, source_url: str, target_dir: Path, max_bytes: int) -> DownloadResult:
        target_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".archivex-media-", dir=target_dir) as temp:
            download_dir = Path(temp)
            command = (
                [self.executable]
                if self.executable is not None
                else [sys.executable, "-m", "gallery_dl"]
            )
            command.extend(["--directory", str(download_dir), "--no-mtime", source_url])
            if max_bytes:
                command.extend(["--filter", f"filesize <= {max_bytes}"])
            try:
                completed = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"gallery-dl timed out after {self.timeout_seconds} seconds"
                ) from exc
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip()
                detail = detail or f"gallery-dl exited with status {completed.returncode}"
                error_type = (
                    PermanentMediaDownloadError
                    if _is_permanent_download_error(detail)
                    else RuntimeError
                )
                raise error_type(_safe_error(detail))

            downloaded = [
                path
                for path in download_dir.rglob("*")
                if path.is_file() and not path.name.endswith(".part")
            ]
            if len(downloaded) != 1:
                raise RuntimeError(f"gallery-dl downloaded {len(downloaded)} files for one media URL")
            staged_path = downloaded[0]
            if max_bytes and staged_path.stat().st_size > max_bytes:
                raise RuntimeError(
                    f"downloaded file exceeds configured maximum of {max_bytes} bytes"
                )

            sha256 = _sha256(staged_path)
            local_path = target_dir / staged_path.name
            if local_path.is_file() and _sha256(local_path) == sha256:
                return DownloadResult(local_path=local_path, sha256=sha256)
            staged_path.replace(local_path)
            return DownloadResult(local_path=local_path, sha256=sha256)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_error(message: str) -> str:
    """Avoid persisting signed media URLs in the database error field."""
    return re.sub(r"https?://[^\s]+", "[media URL omitted]", message)


def _is_permanent_download_error(message: str) -> bool:
    return bool(re.search(
        r"\b(?:401\s+Unauthorized|403\s+Forbidden|404\s+Not\s+Found)\b",
        message,
        re.IGNORECASE,
    ))
