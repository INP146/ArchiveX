from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class DownloadResult:
    local_path: Path
    sha256: str


class MediaDownloader(Protocol):
    def download(self, source_url: str, target_dir: Path, max_bytes: int) -> DownloadResult: ...


class GalleryDlMediaDownloader:
    """Downloads one direct media URL using the gallery-dl executable."""

    def __init__(self, executable: str = "gallery-dl") -> None:
        self.executable = executable

    def download(self, source_url: str, target_dir: Path, max_bytes: int) -> DownloadResult:
        target_dir.mkdir(parents=True, exist_ok=True)
        before = {path.resolve() for path in target_dir.iterdir() if path.is_file()}
        command = [self.executable, "--directory", str(target_dir), "--no-mtime", source_url]
        if max_bytes:
            command.extend(["--filter", f"filesize <= {max_bytes}"])
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(_safe_error(detail or f"gallery-dl exited with status {completed.returncode}"))

        downloaded = [
            path for path in target_dir.iterdir()
            if path.is_file() and path.resolve() not in before and not path.name.endswith(".part")
        ]
        if len(downloaded) != 1:
            raise RuntimeError(f"gallery-dl downloaded {len(downloaded)} files for one media URL")
        local_path = downloaded[0]
        if max_bytes and local_path.stat().st_size > max_bytes:
            local_path.unlink()
            raise RuntimeError(f"downloaded file exceeds configured maximum of {max_bytes} bytes")
        return DownloadResult(local_path=local_path, sha256=_sha256(local_path))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_error(message: str) -> str:
    """Avoid persisting signed media URLs in the database error field."""
    return re.sub(r"https?://[^\s]+", "[media URL omitted]", message)
