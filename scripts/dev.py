#!/usr/bin/env python3
"""Start the complete ArchiveX local development stack."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import start_backend


def validate_frontend(root: Path) -> str:
    required_paths = (
        root / "frontend" / "package.json",
        root / "frontend" / "node_modules",
    )
    missing = [path for path in required_paths if not path.exists()]
    if missing:
        formatted = "\n".join(f"  - {path.relative_to(root)}" for path in missing)
        raise RuntimeError(
            f"Frontend development environment is incomplete:\n{formatted}\n"
            "Run scripts/setup_venv.py first."
        )

    npm = shutil.which("npm")
    if npm is None:
        raise RuntimeError("npm was not found. Install Node.js and run scripts/setup_venv.py.")
    return npm


def build_process_specs(
    root: Path,
    venv_bin: Path,
    npm: str,
) -> tuple[start_backend.ProcessSpec, ...]:
    return (
        *start_backend.build_process_specs(root, venv_bin),
        start_backend.ProcessSpec(
            "web",
            (npm, "run", "dev", "--", "--host", "--strictPort"),
            root / "frontend",
        ),
    )


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    try:
        venv_bin = start_backend.validate_project(root)
        npm = validate_frontend(root)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    specs = build_process_specs(root, venv_bin, npm)
    return start_backend.run_process_specs(
        root,
        venv_bin,
        specs,
        ready_url="http://localhost:5173",
        stack_label="local development stack",
    )


if __name__ == "__main__":
    raise SystemExit(main())
