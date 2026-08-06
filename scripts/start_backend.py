#!/usr/bin/env python3
"""Start the ArchiveX backend development server."""
import subprocess
import sys
from pathlib import Path


def main() -> None:
    script_dir = Path(__file__).parent
    project_root = script_dir.parent
    venv_path = project_root / ".venv"

    if not venv_path.exists():
        print("Virtual environment not found. Run scripts/setup_venv.py first.", file=sys.stderr)
        sys.exit(1)

    archivex_cmd = venv_path / "bin" / "archivex"

    if not archivex_cmd.exists():
        print("archivex command not found. Run:", file=sys.stderr)
        print("  source .venv/bin/activate", file=sys.stderr)
        print("  pip install -e .", file=sys.stderr)
        sys.exit(1)

    print("Starting ArchiveX backend...")
    subprocess.run([str(archivex_cmd)], cwd=project_root)


if __name__ == "__main__":
    main()
