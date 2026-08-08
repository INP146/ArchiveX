#!/usr/bin/env python3
"""Set up the Python and Node.js dependencies for local development."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def find_python() -> str:
    """Find a suitable Python interpreter (>=3.11)."""
    candidates = ["python3.11", "python3.12", "python3.13", "python3"]
    for command in candidates:
        executable = shutil.which(command)
        if executable is None:
            continue
        try:
            result = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                check=True,
            )
            parts = result.stdout.strip().split()[1].split(".")
            if int(parts[0]) == 3 and int(parts[1]) >= 11:
                print(f"Found {result.stdout.strip()}")
                return executable
        except (subprocess.CalledProcessError, ValueError, IndexError):
            continue
    raise RuntimeError("Python 3.11 or higher is required")


def require_tool(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"{name} was not found in PATH")
    subprocess.run([executable, "--version"], check=True)
    return executable


def initialize_env(root: Path) -> None:
    env_path = root / ".env"
    if env_path.exists():
        print("Keeping existing .env")
        return
    example_path = root / ".env.example"
    if not example_path.exists():
        raise RuntimeError(".env.example was not found")
    shutil.copyfile(example_path, env_path)
    print("Created .env from .env.example")


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    venv_path = root / ".venv"
    frontend_path = root / "frontend"

    try:
        node = require_tool("node")
        npm = require_tool("npm")
        initialize_env(root)

        venv_python = venv_path / "bin" / "python"
        if not venv_path.exists():
            python = find_python()
            print(f"Creating virtual environment with {python}...")
            subprocess.run([python, "-m", "venv", str(venv_path)], check=True)
        elif not venv_python.exists():
            raise RuntimeError(f"Existing virtual environment is invalid: {venv_path}")
        else:
            print(f"Updating existing virtual environment at {venv_path}")

        print("Installing Python development dependencies...")
        subprocess.run([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"], check=True)
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "-e", ".[dev]"],
            cwd=root,
            check=True,
        )

        if not (frontend_path / "package-lock.json").exists():
            raise RuntimeError("frontend/package-lock.json was not found")
        print(f"Installing frontend dependencies with {Path(node).name} and npm...")
        subprocess.run([npm, "ci"], cwd=frontend_path, check=True)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1

    print("Setup complete. Start ArchiveX with: python3 scripts/start_backend.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
