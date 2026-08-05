import sqlite3
from pathlib import Path


def initialize_storage(database_path: Path, archive_data_dir: Path, session_path: Path) -> None:
    """Create writable persistent locations and verify the SQLite database can open."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    archive_data_dir.mkdir(parents=True, exist_ok=True)
    session_path.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("SELECT 1")

