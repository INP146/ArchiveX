#!/usr/bin/env python3
"""Set up Python virtual environment for ArchiveX development."""
import shutil
import subprocess
import sys
from pathlib import Path


def find_python() -> str:
    """Find a suitable Python interpreter (>=3.11)."""
    candidates = ["python3.11", "python3.12", "python3.13", "python3"]

    for cmd in candidates:
        if shutil.which(cmd):
            try:
                result = subprocess.run(
                    [cmd, "--version"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                version_str = result.stdout.strip()
                print(f"Found {version_str}")

                # Parse version
                parts = version_str.split()[1].split(".")
                major, minor = int(parts[0]), int(parts[1])

                if major == 3 and minor >= 11:
                    return cmd
            except (subprocess.CalledProcessError, ValueError, IndexError):
                continue

    print("Error: Python 3.11 or higher is required", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    script_dir = Path(__file__).parent
    project_root = script_dir.parent
    venv_path = project_root / ".venv"

    print("Setting up Python virtual environment...")

    if venv_path.exists():
        print(f"Virtual environment already exists at {venv_path}")
        print(f"To recreate, delete it first: rm -rf {venv_path}")
        return

    python_cmd = find_python()

    print(f"Creating virtual environment with {python_cmd}...")
    subprocess.run([python_cmd, "-m", "venv", str(venv_path)], check=True)
    print(f"Created virtual environment at {venv_path}")

    pip_cmd = str(venv_path / "bin" / "pip")

    print("Installing dependencies...")
    subprocess.run([pip_cmd, "install", "--upgrade", "pip"], check=True)
    subprocess.run([pip_cmd, "install", "-e", str(project_root)], check=True)

    print()
    print("✓ Setup complete!")
    print()
    print("To activate the environment, run:")
    print("  source .venv/bin/activate")


if __name__ == "__main__":
    main()
